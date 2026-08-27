"""Experiment: does parent-child (small-to-big) retrieval help?

    docker compose exec backend python -m scripts.try_parent_chunks

The idea: retrieve on small chunks for precision, then expand each hit to
include its neighbours before showing it to the model. A 400-token chunk often
lands mid-conversation; the surrounding turns are what make it judgeable.

**No schema change is needed to test this.** `chunks` already carries
`(episode_id, ord)`, so a chunk's parent context is simply its adjacent
siblings. If the experiment pays off, the implementation is a neighbour lookup
in `retrieve.py` — still no migration, still no re-ingest.

Measured against the relevance gate, because that is where the remaining golden
-set failures happen: three questions the corpus covers are refused before
generation. If expansion flips those to answerable without flipping the
out-of-corpus questions, it is worth shipping. Run before building, like the
reranker — which this project benchmarked first and then did not ship.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Dict, List

from app.agent.router import check_relevance
from app.db import pool as db
from app.logging import configure_logging, get_logger
from app.providers.registry import get_registry
from app.rag.retrieve import format_context, retrieve

log = get_logger("try_parent")

# The three golden-set questions that still fail, plus passers to check for
# regressions, plus out-of-corpus questions to check precision is preserved.
CASES: List[Dict[str, str]] = [
    {"id": "first-pm-hire", "q": "When should a startup hire its first product manager?", "want": "yes"},
    {"id": "pm-interview", "q": "How should I evaluate product managers during interviews?", "want": "yes"},
    {"id": "roadmap-prioritization", "q": "How do experienced PMs prioritize a roadmap when everything feels urgent?", "want": "yes"},
    {"id": "pmf-signal", "q": "How do I know when I've actually found product-market fit?", "want": "yes"},
    {"id": "pricing-change", "q": "How should a company approach changing its pricing model?", "want": "yes"},
    {"id": "north-star", "q": "How do you pick a north star metric that doesn't get gamed?", "want": "yes"},
    {"id": "oos-code", "q": "Write me a Python function that reverses a linked list.", "want": "no"},
    {"id": "oos-weather", "q": "What is the weather forecast for Mumbai tomorrow?", "want": "no"},
    {"id": "oos-quantum", "q": "Explain the mathematics of quantum chromodynamics.", "want": "no"},
]

# Explicit casts: asyncpg cannot infer a type for `$2 - $3` when both
# parameters are unknown, and episode_id arrives as a str from RetrievedChunk.
_NEIGHBOURS = """
SELECT c.ord, c.text
FROM chunks c
WHERE c.episode_id = $1::uuid
  AND c.ord BETWEEN ($2::int - $3::int) AND ($2::int + $3::int)
ORDER BY c.ord
"""


async def expand(chunks, window: int) -> str:
    """Rebuild the context with each hit widened to include its neighbours."""
    parts = []
    for chunk in chunks:
        rows = await db.fetch(_NEIGHBOURS, chunk.episode_id, chunk.ord, window)
        # Chunks overlap by design, so joining raw text duplicates sentences.
        # Good enough for measuring whether more context helps.
        body = " ".join(r["text"] for r in rows)
        header = f"{chunk.episode_title}"
        if chunk.guests:
            header += f" — {', '.join(chunk.guests)}"
        parts.append(f'<source id="{chunk.marker}">\n{header}\n\n{body}\n</source>')
    return "\n\n".join(parts)


async def main() -> int:
    provider, _ = await get_registry().resolve()
    print(f"provider: {provider.id} / {provider.model}\n")
    print(f"{'case':26s} {'want':5s} {'base':6s} {'w=1':6s} {'w=2':6s}")
    print("-" * 56)

    tally = {"base": 0, "w1": 0, "w2": 0}
    for case in CASES:
        result = await retrieve(case["q"])
        if not result.chunks:
            print(f"{case['id']:26s} {case['want']:5s} (no retrieval)")
            continue

        base = await check_relevance(provider, case["q"], format_context(result.chunks))
        w1 = await check_relevance(provider, case["q"], await expand(result.chunks, 1))
        w2 = await check_relevance(provider, case["q"], await expand(result.chunks, 2))

        got = {"base": base.answerable, "w1": w1.answerable, "w2": w2.answerable}
        want_yes = case["want"] == "yes"
        for key, value in got.items():
            tally[key] += int(value == want_yes)

        mark = lambda v: ("YES" if v else "no ") + ("*" if v == want_yes else "!")  # noqa: E731
        print(
            f"{case['id']:26s} {case['want']:5s} "
            f"{mark(got['base']):6s} {mark(got['w1']):6s} {mark(got['w2']):6s}"
        )

    n = len(CASES)
    print("-" * 56)
    print(f"{'correct':26s} {'':5s} {tally['base']}/{n}    {tally['w1']}/{n}    {tally['w2']}/{n}")
    print("\n* = matches expectation, ! = does not")
    print("Ship only if a window beats base on the yes cases without losing the no cases.")
    return 0


if __name__ == "__main__":
    configure_logging()
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
