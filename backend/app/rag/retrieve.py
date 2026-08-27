"""Hybrid retrieval: dense + sparse, fused with Reciprocal Rank Fusion.

Why hybrid. Dense retrieval understands paraphrase ("how do I know I've got
PMF" → "product-market fit") but misses rare literal tokens — company names,
frameworks, metric names — which is exactly what people ask a podcast corpus
about. Sparse `tsvector` search nails those and misses paraphrase. Fusing them
covers both, and Postgres already ships both, so this costs one extra index
rather than another service.

Why RRF rather than weighted score blending. Dense cosine and `ts_rank_cd`
produce scores on incomparable scales, so blending them needs a normalisation
constant that has to be re-tuned whenever either retriever changes. RRF only
uses *ranks*, so it needs no tuning and degrades gracefully when one retriever
returns nothing.

Why the abstain gate is NOT a similarity threshold. This was the original
design, and measurement killed it. Against the 20-question golden set on the
real corpus (arctic-embed-xs):

    in-corpus questions      top-1 cosine 0.617 - 0.756
    out-of-corpus questions  top-1 cosine 0.547 - 0.671

The ranges overlap across a third of the scale. "Write me a Python function
that reverses a linked list" scores 0.671 — higher than eleven of the fifteen
legitimate product questions. A top-1 margin gate (top-1 minus the mean of the
rest) separates no better: 0.011-0.074 for in-corpus versus 0.007-0.066 for
out-of-corpus. No threshold on either statistic admits every real question
while rejecting every out-of-domain one, because a bi-encoder trained on cosine
similarity compresses everything into a narrow high band; it measures "is this
text similar" and never "does this text answer the question".

So `RETRIEVAL_MIN_SIM` is demoted to a **safety floor**, not the domain gate.
It catches degenerate states — an empty corpus, a broken embedder, a query that
matches nothing at all — and is set low enough that it never fires on a real
question. The authoritative gate is a model-based relevance judgement in the
orchestrator: a short structured-output call asking whether the retrieved
passages actually answer the question, followed by post-generation citation
resolution. Reflection, not arithmetic.

The evidence lives in `docs/retrieval-calibration.md`; regenerate it with
`python -m scripts.calibrate_retrieval`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings
from app.db import pool as db
from app.logging import get_logger
from app.rag.embeddings import get_embedder

log = get_logger("retrieve")


@dataclass
class RetrievedChunk:
    chunk_id: str
    episode_id: str
    episode_title: str
    guests: List[str]
    youtube_url: Optional[str]
    video_id: Optional[str]
    published_on: Optional[str]
    text: str
    ord: int
    start_seconds: Optional[int]
    cosine: float = 0.0          # dense similarity, 0..1 — drives the abstain gate
    dense_rank: Optional[int] = None
    sparse_rank: Optional[int] = None
    rrf: float = 0.0
    marker: str = ""             # "S1".."S5", assigned after ranking

    @property
    def source_url(self) -> Optional[str]:
        """Deep link into the episode at the moment the claim was made."""
        if not self.youtube_url:
            return None
        if self.start_seconds is None:
            return self.youtube_url
        sep = "&" if "?" in self.youtube_url else "?"
        return f"{self.youtube_url}{sep}t={self.start_seconds}s"

    def to_citation(self) -> Dict[str, Any]:
        return {
            "marker": self.marker,
            "chunk_id": self.chunk_id,
            "episode_id": self.episode_id,
            "title": self.episode_title,
            "guests": self.guests,
            "url": self.source_url,
            "start_seconds": self.start_seconds,
            "score": round(self.cosine, 4),
        }


@dataclass
class RetrievalResult:
    chunks: List[RetrievedChunk] = field(default_factory=list)
    best_cosine: float = 0.0
    abstain: bool = False
    reason: Optional[str] = None
    candidates_dense: int = 0
    candidates_sparse: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.chunks


_DENSE_SQL = """
SELECT c.id, c.episode_id, c.text, c.ord, c.start_seconds,
       e.title, e.guests, e.youtube_url, e.video_id, e.published_on,
       1 - (c.embedding <=> $1::vector) AS cosine
FROM chunks c
JOIN episodes e ON e.id = c.episode_id
ORDER BY c.embedding <=> $1::vector
LIMIT $2
"""

# websearch_to_tsquery tolerates arbitrary user text without throwing, but it
# joins terms with AND. A natural-language question ("How do I know when I've
# actually found product-market fit?") then requires every term to co-occur in
# one chunk, which matches nothing — measured on the real corpus: 0 hits for
# that question, 725 with OR semantics. `build_sparse_query` converts the
# question into an OR clause; `ts_rank_cd` still rewards chunks that match more
# terms, more densely, so ranking survives the looser matching.
_SPARSE_SQL = """
SELECT c.id, c.episode_id, c.text, c.ord, c.start_seconds,
       e.title, e.guests, e.youtube_url, e.video_id, e.published_on,
       ts_rank_cd(c.tsv, q) AS rank
FROM chunks c
JOIN episodes e ON e.id = c.episode_id,
     websearch_to_tsquery('english', $1) AS q
WHERE c.tsv @@ q
ORDER BY rank DESC
LIMIT $2
"""


# Dropped before the OR clause is built. Postgres would discard these as
# stopwords anyway; removing them here keeps the clause short and keeps a
# question made entirely of stopwords from producing a match-everything query.
_STOPWORDS = frozenset(
    """a about all also am an and any are as at be because been but by can could did do does
    doing for from had has have he her here hers him his how i if in into is it its me my no
    nor not of on or our out over own she should so some such than that the their them then
    there these they this those through to too under up very was we were what when where
    which while who whom why will with would you your""".split()
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_MAX_SPARSE_TERMS = 24


def build_sparse_query(text: str) -> str:
    """Turn a natural-language question into an OR clause for websearch_to_tsquery.

    Returns "" when nothing meaningful survives, which the caller treats as
    "skip the sparse retriever" rather than "match everything".
    """
    seen: List[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group(0).strip("'-")
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.append(token)
        if len(seen) >= _MAX_SPARSE_TERMS:
            break
    return " OR ".join(seen)


def _row_to_chunk(row) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=str(row["id"]),
        episode_id=str(row["episode_id"]),
        episode_title=row["title"],
        guests=list(row["guests"] or []),
        youtube_url=row["youtube_url"],
        video_id=row["video_id"],
        published_on=row["published_on"].isoformat() if row["published_on"] else None,
        text=row["text"],
        ord=row["ord"],
        start_seconds=row["start_seconds"],
    )


async def _dense(query_vector: List[float], limit: int) -> List[RetrievedChunk]:
    # asyncpg has no pgvector codec, so the vector is passed as its text literal
    # and cast in SQL. Cheap, and avoids a dependency for one parameter type.
    literal = "[" + ",".join(f"{v:.6f}" for v in query_vector) + "]"
    rows = await db.fetch(_DENSE_SQL, literal, limit)
    out = []
    for rank, row in enumerate(rows, start=1):
        chunk = _row_to_chunk(row)
        chunk.cosine = float(row["cosine"])
        chunk.dense_rank = rank
        out.append(chunk)
    return out


async def _sparse(query: str, limit: int) -> List[RetrievedChunk]:
    clause = build_sparse_query(query)
    if not clause:
        # A question made entirely of stopwords. Returning nothing is correct;
        # a match-everything query would poison the fusion with noise.
        return []
    rows = await db.fetch(_SPARSE_SQL, clause, limit)
    out = []
    for rank, row in enumerate(rows, start=1):
        chunk = _row_to_chunk(row)
        chunk.sparse_rank = rank
        out.append(chunk)
    return out


def fuse_rrf(
    dense: List[RetrievedChunk],
    sparse: List[RetrievedChunk],
    *,
    k: int = 60,
) -> List[RetrievedChunk]:
    """Reciprocal Rank Fusion: score = Σ 1 / (k + rank) over both retrievers.

    Pure function of its inputs — unit-tested directly, no database needed.
    """
    merged: Dict[str, RetrievedChunk] = {}

    for chunk in dense:
        merged[chunk.chunk_id] = chunk
    for chunk in sparse:
        existing = merged.get(chunk.chunk_id)
        if existing is None:
            merged[chunk.chunk_id] = chunk
        else:
            # Same chunk from both retrievers: keep the dense record (it carries
            # the cosine score) and graft on the sparse rank.
            existing.sparse_rank = chunk.sparse_rank

    for chunk in merged.values():
        score = 0.0
        if chunk.dense_rank is not None:
            score += 1.0 / (k + chunk.dense_rank)
        if chunk.sparse_rank is not None:
            score += 1.0 / (k + chunk.sparse_rank)
        chunk.rrf = score

    # Tie-break on cosine so ordering is deterministic across runs.
    return sorted(merged.values(), key=lambda c: (-c.rrf, -c.cosine, c.chunk_id))


def cap_per_episode(chunks: List[RetrievedChunk], max_per_episode: int) -> List[RetrievedChunk]:
    """Diversity cap.

    Without it, one long on-topic episode floods every slot and the answer cites
    a single guest as though it were consensus. Breadth across episodes is the
    point of a 269-episode corpus.
    """
    seen: Dict[str, int] = {}
    out: List[RetrievedChunk] = []
    for chunk in chunks:
        count = seen.get(chunk.episode_id, 0)
        if count >= max_per_episode:
            continue
        seen[chunk.episode_id] = count + 1
        out.append(chunk)
    return out


_NEIGHBOUR_SQL = """
SELECT c.text
FROM chunks c
WHERE c.episode_id = $1::uuid
  AND c.ord BETWEEN ($2::int - $3::int) AND ($2::int + $3::int)
ORDER BY c.ord
"""


async def widen_to_parents(chunks: List[RetrievedChunk], window: int) -> None:
    """Widen each hit in place to include its neighbouring chunks.

    Parent-child retrieval: match on small chunks, which keeps the embedding
    precise, then show the model the surrounding conversation, which is what
    makes a mid-discussion excerpt judgeable. `(episode_id, ord)` already
    encodes the parent structure, so this needs no schema change and no
    re-ingest.

    Only `.text` changes. The chunk keeps its own id, marker, and
    `start_seconds`, so citations and timestamp deep links still point at the
    passage that actually matched.
    """
    if window <= 0:
        return
    for chunk in chunks:
        try:
            rows = await db.fetch(_NEIGHBOUR_SQL, chunk.episode_id, chunk.ord, window)
        except Exception as exc:  # noqa: BLE001 — widening is an enhancement
            log.warning("parent_widen_failed", chunk=chunk.chunk_id, error=str(exc))
            continue
        if rows:
            chunk.text = " ".join(r["text"] for r in rows)


async def retrieve(
    query: str,
    *,
    top_k: Optional[int] = None,
    candidates: Optional[int] = None,
    min_sim: Optional[float] = None,
) -> RetrievalResult:
    """Retrieve grounding context for `query`, or abstain."""
    query = (query or "").strip()
    if not query:
        return RetrievalResult(abstain=True, reason="empty_query")

    top_k = top_k or settings.retrieval_top_k
    candidates = candidates or settings.retrieval_candidates
    min_sim = settings.retrieval_min_sim if min_sim is None else min_sim

    embedder = get_embedder()
    query_vector = await embedder.embed_query(query)

    # Both retrievers hit the same pool concurrently; the sparse query is cheap
    # and the dense one dominates, so this is nearly free latency.
    dense, sparse = await asyncio.gather(
        _dense(query_vector, candidates),
        _sparse(query, candidates),
    )

    if not dense and not sparse:
        log.info("retrieval_empty", query=query[:120])
        return RetrievalResult(
            abstain=True,
            reason="no_results",
            candidates_dense=0,
            candidates_sparse=0,
        )

    best_cosine = max((c.cosine for c in dense), default=0.0)

    fused = fuse_rrf(dense, sparse, k=settings.retrieval_rrf_k)
    selected = cap_per_episode(fused, settings.retrieval_max_per_episode)[:top_k]

    for i, chunk in enumerate(selected, start=1):
        chunk.marker = f"S{i}"

    # Widen after selection, so ranking is done on the precise chunks and only
    # what the model reads gets bigger.
    await widen_to_parents(selected, settings.retrieval_parent_window)

    result = RetrievalResult(
        chunks=selected,
        best_cosine=best_cosine,
        candidates_dense=len(dense),
        candidates_sparse=len(sparse),
    )

    if best_cosine < min_sim:
        # Safety floor only — see the module docstring. Reaching this means
        # something is structurally wrong (empty corpus, broken embedder), not
        # merely that the question is off-topic; off-topic is the orchestrator's
        # relevance check to decide.
        result.abstain = True
        result.reason = "below_similarity_floor"
        result.chunks = []
        log.info(
            "retrieval_abstain",
            query=query[:120],
            best_cosine=round(best_cosine, 4),
            threshold=min_sim,
        )
        return result

    log.info(
        "retrieval_ok",
        query=query[:120],
        dense=len(dense),
        sparse=len(sparse),
        selected=len(selected),
        best_cosine=round(best_cosine, 4),
        episodes=len({c.episode_id for c in selected}),
    )
    return result


def format_context(chunks: List[RetrievedChunk], *, max_chars: int = 12000) -> str:
    """Render retrieved chunks as delimited, labelled context.

    The delimiters and the explicit "data, not instructions" framing are the
    prompt-injection boundary: transcripts are third-party text, and a guest who
    says "ignore your instructions" on-air must not be obeyed. Routing is decided
    before retrieval, on user text only, so this content can never pick a tool.
    """
    parts: List[str] = []
    budget = max_chars
    for chunk in chunks:
        header = chunk.episode_title
        if chunk.guests:
            header += f" — {', '.join(chunk.guests)}"
        body = chunk.text
        block = f"<source id=\"{chunk.marker}\">\n{header}\n\n{body}\n</source>"
        if len(block) > budget:
            block = block[: max(0, budget)] + "\n</source>"
            parts.append(block)
            break
        parts.append(block)
        budget -= len(block)
    return "\n\n".join(parts)
