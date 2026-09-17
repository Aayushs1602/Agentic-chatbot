"""Pricing arithmetic and the usage ledger.

The pricing half runs without a database. The ledger half is `db`-marked,
because the thing most likely to be wrong is the SQL — as P1 has now
demonstrated three separate times.
"""

from __future__ import annotations

import pytest

from app.providers import pricing


@pytest.fixture(autouse=True)
def _clear_rate_cache():
    pricing.reset_cache()
    yield
    pricing.reset_cache()


class TestPricing:
    def test_local_inference_is_free_and_says_so(self):
        # Free because it is true, not because it is unknown — the two must
        # stay distinguishable.
        assert pricing.is_priced("ollama", "qwen2.5:3b-instruct-q4_K_M")
        assert pricing.cost_micros("ollama", "qwen2.5:3b", 10_000, 5_000) == 0

    def test_an_unconfigured_hosted_model_is_unpriced_not_free(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "model_prices", "")
        pricing.reset_cache()
        assert pricing.rate_for("cloud", "some-hosted-model") is None
        assert not pricing.is_priced("cloud", "some-hosted-model")
        # It still costs 0 in the ledger, which is exactly why /usage has to
        # report it separately rather than letting it vanish into the total.
        assert pricing.cost_micros("cloud", "some-hosted-model", 1_000, 1_000) == 0

    def test_configured_rates_are_applied(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(
            settings, "model_prices",
            '{"m1": {"in": 3000000, "out": 15000000}}',
        )
        pricing.reset_cache()
        # 1M in at 3.0/Mtok + 1M out at 15.0/Mtok = 18.0 -> 18_000_000 micros
        assert pricing.cost_micros("cloud", "m1", 1_000_000, 1_000_000) == 18_000_000
        assert pricing.is_priced("cloud", "m1")

    def test_small_turns_do_not_round_to_zero(self, monkeypatch):
        """Multiply first, divide once.

        Dividing the rate down to a per-token price before multiplying would
        floor a 3.0/Mtok rate to 0 and make every small turn free.
        """
        from app.config import settings

        monkeypatch.setattr(
            settings, "model_prices", '{"m1": {"in": 3000000, "out": 15000000}}'
        )
        pricing.reset_cache()
        # 500 * 3_000_000 // 1e6 = 1500;  200 * 15_000_000 // 1e6 = 3000
        assert pricing.cost_micros("cloud", "m1", 500, 200) == 4_500

    def test_costs_are_integers(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(
            settings, "model_prices", '{"m1": {"in": 1234567, "out": 7654321}}'
        )
        pricing.reset_cache()
        cost = pricing.cost_micros("cloud", "m1", 777, 333)
        assert isinstance(cost, int) and not isinstance(cost, bool)

    def test_malformed_config_degrades_instead_of_crashing(self, monkeypatch):
        # Bad pricing config makes the ledger incomplete. It must not stop the
        # API from serving requests.
        from app.config import settings

        monkeypatch.setattr(settings, "model_prices", "{not json at all")
        pricing.reset_cache()
        assert pricing.cost_micros("cloud", "whatever", 100, 100) == 0


@pytest.mark.db
class TestLedger:
    DEFAULT = "00000000-0000-0000-0000-000000000001"
    OTHER = "00000000-0000-0000-0000-00000000cccc"

    @pytest.fixture
    async def ledger(self, app_db):
        from app.logging import set_tenant_id

        await app_db.execute(
            "INSERT INTO tenants (id, slug, name) VALUES ($1,'usage-test','Usage Test') "
            "ON CONFLICT (id) DO NOTHING",
            self.OTHER,
        )
        set_tenant_id(self.DEFAULT)
        yield app_db
        await app_db.execute(
            "DELETE FROM usage_events WHERE model LIKE 'pytest-%'"
        )
        await app_db.execute("DELETE FROM tenants WHERE id = $1", self.OTHER)
        set_tenant_id("-")

    async def test_a_turn_is_recorded_against_its_tenant(self, ledger, monkeypatch):
        from app.config import settings
        from app.db import usage

        monkeypatch.setattr(
            settings, "model_prices",
            '{"pytest-model": {"in": 2000000, "out": 6000000}}',
        )
        pricing.reset_cache()

        await usage.record_turn(
            session_id=None, message_id=None, provider="cloud",
            model="pytest-model", tokens_in=1_000_000, tokens_out=1_000_000,
            latency_ms=1234,
        )

        row = await ledger.fetchrow(
            "SELECT tenant_id, cost_micros, tokens_in, latency_ms "
            "FROM usage_events WHERE model = 'pytest-model'"
        )
        assert str(row["tenant_id"]) == self.DEFAULT
        assert row["cost_micros"] == 8_000_000
        assert row["latency_ms"] == 1234

    async def test_summary_flags_unpriced_models(self, ledger, monkeypatch):
        from app.config import settings
        from app.db import usage

        monkeypatch.setattr(settings, "model_prices", "")
        pricing.reset_cache()

        await usage.record_turn(
            session_id=None, message_id=None, provider="cloud",
            model="pytest-unknown", tokens_in=5_000, tokens_out=5_000,
        )
        summary = await usage.summary()

        # Tokens were spent and the cost reads 0. A caller must be able to see
        # that the total is understated rather than inferring a free month.
        assert "pytest-unknown" in summary["unpriced_models"]
        assert summary["cost_complete"] is False
        assert summary["totals"]["tokens_in"] >= 5_000

    async def test_aggregated_cost_stays_an_integer(self, ledger, monkeypatch):
        """Money must not become a float on the way out.

        `sum()` over a bigint returns numeric, asyncpg maps numeric to Decimal,
        and FastAPI's encoder serialises Decimal as a float. Without the
        `::bigint` casts in usage.py, every total in the JSON response is a
        float — the exact failure the cost_micros column was chosen to prevent.
        """
        from decimal import Decimal

        from app.config import settings
        from app.db import usage

        monkeypatch.setattr(
            settings, "model_prices",
            '{"pytest-model": {"in": 2000000, "out": 6000000}}',
        )
        pricing.reset_cache()
        await usage.record_turn(
            session_id=None, message_id=None, provider="cloud",
            model="pytest-model", tokens_in=999_999, tokens_out=999_999,
        )

        summary = await usage.summary()
        total = summary["totals"]["cost_micros"]
        assert isinstance(total, int), f"cost_micros came back as {type(total).__name__}"
        assert not isinstance(total, (float, Decimal))
        for row in summary["models"]:
            assert isinstance(row["cost_micros"], int)

        for day in await usage.daily():
            assert isinstance(day["cost_micros"], int)

    async def test_a_recording_failure_never_breaks_the_turn(self, ledger, monkeypatch):
        # The user already has their answer; a ledger write must not be able to
        # turn a delivered response into a 500.
        from app.db import usage

        async def _boom(*_a, **_k):
            raise RuntimeError("database on fire")

        monkeypatch.setattr(usage.db, "execute", _boom)
        await usage.record_turn(
            session_id=None, message_id=None, provider="cloud",
            model="pytest-model", tokens_in=1, tokens_out=1,
        )  # must not raise
