"""API key format, hashing, and the auth dependency.

Everything here runs without Postgres: the key functions are pure, and the
dependency is exercised against a stubbed repository. The parts that genuinely
need a database — RLS policies, the `authenticate` query — are marked `db` and
skip themselves, per the contract in conftest.py.
"""

from __future__ import annotations

import pytest

from app.api import deps
from app.errors import UnauthorizedError
from app.security.api_keys import (
    generate_key,
    hash_key,
    key_prefix,
    looks_like_key,
    parse_bearer,
)


class _Request:
    """The two attributes `require_tenant` actually touches."""

    def __init__(self, authorization: str | None = None) -> None:
        self.headers = {} if authorization is None else {"authorization": authorization}


class TestKeyFormat:
    def test_generated_key_has_the_documented_shape(self):
        key = generate_key()
        assert key.plaintext.startswith("sk_live_")
        # 32 bytes -> 43 unpadded base64url characters.
        assert len(key.plaintext.split("_", 2)[2]) == 43
        assert looks_like_key(key.plaintext)

    def test_env_appears_in_the_key(self):
        assert generate_key("dev").plaintext.startswith("sk_dev_")

    def test_keys_are_unique(self):
        keys = {generate_key().plaintext for _ in range(200)}
        assert len(keys) == 200

    def test_hash_is_stable_and_distinguishing(self):
        key = generate_key()
        assert hash_key(key.plaintext) == hash_key(key.plaintext) == key.hash
        assert hash_key(key.plaintext) != hash_key(generate_key().plaintext)
        assert len(key.hash) == 64  # sha256 hex

    def test_the_stored_fields_do_not_contain_the_key(self):
        # The whole point: a database dump must not yield working credentials.
        key = generate_key()
        secret_tail = key.plaintext.split("_", 2)[2]
        assert secret_tail not in key.hash
        assert secret_tail not in key.prefix
        assert key.plaintext not in key.hash

    def test_prefix_identifies_without_revealing(self):
        key = generate_key()
        assert key.prefix.startswith("sk_live_")
        assert len(key.prefix) == len("sk_live_") + 4
        assert key.plaintext.startswith(key.prefix)

    @pytest.mark.parametrize(
        "junk",
        ["", "hunter2", "sk_", "sk_live", "sk_live_short", "Bearer sk_live_x",
         "pk_live_" + "a" * 43],
    )
    def test_shape_check_rejects_junk(self, junk):
        assert not looks_like_key(junk)

    def test_prefix_of_a_malformed_key_does_not_raise(self):
        # It is called on the failure path, where the input is by definition
        # not trustworthy.
        assert key_prefix("garbage") == "garbage"


class TestBearerParsing:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("Bearer sk_live_abc", "sk_live_abc"),
            ("bearer sk_live_abc", "sk_live_abc"),
            ("BEARER sk_live_abc", "sk_live_abc"),
            ("  Bearer   sk_live_abc  ", "sk_live_abc"),
            ("sk_live_abc", "sk_live_abc"),  # bare keys are accepted
            (None, None),
            ("", None),
            ("Bearer ", None),
        ],
    )
    def test_parsing(self, header, expected):
        assert parse_bearer(header) == expected


class TestRequireTenant:
    async def test_missing_key_is_rejected_when_auth_is_required(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "auth_required", True)
        with pytest.raises(UnauthorizedError):
            await deps.require_tenant(_Request())

    async def test_missing_key_falls_back_to_default_tenant_when_auth_is_off(
        self, monkeypatch
    ):
        from app.config import settings
        from app.logging import get_tenant_id

        monkeypatch.setattr(settings, "auth_required", False)
        principal = await deps.require_tenant(_Request())
        assert principal["tenant_id"] == deps.DEFAULT_TENANT_ID
        assert get_tenant_id() == str(deps.DEFAULT_TENANT_ID)

    async def test_malformed_key_never_reaches_the_database(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "auth_required", True)
        called = False

        async def _should_not_run(_plaintext):
            nonlocal called
            called = True
            return None

        monkeypatch.setattr(deps.tenant_repo, "authenticate", _should_not_run)
        with pytest.raises(UnauthorizedError):
            await deps.require_tenant(_Request("Bearer nonsense"))
        assert not called, "a malformed header should cost a string compare, not a query"

    async def test_valid_key_resolves_and_binds_the_tenant(self, monkeypatch):
        from app.config import settings
        from app.logging import get_tenant_id, set_tenant_id

        monkeypatch.setattr(settings, "auth_required", True)
        set_tenant_id("-")
        key = generate_key()

        async def _ok(plaintext):
            assert plaintext == key.plaintext
            return {"tenant_id": deps.DEV_TENANT_ID, "slug": "dev", "status": "active"}

        monkeypatch.setattr(deps.tenant_repo, "authenticate", _ok)
        principal = await deps.require_tenant(_Request(f"Bearer {key.plaintext}"))
        assert principal["slug"] == "dev"
        # Bound for the logs and, next, for `SET LOCAL app.tenant_id`.
        assert get_tenant_id() == str(deps.DEV_TENANT_ID)

    async def test_unknown_key_is_rejected(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "auth_required", True)

        async def _none(_plaintext):
            return None

        monkeypatch.setattr(deps.tenant_repo, "authenticate", _none)
        with pytest.raises(UnauthorizedError):
            await deps.require_tenant(_Request(f"Bearer {generate_key().plaintext}"))

    async def test_every_rejection_looks_identical(self, monkeypatch):
        """No enumeration oracle.

        If "unknown key" and "revoked key" read differently, an attacker learns
        which of their guesses named a real key.
        """
        from app.config import settings

        monkeypatch.setattr(settings, "auth_required", True)

        async def _none(_plaintext):
            return None

        monkeypatch.setattr(deps.tenant_repo, "authenticate", _none)

        messages = []
        for header in (None, "Bearer nonsense", f"Bearer {generate_key().plaintext}"):
            with pytest.raises(UnauthorizedError) as exc:
                await deps.require_tenant(_Request(header))
            messages.append((exc.value.message, exc.value.detail, exc.value.status_code))

        assert len(set(map(str, messages))) == 1

    async def test_optional_tenant_tolerates_anonymity(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "auth_required", True)
        assert await deps.optional_tenant(_Request()) is None

    async def test_the_key_is_never_logged_in_full(self, monkeypatch, capsys):
        # A credential in a log aggregator is a credential you cannot rotate
        # out of, because you do not know everywhere it was shipped.
        from app.config import settings

        monkeypatch.setattr(settings, "auth_required", True)

        async def _none(_plaintext):
            return None

        monkeypatch.setattr(deps.tenant_repo, "authenticate", _none)
        key = generate_key()
        with pytest.raises(UnauthorizedError):
            await deps.require_tenant(_Request(f"Bearer {key.plaintext}"))

        emitted = capsys.readouterr()
        secret_tail = key.plaintext.split("_", 2)[2]
        assert secret_tail not in emitted.out + emitted.err


@pytest.mark.db
class TestAuthenticateAgainstPostgres:
    """The queries, against a real database.

    These exist because the pure-unit tests above are structurally blind to a
    whole class of defect. `authenticate` shipped binding its staleness window
    as the string "60 seconds"; asyncpg types `$n::interval` as a real interval
    and raised DataError on the first call. Every unit test still passed,
    because not one of them reaches Postgres.
    """

    @pytest.fixture
    async def tenant(self, app_db):
        from app.db import tenants as repo

        app_pool = app_db

        row = await repo.create_tenant(slug="pytest-tenancy", name="pytest")
        yield row
        await app_pool.execute("DELETE FROM tenants WHERE slug = 'pytest-tenancy'")

    async def test_full_key_lifecycle(self, tenant):
        from app.db import pool as app_pool
        from app.db import tenants as repo

        generated, row = await repo.create_api_key(tenant["id"], name="pytest")

        principal = await repo.authenticate(generated.plaintext)
        assert principal is not None
        assert principal["tenant_id"] == tenant["id"]
        assert principal["key_name"] == "pytest"

        # The plaintext must not be recoverable from what was stored.
        stored = await app_pool.fetchrow(
            "SELECT key_hash, key_prefix FROM api_keys WHERE id = $1", row["id"]
        )
        assert generated.plaintext not in (stored["key_hash"], stored["key_prefix"])
        assert stored["key_hash"] == hash_key(generated.plaintext)

        assert await repo.authenticate("sk_live_" + "x" * 43) is None

        await repo.revoke_api_key(tenant["id"], row["id"])
        assert await repo.authenticate(generated.plaintext) is None

    async def test_last_used_is_written_once_then_throttled(self, tenant):
        from app.db import pool as app_pool
        from app.db import tenants as repo

        generated, row = await repo.create_api_key(tenant["id"])
        await repo.authenticate(generated.plaintext)
        first = await app_pool.fetchval(
            "SELECT last_used_at FROM api_keys WHERE id = $1", row["id"]
        )
        assert first is not None

        # Within the staleness window, a second auth must not write again —
        # otherwise every authenticated request carries a row update.
        await repo.authenticate(generated.plaintext)
        second = await app_pool.fetchval(
            "SELECT last_used_at FROM api_keys WHERE id = $1", row["id"]
        )
        assert first == second

    async def test_a_suspended_tenant_cannot_authenticate(self, tenant):
        from app.db import pool as app_pool
        from app.db import tenants as repo

        generated, _ = await repo.create_api_key(tenant["id"])
        await app_pool.execute(
            "UPDATE tenants SET status = 'suspended' WHERE id = $1", tenant["id"]
        )
        assert await repo.authenticate(generated.plaintext) is None

    async def test_revocation_is_scoped_to_the_owning_tenant(self, tenant):
        # Without the tenant_id predicate, knowing any key's UUID would be
        # enough to revoke it out of someone else's account.
        from app.db import pool as app_pool
        from app.db import tenants as repo
        from app.errors import NotFoundError

        _, row = await repo.create_api_key(tenant["id"])
        attacker = await repo.create_tenant(slug="pytest-attacker", name="attacker")
        try:
            with pytest.raises(NotFoundError):
                await repo.revoke_api_key(attacker["id"], row["id"])
        finally:
            await app_pool.execute("DELETE FROM tenants WHERE slug = 'pytest-attacker'")


class TestMigrationOrdering:
    def test_tenancy_migration_is_discovered_after_init(self):
        from app.db.migrate import _migration_files

        names = [p.stem for p in _migration_files()]
        assert "002_tenancy" in names
        assert names.index("001_init") < names.index("002_tenancy")
