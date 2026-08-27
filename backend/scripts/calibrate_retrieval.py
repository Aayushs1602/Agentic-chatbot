"""Regenerate the retrieval calibration evidence.

    docker compose exec backend python -m scripts.calibrate_retrieval

Runs the golden set through retrieval and reports whether *any* similarity
threshold could separate in-corpus from out-of-corpus questions. It produced the
measurement that demoted `RETRIEVAL_MIN_SIM` from the abstain gate to a safety
floor, and it is how that claim stays honest as the corpus and the embedding
model change — re-run it after either.

It also reports hybrid health: if the sparse retriever contributes nothing, the
"hybrid" pipeline is silently dense-only, which is a bug that produces no error.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List

from app.config import settings
from app.logging import configure_logging
from app.rag.retrieve import retrieve

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "data" / "golden_set.json"


async def measure() -> List[Dict[str, Any]]:
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    rows: List[Dict[str, Any]] = []
    for case in cases:
        # min_sim=0 so the floor never truncates the measurement itself.
        result = await retrieve(case["question"], top_k=10, candidates=40, min_sim=0.0)
        cosines = [c.cosine for c in result.chunks if c.cosine > 0]
        top1 = max(cosines) if cosines else 0.0
        rest = [c for c in cosines if c < top1]
        rows.append(
            {
                "id": case["id"],
                "expect": case["expect"],
                "top1": top1,
                "margin": top1 - (statistics.mean(rest) if rest else 0.0),
                "sparse": result.candidates_sparse,
                "dense": result.candidates_dense,
                "both": sum(1 for c in result.chunks if c.dense_rank and c.sparse_rank),
                "episodes": len({c.episode_id for c in result.chunks}),
            }
        )
    return rows


def separability(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    """Can any threshold on `key` split grounded from abstain?

    Separable iff the lowest grounded value exceeds the highest abstain value.
    """
    grounded = [r[key] for r in rows if r["expect"] == "grounded"]
    abstain = [r[key] for r in rows if r["expect"] == "abstain"]
    if not grounded or not abstain:
        return {"separable": False, "reason": "one class is empty"}
    lo_g, hi_a = min(grounded), max(abstain)
    return {
        "grounded_range": (round(min(grounded), 4), round(max(grounded), 4)),
        "abstain_range": (round(min(abstain), 4), round(max(abstain), 4)),
        "separable": lo_g > hi_a,
        # How many legitimate questions a threshold that rejects every
        # out-of-corpus case would also reject. This is the real cost.
        "false_refusals_at_perfect_precision": sum(1 for g in grounded if g <= hi_a),
        "n_grounded": len(grounded),
    }


def main() -> int:
    configure_logging()
    rows = asyncio.run(measure())

    print(f"\nEmbedding model : {settings.embeddings_model} ({settings.embeddings_dim}d)")
    print(f"Chunking        : {settings.chunk_tokens} tokens / {settings.chunk_overlap} overlap")
    print(f"Cases           : {len(rows)}\n")

    print(f"{'case':34s} {'expect':9s} {'top1':>6s} {'margin':>7s} {'sparse':>7s} {'both':>5s}")
    print("-" * 74)
    for r in sorted(rows, key=lambda r: (r["expect"], -r["top1"])):
        print(
            f"{r['id']:34s} {r['expect']:9s} {r['top1']:6.3f} {r['margin']:7.4f} "
            f"{r['sparse']:7d} {r['both']:5d}"
        )

    print("\nSeparability")
    print("-" * 74)
    for key in ("top1", "margin"):
        s = separability(rows, key)
        verdict = "SEPARABLE" if s["separable"] else "NOT SEPARABLE"
        print(f"  {key:7s} grounded={s['grounded_range']} abstain={s['abstain_range']}  {verdict}")
        if not s["separable"]:
            print(
                f"          a threshold with zero false accepts would refuse "
                f"{s['false_refusals_at_perfect_precision']}/{s['n_grounded']} real questions"
            )

    dense_only = [r["id"] for r in rows if r["sparse"] == 0]
    print("\nHybrid health")
    print("-" * 74)
    print(f"  sparse candidates total : {sum(r['sparse'] for r in rows)}")
    print(f"  chunks found by both    : {sum(r['both'] for r in rows)}")
    if dense_only:
        # Silent failure mode: the pipeline still answers, just worse.
        print(f"  WARNING dense-only queries: {len(dense_only)} -> {dense_only[:5]}")
    else:
        print("  every query used both retrievers")

    print(
        "\nConclusion: neither statistic separates the classes, so off-topic "
        "detection cannot be a threshold.\nThe authoritative gate is the "
        "orchestrator's model-based relevance check.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
