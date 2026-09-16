-- 002_tenancy — tenants, API keys, usage accounting, and tenant scoping.
--
-- P1 of ROADMAP.md. Three things that look separate and are one change:
-- rate-limit buckets, the usage ledger, and (later) the semantic cache key all
-- need a tenant to hang off, so the column has to land before any of them.
--
-- This migration deliberately does NOT create RLS policies. Enabling RLS
-- without the `SET LOCAL app.tenant_id` wrapper in db/pool.py would make every
-- query return zero rows; the two have to arrive together, and they do in the
-- next migration alongside that wrapper.
--
-- Forward-only, per migrate.py. To undo: restore from a pg_dump, or add a
-- compensating 00N_*.sql.

-- ────────────────────────────────────────────────────────────────────────
-- Tenants
-- ────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE,
    name        text NOT NULL,
    plan        text NOT NULL DEFAULT 'free',
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended')),
    -- Per-tenant overrides for the P1 rate limiter. NULL means "use the
    -- plan default", so changing a plan's limits does not require touching
    -- every tenant row.
    rpm_limit   integer,
    rpd_limit   integer,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- A fixed id, not a generated one: every existing row is backfilled to this
-- tenant by the ALTER ... DEFAULT below, which needs the value to be a literal
-- known at migration time. It is an identifier, not a secret.
INSERT INTO tenants (id, slug, name, plan)
VALUES ('00000000-0000-0000-0000-000000000001', 'default', 'Default tenant', 'free')
ON CONFLICT (id) DO NOTHING;

-- The tenant the local frontend talks to. Created here; its API key is NOT
-- created here — see app/tenancy/keys.py. A key hash committed to git is a
-- fixed credential in source control, and the fact that it is "only for dev"
-- is exactly what gets it copied to staging.
INSERT INTO tenants (id, slug, name, plan)
VALUES ('00000000-0000-0000-0000-000000000002', 'dev', 'Local development', 'free')
ON CONFLICT (id) DO NOTHING;

-- ────────────────────────────────────────────────────────────────────────
-- API keys
-- ────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS api_keys (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Displayable head of the key ("sk_live_a3f2"), stored separately so the
    -- UI can identify a key in a list without the hash being reversible.
    key_prefix  text NOT NULL,
    -- SHA-256 of the full key, hex. Not bcrypt/argon2 on purpose: those exist
    -- to slow brute force against *low-entropy human passwords*. These keys
    -- are 32 bytes from a CSPRNG, so there is nothing to brute force, and a
    -- deliberately slow KDF would put ~100ms on every authenticated request
    -- while buying no security. Same reasoning Stripe and GitHub publish.
    key_hash    text NOT NULL UNIQUE,
    name        text NOT NULL DEFAULT 'default',
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at  timestamptz
);

-- The authentication lookup: one indexed equality probe per request.
CREATE INDEX IF NOT EXISTS api_keys_hash_idx ON api_keys (key_hash)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS api_keys_tenant_idx ON api_keys (tenant_id, created_at DESC);

-- ────────────────────────────────────────────────────────────────────────
-- Usage ledger
-- ────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS usage_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- Correlates a ledger row with the structured logs for the same request.
    request_id    text,
    session_id    uuid REFERENCES sessions(id) ON DELETE SET NULL,
    message_id    uuid REFERENCES messages(id) ON DELETE SET NULL,
    event_type    text NOT NULL DEFAULT 'chat'
                  CHECK (event_type IN ('chat', 'embedding', 'agent_job', 'cache_hit')),
    provider      text,
    model         text,
    tokens_in     integer NOT NULL DEFAULT 0,
    tokens_out    integer NOT NULL DEFAULT 0,
    latency_ms    integer,
    -- Millionths of a currency unit, as bigint. Never a float: money in
    -- floating point is a bug you do not find in one row, you find it in the
    -- monthly total, after you have already invoiced it.
    cost_micros   bigint NOT NULL DEFAULT 0,
    -- Which provider actually served, when it was not the one requested.
    fell_back_from text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- The aggregation this table exists to answer: spend for one tenant over a
-- date range. Leading tenant_id because every such query is tenant-scoped.
CREATE INDEX IF NOT EXISTS usage_events_tenant_time_idx
    ON usage_events (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_message_idx ON usage_events (message_id);

-- ────────────────────────────────────────────────────────────────────────
-- Tenant scoping on the existing tables
-- ────────────────────────────────────────────────────────────────────────
--
-- `ADD COLUMN ... NOT NULL DEFAULT <literal>` is a catalog-only change on
-- PostgreSQL 11+, so this backfills ~19k chunks without rewriting the table.
-- The DEFAULT is then dropped: it exists to backfill history, and leaving it
-- in place would let a future INSERT silently land in the default tenant
-- instead of failing loudly, which is the exact bug this column prevents.
--
-- At a size where this did lock too long, the online form is: add nullable,
-- backfill in batches, ADD CONSTRAINT ... NOT VALID, VALIDATE CONSTRAINT,
-- then SET NOT NULL. Not needed here; noted because the difference matters.

ALTER TABLE episodes   ADD COLUMN IF NOT EXISTS tenant_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE chunks     ADD COLUMN IF NOT EXISTS tenant_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE sessions   ADD COLUMN IF NOT EXISTS tenant_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE messages   ADD COLUMN IF NOT EXISTS tenant_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE artifacts  ADD COLUMN IF NOT EXISTS tenant_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000001';
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS tenant_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000001';

ALTER TABLE episodes   ALTER COLUMN tenant_id DROP DEFAULT;
ALTER TABLE chunks     ALTER COLUMN tenant_id DROP DEFAULT;
ALTER TABLE sessions   ALTER COLUMN tenant_id DROP DEFAULT;
ALTER TABLE messages   ALTER COLUMN tenant_id DROP DEFAULT;
ALTER TABLE artifacts  ALTER COLUMN tenant_id DROP DEFAULT;
ALTER TABLE tool_calls ALTER COLUMN tenant_id DROP DEFAULT;

-- Referential integrity, added after the backfill so validation has rows to
-- validate against. DO blocks because ALTER TABLE ... ADD CONSTRAINT has no
-- IF NOT EXISTS, and migrate.py must stay safely re-runnable.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['episodes','chunks','sessions','messages','artifacts','tool_calls']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = t || '_tenant_fk'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (tenant_id) '
                'REFERENCES tenants(id) ON DELETE CASCADE',
                t, t || '_tenant_fk'
            );
        END IF;
    END LOOP;
END $$;

-- ────────────────────────────────────────────────────────────────────────
-- Indexes for the tenant-scoped read paths
-- ────────────────────────────────────────────────────────────────────────

-- Replaces sessions_updated_at_idx as the useful shape: every session listing
-- is now "this tenant's sessions, newest first".
CREATE INDEX IF NOT EXISTS sessions_tenant_updated_idx
    ON sessions (tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS messages_tenant_idx ON messages (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS artifacts_tenant_idx ON artifacts (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS episodes_tenant_idx ON episodes (tenant_id);

-- Sparse retrieval can use a composite; the GIN index on tsv stays as it is
-- and the planner intersects.
CREATE INDEX IF NOT EXISTS chunks_tenant_idx ON chunks (tenant_id);

-- NOTE for P1/P5: dense retrieval is the one path a plain composite index does
-- not fix. An HNSW index cannot be composite, so a tenant predicate is applied
-- *after* the approximate scan returns its candidates — meaning a query can
-- come back with fewer than RETRIEVAL_CANDIDATES rows for the tenant, or none,
-- while the index reports success. At one real tenant this is invisible. With
-- many tenants sharing the table it is a silent recall hole of exactly the kind
-- docs/retrieval-calibration.md already documents twice.
--
-- The two real options are partial HNSW indexes per tenant (fast, but DDL per
-- signup) or raising hnsw.ef_search until recall is acceptable (no DDL, costs
-- latency). Deferred until there is a second tenant with a real corpus, and
-- called out here so it is a decision rather than a discovery.
