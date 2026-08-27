"""Provider registry, availability, and fallback.

These are the failure paths the brief calls out by name: missing keys, an
unavailable Ollama, timeouts, and degrading without crashing. Everything runs
against fakes, so the suite still needs no Ollama and no API keys.
"""

from __future__ import annotations

import pytest

from app.providers.anthropic_sdk import AnthropicAgentProvider, _flatten, _parse_json
from app.providers.base import (
    AgenticProvider,
    Completed,
    LLMProvider,
    Message,
    StreamError,
    TextDelta,
    supports_agent_loop,
)
from app.providers.openai_compat import OpenAICompatProvider
from app.providers.registry import ProviderRegistry
from tests.fakes import FakeProvider


class TestPortConformance:
    def test_fake_satisfies_the_port(self):
        assert isinstance(FakeProvider(), LLMProvider)

    def test_cloud_satisfies_the_port(self):
        assert isinstance(OpenAICompatProvider(), LLMProvider)

    def test_anthropic_satisfies_the_port(self):
        assert isinstance(AnthropicAgentProvider(), LLMProvider)

    def test_only_anthropic_brings_its_own_agent_loop(self):
        # The capability that lets the orchestrator hand off to the Claude Agent
        # SDK, while everything else shares the deterministic pipeline.
        assert supports_agent_loop(AnthropicAgentProvider())
        assert not supports_agent_loop(FakeProvider())
        assert isinstance(AnthropicAgentProvider(), AgenticProvider)


class TestAvailability:
    async def test_cloud_unavailable_without_a_key(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "cloud_api_key", "")
        status = await OpenAICompatProvider().status()
        assert not status.available
        # An unavailable provider must say what to do about it — a greyed-out
        # option with no reason reads as a broken product.
        assert "CLOUD_API_KEY" in status.reason
        assert status.hint

    async def test_cloud_available_with_a_key(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "cloud_api_key", "test-key")
        assert (await OpenAICompatProvider().status()).available

    async def test_anthropic_unavailable_without_a_key(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "anthropic_api_key", "")
        status = await AnthropicAgentProvider().status()
        assert not status.available
        assert "ANTHROPIC_API_KEY" in status.reason

    async def test_missing_key_is_reported_not_raised(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "cloud_api_key", "")
        events = [e async for e in OpenAICompatProvider().stream_chat([Message("user", "hi")])]
        assert isinstance(events[0], StreamError)
        assert events[0].code == "provider_unavailable"


class TestRegistry:
    def test_registers_ollama(self):
        assert "ollama" in ProviderRegistry().ids()

    def test_unknown_provider_raises_with_the_list(self):
        from app.errors import ProviderUnavailableError

        with pytest.raises(ProviderUnavailableError) as exc:
            ProviderRegistry().get("does-not-exist")
        assert "available" in exc.value.detail

    async def test_statuses_never_raise(self):
        # /readyz depends on this: a probe that throws would take down the one
        # endpoint that explains what is wrong.
        statuses = await ProviderRegistry().statuses()
        assert statuses
        assert all(hasattr(s, "available") for s in statuses)

    async def test_resolve_returns_an_available_provider(self):
        registry = ProviderRegistry()
        registry._providers["fake"] = FakeProvider(available=True)
        provider, fell_back = await registry.resolve("fake")
        assert provider.id == "fake"
        assert fell_back is None

    async def test_falls_back_when_the_requested_provider_is_down(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "provider_fallback", True)
        monkeypatch.setattr(settings, "provider_fallback_order", "down,up")
        registry = ProviderRegistry()
        registry._providers = {
            "down": FakeProvider(available=False),
            "up": FakeProvider(available=True),
        }
        registry._providers["down"].id = "down"
        registry._providers["up"].id = "up"

        provider, fell_back = await registry.resolve("down")
        assert provider.id == "up"
        # Answering from a different model than the user chose is reported, not
        # hidden — the UI and the message row both record it.
        assert fell_back == "down"

    async def test_no_fallback_when_disabled(self, monkeypatch):
        from app.config import settings
        from app.errors import ProviderUnavailableError

        monkeypatch.setattr(settings, "provider_fallback", False)
        registry = ProviderRegistry()
        registry._providers = {"down": FakeProvider(available=False)}
        registry._providers["down"].id = "down"

        with pytest.raises(ProviderUnavailableError):
            await registry.resolve("down")

    async def test_raises_when_nothing_is_available(self, monkeypatch):
        from app.config import settings
        from app.errors import ProviderUnavailableError

        monkeypatch.setattr(settings, "provider_fallback", True)
        monkeypatch.setattr(settings, "provider_fallback_order", "a,b")
        registry = ProviderRegistry()
        registry._providers = {"a": FakeProvider(available=False), "b": FakeProvider(available=False)}
        registry._providers["a"].id, registry._providers["b"].id = "a", "b"

        with pytest.raises(ProviderUnavailableError) as exc:
            await registry.resolve("a")
        assert exc.value.detail.get("tried")


class TestStreamContract:
    async def test_text_then_completed(self):
        events = [e async for e in FakeProvider(text="hello world").stream_chat([Message("user", "hi")])]
        assert isinstance(events[-1], Completed)
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "hello world"

    async def test_failure_arrives_as_an_event_not_an_exception(self):
        # By the time generation fails the HTTP response has already begun
        # streaming; raising would truncate the connection with no explanation
        # the client could render.
        failing = FakeProvider(fail_stream=StreamError(code="provider_timeout", message="too slow"))
        events = [e async for e in failing.stream_chat([Message("user", "hi")])]
        assert isinstance(events[0], StreamError)


class TestAnthropicHelpers:
    def test_single_message_is_passed_through(self):
        assert _flatten([Message("user", "just this")]) == "just this"

    def test_multiple_messages_are_labelled(self):
        out = _flatten([Message("user", "q"), Message("assistant", "a")])
        assert "User: q" in out and "Assistant: a" in out

    def test_parses_plain_json(self):
        assert _parse_json('{"intent": "chitchat"}')["intent"] == "chitchat"

    def test_parses_fenced_json(self):
        assert _parse_json('```json\n{"intent": "chitchat"}\n```')["intent"] == "chitchat"

    def test_invalid_json_raises_a_structured_error(self):
        from app.errors import ProviderUnavailableError

        with pytest.raises(ProviderUnavailableError):
            _parse_json("not json at all")
