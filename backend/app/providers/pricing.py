"""What a turn cost.

Prices are **configuration, not constants**. Provider pricing changes on the
provider's schedule, and a ledger seeded with numbers someone half-remembered
at commit time produces confident, wrong invoices — which is strictly worse
than a ledger that says it does not know.

So the built-in map contains only what is true by definition: local inference
through Ollama costs no API dollars. Every hosted model is **unpriced** until
someone supplies a rate via `MODEL_PRICES`, and unpriced usage is recorded with
`cost_micros = 0` *and* reported separately by `/usage`, so a zero total can
never be mistaken for a free month.

Units: micros (millionths of a currency unit) per **million** tokens, as
integers. Money never touches a float — not here, not in the column, not in the
aggregate. `1_000_000` micros = 1 unit of currency, so a rate of `3_000_000`
means 3 currency units per million tokens.

Configure with `MODEL_PRICES` as JSON:

    MODEL_PRICES={"some-model":{"in":3000000,"out":15000000}}

Check the provider's current pricing page before trusting any number here for
anything that reaches a customer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Optional

from app.config import settings
from app.logging import get_logger

log = get_logger("pricing")


@dataclass(frozen=True)
class Rate:
    """Micros per million tokens, input and output."""

    input_per_mtok: int
    output_per_mtok: int


# Zero because it is *true*, not because it is unknown: a model running on your
# own GPU bills nothing to any API. Electricity and wall-clock are real costs
# and this ledger does not claim to measure them.
_LOCAL_IS_FREE = Rate(0, 0)

# Deliberately not seeded with hosted-model rates. See the module docstring.
_DEFAULT_RATES: Dict[str, Rate] = {}

_rates_cache: Optional[Dict[str, Rate]] = None


def _load_rates() -> Dict[str, Rate]:
    global _rates_cache
    if _rates_cache is not None:
        return _rates_cache

    rates = dict(_DEFAULT_RATES)
    raw = (settings.model_prices or "").strip()
    if raw:
        try:
            for model, spec in json.loads(raw).items():
                rates[model] = Rate(int(spec["in"]), int(spec["out"]))
        except Exception as exc:  # noqa: BLE001
            # Bad pricing config must not take down request serving; it makes
            # the ledger incomplete, which /usage already reports.
            log.error(
                "model_prices_invalid",
                error=str(exc),
                hint='Expected {"model":{"in":<micros/Mtok>,"out":<micros/Mtok>}}',
            )
    _rates_cache = rates
    return rates


def reset_cache() -> None:
    """Drop the parsed rates. For tests, and for a future config reload."""
    global _rates_cache
    _rates_cache = None


def rate_for(provider: str, model: str) -> Optional[Rate]:
    """The rate for a model, or None when nothing has priced it.

    None is meaningfully different from `Rate(0, 0)`: the first means "we do
    not know what this cost", the second means "this genuinely cost nothing".
    Collapsing them is how a billing system quietly under-reports.
    """
    rates = _load_rates()
    if model in rates:
        return rates[model]
    if provider == "ollama":
        return _LOCAL_IS_FREE
    return None


def cost_micros(provider: str, model: str, tokens_in: int, tokens_out: int) -> int:
    """Cost of one turn in micros. Unpriced models cost 0 and say so."""
    rate = rate_for(provider, model)
    if rate is None:
        log.warning(
            "model_unpriced",
            provider=provider,
            model=model,
            hint="Add it to MODEL_PRICES; /usage reports it as unpriced.",
        )
        return 0
    # Integer arithmetic throughout: `tokens * rate` first, then one floor
    # division. Dividing first would round every small turn to zero.
    return (
        tokens_in * rate.input_per_mtok + tokens_out * rate.output_per_mtok
    ) // 1_000_000


def is_priced(provider: str, model: str) -> bool:
    return rate_for(provider, model) is not None
