"""The repository's write paths, against a real database.

These exist because of a specific failure: `002_tenancy` made `tenant_id`
NOT NULL and dropped its default, `repository.py` was not updated, and every
INSERT it performed began raising `NotNullViolationError`. The whole suite
stayed green and the application could not start a conversation.

Nothing here is clever. The point is only that each write is actually executed
against Postgres once, because a schema constraint is invisible to a test that
never reaches the schema.
"""

from __future__ import annotations

import pytest

from app.db import repository as repo
from app.logging import set_tenant_id
from app.tenancy import TenantRequiredError

pytestmark = pytest.mark.db

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
async def as_default_tenant(app_db):
    """Bind a tenant, and clean up whatever the test wrote."""
    set_tenant_id(DEFAULT_TENANT)
    created: list[str] = []
    yield created
    for session_id in created:
        try:
            await app_db.execute("DELETE FROM sessions WHERE id = $1", session_id)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
    set_tenant_id("-")


class TestWritePathsCarryTheTenant:
    async def test_create_session(self, app_db, as_default_tenant):
        session = await repo.create_session(title="write path")
        as_default_tenant.append(session["id"])

        stored = await app_db.fetchval(
            "SELECT tenant_id FROM sessions WHERE id = $1", session["id"]
        )
        assert str(stored) == DEFAULT_TENANT

    async def test_a_whole_turn_persists(self, app_db, as_default_tenant):
        """session -> message -> tool_calls -> artifact, the real chat path."""
        from uuid import UUID

        session = await repo.create_session(title="full turn")
        as_default_tenant.append(session["id"])
        sid = UUID(session["id"])

        message = await repo.add_message(
            sid, role="assistant", content="grounded answer [S1]",
            provider="fake", model="fake-1", tokens_in=10, tokens_out=5,
        )
        mid = UUID(message["id"])

        class _Call:
            name, args, result_summary = "retrieve", {"q": "x"}, {"hits": 3}
            duration_ms, ok, error = 12, True, None

        await repo.record_tool_calls(sid, mid, [_Call()])
        await repo.add_artifact(
            sid, message_id=mid, kind="markdown", title="notes",
            content_raw="# hi", content_sanitized="<h1>hi</h1>", sanitizer_report={},
        )

        # Every row from the turn belongs to the tenant that created it.
        for table in ("messages", "tool_calls", "artifacts"):
            tenants = await app_db.fetch(
                f"SELECT DISTINCT tenant_id FROM {table} WHERE session_id = $1", sid
            )
            assert [str(r["tenant_id"]) for r in tenants] == [DEFAULT_TENANT], table


class TestWritesRefuseWithoutATenant:
    """A write with no tenant must fail as a wiring error, not as a constraint
    violation forty lines deeper — and certainly not by silently picking one."""

    async def test_create_session_refuses(self, app_db):
        set_tenant_id("-")
        with pytest.raises(TenantRequiredError):
            await repo.create_session(title="no tenant")

    async def test_the_error_names_the_actual_problem(self, app_db):
        set_tenant_id("-")
        with pytest.raises(TenantRequiredError) as exc:
            await repo.create_session(title="no tenant")
        # The hint has to be actionable; "null value in column tenant_id" sent
        # the last person reading it to the migration instead of the wiring.
        assert "require_tenant" in exc.value.detail["hint"]

    async def test_a_malformed_tenant_is_rejected_before_the_database(self, app_db):
        set_tenant_id("not-a-uuid")
        with pytest.raises(TenantRequiredError):
            await repo.create_session(title="bad tenant")
