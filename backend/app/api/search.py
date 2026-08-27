"""Retrieval debug endpoints.

`POST /api/search` exposes raw retrieval with no model in the loop. That is a
deliberate evaluator affordance: when an answer looks wrong, this separates
"retrieval found the wrong passages" from "the model misused good passages" in
one request. It is also how the abstain threshold gets tuned against the golden
set, and the first thing to check when grounding degrades in production.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings
from app.db import pool as db
from app.rag.retrieve import retrieve

router = APIRouter(tags=["retrieval"])


class SearchRequest(BaseModel):
    q: str = Field(..., min_length=1, max_length=2000, description="Natural-language query")
    k: Optional[int] = Field(None, ge=1, le=20, description="Chunks to return")
    min_sim: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="Override the abstain threshold, for calibration",
    )
    include_text: bool = Field(True, description="Include full chunk text in the response")


class SearchHit(BaseModel):
    marker: str
    chunk_id: str
    episode_id: str
    title: str
    guests: List[str]
    url: Optional[str]
    published_on: Optional[str]
    start_seconds: Optional[int]
    cosine: float
    dense_rank: Optional[int]
    sparse_rank: Optional[int]
    rrf: float
    text: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    abstain: bool
    reason: Optional[str]
    best_cosine: float
    threshold: float
    candidates: Dict[str, int]
    hits: List[SearchHit]


@router.post("/search", response_model=SearchResponse, summary="Raw hybrid retrieval")
async def search(req: SearchRequest) -> SearchResponse:
    result = await retrieve(req.q, top_k=req.k, min_sim=req.min_sim)
    return SearchResponse(
        query=req.q,
        abstain=result.abstain,
        reason=result.reason,
        best_cosine=round(result.best_cosine, 4),
        threshold=req.min_sim if req.min_sim is not None else settings.retrieval_min_sim,
        candidates={"dense": result.candidates_dense, "sparse": result.candidates_sparse},
        hits=[
            SearchHit(
                marker=c.marker,
                chunk_id=c.chunk_id,
                episode_id=c.episode_id,
                title=c.episode_title,
                guests=c.guests,
                url=c.source_url,
                published_on=c.published_on,
                start_seconds=c.start_seconds,
                cosine=round(c.cosine, 4),
                dense_rank=c.dense_rank,
                sparse_rank=c.sparse_rank,
                rrf=round(c.rrf, 6),
                text=c.text if req.include_text else None,
            )
            for c in result.chunks
        ],
    )


@router.get("/ingest/status", summary="Most recent ingest run")
async def ingest_status() -> Dict[str, Any]:
    row = await db.fetchrow(
        "SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 1"
    )
    counts = await db.fetchrow(
        "SELECT (SELECT count(*) FROM episodes) AS episodes,"
        "       (SELECT count(*) FROM chunks)   AS chunks"
    )
    return {
        "corpus": {"episodes": counts["episodes"], "chunks": counts["chunks"]},
        "last_run": dict(row) if row else None,
    }
