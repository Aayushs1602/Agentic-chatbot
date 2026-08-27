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
    async def test_accepts_context_with_useful_sources(self):
        provider = FakeProvider(json_responses=[{"useful_sources": ["S1", "S2"]}])
        result = await check_relevance(provider, "q", '<source id="S1">text</source>')
        assert result.answerable
        assert result.relevant_sources == ["S1", "S2"]

    async def test_rejects_context_with_no_useful_sources(self):
        provider = FakeProvider(json_responses=[
            {"useful_sources": [], "missing": "anything about pricing"}
        ])
        result = await check_relevance(provider, "q", '<source id="S1">text</source>')
        assert not result.answerable
        assert result.missing == "anything about pricing"

    async def test_answerability_is_derived_not_asserted(self):
        # A small model routinely returned answerable=false alongside a
        # populated source list. Deriving it from the list removes the
        # contradiction rather than picking a winner.
        provider = FakeProvider(json_responses=[
            {"answerable": False, "useful_sources": ["S1"]}
        ])
        result = await check_relevance(provider, "q", '<source id="S1">text</source>')
        assert result.answerable

    async def test_blank_source_ids_are_ignored(self):
        provider = FakeProvider(json_responses=[{"useful_sources": ["", "  "]}])
        result = await check_relevance(provider, "q", '<source id="S1">text</source>')
        assert not result.answerable

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


class TestSearchQueryCleaning:
    """Corpus meta-terms in a query are pure noise.

    Observed live: the router produced "approach to changing pricing model
    company podcast transcript" and "product teams AI changes work product
    growth strategy podcast transcripts expand on that". Every document in the
    corpus is a podcast transcript, so those words carry no signal and drag the
    embedding off-topic.
    """

    def test_strips_corpus_terms(self):
        from app.agent.router import clean_search_query

        out = clean_search_query("changing pricing model podcast transcript", "orig")
        assert "podcast" not in out.lower()
        assert "transcript" not in out.lower()
        assert "pricing" in out

    def test_strips_instruction_fragments(self):
        from app.agent.router import clean_search_query

        out = clean_search_query("how AI changes product teams expand on that", "orig")
        assert "expand on that" not in out.lower()
        assert "product teams" in out

    def test_leaves_a_clean_query_alone(self):
        from app.agent.router import clean_search_query

        q = "how to know when you have product-market fit"
        assert clean_search_query(q, "orig") == q

    def test_falls_back_when_nothing_usable_survives(self):
        from app.agent.router import clean_search_query

        # Nothing meaningful survives, so the user's own words beat an empty query.
        assert clean_search_query("podcast transcripts episodes", "how do I price?") == "how do I price?"
        assert clean_search_query("", "how do I price?") == "how do I price?"

    def test_short_valid_queries_are_kept(self):
        from app.agent.router import clean_search_query

        # An earlier version required three words and threw these away.
        assert clean_search_query("product-market fit", "orig") == "product-market fit"
        assert clean_search_query("pricing strategy podcast", "orig") == "pricing strategy"

    async def test_applied_to_router_output(self):
        from app.agent.router import route
        from tests.fakes import FakeProvider

        provider = FakeProvider(json_responses=[{
            "intent": "knowledge_question",
            "search_query": "pricing strategy podcast transcript expand on that",
            "confidence": 0.9,
        }])
        # A follow-up, so the rewrite path runs and its output gets cleaned.
        result = await route(provider, "Tell me more about that")
        assert "podcast" not in result.search_query.lower()
        assert "transcript" not in result.search_query.lower()
        assert "pricing strategy" in result.search_query


class TestRewriteGating:
    """The query rewrite exists to resolve references, not to add terms.

    Measured live: "should I stay an individual contributor or move into
    management" came back as "...management product growth metrics strategy",
    and those appended category words pulled the embedding off the question.
    """

    def test_self_contained_questions_need_no_rewrite(self):
        from app.agent.router import needs_rewrite

        assert not needs_rewrite("How do I know when I have product-market fit?")
        assert not needs_rewrite("When should a startup hire a product manager?")

    def test_references_need_a_rewrite(self):
        from app.agent.router import needs_rewrite

        assert needs_rewrite("Expand on that")
        assert needs_rewrite("Turn that into an essay")
        assert needs_rewrite("Tell me more about it")

    async def test_self_contained_question_uses_the_users_own_words(self):
        from app.agent.router import route
        from tests.fakes import FakeProvider

        provider = FakeProvider(json_responses=[{
            "intent": "knowledge_question",
            "search_query": "individual contributor management product growth metrics strategy",
            "confidence": 0.9,
        }])
        question = "Should I stay an individual contributor or move into management?"
        result = await route(provider, question)
        assert result.search_query == question

    async def test_follow_up_uses_the_rewritten_query(self):
        from app.agent.router import route
        from tests.fakes import FakeProvider

        provider = FakeProvider(json_responses=[{
            "intent": "knowledge_question",
            "search_query": "retention curve signals product-market fit",
            "confidence": 0.9,
        }])
        result = await route(provider, "Expand on that")
        assert "retention curve" in result.search_query
