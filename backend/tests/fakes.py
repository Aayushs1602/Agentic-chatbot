"""Test doubles.

`FakeProvider` is what lets the whole suite run on a cold machine with no Ollama
and no API keys — the contract stated in `conftest.py`, and the first thing an
evaluator exercises. It implements the `LLMProvider` port exactly, so anything
that works against it works against a real provider.

It is also scriptable: queue specific JSON responses or force failures, so the
router's fallback, the relevance gate, and the citation validator can each be
driven down paths a live model would only reach by accident.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from app.providers.base import (
    Completed,
    Message,
    ProviderStatus,
    StreamError,
    StreamEvent,
    TextDelta,
    Usage,
)


class FakeProvider:
    """A deterministic stand-in for a real LLM provider."""

    id = "fake"
    label = "Fake (tests)"
    model = "fake-model-1"

    def __init__(
        self,
        *,
        text: str = "A grounded answer. [S1]",
        json_responses: Optional[List[Dict[str, Any]]] = None,
        available: bool = True,
        fail_stream: Optional[StreamError] = None,
        fail_json: Optional[Exception] = None,
        chunk_size: int = 8,
    ) -> None:
        self.text = text
        self.json_responses = list(json_responses or [])
        self._available = available
        self.fail_stream = fail_stream
        self.fail_json = fail_json
        self.chunk_size = chunk_size

        # Recorded so tests can assert on what the orchestrator actually sent —
        # e.g. that retrieved context reached the system prompt, or that the
        # relevance check received a digest rather than the full context.
        self.stream_calls: List[Dict[str, Any]] = []
        self.json_calls: List[Dict[str, Any]] = []
        self.warmups = 0

    async def status(self) -> ProviderStatus:
        return ProviderStatus(
            id=self.id,
            label=self.label,
            model=self.model,
            available=self._available,
            reason=None if self._available else "fake provider marked unavailable",
        )

    async def warmup(self) -> None:
        self.warmups += 1

    async def stream_chat(
        self,
        messages: List[Message],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_calls.append(
            {"messages": list(messages), "system": system, "temperature": temperature}
        )
        if self.fail_stream is not None:
            yield self.fail_stream
            return

        # Emitted in pieces so tests exercise the same incremental path as a
        # real stream, including partial-token reassembly.
        for i in range(0, len(self.text), self.chunk_size):
            yield TextDelta(self.text[i : i + self.chunk_size])

        yield Completed(
            finish_reason="stop",
            usage=Usage(tokens_in=100, tokens_out=len(self.text) // 4),
        )

    async def complete_json(
        self,
        messages: List[Message],
        *,
        schema: Dict[str, Any],
        system: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        self.json_calls.append(
            {"messages": list(messages), "system": system, "schema": schema}
        )
        if self.fail_json is not None:
            raise self.fail_json
        if self.json_responses:
            return self.json_responses.pop(0)
        return _default_for_schema(schema)


def _default_for_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """A schema-valid response, so an unscripted call still behaves plausibly."""
    props = schema.get("properties", {})
    out: Dict[str, Any] = {}
    for key, spec in props.items():
        if "enum" in spec:
            out[key] = spec["enum"][0]
        elif spec.get("type") == "boolean":
            out[key] = True
        elif spec.get("type") == "number":
            out[key] = 0.9
        elif spec.get("type") == "array":
            out[key] = []
        else:
            out[key] = ""
    return out


def fake_chunk(
    marker: str = "S1",
    *,
    chunk_id: str = "chunk-1",
    episode_id: str = "ep-1",
    title: str = "Finding product-market fit",
    text: str = "Retention is the signal I trust most.",
):
    """A `RetrievedChunk` with a marker already assigned."""
    from app.rag.retrieve import RetrievedChunk

    chunk = RetrievedChunk(
        chunk_id=chunk_id,
        episode_id=episode_id,
        episode_title=title,
        guests=["A Guest"],
        youtube_url="https://www.youtube.com/watch?v=abc123",
        video_id="abc123",
        published_on="2024-05-01",
        text=text,
        ord=0,
        start_seconds=120,
        cosine=0.71,
        dense_rank=1,
    )
    chunk.marker = marker
    return chunk
