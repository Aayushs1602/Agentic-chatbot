"""Measure the PRD's product success metric, end to end.

    docker compose exec backend python -m scripts.evaluate

Runs the golden set through the **whole agent loop** — routing, retrieval, the
relevance gate, generation, and citation resolution — and reports:

* **grounded rate** — in-corpus questions returning at least one *resolved*
  citation. Target >= 90%.
* **refusal rate** — out-of-corpus questions correctly declined. Target 5/5.

This is the acceptance test for the product's core promise, and the thing to
re-run after touching a prompt, a skill, or the retrieval configuration. It
needs a live model, so it is a script rather than part of the pytest suite —
which must stay runnable with no Ollama.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agent.orchestrator import Orchestrator, TurnResult
from app.logging import configure_logging
from app.providers.registry import get_registry

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "data" / "golden_set.json"


async def run_case(case: Dict[str, Any], provider) -> Dict[str, Any]:
    result = TurnResult()
    started = time.perf_counter()
    async for _ in Orchestrator(provider).run(case["question"], result=result):
        pass
    elapsed = time.perf_counter() - started

    grounded = bool(result.citations)
    expect = case["expect"]
    # A refusal that cites nothing is correct for an out-of-corpus question and
    # wrong for an in-corpus one, so success is direction-dependent.
    ok = (grounded and not result.abstained) if expect == "grounded" else result.abstained

    return {
        "id": case["id"],
        "expect": expect,
        "abstained": result.abstained,
        "citations": len(result.citations),
        "intent": result.intent,
        "seconds": round(elapsed, 1),
        "ok": ok,
    }


async def evaluate(limit: Optional[int] = None) -> int:
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    if limit:
        cases = cases[:limit]

    provider, _ = await get_registry().resolve()
    print(f"provider : {provider.id} / {provider.model}")
    print(f"cases    : {len(cases)}\n")

    rows: List[Dict[str, Any]] = []
    for case in cases:
        row = await run_case(case, provider)
        rows.append(row)
        print(
            f"  {'PASS' if row['ok'] else 'FAIL'}  {row['id']:28s} "
            f"{row['expect']:9s} cites={row['citations']:2d} "
            f"abstained={str(row['abstained']):5s} {row['seconds']:6.1f}s"
        )

    grounded = [r for r in rows if r["expect"] == "grounded"]
    abstain = [r for r in rows if r["expect"] == "abstain"]
    grounded_ok = sum(r["ok"] for r in grounded)
    abstain_ok = sum(r["ok"] for r in abstain)
    rate = 100 * grounded_ok / max(1, len(grounded))

    print("\n" + "=" * 62)
    print(f"grounded rate : {grounded_ok}/{len(grounded)} ({rate:.0f}%)   target >= 90%")
    print(f"refusal rate  : {abstain_ok}/{len(abstain)}          target 5/5")
    if rows:
        print(f"median latency: {sorted(r['seconds'] for r in rows)[len(rows) // 2]:.1f}s")

    passed = rate >= 90 and abstain_ok == len(abstain)
    print(f"\n{'MET' if passed else 'NOT MET'}: PRD success metric")
    return 0 if passed else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.evaluate")
    parser.add_argument("--limit", type=int, help="only the first N cases")
    args = parser.parse_args(argv)

    configure_logging()
    try:
        return asyncio.run(evaluate(limit=args.limit))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
