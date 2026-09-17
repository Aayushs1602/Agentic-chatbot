"""The usage ledger: what each turn consumed, and what it cost.

One row per billable event. `messages` already records tokens for the *chat*
transcript; this table exists separately because the two answer different
questions and have different lifetimes. A message belongs to a conversation and
disappears when the user deletes it; a ledger row belongs to an accounting
period and must survive that deletion, or a tenant could erase their own bill.

That is why `session_id` and `message_id` are `ON DELETE SET NULL` rather than
`CASCADE` in 002_tenancy.sql — the link goes, the charge stays.

RLS applies to this table like any other, so a tenant reads only its own usage
and the aggregation queries below need no tenant predicate of their own.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.db import pool as db
from app.logging import get_logger, get_request_id
from app.providers.pricing import cost_micros, is_priced
from app.tenancy import current_tenant

log = get_logger("db.usage")


async def record_turn(
    *,
    session_id: Optional[UUID],
    message_id: Optional[UUID],
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: Optional[int] = None,
    fell_back_from: Optional[str] = None,
    event_type: str = "chat",
) -> None:
    """Record one turn. Never raises.

    Best-effort by the same reasoning as `record_tool_calls`: the user already
    has their answer, and failing the request to report on it would trade a
    working product for a complete ledger. A dropped row is logged loudly
    enough to notice, and `/usage` reports the gap rather than hiding it.
    """
    try:
        await db.execute(
            """
            INSERT INTO usage_events (
                tenant_id, request_id, session_id, message_id, event_type,
                provider, model, tokens_in, tokens_out, latency_ms,
                cost_micros, fell_back_from
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            current_tenant(),
            get_request_id(),
            session_id,
            message_id,
            event_type,
            provider,
            model,
            tokens_in,
            tokens_out,
            latency_ms,
            cost_micros(provider, model, tokens_in, tokens_out),
            fell_back_from,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("usage_not_recorded", error=str(exc), model=model)


# ── Aggregation ─────────────────────────────────────────────────────────


def _window(since: Optional[date], until: Optional[date]) -> tuple[datetime, datetime]:
    """Default to the last 30 days. `until` is inclusive of its whole day."""
    now = datetime.now(timezone.utc)
    start = (
        datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
        if since
        else now - timedelta(days=30)
    )
    end = (
        datetime.combine(until, datetime.min.time(), tzinfo=timezone.utc)
        + timedelta(days=1)
        if until
        else now
    )
    return start, end


async def summary(
    *, since: Optional[date] = None, until: Optional[date] = None
) -> Dict[str, Any]:
    """Totals and a per-model breakdown for the calling tenant."""
    start, end = _window(since, until)

    totals = await db.fetchrow(
        """
        SELECT count(*)                        AS events,
               coalesce(sum(tokens_in),  0)    AS tokens_in,
               coalesce(sum(tokens_out), 0)    AS tokens_out,
               -- ::bigint is load-bearing. sum() over a bigint returns
               -- numeric, asyncpg maps numeric to Decimal, and FastAPI's
               -- encoder serialises Decimal as a float -- which is exactly the
               -- thing this column exists to avoid. The cast keeps it an int
               -- all the way to the JSON.
               coalesce(sum(cost_micros), 0)::bigint AS cost_micros,
               count(*) FILTER (WHERE fell_back_from IS NOT NULL) AS fallbacks
        FROM usage_events
        WHERE created_at >= $1 AND created_at < $2
        """,
        start, end,
    )

    rows = await db.fetch(
        """
        SELECT provider, model,
               count(*)                      AS events,
               coalesce(sum(tokens_in),  0)  AS tokens_in,
               coalesce(sum(tokens_out), 0)  AS tokens_out,
               coalesce(sum(cost_micros), 0)::bigint AS cost_micros,
               -- p95 matters more than the mean here: the mean hides the
               -- turns that made someone close the tab.
               coalesce(
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms), 0
               ) AS p95_latency_ms
        FROM usage_events
        WHERE created_at >= $1 AND created_at < $2
        GROUP BY provider, model
        ORDER BY sum(cost_micros) DESC, count(*) DESC
        """,
        start, end,
    )

    models: List[Dict[str, Any]] = []
    unpriced: List[str] = []
    for r in rows:
        priced = is_priced(r["provider"], r["model"])
        if not priced:
            unpriced.append(r["model"])
        models.append({**dict(r), "priced": priced})

    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "totals": dict(totals),
        "models": models,
        # Surfaced, not buried. Tokens were spent on these and the cost column
        # reads 0, so a total that omits them is understated — and a caller who
        # cannot see that will read "0" as "free".
        "unpriced_models": sorted(set(unpriced)),
        "cost_complete": not unpriced,
    }


async def daily(
    *, since: Optional[date] = None, until: Optional[date] = None
) -> List[Dict[str, Any]]:
    """Per-day series, for the dashboard P8 will build."""
    start, end = _window(since, until)
    rows = await db.fetch(
        """
        SELECT date_trunc('day', created_at)::date AS day,
               count(*)                      AS events,
               coalesce(sum(tokens_in),  0)  AS tokens_in,
               coalesce(sum(tokens_out), 0)  AS tokens_out,
               coalesce(sum(cost_micros), 0)::bigint AS cost_micros
        FROM usage_events
        WHERE created_at >= $1 AND created_at < $2
        GROUP BY 1 ORDER BY 1
        """,
        start, end,
    )
    return [dict(r) for r in rows]
