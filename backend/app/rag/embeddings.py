"""Embeddings.

Default is `fastembed` — ONNX on CPU, no torch, no CUDA. That is a deliberate
choice, not a fallback: the demo machine has 4GB of VRAM, and sharing it between
the chat model and an embedding model makes Ollama swap models on *every query*,
which costs seconds per turn. Keeping embeddings on CPU means Ollama only ever
holds the chat model resident.

`EMBEDDINGS_PROVIDER=ollama` (nomic-embed-text) is supported for machines with
headroom. Switching requires `EMBEDDINGS_DIM=768`, a schema change to the vector
column, and a full re-ingest — the config validator catches the mismatch.

Model choice was measured, not assumed. Benchmarked on the target machine
(Ryzen 5 4600H, 12 threads, in-container), ~400-token inputs:

    BAAI/bge-small-en-v1.5 (int8)          1.8 chunks/s
    BAAI/bge-small-en      (fp32)         11.5 chunks/s
    snowflake-arctic-embed-s              8.8 chunks/s
    snowflake-arctic-embed-xs            17.6 chunks/s   <- default
    all-MiniLM-L6-v2                     44.6 chunks/s

The obvious pick, bge-small-en-v1.5, is an int8-quantized ONNX build, and this
CPU is Zen 2 — no AVX512-VNNI, so int8 is emulated and lands ~10x slower than
the fp32 models it is meant to beat. arctic-embed-xs gives near-bge retrieval
quality at 10x the throughput, which is the difference between a 90-minute and
a 15-minute full-corpus ingest. MiniLM is faster still but measurably weaker at
retrieval, and retrieval quality is the product.

All candidates are 384-dim, so switching between them needs no schema change.

Queries and documents are embedded asymmetrically: bge and arctic-embed are both
trained with a query-side instruction prefix, and using it lifts retrieval
quality measurably for free.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Sequence

import httpx

from app.config import settings
from app.errors import AppError
from app.logging import get_logger

log = get_logger("embeddings")

# bge-family query prefix. Applied to queries only, never to stored documents.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class EmbeddingError(AppError):
    code = "embedding_failed"
    status_code = 503
    message = "Could not compute embeddings."


class Embedder:
    """Lazily-loaded embedding backend.

    Loading is deferred so the API starts instantly and a missing model surfaces
    on first use with a clear message, rather than adding ~10s to every boot.
    """

    def __init__(self) -> None:
        self._model = None
        self._lock = asyncio.Lock()

    @property
    def dim(self) -> int:
        return settings.embeddings_dim

    async def _ensure_model(self):
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is not None:  # another coroutine won the race
                return self._model
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover
                raise EmbeddingError(
                    "fastembed is not installed.",
                    detail={"hint": "pip install -r requirements.txt"},
                ) from exc

            log.info("embedder_loading", model=settings.embeddings_model)
            # First call downloads ~130MB of ONNX weights into the cache volume.
            # Blocking, so it goes to a thread to keep the event loop responsive.
            self._model = await asyncio.to_thread(
                TextEmbedding, model_name=settings.embeddings_model
            )
            log.info("embedder_ready", model=settings.embeddings_model, dim=self.dim)
            return self._model

    async def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        if settings.embeddings_provider == "ollama":
            return await self._embed_ollama(list(texts))
        return await self._embed_fastembed(list(texts))

    async def embed_query(self, text: str) -> List[float]:
        prefixed = (
            _QUERY_PREFIX + text
            if _needs_query_prefix(settings.embeddings_model)
            else text
        )
        vectors = await self.embed_documents([prefixed])
        return vectors[0]

    async def _embed_fastembed(self, texts: List[str]) -> List[List[float]]:
        model = await self._ensure_model()
        try:
            vectors = await asyncio.to_thread(
                lambda: [v.tolist() for v in model.embed(texts, batch_size=settings.embeddings_batch)]
            )
        except Exception as exc:  # noqa: BLE001
            log.error("embed_failed", provider="fastembed", error=str(exc))
            raise EmbeddingError(detail={"error": str(exc)}) from exc
        _assert_dim(vectors)
        return vectors

    async def _embed_ollama(self, texts: List[str]) -> List[List[float]]:
        url = f"{settings.ollama_base_url.rstrip('/')}/api/embed"
        try:
            async with httpx.AsyncClient(timeout=settings.ollama_timeout_s) as client:
                resp = await client.post(
                    url, json={"model": settings.ollama_embed_model, "input": texts}
                )
                resp.raise_for_status()
                vectors = resp.json()["embeddings"]
        except Exception as exc:  # noqa: BLE001
            log.error("embed_failed", provider="ollama", error=str(exc))
            raise EmbeddingError(
                "Ollama embeddings are not available.",
                detail={
                    "hint": f"Run `ollama pull {settings.ollama_embed_model}`",
                    "error": str(exc),
                },
            ) from exc
        _assert_dim(vectors)
        return vectors


def _needs_query_prefix(model_name: str) -> bool:
    """bge and arctic-embed are both trained with this query-side instruction."""
    name = model_name.lower()
    return "bge" in name or "arctic-embed" in name


def _assert_dim(vectors: List[List[float]]) -> None:
    """Guard the schema invariant.

    The `embedding` column is a fixed-width `vector(384)`. A wrong-width vector
    fails at INSERT with an opaque asyncpg error thousands of rows into an
    ingest; catching it here names the actual problem.
    """
    if not vectors:
        return
    got = len(vectors[0])
    if got != settings.embeddings_dim:
        raise EmbeddingError(
            f"Model returned {got}-dim vectors but EMBEDDINGS_DIM is "
            f"{settings.embeddings_dim}.",
            detail={
                "hint": "Set EMBEDDINGS_DIM to match the model, update the "
                "vector(N) column in a new migration, and re-ingest."
            },
        )


_embedder: Optional[Embedder] = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder
