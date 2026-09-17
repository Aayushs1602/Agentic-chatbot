-- 003_rls — database-enforced tenant isolation.
--
-- 002 added `tenant_id` everywhere. This makes Postgres *enforce* it, so
-- isolation stops depending on every query remembering a WHERE clause.
--
-- Four things have to be true at once, and missing any one of them produces a
-- system that looks isolated and is not:
--
--   1. RLS enabled on the table.
--   2. FORCE, or the table's owner silently bypasses every policy.
--   3. The connecting role is not a superuser — superusers bypass RLS even
--      with FORCE. Hence `lenny_app` below.
--   4. Each transaction sets `app.tenant_id`, via the wrapper in db/pool.py.
--
-- Points 2 and 3 are the dangerous ones: they fail *open*, silently, with the
-- policies sitting in pg_policies looking perfectly correct.

-- ────────────────────────────────────────────────────────────────────────
-- The tenant accessor
-- ────────────────────────────────────────────────────────────────────────

-- Every policy calls this instead of casting current_setting() inline.
--
-- Inline casting fails *open-ended*: `''::uuid` and `'-'::uuid` both raise
-- invalid_text_representation, so an unauthenticated request got a 500 from
-- the security layer rather than an empty result. A policy should answer
-- "which rows can you see" with "none", never with an exception.
--
-- STABLE, not IMMUTABLE: it reads session state, so it may return different
-- values in different transactions, but not within one query.
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
    LANGUAGE plpgsql
    STABLE
    -- Pinned so the body cannot be influenced by a caller's search_path.
    SET search_path = pg_catalog
AS $$
DECLARE
    raw text := current_setting('app.tenant_id', true);
BEGIN
    IF raw IS NULL OR raw = '' THEN
        RETURN NULL;   -- no tenant set: see nothing
    END IF;
    RETURN raw::uuid;
EXCEPTION WHEN invalid_text_representation THEN
    -- Anything unparseable is treated as "no tenant", not as an error.
    -- Fail closed.
    RETURN NULL;
END $$;

-- ────────────────────────────────────────────────────────────────────────
-- The application role
-- ────────────────────────────────────────────────────────────────────────
--
-- Migrations keep running as the owner (`lenny`), because they create tables.
-- The application gets a role that can only move rows around — it cannot
-- create, drop, or alter anything, and critically it is not a superuser, so
-- RLS actually applies to it.
--
-- Created NOLOGIN here, and it stays NOLOGIN until someone decides how the
-- app authenticates as it. A password belongs in the environment, not in a
-- file in git, so it cannot be set from this migration.
--
-- UNTIL THAT IS DONE, RLS IS NOT ENFORCED FOR THE RUNNING APPLICATION: the app
-- connects as `lenny`, a superuser, which bypasses every policy below. The
-- policies are real and tested (see tests/test_rls.py, which exercises them
-- via SET ROLE), but the last mile is the connection string.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lenny_app') THEN
        CREATE ROLE lenny_app NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO lenny_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lenny_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lenny_app;

-- Tables created by later migrations are covered automatically; without this,
-- every new table silently becomes invisible to the app until someone
-- remembers to grant on it.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lenny_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO lenny_app;

-- Explicitly NOT granted: CREATE on the schema. The app must not be able to
-- define a function that policies might resolve to, or replace
-- app_current_tenant() with one that returns whatever it likes.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ────────────────────────────────────────────────────────────────────────
-- Policies
-- ────────────────────────────────────────────────────────────────────────
--
-- One policy per tenant-scoped table, all identical in shape.
--
-- No FOR clause means FOR ALL, and with WITH CHECK omitted Postgres reuses the
-- USING expression for it. So this governs writes as well as reads: the app
-- cannot INSERT or UPDATE a row into a tenant that is not its own.
--
-- `tenants` and `api_keys` are deliberately excluded. Authentication has to
-- read a key row *before* it knows which tenant is asking — that lookup is by
-- key hash across all tenants, and a policy here would make every key appear
-- invalid. app/db/tenants.py is the one module allowed to read across
-- tenants, which is why it is small and takes no caller-supplied filters.

DO $$
DECLARE
    t text;
    protected text[] := ARRAY[
        'episodes', 'chunks', 'sessions', 'messages',
        'artifacts', 'tool_calls', 'usage_events'
    ];
BEGIN
    FOREACH t IN ARRAY protected LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        -- Without FORCE, `lenny` owns these tables and ignores every policy
        -- below. This single word is the difference between isolation and
        -- the appearance of it.
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);

        -- CREATE POLICY has no IF NOT EXISTS in any Postgres version, so the
        -- catalog check is the guard (same reason 002 uses a DO block).
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = t
              AND policyname = 'tenant_isolation'
        ) THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I '
                'USING (tenant_id = app_current_tenant())',
                t
            );
        END IF;
    END LOOP;
END $$;
