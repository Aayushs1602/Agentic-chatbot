"""Artifact envelope extraction.

Parsing is forgiving on purpose. A 3B model produces malformed fences often
enough that strict parsing loses real documents, and every case here was
observed coming out of qwen2.5:3b against the live corpus.
"""

from __future__ import annotations

from app.agent.artifacts import extract_artifacts

F = "`" * 3


class TestWellFormed:
    def test_extracts_html_envelope(self):
        reply = (
            "Here's the one-pager.\n\n"
            f'{F}artifact {{"kind": "html", "title": "Q3 Review"}}\n'
            "<h1>Q3 Review</h1><p>Retention flattened [S1].</p>\n"
            f"{F}\n"
        )
        result = extract_artifacts(reply)
        assert len(result.artifacts) == 1
        assert result.artifacts[0].kind == "html"
        assert result.artifacts[0].title == "Q3 Review"
        # The document must leave the chat text; repeating hundreds of lines of
        # markup in the transcript is noise.
        assert "<h1>" not in result.text
        assert "Here's the one-pager." in result.text

    def test_extracts_markdown_envelope(self):
        reply = f'{F}artifact {{"kind": "markdown", "title": "Checklist"}}\n# Checklist\n\n- one\n{F}'
        result = extract_artifacts(reply)
        assert result.artifacts[0].kind == "markdown"
        assert result.artifacts[0].title == "Checklist"

    def test_multiple_artifacts(self):
        reply = (
            f'{F}artifact {{"kind":"markdown","title":"A"}}\n# A\n{F}\n\n'
            f'{F}artifact {{"kind":"markdown","title":"B"}}\n# B\n{F}\n'
        )
        assert [a.title for a in extract_artifacts(reply).artifacts] == ["A", "B"]


class TestMalformedButRecoverable:
    def test_envelope_wrapped_in_another_fence(self):
        # Observed repeatedly from qwen2.5:3b. A single combined pattern matches
        # the OUTER fence, whose body ends immediately at the inner fence's
        # backticks — losing the document entirely and dumping raw markup into
        # the chat.
        reply = (
            f"{F}html\n"
            f'{F}artifact {{"kind": "html", "title": "Pricing Strategy"}}\n'
            "<h1>Pricing Strategy</h1><p>Value-based pricing wins [S1].</p>\n"
            f"{F}\n"
        )
        result = extract_artifacts(reply)
        assert len(result.artifacts) == 1
        assert result.artifacts[0].title == "Pricing Strategy"
        assert "<h1>" in result.artifacts[0].content

    def test_orphan_fence_is_stripped_from_chat_text(self):
        reply = f"{F}html\n{F}artifact\n<h1>X</h1>\n{F}\n"
        assert F not in extract_artifacts(reply).text

    def test_unclosed_fence_is_recovered(self):
        # Generation hitting a token limit still represents a real document.
        reply = f'{F}artifact {{"kind":"html","title":"Truncated"}}\n<h1>Truncated</h1><p>text'
        result = extract_artifacts(reply)
        assert len(result.artifacts) == 1
        assert "Truncated" in result.artifacts[0].content

    def test_loose_non_json_metadata(self):
        reply = f"{F}artifact {{kind: html, title: Loose Meta}}\n<h1>Hi</h1>\n{F}"
        result = extract_artifacts(reply)
        assert result.artifacts[0].kind == "html"
        assert result.artifacts[0].title == "Loose Meta"

    def test_missing_metadata_infers_from_content(self):
        reply = f"{F}artifact\n<h1>Inferred Title</h1><p>body</p>\n{F}"
        result = extract_artifacts(reply)
        assert result.artifacts[0].kind == "html"
        assert result.artifacts[0].title == "Inferred Title"

    def test_markdown_heading_becomes_the_title(self):
        reply = f"{F}artifact\n# My Document\n\nSome body text here.\n{F}"
        result = extract_artifacts(reply)
        # `.` with re.S once swallowed the whole document into the title.
        assert result.artifacts[0].title == "My Document"

    def test_bare_language_fence_without_envelope(self):
        reply = f"Here you go.\n\n{F}html\n<h1>Doc</h1><p>x</p>\n{F}"
        result = extract_artifacts(reply)
        assert len(result.artifacts) == 1
        assert result.artifacts[0].kind == "html"

    def test_body_overrides_a_wrong_kind_label(self):
        # Models label a block `markdown` and then write HTML. Honouring the
        # label sends raw tags down the markdown path, where they render as text.
        reply = f'{F}artifact {{"kind":"markdown"}}\n<h1>Actually HTML</h1><p>x</p>\n{F}'
        assert extract_artifacts(reply).artifacts[0].kind == "html"


class TestNoArtifact:
    def test_plain_reply_is_untouched(self):
        reply = "Retention curves that flatten indicate product-market fit [S1]."
        result = extract_artifacts(reply)
        assert result.artifacts == []
        assert result.text == reply

    def test_ordinary_code_block_is_not_an_artifact(self):
        # A python snippet in an answer is not a document the user asked for.
        reply = f"Try this:\n\n{F}python\nprint('hi')\n{F}"
        assert extract_artifacts(reply).artifacts == []

    def test_empty_fence_is_ignored(self):
        assert extract_artifacts(f"{F}artifact\n\n{F}").artifacts == []

    def test_empty_input(self):
        assert extract_artifacts("").artifacts == []
