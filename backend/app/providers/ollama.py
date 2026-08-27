"""Ollama provider — the one the submitted demo runs on.

Three things here exist because of measurements on the target machine
(GTX 1650, 4GB VRAM, qwen2.5:3b-instruct-q4_K_M):

* ``num_ctx`` is always sent. Ollama defaults to 4096 and truncates the prompt
  **silently** — five retrieved chunks plus a skill prompt overflows that, and
  the failure looks like a model that ignored its context rather than an error.

* ``keep_alive`` is always sent. Cold-loading the model measured ~77s; without
  it, the first question after any idle period is unusable.

* JSON comes from ``format: <json schema>`` (constrained decoding), never from
  function calling. Intent classification scored 4/4 this way on the 3B, where
  tool calling is not dependable. This is the core of the local-path design.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from app.config import settings
from app.errors import ProviderTimeoutError, ProviderUnavailableError
from app.logging import get_logger
from app.providers.base import (
    Completed,
    Message,
    ProviderStatus,
    StreamError,
    StreamEvent,
    TextDelta,
    Usage,
)

log = get_logger("providers.ollama")


class OllamaProvider:
    id = "ollama"
    label = "Ollama (local)"

    def __init__(self) -> None:
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.ollama_model

    # ── Options ─────────────────────────────────────────────────────────

    def _options(self, temperature: Optional[float] = None) -> Dict[str, Any]:
        return {
            "num_ctx": settings.ollama_num_ctx,
            "temperature": (
                settings.ollama_temperature if temperature is None else temperature
            ),
        }

    @staticmethod
    def _payload_messages(
        messages: List[Message], system: Optional[str]
    ) -> List[Dict[str, str]]:
        out = [{"role": "system", "content": system}] if system else []
        out.extend(m.to_dict() for m in messages)
        return out

    # ── Health ──────────────────────────────────────────────────────────

    async def status(self) -> ProviderStatus:
        base = ProviderStatus(
            id=self.id, label=self.label, model=self.model, available=False
        )
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                names = {
                    m.get("name", "").removesuffix(":latest")
                    for m in resp.json().get("models", [])
                }
        except Exception as exc:  # noqa: BLE001
            base.reason = f"Not reachable at {self.base_url}"
            base.hint = "Start it with `ollama serve` on the host."
            log.warning("ollama_unreachable", error=str(exc))
            return base

        # A running Ollama that never pulled the model is the most common local
        # failure; without this check it only surfaces as a 404 mid-generation.
        if self.model.removesuffix(":latest") not in names:
            base.reason = f"Model '{self.model}' is not pulled"
            base.hint = f"Run `ollama pull {self.model}`"
            return base

        base.available = True
        return base

    async def warmup(self) -> None:
        """Load the model into VRAM so the first real request doesn't wait ~77s."""
        if not settings.ollama_warmup:
            return
        try:
            started = time.perf_counter()
            async with httpx.AsyncClient(timeout=180.0) as client:
                await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,
                        "keep_alive": settings.ollama_keep_alive,
                        "options": {"num_ctx": 256, "num_predict": 1},
                    },
                )
            log.info(
                "ollama_warm",
                model=self.model,
                seconds=round(time.perf_counter() - started, 1),
            )
        except Exception as exc:  # noqa: BLE001 — warmup is best-effort by design
            log.warning("ollama_warmup_failed", error=str(exc))

    # ── Generation ──────────────────────────────────────────────────────

    async def stream_chat(
        self,
        messages: List[Message],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamEvent]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._payload_messages(messages, system),
            "stream": True,
            "keep_alive": settings.ollama_keep_alive,
            "options": self._options(temperature),
        }
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens

        started = time.perf_counter()
        usage = Usage()
        try:
            async with httpx.AsyncClient(timeout=settings.ollama_timeout_s) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/api/chat", json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode(errors="replace")[:400]
                        yield StreamError(
                            code="provider_error",
                            message=f"Ollama returned {resp.status_code}.",
                            detail={"body": body, "hint": f"Is `{self.model}` pulled?"},
                        )
                        return

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            # A partial line is not fatal; skip it and keep going
                            # rather than killing a stream the user is watching.
                            continue

                        piece = (chunk.get("message") or {}).get("content", "")
                        if piece:
                            yield TextDelta(piece)

                        if chunk.get("done"):
                            usage = Usage(
                                tokens_in=chunk.get("prompt_eval_count", 0) or 0,
                                tokens_out=chunk.get("eval_count", 0) or 0,
                            )
                            log.info(
                                "ollama_complete",
                                model=self.model,
                                tokens_in=usage.tokens_in,
                                tokens_out=usage.tokens_out,
                                duration_ms=int((time.perf_counter() - started) * 1000),
                                reason=chunk.get("done_reason", "stop"),
                            )
                            yield Completed(
                                finish_reason=chunk.get("done_reason") or "stop",
                                usage=usage,
                            )
                            return

            # Stream ended without a done frame — treat as a truncated response
            # rather than silently pretending it completed.
            yield Completed(finish_reason="incomplete", usage=usage)

        except httpx.TimeoutException:
            log.warning("ollama_timeout", model=self.model, timeout=settings.ollama_timeout_s)
            yield StreamError(
                code="provider_timeout",
                message=f"The model did not respond within {settings.ollama_timeout_s:.0f}s.",
                detail={"hint": "Raise OLLAMA_TIMEOUT_S or use a smaller model."},
            )
        except Exception as exc:  # noqa: BLE001
            log.error("ollama_stream_failed", error=str(exc))
            yield StreamError(
                code="provider_unavailable",
                message="Lost connection to Ollama.",
                detail={"error": str(exc), "hint": "Is `ollama serve` still running?"},
            )

    async def complete_json(
        self,
        messages: List[Message],
        *,
        schema: Dict[str, Any],
        system: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        """Constrained decoding against a JSON schema.

        Raises rather than yielding errors: unlike ``stream_chat`` these calls
        happen before any bytes reach the client, so a normal exception can
        still become a clean structured error response.
        """
        payload = {
            "model": self.model,
            "messages": self._payload_messages(messages, system),
            "stream": False,
            "format": schema,
            "keep_alive": settings.ollama_keep_alive,
            "options": self._options(temperature),
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=settings.ollama_timeout_s) as client:
                resp = await client.post(f"{self.base_url}/api/chat", json=payload)
                resp.raise_for_status()
                body = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Structured generation exceeded {settings.ollama_timeout_s:.0f}s.",
                detail={"provider": self.id, "model": self.model},
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                "Ollama is not reachable.",
                detail={"error": str(exc), "hint": "Run `ollama serve`."},
            ) from exc

        raw = (body.get("message") or {}).get("content", "")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Constrained decoding makes this rare, but a truncated generation
            # (hitting num_predict mid-object) still produces invalid JSON.
            log.warning("ollama_json_invalid", raw=raw[:300])
            raise ProviderUnavailableError(
                "The model did not return valid JSON.",
                detail={"raw": raw[:300], "hint": "Try a larger model or raise num_ctx."},
            ) from exc

        log.info(
            "ollama_json",
            model=self.model,
            duration_ms=int((time.perf_counter() - started) * 1000),
            keys=sorted(parsed)[:8] if isinstance(parsed, dict) else None,
        )
        return parsed
