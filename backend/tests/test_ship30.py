"""The Ship 30 rubric.

This is what separates a skill from a prompt: the checkable half of
`reference/principles.md` expressed as assertions, so "skimmable formatting" and
"grounded claims" are pass/fail rather than aspirations, and a failure names its
own fix for the repair pass.
"""

from __future__ import annotations

import pytest

from app.agent.ship30 import (
    MAX_SECTION_WORDS,
    MIN_CITATIONS,
    Outline,
    evaluate,
    repair_prompt,
    section_prompt,
    split_sections,
    word_count,
)

MARKERS = ["S1", "S2", "S3", "S4", "S5"]


def build_essay(
    *,
    words_per_section: int = 300,
    sections: int = 3,
    bullets: bool = True,
    bold: bool = True,
    citations: int = 5,
    headline: bool = True,
) -> str:
    parts = []
    if headline:
        parts.append("# What Ramanujam Gets Right About Pricing\n")
    parts.append("Most teams price last, and it costs them [S1].\n")
    for i in range(sections):
        parts.append(f"## Section {i + 1}\n")
        filler = " ".join(f"Concrete point number {j} about pricing." for j in range(words_per_section // 5))
        parts.append(filler)
        if bold:
            parts.append("**This is the key sentence of the section.**")
        if bullets and i == 0:
            parts.append("- First point\n- Second point\n- Third point")
        parts.append("")
    marker_line = " ".join(f"Claim {i} [S{i}]." for i in range(1, citations + 1))
    parts.append(f"## The takeaway\n{marker_line}\n")
    return "\n".join(parts)


class TestWordCount:
    def test_ignores_markdown_syntax(self):
        assert word_count("# Heading") == 1
        assert word_count("**bold words here**") == 3

    def test_ignores_citation_markers(self):
        # Markers are apparatus, not prose; counting them inflates the length.
        assert word_count("A real claim [S1].") == word_count("A real claim.")

    def test_empty(self):
        assert word_count("") == 0


class TestSplitSections:
    def test_splits_on_h2(self):
        sections = split_sections("# T\nintro\n## One\nbody one\n## Two\nbody two")
        assert [h for h, _ in sections] == ["One", "Two"]
        assert "body one" in sections[0][1]

    def test_no_sections(self):
        assert split_sections("just prose") == []


class TestRubric:
    def test_a_good_essay_passes(self):
        rubric = evaluate(build_essay(), available_markers=MARKERS)
        assert rubric.passed, [c.detail for c in rubric.failures]

    def test_too_short_fails_length(self):
        rubric = evaluate(build_essay(words_per_section=50), available_markers=MARKERS)
        assert not rubric.passed
        assert any(c.name == "length" for c in rubric.failures)

    def test_too_long_fails_length(self):
        rubric = evaluate(build_essay(words_per_section=900), available_markers=MARKERS)
        assert any(c.name == "length" for c in rubric.failures)

    def test_missing_headline_fails(self):
        rubric = evaluate(build_essay(headline=False), available_markers=MARKERS)
        assert any(c.name == "headline" for c in rubric.failures)

    def test_too_few_sections_fails(self):
        rubric = evaluate(build_essay(sections=1, words_per_section=1100), available_markers=MARKERS)
        assert any(c.name == "sections" for c in rubric.failures)

    def test_no_bullets_fails(self):
        rubric = evaluate(build_essay(bullets=False), available_markers=MARKERS)
        assert any(c.name == "bullets" for c in rubric.failures)

    def test_no_bold_fails(self):
        # Bold absent is as much a formatting failure as bold everywhere.
        rubric = evaluate(build_essay(bold=False), available_markers=MARKERS)
        assert any(c.name == "emphasis" for c in rubric.failures)

    def test_over_bolding_fails(self):
        essay = build_essay(words_per_section=60)
        rubric = evaluate(essay + "\n\n**" + " ".join(["loud"] * 400) + "**", available_markers=MARKERS)
        assert any(c.name == "emphasis" for c in rubric.failures)

    def test_too_few_citations_fails(self):
        rubric = evaluate(build_essay(citations=1), available_markers=MARKERS)
        assert any(c.name == "grounding" for c in rubric.failures)

    def test_invented_markers_do_not_count_toward_grounding(self):
        # An unresolvable marker looks like evidence but is not; counting it
        # would let a fabricated essay pass the grounding check.
        essay = build_essay(citations=5)
        rubric = evaluate(essay, available_markers=["S1"])
        assert rubric.citation_count == 1
        assert any(c.name == "grounding" for c in rubric.failures)

    def test_over_long_section_fails_balance(self):
        essay = "# T\nintro [S1]\n\n## Huge\n" + " ".join(["word"] * (MAX_SECTION_WORDS + 200))
        rubric = evaluate(essay, available_markers=MARKERS)
        assert any(c.name == "section_balance" for c in rubric.failures)

    def test_every_failure_carries_a_fix(self):
        # The repair pass consumes these. A check that cannot say how to fix
        # itself is a complaint, not a rubric.
        rubric = evaluate("nothing here", available_markers=MARKERS)
        assert rubric.failures
        for check in rubric.failures:
            assert check.fix, f"{check.name} has no fix"

    def test_report_is_serialisable(self):
        report = evaluate(build_essay(), available_markers=MARKERS).to_dict()
        assert set(report) >= {"passed", "word_count", "section_count", "citation_count", "checks"}


class TestPrompts:
    @pytest.fixture
    def outline(self):
        return Outline(
            headline="What Ramanujam Gets Right About Pricing",
            hook="Most teams price last.",
            sections=[
                {"heading": "Start with willingness to pay", "argument": "Test first", "sources": ["S1"]},
                {"heading": "Segment before discounting", "argument": "Segment", "sources": ["S2"]},
                {"heading": "Revisit on a cadence", "argument": "Cadence", "sources": ["S3"]},
            ],
        )

    def test_section_prompt_scopes_to_one_section(self, outline):
        prompt = section_prompt(outline, 0, "<source id='S1'>x</source>")
        assert "Start with willingness to pay" in prompt
        # Naming the other headings is what stops a 3B from writing the whole
        # essay three times over.
        assert "Segment before discounting" in prompt
        assert "do not write these" in prompt

    def test_section_prompt_carries_the_central_claim(self, outline):
        assert "Most teams price last." in section_prompt(outline, 1, "ctx")

    def test_repair_prompt_names_failures_and_fixes(self):
        rubric = evaluate("too short", available_markers=MARKERS)
        prompt = repair_prompt("too short", rubric, "ctx")
        assert "Problems:" in prompt
        assert "FIX:" in prompt
        for check in rubric.failures:
            assert check.name in prompt
