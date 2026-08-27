"""Provider reachability probes.

Used by /readyz and by the provider registry (P2) to decide which providers are
selectable. Probes never raise — an unreachable provider is a fact to report,
not an exception to handle. Each returns a small dict the UI renders directly.
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx

from app.config import settings
from app.logging import get_logger

log = get_logger("providers.probe")

_PROBE_TIMEOUT = 3.0


async def probe_ollama() -> Dict[str, Any]:
    """Check Ollama is up AND that the configured model is actually pulled.

    "Reachable" is not enough: the most common local failure is a running
    Ollama that has never pulled the model, which otherwise only surfaces as a
    confusing 404 mid-generation.
    """
    info: Dict[str, Any] = {
        "id": "ollama",
        "label": "Ollama (local)",
        "model": settings.ollama_model,
        "available": False,
        "reason": None,
    }
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            names = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"Not reachable at {settings.ollama_base_url}"
        info["hint"] = "Start it with `ollama serve` on the host."
        log.warning("probe_ollama_failed", error=str(exc))
        return info

    # Ollama appends ":latest" to tagless pulls, so compare with it normalised off.
    want = settings.ollama_model
    normalised = {n.removesuffix(":latest") for n in names}
    if want.removesuffix(":latest") in normalised:
        info["available"] = True
    else:
        info["reason"] = f"Model '{want}' is not pulled"
        info["hint"] = f"Run `ollama pull {want}`"
        info["models_present"] = names[:10]
    return info


async def probe_cloud() -> Dict[str, Any]:
    """OpenAI-compatible endpoint (Gemini free tier by default).

    Presence of a key only — a live call would burn quota on every readiness
    check, and the free tiers are rate-limited.
    """
    info: Dict[str, Any] = {
        "id": "cloud",
        "label": "Cloud (OpenAI-compatible)",
        "model": settings.cloud_model,
        "available": bool(settings.cloud_api_key),
        "reason": None,
    }
    if not info["available"]:
        info["reason"] = "CLOUD_API_KEY is not set"
        info["hint"] = "Get a free Gemini key at https://aistudio.google.com/apikey"
    return info


async def probe_anthropic() -> Dict[str, Any]:
    """Claude Agent SDK path.

    A Claude subscription cannot power an Agent SDK application, so this
    requires a real ANTHROPIC_API_KEY. Without one the provider is reported
    unavailable and hidden from the UI toggle rather than failing at call time.
    """
    info: Dict[str, Any] = {
        "id": "anthropic",
        "label": "Anthropic Claude Agent SDK",
        "model": settings.anthropic_model,
        "available": bool(settings.anthropic_api_key),
        "reason": None,
    }
    if not info["available"]:
        info["reason"] = "ANTHROPIC_API_KEY is not set"
        info["hint"] = "Optional — the demo runs entirely on local Ollama."
    return info


_PROBES = {
    "ollama": probe_ollama,
    "cloud": probe_cloud,
    "anthropic": probe_anthropic,
}


async def probe_one(name: str) -> Dict[str, Any]:
    probe = _PROBES.get(name)
    if probe is None:
        return {"id": name, "available": False, "reason": f"Unknown provider '{name}'"}
    return await probe()


async def probe_all() -> List[Dict[str, Any]]:
    import asyncio

    results = await asyncio.gather(*(p() for p in _PROBES.values()))
    return list(results)
