"""Provider registry: selection, health, and fallback.

One place decides which provider serves a request, so the orchestrator never
branches on provider identity and the UI has a single source of truth for what
is selectable.

Fallback is opt-in (`PROVIDER_FALLBACK`). Silently answering from a different
model than the user selected is its own kind of failure — so when fallback does
fire it is logged, recorded on the message row, and surfaced in the UI, rather
than hidden.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from app.config import settings
from app.errors import ProviderUnavailableError
from app.logging import get_logger
from app.providers.base import LLMProvider, ProviderStatus
from app.providers.ollama import OllamaProvider

log = get_logger("providers.registry")


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: Dict[str, LLMProvider] = {}
        self._active: str = settings.llm_provider
        self._register_all()

    def _register_all(self) -> None:
        self._providers["ollama"] = OllamaProvider()

        # Cloud and Anthropic adapters arrive in P6. Registering them lazily and
        # tolerating an import failure means a missing optional dependency
        # degrades one provider instead of taking down the whole API.
        try:
            from app.providers.openai_compat import OpenAICompatProvider

            self._providers["cloud"] = OpenAICompatProvider()
        except Exception as exc:  # noqa: BLE001
            log.debug("provider_not_registered", provider="cloud", error=str(exc))

        try:
            from app.providers.anthropic_sdk import AnthropicAgentProvider

            self._providers["anthropic"] = AnthropicAgentProvider()
        except Exception as exc:  # noqa: BLE001
            log.debug("provider_not_registered", provider="anthropic", error=str(exc))

    # ── Introspection ───────────────────────────────────────────────────

    @property
    def active_id(self) -> str:
        return self._active

    def ids(self) -> List[str]:
        return list(self._providers)

    def get(self, provider_id: str) -> LLMProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ProviderUnavailableError(
                f"Unknown provider '{provider_id}'.",
                detail={"available": self.ids()},
            )
        return provider

    async def statuses(self) -> List[ProviderStatus]:
        """Probe every provider concurrently. Never raises."""
        async def safe(p: LLMProvider) -> ProviderStatus:
            try:
                return await p.status()
            except Exception as exc:  # noqa: BLE001
                return ProviderStatus(
                    id=p.id, label=p.label, model=p.model,
                    available=False, reason=f"Probe failed: {exc}",
                )

        return list(await asyncio.gather(*(safe(p) for p in self._providers.values())))

    # ── Selection ───────────────────────────────────────────────────────

    async def set_active(self, provider_id: str) -> ProviderStatus:
        """Switch providers at runtime.

        An unavailable provider can still be selected — refusing would make it
        impossible to pick a provider before starting it, and /readyz already
        reports the state clearly.
        """
        provider = self.get(provider_id)
        status = await provider.status()
        self._active = provider_id
        log.info("provider_switched", provider=provider_id, available=status.available)
        return status

    async def resolve(
        self, requested: Optional[str] = None
    ) -> tuple[LLMProvider, Optional[str]]:
        """Pick the provider to serve a request.

        Returns ``(provider, fell_back_from)``. ``fell_back_from`` is non-None
        only when the requested provider was unavailable and fallback took over,
        so callers can tell the user what actually answered them.
        """
        wanted = requested or self._active
        provider = self.get(wanted)

        status = await provider.status()
        if status.available:
            return provider, None

        if not settings.provider_fallback:
            raise ProviderUnavailableError(
                f"{provider.label} is unavailable.",
                detail={"reason": status.reason, "hint": status.hint,
                        "enable": "Set PROVIDER_FALLBACK=true to use another provider."},
            )

        for candidate_id in settings.fallback_order:
            if candidate_id == wanted or candidate_id not in self._providers:
                continue
            candidate = self._providers[candidate_id]
            candidate_status = await candidate.status()
            if candidate_status.available:
                log.warning(
                    "provider_fallback",
                    requested=wanted, using=candidate_id, reason=status.reason,
                )
                return candidate, wanted

        raise ProviderUnavailableError(
            "No model provider is available.",
            detail={
                "requested": wanted,
                "reason": status.reason,
                "hint": status.hint or "Start Ollama, or set CLOUD_API_KEY.",
                "tried": settings.fallback_order,
            },
        )

    async def warmup_active(self) -> None:
        try:
            await self.get(self._active).warmup()
        except Exception as exc:  # noqa: BLE001
            log.debug("warmup_skipped", error=str(exc))


_registry: Optional[ProviderRegistry] = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry
