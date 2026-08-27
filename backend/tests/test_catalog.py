"""Catalogue questions — about the archive, not about what was said in it.

These were all being refused on data the system holds: `episodes` has titles,
guests, durations and dates, but every question went through semantic search
over transcript chunks, which cannot answer "how many episodes are there".
"""

from __future__ import annotations

import pytest

from app.agent.catalog import classify, looks_like_catalog_question


class TestDetection:
    @pytest.mark.parametrize(
        "message",
        [
            "how many episodes are there",
            "give the longest lenny episode",
            "what is the shortest episode",
            "what was in episode number 128",
            "which episodes feature Shreyas Doshi",
            "what episodes do you have",
            "how many guests have been on",
        ],
    )
    def test_detects_catalogue_questions(self, message):
        assert looks_like_catalog_question(message)

    @pytest.mark.parametrize(
        "message",
        [
            "How do I know when I have product-market fit?",
            "How should I price a B2B product?",
            "What did guests say about hiring a first PM?",
            "Write me an essay about retention",
        ],
    )
    def test_leaves_content_questions_alone(self, message):
        # A false positive here sends a real question to SQL, which cannot
        # answer it — worse than the gap being fixed.
        assert not looks_like_catalog_question(message)


class TestClassification:
    """Deterministic, because the model got it backwards.

    Asked to classify, qwen2.5:3b labelled "which is the SHORTEST episode" as
    `longest`, and returned `unsupported` for "give the longest lenny episode"
    because the word "lenny" was present. A reversed superlative is a
    confidently wrong factual answer.
    """

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("give the longest lenny episode", "longest"),
            ("what is the longest episode", "longest"),
            ("which is the shortest episode", "shortest"),
            ("what's the briefest one", "shortest"),
            ("how many episodes are there", "count"),
            ("total number of episodes", "count"),
            ("what is the most recent episode", "newest"),
            ("show me the latest episodes", "newest"),
            ("what was the earliest episode", "oldest"),
            ("which episodes feature Shreyas Doshi", "by_guest"),
        ],
    )
    def test_classifies_deterministically(self, message, expected):
        assert classify(message) == expected

    def test_shortest_is_never_confused_with_longest(self):
        # The exact live failure, pinned.
        assert classify("which is the shortest episode") == "shortest"
        assert classify("what is the longest episode") == "longest"

    def test_shortest_wins_over_longest_when_both_appear(self):
        # "shortest" is checked first precisely so a query containing both
        # words resolves to the more specific ask.
        assert classify("not the longest, the shortest episode") == "shortest"

    def test_unknown_shapes_return_unsupported(self):
        assert classify("what colour is the podcast logo") == "unsupported"


@pytest.mark.db
class TestAnswers:
    """Answers come from SQL, so they are exact by construction."""

    async def test_count_reports_real_totals(self, db_pool):
        from app.agent.catalog import answer
        from tests.fakes import FakeProvider

        result = await answer(FakeProvider(), "how many episodes are there")
        assert result.handled
        assert "episodes" in result.text

    async def test_episode_number_explains_the_gap(self, db_pool):
        from app.agent.catalog import answer
        from tests.fakes import FakeProvider

        result = await answer(FakeProvider(), "what was in episode number 128")
        assert result.handled
        # A specific explanation beats a generic refusal: the question is
        # coherent, the data simply has no episode numbers.
        assert "numbered" in result.text.lower()


@pytest.mark.db
class TestDataQuality:
    async def test_zero_duration_rows_are_excluded(self, db_pool):
        from app.agent.catalog import answer
        from tests.fakes import FakeProvider

        # Two episodes carry duration 0. Reporting missing metadata as "the
        # shortest episode" presents absent data as a fact.
        result = await answer(FakeProvider(), "which is the shortest episode")
        assert result.handled
        assert "unknown length" not in result.text

    async def test_short_clips_are_labelled(self, db_pool):
        from app.agent.catalog import answer
        from tests.fakes import FakeProvider

        result = await answer(FakeProvider(), "which is the shortest episode")
        assert "clip" in result.text.lower()


class TestRealPhrasings:
    """Detection is operation-first because noun-first missed how people type.

    Every phrasing below was used against the running system and fell through to
    semantic search. One produced a fabricated episode duration with a valid
    citation attached — the worst output this system has generated.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "give me the lonngest lenny ep",   # typo + abbreviation
            "what is the longest video",       # "video", not "episode"
            "longest ep",                      # two words
            "whats the longest one",           # "one" as the noun
            "which is the shortest pod",
            "how many eps are there",
        ],
    )
    def test_detects_real_phrasings(self, message):
        assert looks_like_catalog_question(message)

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("give me the lonngest lenny ep", "longest"),
            ("what is the longest video", "longest"),
            ("whats the shortst one", "shortest"),
        ],
    )
    def test_classifies_through_typos(self, message, expected):
        # A reversed or missed superlative is a confidently wrong fact.
        assert classify(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            "How do I know when I have product-market fit?",
            "What was the first thing they tried when scaling the team?",
            "How should I price a B2B product?",
            "time",
        ],
    )
    def test_still_leaves_content_questions_alone(self, message):
        # "first" is an operation word, so a long content question containing it
        # must not be captured — the noun and length guards carry that.
        assert not looks_like_catalog_question(message)
