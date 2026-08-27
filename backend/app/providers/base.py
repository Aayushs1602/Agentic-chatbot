"""The provider port.

This is the boundary that resolves the brief's central tension: it asks for the
agent layer to be built on the Claude Agent SDK, *and* for the demo to run on
local Ollama. Those cannot be one code path — Anthropic's documentation states
that routing Claude Code or the Agent SDK to non-Claude models through a gateway
is unsupported, and a 3B model does not survive that tool protocol regardless.

So the agent layer is ours, and providers are adapters behind it, at two levels:

* ``LLMProvider`` — the primitives every provider implements: stream text, and
  produce JSON matching a schema. The orchestrator's deterministic pipeline
  (route -> retrieve -> check relevance -> apply skill -> emit) is written
  against only these, so it runs identically on Ollama, Gemini, or Claude.

* ``AgenticProvider`` — an *optional* capability for providers that bring their
  own agent loop. The Anthropic adapter implements it with the Claude Agent SDK,
  handing the model real tools and letting it plan. The orchestrator prefers it
  when present and falls back to the deterministic pipeline when it isn't.

The skills are shared across both: one set of ``SKILL.md`` files, loaded natively
by the Agent SDK and rendered into prompts by everything else. Same behavioural
contract, two execution strategies — and the difference is visible in the UI
rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, runtime_checkable


# ── Wire types ──────────────────────────────────────────────────────────


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


# ── Stream events ───────────────────────────────────────────────────────
# A small closed set rather than raw provider payloads, so the SSE layer and the
# persistence layer never learn a provider's response shape.


@dataclass
class TextDelta:
    """Incremental text. The only event the user sees token by token."""

    text: str


@dataclass
class ToolEvent:
    """A tool ran. Recorded on every path, so observability is provider-agnostic."""

    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    result_summary: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    ok: bool = True
    error: Optional[str] = None


@dataclass
class Completed:
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)


@dataclass
class StreamError:
    """A failure that ended the stream.

    Carried as an event rather than raised, because by the time it happens the
    HTTP response has already begun streaming — an exception would truncate the
    connection with no explanation the client could render.
    """

    code: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)


StreamEvent = Any  # TextDelta | ToolEvent | Completed | StreamError


@dataclass
class ProviderStatus:
    id: str
    label: str
    model: str
    available: bool
    reason: Optional[str] = None
    hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "label": self.label,
            "model": self.model,
            "available": self.available,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.hint:
            out["hint"] = self.hint
        return out


# ── Ports ───────────────────────────────────────────────────────────────


@runtime_checkable
class LLMProvider(Protocol):
    """What every provider must do."""

    id: str
    label: str
    model: str

    async def status(self) -> ProviderStatus:
        """Never raises. An unreachable provider is a fact to report."""
        ...

    def stream_chat(
        self,
        messages: List[Message],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a reply. Yields TextDelta..., then Completed or StreamError."""
        ...

    async def complete_json(
        self,
        messages: List[Message],
        *,
        schema: Dict[str, Any],
        system: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        """Return JSON conforming to `schema`.

        This is the workhorse for routing and relevance checks. Constrained
        decoding against a schema is dramatically more reliable than function
        calling on small local models — measured 4/4 on intent classification
        with qwen2.5:3b, where tool calling is not dependable at all.
        """
        ...

    async def warmup(self) -> None:
        """Best-effort readiness. Never raises."""
        ...


@runtime_checkable
class AgenticProvider(Protocol):
    """Optional: providers that bring their own agent loop and tool use."""

    def run_agent(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        skill: Optional[str] = None,
    ) -> AsyncIterator[StreamEvent]:
        ...


def supports_agent_loop(provider: Any) -> bool:
    return isinstance(provider, AgenticProvider) and hasattr(provider, "run_agent")
