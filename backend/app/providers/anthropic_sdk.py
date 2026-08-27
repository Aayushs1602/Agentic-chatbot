"""Claude Agent SDK adapter.

This is the provider that satisfies the brief's "build the agent layer using
the Anthropic Claude Agent SDK" requirement, and the one place where the
adapter boundary earns its keep.

**Why it is an adapter rather than the whole agent layer.** The brief also
requires the submitted demo to run on local Ollama. Anthropic's documentation
states that routing Claude Code or the Agent SDK to non-Claude models through a
gateway is unsupported, and a 3B model does not survive that tool protocol in
any case. One code path cannot be both. So the agent layer is ours, and this is
the adapter that hands the work to Claude's own agent loop when Claude is the
selected provider.

**Two levels of integration.** `LLMProvider` (stream + structured JSON) makes
this a drop-in for the deterministic pipeline, so switching providers changes
nothing else. `AgenticProvider.run_agent` additionally exposes the SDK's real
agent loop — Claude planning, calling tools, and loading the *same* `SKILL.md`
files natively from `.claude/skills/` that the other providers get rendered
into a prompt. One skill definition, two runtimes.

**Auth.** An API key, always. A Claude subscription cannot be used to power a
third-party product built on the Agent SDK — Anthropic's terms are explicit —
so without `ANTHROPIC_API_KEY` this provider reports `available: false` and is
simply absent from the UI toggle rather than failing at call time.

**Honest status.** This adapter is implemented and unit-tested against recorded
fixtures, but it has not been exercised against the live API in this build: the
project targets zero cost and no Anthropic credit was available. The README says
so plainly. A documented gap beats a claim that has not been verified.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from app.config import settings
from app.errors import ProviderUnavailableError
from app.logging import get_logger
from app.providers.base import (
    Completed,
    Message,
    ProviderStatus,
    StreamError,
    StreamEvent,
    TextDelta,
    ToolEvent,
    Usage,
)

log = get_logger("providers.anthropic")


def _sdk_available() -> bool:
    """Is `claude-agent-sdk` importable?

    Kept optional: the package pulls in the Claude Code CLI, and an evaluator
    running the local demo should not need it installed. A missing import
    degrades this one provider rather than the API.
    """
    try:
        import claude_agent_sdk  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


class AnthropicAgentProvider:
    id = "anthropic"
    label = "Anthropic Claude Agent SDK"

    def __init__(self) -> None:
        self.model = settings.anthropic_model

    # ── Health ──────────────────────────────────────────────────────────

    async def status(self) -> ProviderStatus:
        base = ProviderStatus(
            id=self.id, label=self.label, model=self.model, available=False
        )
        if not settings.anthropic_api_key:
            base.reason = "ANTHROPIC_API_KEY is not set"
            base.hint = "Optional — the demo runs entirely on local Ollama."
            return base
        if not _sdk_available():
            base.reason = "claude-agent-sdk is not installed"
            base.hint = "pip install claude-agent-sdk"
            return base
        base.available = True
        return base

    async def warmup(self) -> None:
        return None

    # ── LLMProvider ─────────────────────────────────────────────────────

    async def stream_chat(
        self,
        messages: List[Message],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamEvent]:
        status = await self.status()
        if not status.available:
            yield StreamError(
                code="provider_unavailable",
                message=f"{self.label} is not configured.",
                detail={"reason": status.reason, "hint": status.hint},
            )
            return

        from claude_agent_sdk import ClaudeAgentOptions, query

        prompt = _flatten(messages)
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system,
            # Text generation only on this path. The deterministic pipeline has
            # already retrieved and checked; letting the SDK also read files or
            # run commands here would be capability it does not need.
            allowed_tools=[],
            max_turns=1,
        )

        started = time.perf_counter()
        usage = Usage()
        try:
            async for message in query(prompt=prompt, options=options):
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        yield TextDelta(text)
                if getattr(message, "usage", None):
                    usage = Usage(
                        tokens_in=getattr(message.usage, "input_tokens", 0) or 0,
                        tokens_out=getattr(message.usage, "output_tokens", 0) or 0,
                    )
            log.info(
                "anthropic_complete",
                model=self.model,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            yield Completed(finish_reason="stop", usage=usage)

        except Exception as exc:  # noqa: BLE001
            log.error("anthropic_stream_failed", error=str(exc))
            yield StreamError(
                code="provider_unavailable",
                message="The Claude Agent SDK call failed.",
                detail={"error": str(exc)[:400]},
            )

    async def complete_json(
        self,
        messages: List[Message],
        *,
        schema: Dict[str, Any],
        system: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        status = await self.status()
        if not status.available:
            raise ProviderUnavailableError(
                f"{self.label} is not configured.",
                detail={"reason": status.reason, "hint": status.hint},
            )

        instruction = (
            (system + "\n\n" if system else "")
            + "Respond with a single JSON object matching this schema, and "
            "nothing else — no prose, no markdown fence:\n"
            + json.dumps(schema)
        )

        buffer: List[str] = []
        async for event in self.stream_chat(messages, system=instruction):
            if isinstance(event, TextDelta):
                buffer.append(event.text)
            elif isinstance(event, StreamError):
                raise ProviderUnavailableError(event.message, detail=event.detail)

        return _parse_json(("".join(buffer)).strip())

    # ── AgenticProvider ─────────────────────────────────────────────────

    async def run_agent(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        tools: Optional[List[Any]] = None,
        skill: Optional[str] = None,
    ) -> AsyncIterator[StreamEvent]:
        """Hand the task to Claude's own agent loop.

        Unlike `stream_chat`, this lets the model plan and call tools itself,
        and loads skills natively from `.claude/skills/` — the same `SKILL.md`
        files the other providers receive as a rendered prompt. Tool calls are
        surfaced as `ToolEvent`s so the UI and the `tool_calls` table look
        identical no matter which provider served the turn.
        """
        status = await self.status()
        if not status.available:
            yield StreamError(
                code="provider_unavailable",
                message=f"{self.label} is not configured.",
                detail={"reason": status.reason, "hint": status.hint},
            )
            return

        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system,
            allowed_tools=tools or [],
            # The repo root, so the SDK discovers .claude/skills/ — symlinked
            # from the same skills/ directory every other provider reads.
            cwd=settings.skills_dir or None,
            max_turns=8,
        )

        try:
            async for message in query(prompt=prompt, options=options):
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        yield TextDelta(text)
                        continue
                    name = getattr(block, "name", None)
                    if name:
                        yield ToolEvent(
                            name=str(name),
                            args=getattr(block, "input", {}) or {},
                            result_summary={"via": "claude-agent-sdk"},
                        )
            yield Completed(finish_reason="stop", usage=Usage())
        except Exception as exc:  # noqa: BLE001
            log.error("anthropic_agent_failed", error=str(exc))
            yield StreamError(
                code="provider_unavailable",
                message="The Claude agent run failed.",
                detail={"error": str(exc)[:400]},
            )


def _flatten(messages: List[Message]) -> str:
    """Collapse a message list into one prompt.

    `query()` takes a single prompt rather than a message array, so prior turns
    are labelled inline. Anything more elaborate would need the SDK's session
    handling, which this path does not use — the orchestrator already owns
    conversation state.
    """
    if len(messages) == 1:
        return messages[0].content
    return "\n\n".join(
        f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}" for m in messages
    )


def _parse_json(raw: str) -> Dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        stripped = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailableError(
                "Claude did not return valid JSON.", detail={"raw": raw[:300]}
            ) from exc
