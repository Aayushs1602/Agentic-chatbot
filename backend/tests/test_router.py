"""Intent routing and the relevance gate.

Driven entirely through `FakeProvider`, so these run with no Ollama and no keys.
The failure paths matter most: a routing error must not lose the user's turn,
and the relevance gate must fail in the safe direction.
"""

from __future__ import annotations

import pytest

from app.agent.router import Intent, _digest, check_relevance, route
from app.providers.base import Message
from tests.fakes import FakeProvider


class TestIntentEnum:
    def test_chitchat_skips_retrieval(self):
        assert not Intent.CHITCHAT.needs_retrieval

    @pytest.mark.parametrize("intent", [Intent.KNOWLEDGE, Intent.ESSAY, Intent.ARTIFACT])
    def test_everything_else_retrieves(self, intent):
        assert intent.needs_retrieval


class TestRouting:
    async def test_uses_the_model_decision(self):
        provider = FakeProvider(json_responses=[
            {"intent": "write_essay", "search_query": "product-market fit", "confidence": 0.9}
        ])
        result = await route(provider, "Turn that into an essay")
        assert result.intent is Intent.ESSAY
        assert result.search_query == "product-market fit"
        assert not result.fallback_used

    @pytest.mark.parametrize("greeting", ["hi", "Hello!", "thanks", "  ok  ", "good morning"])
    async def test_greetings_short_circuit_without_a_model_call(self, greeting):
        # A greeting shouldn't cost an inference plus a retrieval round-trip —
        # that is most of the latency budget for no benefit.
        provider = FakeProvider()
        result = await route(provider, greeting)
        assert result.intent is Intent.CHITCHAT
        assert provider.json_calls == []

    async def test_a_question_is_not_treated_as_chitchat(self):
        provider = FakeProvider(json_responses=[
            {"intent": "knowledge_question", "search_query": "pricing", "confidence": 0.8}
        ])
        result = await route(provider, "hey, how should I price my product?")
        assert result.intent is Intent.KNOWLEDGE

    async def test_falls_back_to_knowledge_when_the_model_fails(self):
        # Defaulting to KNOWLEDGE is the safe direction: it retrieves and
        # grounds. Guessing ESSAY would produce 1,250 confident ungrounded words.
        provider = FakeProvider(fail_json=RuntimeError("ollama down"))
        result = await route(provider, "How do I hire a PM?")
        assert result.intent is Intent.KNOWLEDGE
        assert result.fallback_used
        assert result.search_query == "How do I hire a PM?"

    async def test_fallback_still_detects_an_essay_request(self):
        provider = FakeProvider(fail_json=RuntimeError("down"))
        result = await route(provider, "Write a Ship 30 for 30 essay about retention")
        assert result.intent is Intent.ESSAY
        assert result.fallback_used

    async def test_empty_search_query_falls_back_to_the_message(self):
        provider = FakeProvider(json_responses=[
            {"intent": "knowledge_question", "search_query": "", "confidence": 0.7}
        ])
        result = await route(provider, "How do I find PMF?")
        assert result.search_query == "How do I find PMF?"

    async def test_history_is_passed_for_pronoun_resolution(self):
        provider = FakeProvider(json_responses=[
            {"intent": "write_essay", "search_query": "retention curves", "confidence": 0.9}
        ])
        history = [
            Message(role="user", content="What signals product-market fit?"),
            Message(role="assistant", content="Retention curves that flatten."),
        ]
        await route(provider, "Turn that into an essay", history=history)
        sent = provider.json_calls[0]["messages"]
        assert len(sent) > 1  # history reached the router
        assert any("Retention" in m.content for m in sent)


class TestDigest:
    def test_shrinks_the_context(self):
        context = "".join(
            f'<source id="S{i}">\nTitle\n\n{"word " * 400}\n</source>\n\n' for i in range(1, 6)
        )
        digest = _digest(context)
        # Prompt evaluation dominates latency on a 3B; the full context measured
        # ~30s of a 34s turn.
        assert len(digest) < len(context) / 2

    def test_preserves_every_marker(self):
        context = "".join(
            f'<source id="S{i}">\nTitle\n\nbody text here\n</source>\n\n' for i in range(1, 6)
        )
        digest = _digest(context)
        for i in range(1, 6):
            assert f"[S{i}]" in digest

    def test_handles_unstructured_context(self):
        assert _digest("no source blocks here") == "no source blocks here"


class TestRelevanceGate:
    async def test_accepts_answerable_context(self):
        provider = FakeProvider(json_responses=[
            {"answerable": True, "relevant_sources": ["S1", "S2"]}
        ])
        result = await check_relevance(provider, "q", '<source id="S1">text</source>')
        assert result.answerable
        assert result.relevant_sources == ["S1", "S2"]

    async def test_rejects_unanswerable_context(self):
        provider = FakeProvider(json_responses=[
            {"answerable": False, "relevant_sources": [], "missing": "anything about pricing"}
        ])
        result = await check_relevance(provider, "q", '<source id="S1">text</source>')
        assert not result.answerable
        assert result.missing == "anything about pricing"

    async def test_empty_context_is_not_answerable_without_a_model_call(self):
        provider = FakeProvider()
        result = await check_relevance(provider, "q", "")
        assert not result.answerable
        assert provider.json_calls == []

    async def test_fails_open_when_the_model_errors(self):
        # Failing closed would turn every provider hiccup into a refusal, which
        # is worse for a corpus the user knows covers their question. Citation
        # resolution is the second line of defence.
        provider = FakeProvider(fail_json=RuntimeError("timeout"))
        result = await check_relevance(provider, "q", '<source id="S1">text</source>')
        assert result.answerable
        assert not result.checked

    async def test_sends_a_digest_not_the_full_context(self):
        provider = FakeProvider(json_responses=[{"answerable": True, "relevant_sources": ["S1"]}])
        context = "".join(
            f'<source id="S{i}">\nTitle\n\n{"word " * 400}\n</source>\n\n' for i in range(1, 6)
        )
        await check_relevance(provider, "q", context)
        assert len(provider.json_calls[0]["messages"][0].content) < len(context)
