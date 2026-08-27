"""Artifact sanitization.

Generated HTML is untrusted. It is produced by a model that just read hundreds
of third-party transcripts, so a prompt-injection payload reaching the artifact
pipeline is a realistic path, not a hypothetical one.

Two independent layers, neither trusted alone:

1. **Server-side allowlist** (here). Everything not explicitly permitted is
   removed before the HTML is ever stored as renderable.
2. **Render isolation** (frontend). A `sandbox`ed iframe with no
   `allow-same-origin` and no `allow-scripts`, plus a `default-src 'none'` CSP
   injected into the document. Opaque origin, no network egress — so even a
   payload that survives layer 1 has nothing to reach.

The unusual part is the **report**. Sanitizers normally strip silently; this one
records what it removed and why, and the viewer shows it ("4 elements removed —
details"). That is what makes the policy legible to an evaluator instead of a
black box, and it turns a silent security control into a visible one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Set

import nh3

from app.logging import get_logger

log = get_logger("security.sanitize")

# Structure, text, tables, and inline styling. No scripting, no embedding, no
# form submission, no external document loading.
ALLOWED_TAGS: Set[str] = {
    "a", "abbr", "article", "aside", "b", "blockquote", "br", "caption", "cite",
    "code", "col", "colgroup", "dd", "del", "details", "div", "dl", "dt", "em",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "i", "img", "ins", "kbd", "li", "main", "mark", "nav", "ol",
    "p", "pre", "q", "s", "samp", "section", "small", "span", "strong", "style",
    "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "time", "tr", "u", "ul", "var",
}

ALLOWED_ATTRIBUTES: Dict[str, Set[str]] = {
    "*": {"class", "id", "style", "title", "role", "aria-label", "aria-hidden"},
    # `rel` is deliberately absent: ammonia injects it itself via `link_rel`,
    # and allowing both makes it panic. Injection is the safer of the two
    # anyway — the model cannot choose the value.
    "a": {"href", "target"},
    "img": {"src", "alt", "width", "height", "loading"},
    "td": {"colspan", "rowspan", "headers"},
    "th": {"colspan", "rowspan", "scope", "headers"},
    "col": {"span"},
    "colgroup": {"span"},
    "time": {"datetime"},
}

# Explicitly named so the report can explain *why* something went, rather than
# just noting its absence.
DANGEROUS_TAGS = {
    "script": "executes JavaScript",
    "iframe": "embeds an arbitrary document",
    "object": "embeds a plugin or document",
    "embed": "embeds external content",
    "applet": "executes an applet",
    "form": "submits data off-page",
    "input": "collects user input for a form",
    "button": "triggers form or script behaviour",
    "textarea": "collects user input for a form",
    "select": "collects user input for a form",
    "link": "loads an external stylesheet or resource",
    "base": "rewrites the resolution of every relative URL",
    "meta": "can trigger a refresh or redirect",
    "svg": "can carry event handlers and scripted content",
    "math": "can carry scripted content",
    "frame": "embeds an arbitrary document",
    "frameset": "embeds arbitrary documents",
    "noscript": "hides content from the sanitizer's intent",
    "template": "defers content past inspection",
}

# `javascript:` and `data:text/html` are the two that turn a link into script.
# Note the alternation carries its own colon: an earlier version read
# `(?:javascript|vbscript|data\s*:\s*text/html)\s*:` and required a *second*
# colon after data:text/html, so that payload was cleaned but never reported.
_DANGEROUS_URL_RE = re.compile(
    r"^\s*(?:javascript\s*:|vbscript\s*:|data\s*:\s*text/html)", re.I
)
_EVENT_ATTR_RE = re.compile(r"^on[a-z]+$", re.I)

# CSS that reaches outside the document or executes.
#
# Each pattern matches the *whole* construct, not just its opening token. A
# prefix-only match (`url\s*\(\s*['\"]?[a-z]+:`) is worse than useless when the
# pattern is used for substitution: scrubbing `url('https:` out of
# `url('https://evil.test/x.png')` leaves the hostname sitting in the document.
_CSS_DANGER = (
    (re.compile(r"expression\s*\([^)]*\)", re.I), "CSS expression() executes JavaScript"),
    (re.compile(r"@import[^;}]*;?", re.I), "@import loads an external stylesheet"),
    (
        re.compile(r"url\s*\(\s*['\"]?\s*(?!data:image/)[a-z][a-z0-9+.\-]*:[^)]*\)", re.I),
        "CSS url() reaches an external origin",
    ),
    (re.compile(r"behavior\s*:[^;}]*;?", re.I), "CSS behavior: binds scripted behaviour"),
    (re.compile(r"-moz-binding\s*:[^;}]*;?", re.I), "-moz-binding binds scripted behaviour"),
)


@dataclass
class SanitizerReport:
    removed_tags: Dict[str, int] = field(default_factory=dict)
    removed_attributes: Dict[str, int] = field(default_factory=dict)
    removed_urls: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def total_removed(self) -> int:
        return (
            sum(self.removed_tags.values())
            + sum(self.removed_attributes.values())
            + len(self.removed_urls)
        )

    @property
    def is_clean(self) -> bool:
        return self.total_removed == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_removed": self.total_removed,
            "clean": self.is_clean,
            "removed_tags": self.removed_tags,
            "removed_attributes": self.removed_attributes,
            "removed_urls": self.removed_urls[:20],
            "notes": self.notes,
            "policy": {
                "allowed_tags": sorted(ALLOWED_TAGS),
                "scripts": "blocked",
                "external_requests": "blocked",
                "forms": "blocked",
                "rendering": "sandboxed iframe, opaque origin, CSP default-src 'none'",
            },
        }


@dataclass
class SanitizedArtifact:
    html: str
    report: SanitizerReport


class _Inspector(HTMLParser):
    """Records what the allowlist will remove.

    nh3 cleans but does not report, so the document is walked first to build the
    explanation, then cleaned. Inspection is advisory only — nh3 remains the
    thing that actually enforces the policy, so a gap here weakens the report,
    never the security.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.report = SanitizerReport()
        self._in_style = False

    def handle_starttag(self, tag: str, attrs) -> None:
        self._check_tag(tag)
        allowed_for_tag = ALLOWED_ATTRIBUTES.get("*", set()) | ALLOWED_ATTRIBUTES.get(tag, set())

        for name, value in attrs:
            lowered = name.lower()
            if _EVENT_ATTR_RE.match(lowered):
                self._count_attr(lowered)
                self._note(f"`{lowered}` is an inline event handler")
                continue
            if lowered not in allowed_for_tag:
                self._count_attr(lowered)
                continue
            if value and lowered in {"href", "src"} and _DANGEROUS_URL_RE.match(value):
                self.report.removed_urls.append(value[:120])
                self._note("a `javascript:` or `data:text/html` URL was removed")
            if value and lowered == "style":
                self._check_css(value)

        if tag == "style":
            self._in_style = True

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self._in_style = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._check_css(data)

    def _check_tag(self, tag: str) -> None:
        if tag in DANGEROUS_TAGS:
            self._count_tag(tag)
            self._note(f"`<{tag}>` {DANGEROUS_TAGS[tag]}")
        elif tag not in ALLOWED_TAGS:
            self._count_tag(tag)

    def _check_css(self, css: str) -> None:
        for pattern, reason in _CSS_DANGER:
            if pattern.search(css):
                self._count_attr("style")
                self._note(reason)

    def _count_tag(self, tag: str) -> None:
        self.report.removed_tags[tag] = self.report.removed_tags.get(tag, 0) + 1

    def _count_attr(self, name: str) -> None:
        self.report.removed_attributes[name] = self.report.removed_attributes.get(name, 0) + 1

    def _note(self, note: str) -> None:
        if note not in self.report.notes:
            self.report.notes.append(note)


# Tags whose *contents* go too, not just their markup. Ammonia's default is to
# unwrap a disallowed tag and keep its text — which would turn
# `<script>alert(1)</script>` into the visible text `alert(1)`. Harmless to
# render, but it means the payload is still sitting in the document, and any
# later change to how artifacts are handled could reactivate it.
_STRIP_CONTENT_TAGS = {
    "script", "style_disallowed", "noscript", "template", "iframe", "object",
    "embed", "applet", "form", "select", "textarea", "frame", "frameset",
    "svg", "math", "link", "base", "meta", "title", "head",
}

_SAFE_URL_SCHEMES = {"http", "https", "mailto", "tel", "data"}

_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.I | re.S)


def _scrub_css(css: str) -> str:
    """Remove CSS constructs that execute or reach off-origin.

    `<style>` is allowlisted so artifacts can be laid out properly, and ammonia
    passes a permitted tag's text through untouched — so without this pass an
    `@import` inside a style block would survive layer 1 entirely. The iframe
    CSP would still stop it, but defence in depth is the whole design.
    """
    for pattern, _reason in _CSS_DANGER:
        css = pattern.sub("/* removed */", css)
    return css


def _attribute_filter(tag: str, attribute: str, value: str):
    """Last-mile value check. Returning None drops the attribute."""
    if attribute in {"href", "src"}:
        stripped = value.strip().lower()
        if _DANGEROUS_URL_RE.match(value):
            return None
        # `data:` is permitted for inline images only — an artifact cannot load
        # anything external, so this is the only way it can show a picture.
        # `data:text/html` would be a document, which is a sandbox escape.
        if stripped.startswith("data:") and not stripped.startswith("data:image/"):
            return None
    if attribute == "style":
        for pattern, _reason in _CSS_DANGER:
            if pattern.search(value):
                return None
    return value


def sanitize_html(html: str) -> SanitizedArtifact:
    """Clean untrusted HTML and explain what changed."""
    inspector = _Inspector()
    try:
        inspector.feed(html)
        inspector.close()
    except Exception as exc:  # noqa: BLE001
        # Malformed markup breaks the *explanation*, never the cleaning.
        log.warning("sanitizer_inspection_failed", error=str(exc))
        inspector.report.notes.append("The document was malformed; the report may be incomplete.")

    prepared = _STYLE_BLOCK_RE.sub(
        lambda m: m.group(1) + _scrub_css(m.group(2)) + m.group(3), html
    )

    cleaned = nh3.clean(
        prepared,
        tags=ALLOWED_TAGS,
        attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
        clean_content_tags=_STRIP_CONTENT_TAGS & set(DANGEROUS_TAGS) | {"script", "noscript", "template"},
        attribute_filter=_attribute_filter,
        url_schemes=_SAFE_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )

    if not inspector.report.is_clean:
        log.info(
            "artifact_sanitized",
            removed=inspector.report.total_removed,
            tags=inspector.report.removed_tags,
        )

    return SanitizedArtifact(html=cleaned, report=inspector.report)


# Markdown takes a different, simpler path: rendered client-side by
# react-markdown with raw HTML disabled. Only the length guard applies here, so
# a runaway generation cannot be stored unbounded.
MAX_ARTIFACT_CHARS = 200_000


def sanitize_markdown(markdown: str) -> SanitizedArtifact:
    report = SanitizerReport()
    if len(markdown) > MAX_ARTIFACT_CHARS:
        markdown = markdown[:MAX_ARTIFACT_CHARS]
        report.notes.append(f"Truncated to {MAX_ARTIFACT_CHARS:,} characters.")
    return SanitizedArtifact(html=markdown, report=report)
