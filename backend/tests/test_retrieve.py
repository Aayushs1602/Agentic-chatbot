"""Fusion, diversity, and the abstain gate.

`fuse_rrf` and `cap_per_episode` are pure functions, so the ranking logic that
decides what the model is allowed to see is testable without a database, an
embedder, or a model.
"""

from __future__ import annotations

from app.rag.retrieve import RetrievedChunk, cap_per_episode, format_context, fuse_rrf


def chunk(cid: str, episode: str = "ep1", *, dense=None, sparse=None, cosine=0.0):
    return RetrievedChunk(
        chunk_id=cid,
        episode_id=episode,
        episode_title=f"Episode {episode}",
        guests=["Guest"],
        youtube_url="https://www.youtube.com/watch?v=abc123",
        video_id="abc123",
        published_on="2024-01-01",
        text=f"content of {cid}",
        ord=0,
        start_seconds=90,
        cosine=cosine,
        dense_rank=dense,
        sparse_rank=sparse,
    )


class TestRRF:
    def test_agreement_outranks_single_retriever(self):
        both = chunk("a", dense=1, sparse=1)
        dense_only = chunk("b", dense=2)
        ranked = fuse_rrf([both, dense_only], [both], k=60)
        assert ranked[0].chunk_id == "a"

    def test_merges_duplicates_across_retrievers(self):
        d = chunk("a", dense=3)
        s = chunk("a", sparse=1)
        ranked = fuse_rrf([d], [s], k=60)
        assert len(ranked) == 1
        # Both ranks must survive the merge, or the fused score is wrong.
        assert ranked[0].dense_rank == 3
        assert ranked[0].sparse_rank == 1

    def test_scores_match_the_formula(self):
        ranked = fuse_rrf([chunk("a", dense=1)], [chunk("a", sparse=2)], k=60)
        assert abs(ranked[0].rrf - (1 / 61 + 1 / 62)) < 1e-9

    def test_survives_one_empty_retriever(self):
        assert len(fuse_rrf([chunk("a", dense=1)], [], k=60)) == 1
        assert len(fuse_rrf([], [chunk("a", sparse=1)], k=60)) == 1
        assert fuse_rrf([], [], k=60) == []

    def test_ordering_is_deterministic_under_ties(self):
        # Identical ranks and cosines: chunk_id breaks the tie, so two runs
        # never disagree about which sources an answer cited.
        a = fuse_rrf([chunk("b", dense=1), chunk("a", dense=1)], [], k=60)
        b = fuse_rrf([chunk("a", dense=1), chunk("b", dense=1)], [], k=60)
        assert [c.chunk_id for c in a] == [c.chunk_id for c in b]


class TestDiversityCap:
    def test_limits_chunks_per_episode(self):
        chunks = [chunk(f"c{i}", episode="ep1") for i in range(5)]
        assert len(cap_per_episode(chunks, 3)) == 3

    def test_preserves_relative_order(self):
        chunks = [chunk("a", "ep1"), chunk("b", "ep2"), chunk("c", "ep1"), chunk("d", "ep3")]
        assert [c.chunk_id for c in cap_per_episode(chunks, 1)] == ["a", "b", "d"]

    def test_does_not_drop_other_episodes(self):
        chunks = [chunk(f"c{i}", episode="ep1") for i in range(5)] + [chunk("x", "ep2")]
        assert "x" in {c.chunk_id for c in cap_per_episode(chunks, 2)}


class TestCitations:
    def test_deep_link_includes_timestamp(self):
        c = chunk("a")
        assert c.source_url == "https://www.youtube.com/watch?v=abc123&t=90s"

    def test_falls_back_to_plain_url_without_timestamp(self):
        c = chunk("a")
        c.start_seconds = None
        assert c.source_url == "https://www.youtube.com/watch?v=abc123"

    def test_handles_missing_url(self):
        c = chunk("a")
        c.youtube_url = None
        assert c.source_url is None

    def test_citation_payload_shape(self):
        c = chunk("a", cosine=0.7231)
        c.marker = "S1"
        payload = c.to_citation()
        assert payload["marker"] == "S1"
        assert payload["score"] == 0.7231
        assert payload["url"].endswith("&t=90s")


class TestContextFormatting:
    def test_wraps_sources_in_delimiters(self):
        c = chunk("a")
        c.marker = "S1"
        out = format_context([c])
        # The delimiters are the prompt-injection boundary — transcripts are
        # third-party text and must be framed as data, not instructions.
        assert '<source id="S1">' in out
        assert "</source>" in out

    def test_respects_the_character_budget(self):
        chunks = []
        for i in range(10):
            c = chunk(f"c{i}")
            c.marker = f"S{i}"
            c.text = "x" * 5000
            chunks.append(c)
        assert len(format_context(chunks, max_chars=6000)) < 12000

    def test_empty_input(self):
        assert format_context([]) == ""


class TestSparseQueryBuilder:
    """Regression guard for a silent hybrid failure.

    websearch_to_tsquery ANDs its terms, so passing a natural-language question
    straight through matched zero chunks on the real corpus while the pipeline
    reported success — "hybrid" retrieval was quietly dense-only.
    """

    def test_builds_an_or_clause(self):
        from app.rag.retrieve import build_sparse_query

        out = build_sparse_query("How do I know when I've found product-market fit?")
        assert " OR " in out
        assert "product-market" in out
        assert "fit" in out

    def test_drops_stopwords(self):
        from app.rag.retrieve import build_sparse_query

        terms = build_sparse_query("What is the best way to do this").split(" OR ")
        assert "the" not in terms and "is" not in terms and "to" not in terms
        assert "best" in terms and "way" in terms

    def test_all_stopwords_yields_empty(self):
        from app.rag.retrieve import build_sparse_query

        # Must be empty, not a match-everything query — the caller skips the
        # sparse retriever rather than flooding fusion with noise.
        assert build_sparse_query("what is the of and a") == ""
        assert build_sparse_query("") == ""
        assert build_sparse_query("?!.,") == ""

    def test_deduplicates_terms(self):
        from app.rag.retrieve import build_sparse_query

        terms = build_sparse_query("growth growth growth loops").split(" OR ")
        assert terms.count("growth") == 1

    def test_caps_term_count(self):
        from app.rag.retrieve import build_sparse_query

        many = " ".join(f"term{i}" for i in range(100))
        assert len(build_sparse_query(many).split(" OR ")) <= 24

    def test_preserves_hyphenated_and_numeric_tokens(self):
        from app.rag.retrieve import build_sparse_query

        terms = build_sparse_query("product-market fit in 2024 b2b").split(" OR ")
        assert "product-market" in terms
        assert "2024" in terms
        assert "b2b" in terms


class TestTimestampStripping:
    """Transcript time markers must not reach the model.

    Observed live: asked which episode was longest, the model answered "spans
    from 00:50:53 to 01:01:34, covering approximately 50 minutes" — reading chunk
    markers as a duration. Fabricated three ways (those are markers, the span is
    11 minutes, and that episode is not the longest), and it carried a valid
    citation, so every grounding check passed it.
    """

    def test_strips_bracketed_markers(self):
        from app.rag.retrieve import strip_timestamps

        assert "00:12:34" not in strip_timestamps("[00:12:34] Guest: hello")

    def test_strips_speaker_prefix_markers(self):
        from app.rag.retrieve import strip_timestamps

        out = strip_timestamps("Lenny (00:50:53): So what happened?")
        assert "00:50:53" not in out
        assert out.startswith("Lenny:")  # speaker survives, marker does not

    def test_keeps_times_mentioned_in_speech(self):
        from app.rag.retrieve import strip_timestamps

        # An earlier version stripped bare digits too, turning a real detail
        # into "we shipped at today".
        out = strip_timestamps("We shipped at 12:30 today")
        assert "12:30" in out

    def test_leaves_ordinary_text_alone(self):
        from app.rag.retrieve import strip_timestamps

        text = "Retention curves that flatten indicate product-market fit."
        assert strip_timestamps(text) == text

    def test_applied_by_format_context(self):
        from app.rag.retrieve import format_context

        c = chunk("a")
        c.marker = "S1"
        c.text = "[00:50:53] Guest: retention matters."
        assert "00:50:53" not in format_context([c])
