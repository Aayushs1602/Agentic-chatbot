"""Citation resolution — the second line of defence on grounding.

Small models invent markers. An unresolvable `[S7]` is worse than no citation
at all, because it looks like evidence. These are the rules that decide whether
an answer counts as grounded, so they are tested directly.
"""

from __future__ import annotations

from app.agent.citations import (
    extract_markers,
    format_sources_footer,
    resolve_citations,
)
from tests.fakes import fake_chunk


class TestExtractMarkers:
    def test_simple(self):
        assert extract_markers("Claim one [S1]. Claim two [S2].") == ["S1", "S2"]

    def test_grouped_markers(self):
        assert extract_markers("Both guests agree [S1, S3].") == ["S1", "S3"]

    def test_adjacent_brackets(self):
        assert extract_markers("Strong support [S1][S2].") == ["S1", "S2"]

    def test_deduplicates_preserving_order(self):
        assert extract_markers("[S2] then [S1] then [S2] again") == ["S2", "S1"]

    def test_case_insensitive(self):
        assert extract_markers("lowercase [s1] marker") == ["S1"]

    def test_ignores_non_markers(self):
        assert extract_markers("An aside [note] and [1] and [Section 2]") == []

    def test_empty(self):
        assert extract_markers("") == []


class TestResolveCitations:
    def test_keeps_valid_markers(self):
        chunks = [fake_chunk("S1"), fake_chunk("S2", chunk_id="c2")]
        report = resolve_citations("Retention matters [S1]. Pull matters [S2].", chunks)
        assert report.resolved == ["S1", "S2"]
        assert report.invented == []
        assert len(report.citations) == 2
        assert report.is_grounded

    def test_strips_invented_markers(self):
        # The core case: the model cited a source that was never provided.
        report = resolve_citations("Real [S1]. Invented [S7].", [fake_chunk("S1")])
        assert report.invented == ["S7"]
        assert "[S7]" not in report.text
        assert "[S1]" in report.text

    def test_keeps_valid_marker_inside_a_mixed_group(self):
        report = resolve_citations("Mixed [S1, S9] claim.", [fake_chunk("S1")])
        assert "[S1]" in report.text
        assert "S9" not in report.text

    def test_tidies_punctuation_after_stripping(self):
        report = resolve_citations("A claim [S9] .", [fake_chunk("S1")])
        assert "  " not in report.text
        assert " ." not in report.text

    def test_answer_with_only_invented_markers_is_not_grounded(self):
        # This is what triggers replacing the answer entirely — a fluent,
        # confident, entirely uncited response is the failure mode the product
        # exists to prevent.
        report = resolve_citations("Confident but unsupported [S4].", [fake_chunk("S1")])
        assert report.invented == ["S4"]
        assert not report.is_grounded

    def test_answer_with_no_markers_is_not_grounded(self):
        report = resolve_citations("No citations at all.", [fake_chunk("S1")])
        assert not report.is_grounded
        assert report.citations == []

    def test_reports_unused_sources(self):
        chunks = [fake_chunk("S1"), fake_chunk("S2", chunk_id="c2")]
        report = resolve_citations("Only cites one [S1].", chunks)
        assert report.unused == ["S2"]

    def test_citation_payload_carries_deep_link(self):
        report = resolve_citations("Claim [S1].", [fake_chunk("S1")])
        assert report.citations[0]["url"].endswith("&t=120s")
        assert report.citations[0]["marker"] == "S1"

    def test_no_chunks_means_not_grounded(self):
        assert not resolve_citations("Claim [S1].", []).is_grounded


class TestSourcesFooter:
    def test_lists_sources(self):
        report = resolve_citations("Claim [S1].", [fake_chunk("S1")])
        footer = format_sources_footer(report.citations)
        assert "Finding product-market fit" in footer
        assert "listen" in footer

    def test_empty_when_no_citations(self):
        assert format_sources_footer([]) == ""
