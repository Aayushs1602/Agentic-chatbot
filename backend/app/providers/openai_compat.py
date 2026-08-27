"""Any OpenAI-compatible endpoint.

One adapter covers Google Gemini (its OpenAI-compatible surface), Groq, OpenAI
itself, and most self-hosted gateways — they differ only in base URL, model
name, and key. Gemini's free tier is the default, because the brief requires a
cloud provider and this project targets zero cost.

The `/chat/completions` shape is deliberately the *only* thing assumed. No
provider-specific features, no vendor SDK — so switching providers is two
environment variables rather than a code change, which is exactly what the
"swap the model without touching application code" requirement asks for.
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

log = get_logger("providers.cloud")


class OpenAICompatProvider:
    id = "cloud"
    label = "Cloud (OpenAI-compatible)"

    def __init__(self) -> None:
        self.base_url = settings.cloud_base_url.rstrip("/")
        self.model = settings.cloud_model

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.cloud_api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _messages(messages: List[Message], system: Optional[str]) -> List[Dict[str, str]]:
        out = [{"role": "system", "content": system}] if system else []
        out.extend(m.to_dict() for m in messages)
        return out

    async def status(self) -> ProviderStatus:
        base = ProviderStatus(
            id=self.id, label=self.label, model=self.model, available=False
        )
        # Key presence only. A live call on every readiness poll would burn the
        # free tier's quota on health checks.
        if not settings.cloud_api_key:
            base.reason = "CLOUD_API_KEY is not set"
            base.hint = "Get a free Gemini key at https://aistudio.google.com/apikey"
            return base
        base.available = True
        return base

    async def warmup(self) -> None:
        """Nothing to warm — a hosted endpoint has no cold model to load."""
        return None

    async def stream_chat(
        self,
        messages: List[Message],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[StreamEvent]:
        if not settings.cloud_api_key:
            yield StreamError(
                code="provider_unavailable",
                message="No cloud API key is configured.",
                detail={"hint": "Set CLOUD_API_KEY, or switch to the Ollama provider."},
            )
            return

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(messages, system),
            "stream": True,
            "temperature": settings.ollama_temperature if temperature is None else temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        started = time.perf_counter()
        usage = Usage()
        try:
            async with httpx.AsyncClient(timeout=settings.cloud_timeout_s) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")[:400]
                        yield StreamError(
                            code="provider_error",
                            message=f"{self.label} returned {response.status_code}.",
                            detail={
                                "body": body,
                                "hint": "Check CLOUD_API_KEY and CLOUD_MODEL."
                                if response.status_code in (401, 403, 404)
                                else "The provider may be rate limiting.",
                            },
                        )
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        choices = chunk.get("choices") or []
                        if choices:
                            piece = (choices[0].get("delta") or {}).get("content")
                            if piece:
                                yield TextDelta(piece)

                        # Some providers only send usage on the final chunk;
                        # others omit it entirely. Take it wherever it appears.
                        if chunk.get("usage"):
                            usage = Usage(
                                tokens_in=chunk["usage"].get("prompt_tokens", 0) or 0,
                                tokens_out=chunk["usage"].get("completion_tokens", 0) or 0,
                            )

            log.info(
                "cloud_complete",
                model=self.model,
                duration_ms=int((time.perf_counter() - started) * 1000),
                tokens_out=usage.tokens_out,
            )
            yield Completed(finish_reason="stop", usage=usage)

        except httpx.TimeoutException:
            yield StreamError(
                code="provider_timeout",
                message=f"{self.label} did not respond within {settings.cloud_timeout_s:.0f}s.",
                detail={"hint": "Raise CLOUD_TIMEOUT_S."},
            )
        except Exception as exc:  # noqa: BLE001
            log.error("cloud_stream_failed", error=str(exc))
            yield StreamError(
                code="provider_unavailable",
                message=f"Could not reach {self.label}.",
                detail={"error": str(exc)},
            )

    async def complete_json(
        self,
        messages: List[Message],
        *,
        schema: Dict[str, Any],
        system: Optional[str] = None,
        temperature: float = 0.0,
    ) -> Dict[str, Any]:
        if not settings.cloud_api_key:
            raise ProviderUnavailableError(
                "No cloud API key is configured.",
                detail={"hint": "Set CLOUD_API_KEY."},
            )

        payload = {
            "model": self.model,
            "messages": self._messages(messages, system),
            "temperature": temperature,
            # `json_schema` is the strict mode, but support is uneven across
            # OpenAI-compatible surfaces. `json_object` plus the schema stated
            # in the prompt is the portable floor, and the parse below is what
            # actually enforces the contract.
            "response_format": {"type": "json_object"},
        }
        instruction = (
            "Respond with a single JSON object matching this schema:\n"
            f"{json.dumps(schema)}"
        )
        payload["messages"].insert(0, {"role": "system", "content": instruction})

        try:
            async with httpx.AsyncClient(timeout=settings.cloud_timeout_s) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
                response.raise_for_status()
                body = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"{self.label} timed out during structured generation."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                f"Could not reach {self.label}.", detail={"error": str(exc)}
            ) from exc

        raw = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # Some providers wrap JSON in a markdown fence despite json_object.
            stripped = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                raise ProviderUnavailableError(
                    "The provider did not return valid JSON.",
                    detail={"raw": raw[:300]},
                ) from exc
