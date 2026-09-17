"""Row-Level Security: does the database actually refuse cross-tenant reads?

These tests run as `lenny_app` via `SET LOCAL ROLE`, because that is the only
way to observe RLS at all. The test suite connects as `lenny`, which owns the
tables *and* is a superuser — two independent exemptions, either of which makes
every policy below invisible.

That is the whole reason this file is careful about roles. A version of these
tests that forgot `SET LOCAL ROLE` would pass against a database with no
policies whatsoever, and would have told us nothing.

`SET LOCAL ROLE` rather than `SET ROLE`: the connection goes back to the pool
afterwards, and a role that leaked into the next test would be its own bug.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.db

TENANT_A = "00000000-0000-0000-0000-00000000aaaa"
TENANT_B = "00000000-0000-0000-0000-00000000bbbb"

PROTECTED_TABLES = [
    "episodes", "chunks", "sessions", "messages",
    "artifacts", "tool_calls", "usage_events",
]


@pytest.fixture
async def two_tenants(db_pool):
    """Two tenants, one session each. Created as the owner, cleaned up after."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (id, slug, name) VALUES
              ($1, 'rls-test-a', 'RLS Test A'),
              ($2, 'rls-test-b', 'RLS Test B')
            ON CONFLICT (id) DO NOTHING
            """,
            TENANT_A, TENANT_B,
        )
        await conn.execute(
            "INSERT INTO sessions (title, tenant_id) VALUES ('A private', $1), ('B private', $2)",
            TENANT_A, TENANT_B,
        )
    yield
    async with db_pool.acquire() as conn:
        # Sessions cascade from tenants.
        await conn.execute(
            "DELETE FROM tenants WHERE id = ANY($1::uuid[])", [TENANT_A, TENANT_B]
        )


async def _as_tenant(conn, tenant: str, query: str, *args):
    """Run one query as `lenny_app` with `tenant` set, exactly as pool.py does."""
    async with conn.transaction():
        await conn.execute("SET LOCAL ROLE lenny_app")
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
        return await conn.fetch(query, *args)


class TestConfiguration:
    """The two settings that fail open, silently, if anyone drops them."""

    async def test_every_protected_table_has_rls_forced(self, db_pool):
        # FORCE is the one that matters: without it the owner — which is the
        # role the app connects as — ignores every policy, and nothing errors.
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class WHERE relname = ANY($1::text[])
                """,
                PROTECTED_TABLES,
            )
        assert len(rows) == len(PROTECTED_TABLES)
        for row in rows:
            assert row["relrowsecurity"], f"{row['relname']}: RLS not enabled"
            assert row["relforcerowsecurity"], f"{row['relname']}: RLS not FORCEd"

    async def test_every_protected_table_has_a_policy(self, db_pool):
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tablename FROM pg_policies "
                "WHERE schemaname='public' AND policyname='tenant_isolation'"
            )
        assert {r["tablename"] for r in rows} == set(PROTECTED_TABLES)

    async def test_the_app_role_cannot_change_the_schema(self, db_pool):
        # If the app could CREATE, it could replace app_current_tenant() with
        # one that returns whatever it liked, and the policies would obey.
        async with db_pool.acquire() as conn:
            allowed = await conn.fetchval(
                "SELECT has_schema_privilege('lenny_app', 'public', 'CREATE')"
            )
        assert not allowed


class TestAccessorFailsClosed:
    """`app_current_tenant()` must answer "nobody", never raise.

    An exception from inside a policy turns an unauthenticated request into a
    500 from the security layer instead of an empty result.
    """

    @pytest.mark.parametrize("value", ["", "-", "not-a-uuid", "'; DROP TABLE sessions--"])
    async def test_unparseable_tenants_resolve_to_null(self, db_pool, value):
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", value)
                assert await conn.fetchval("SELECT app_current_tenant() IS NULL")

    async def test_a_valid_uuid_resolves(self, db_pool):
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", TENANT_A
                )
                assert str(await conn.fetchval("SELECT app_current_tenant()")) == TENANT_A


class TestIsolation:
    async def test_a_tenant_sees_only_its_own_sessions(self, db_pool, two_tenants):
        async with db_pool.acquire() as conn:
            a = await _as_tenant(conn, TENANT_A, "SELECT title FROM sessions")
            b = await _as_tenant(conn, TENANT_B, "SELECT title FROM sessions")

        assert [r["title"] for r in a] == ["A private"]
        assert [r["title"] for r in b] == ["B private"]

    async def test_naming_another_tenants_row_explicitly_still_returns_nothing(
        self, db_pool, two_tenants
    ):
        # The policy is not a default filter you can override by being specific.
        async with db_pool.acquire() as conn:
            rows = await _as_tenant(
                conn, TENANT_A,
                "SELECT title FROM sessions WHERE tenant_id = $1", TENANT_B,
            )
        assert rows == []

    async def test_no_tenant_sees_nothing(self, db_pool, two_tenants):
        async with db_pool.acquire() as conn:
            rows = await _as_tenant(conn, "", "SELECT title FROM sessions")
        assert rows == []

    async def test_aggregates_cannot_leak_counts(self, db_pool, two_tenants):
        # A count() that saw hidden rows would leak their existence without
        # returning them, which is a subtler version of the same failure.
        async with db_pool.acquire() as conn:
            rows = await _as_tenant(conn, TENANT_A, "SELECT count(*) AS n FROM sessions")
        assert rows[0]["n"] == 1

    async def test_writes_are_governed_too(self, db_pool, two_tenants):
        # CREATE POLICY with USING and no WITH CHECK reuses USING for writes,
        # so a tenant cannot insert a row belonging to someone else.
        import asyncpg

        async with db_pool.acquire() as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute("SET LOCAL ROLE lenny_app")
                    await conn.execute(
                        "SELECT set_config('app.tenant_id', $1, true)", TENANT_A
                    )
                    await conn.execute(
                        "INSERT INTO sessions (title, tenant_id) VALUES ('smuggled', $1)",
                        TENANT_B,
                    )

    async def test_deletes_cannot_reach_across_tenants(self, db_pool, two_tenants):
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL ROLE lenny_app")
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", TENANT_A
                )
                result = await conn.execute(
                    "DELETE FROM sessions WHERE tenant_id = $1", TENANT_B
                )
            assert result.endswith(" 0"), "a DELETE reached another tenant's rows"

            # And B's row is still there when B looks for it.
            rows = await _as_tenant(conn, TENANT_B, "SELECT title FROM sessions")
            assert [r["title"] for r in rows] == ["B private"]
