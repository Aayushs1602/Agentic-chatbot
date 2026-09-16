"""Provider registry: selection, health, and fallback.

One place decides which provider serves a request, so the orchestrator never
branches on provider identity and the UI has a single source of truth for what
is selectable.

Fallback is opt-in (`PROVIDER_FALLBACK`). Silently answering from a different
model than the user selected is its own kind of failure — so when fallback does
fire it is logged, recorded on the message row, and surfaced in the UI, rather
than hidden.

Availability is **cached** (`PROVIDER_HEALTH_TTL_S`). `status()` is a live HTTP
call on the Ollama path, and resolve() runs on every chat turn, so an uncached
probe put a network round-trip in front of every single generation. The cache is
per-registry rather than global so tests can build isolated registries, and
single-flighted so a cold cache under concurrency produces one probe rather than
one per caller.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.config import settings
from app.errors import ProviderUnavailableError
from app.logging import get_logger
from app.providers.base import LLMProvider, ProviderStatus
from app.providers.ollama import OllamaProvider

log = get_logger("providers.registry")


@dataclass
class _Health:
    status: ProviderStatus
    expires_at: float


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: Dict[str, LLMProvider] = {}
        self._active: str = settings.llm_provider
        self._health: Dict[str, _Health] = {}
        # In-flight probes, keyed the same way. Concurrent callers that miss the
        # cache await the same task instead of each starting their own.
        self._probes: Dict[str, "asyncio.Task[ProviderStatus]"] = {}
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

    # ── Health ──────────────────────────────────────────────────────────

    async def _probe(self, key: str, provider: LLMProvider) -> ProviderStatus:
        """Probe one provider and cache the result. Never raises.

        The port says `status()` never raises, but /readyz and the UI both
        depend on this path, so a misbehaving adapter degrades one provider
        instead of taking down the endpoint that explains what is wrong.
        """
        try:
            status = await provider.status()
        except Exception as exc:  # noqa: BLE001
            status = ProviderStatus(
                id=getattr(provider, "id", key),
                label=getattr(provider, "label", key),
                model=getattr(provider, "model", ""),
                available=False,
                reason=f"Probe failed: {exc}",
            )
        finally:
            self._probes.pop(key, None)

        ttl = settings.provider_health_ttl_s
        if ttl > 0:
            # Unavailability is cached for the same TTL as availability: the
            # point is to stop hammering a provider that is down, and a short
            # window is what keeps "I just started Ollama" from feeling stuck.
            self._health[key] = _Health(status, time.monotonic() + ttl)
        return status

    async def health(
        self, key: str, provider: LLMProvider, *, force: bool = False
    ) -> ProviderStatus:
        """Availability for one provider, from cache when it is fresh."""
        if not force:
            cached = self._health.get(key)
            if cached is not None and cached.expires_at > time.monotonic():
                return cached.status

        # No await between the lookup and the insert, so under asyncio this is
        # atomic: exactly one caller creates the task and the rest join it.
        task = None if force else self._probes.get(key)
        if task is None:
            task = asyncio.ensure_future(self._probe(key, provider))
            if not force:
                self._probes[key] = task

        # Shielded so a cancelled caller (a client that hung up mid-probe) does
        # not cancel the probe every other caller is waiting on.
        return await asyncio.shield(task)

    def invalidate(self, key: Optional[str] = None) -> None:
        """Force the next check to re-probe. Used when reality contradicts the
        cache — a provider that was 'available' a moment ago and just failed."""
        if key is None:
            self._health.clear()
        else:
            self._health.pop(key, None)

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

    async def statuses(self, *, force: bool = False) -> List[ProviderStatus]:
        """Probe every provider concurrently. Never raises.

        The UI's provider list passes `force=True`: someone looking at that
        screen has usually just started or stopped something, and a stale
        answer there is worse than a slow one.
        """
        items = list(self._providers.items())
        return list(
            await asyncio.gather(*(self.health(k, p, force=force) for k, p in items))
        )

    # ── Selection ───────────────────────────────────────────────────────

    async def set_active(self, provider_id: str) -> ProviderStatus:
        """Switch providers at runtime.

        An unavailable provider can still be selected — refusing would make it
        impossible to pick a provider before starting it, and /readyz already
        reports the state clearly.
        """
        provider = self.get(provider_id)
        # Deliberate user action against a specific provider: re-probe rather
        # than answer from a cache that may predate whatever they just did.
        status = await self.health(provider_id, provider, force=True)
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

        status = await self.health(wanted, provider)
        if status.available:
            return provider, None

        if not settings.provider_fallback:
            raise ProviderUnavailableError(
                f"{provider.label} is unavailable.",
                detail={"reason": status.reason, "hint": status.hint,
                        "enable": "Set PROVIDER_FALLBACK=true to use another provider."},
            )

        candidates = [
            cid
            for cid in settings.fallback_order
            if cid != wanted and cid in self._providers
        ]
        if candidates:
            # Probed concurrently, then chosen in configured order. The
            # concurrency changes only how long it takes to find out, never
            # which provider wins — priority is still the order in the config.
            #
            # Serially, each unavailable candidate cost its own timeout before
            # the next was even attempted, so a user whose provider was down
            # waited out the whole chain before seeing a single token.
            results = await asyncio.gather(
                *(self.health(cid, self._providers[cid]) for cid in candidates)
            )
            for cid, candidate_status in zip(candidates, results):
                if candidate_status.available:
                    log.warning(
                        "provider_fallback",
                        requested=wanted, using=cid, reason=status.reason,
                    )
                    return self._providers[cid], wanted

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
