"""Artifact extraction.

The model emits documents inside a fenced envelope:

    ```artifact {"kind": "markdown", "title": "Hiring Your First PM"}
    # Hiring Your First PM
    ...
    ```

Parsing is deliberately forgiving. A 3B model produces malformed fences often
enough that strict parsing would lose real documents: the metadata may be bare
JSON, a loose `kind=html` pair, or absent entirely; the closing fence may be
missing when generation hit a token limit. Every one of those still represents a
document the user asked for, so each is recovered rather than discarded.

What is *not* forgiving is what happens next: everything extracted here is
untrusted input to `security.sanitize`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.logging import get_logger

log = get_logger("agent.artifacts")

# Two passes, because models wrap the envelope in another fence often enough
# that a single pattern loses real documents. A reply like
#
#     ```html
#     ```artifact {"kind":"html", ...}
#     <h1>…
#     ```
#
# makes a combined pattern match the *outer* fence, whose body terminates
# immediately at the inner fence's backticks — yielding an empty body, no
# artifact, and the whole document dumped into the chat as raw markup.
#
# So an explicitly tagged ```artifact fence wins wherever it appears, and the
# generic language fences are only consulted when no artifact fence exists.
_ARTIFACT_FENCE_RE = re.compile(
    r"```[ \t]*artifact[ \t]*(\{.*?\})?[ \t]*\n(.*?)(?:\n[ \t]*```|\Z)", re.S | re.I
)
_GENERIC_FENCE_RE = re.compile(
    r"```[ \t]*(html|markdown|md)[ \t]*(\{.*?\})?[ \t]*\n(.*?)(?:\n[ \t]*```|\Z)",
    re.S | re.I,
)
# Fence lines orphaned once the block between them is removed. `{3,}` rather
# than exactly three: a model that wraps the envelope in another fence leaves a
# run of backticks behind, and a stray ``````` in the chat looks like a bug to
# anyone reading it.
_ORPHAN_FENCE_RE = re.compile(r"^[ \t]*`{3,}[ \t]*\w*[ \t]*$\n?", re.M)
# Whatever a reply is left with once the document is removed, if it is only
# fence punctuation it is not a message.
_FENCE_ONLY_RE = re.compile(r"^[\s`~]*$")
_KV_RE = re.compile(r"(\w+)\s*[:=]\s*\"?([\w \-/.]+)\"?")
_HTML_HINT_RE = re.compile(r"<(?:h[1-6]|div|table|section|article|p|ul|ol)\b", re.I)
# `[^\n]+` for the markdown branch, not `.+`: with re.S (needed so an <h1> can
# span lines) a greedy `.` swallows the entire document into the title.
_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>|^#[ \t]+([^\n]+)", re.S | re.I | re.M)

MAX_TITLE = 120


@dataclass
class ParsedArtifact:
    kind: str  # "html" | "markdown"
    title: str
    content: str


@dataclass
class ExtractionResult:
    text: str                       # the reply with artifact blocks removed
    artifacts: List[ParsedArtifact]


def extract_artifacts(reply: str) -> ExtractionResult:
    """Pull artifact blocks out of a model reply."""
    artifacts: List[ParsedArtifact] = []
    spans: List[Tuple[int, int]] = []

    # Pass 1: explicitly tagged envelopes, wherever they sit.
    for match in _ARTIFACT_FENCE_RE.finditer(reply):
        body = (match.group(2) or "").strip()
        if not body:
            continue
        meta = _parse_meta(match.group(1))
        kind = _resolve_kind(meta.get("kind"), "artifact", body)
        title = _clean_title(meta.get("title")) or _infer_title(body, kind)
        if kind == "markdown":
            body = normalise_markdown(body)
        artifacts.append(ParsedArtifact(kind=kind, title=title, content=body))
        spans.append((match.start(), match.end()))

    # Pass 2: only if the model never used the envelope at all.
    if not artifacts:
        for match in _GENERIC_FENCE_RE.finditer(reply):
            body = (match.group(3) or "").strip()
            if not body:
                continue
            meta = _parse_meta(match.group(2))
            kind = _resolve_kind(meta.get("kind"), (match.group(1) or "").lower(), body)
            title = _clean_title(meta.get("title")) or _infer_title(body, kind)
            if kind == "markdown":
                body = normalise_markdown(body)
            artifacts.append(ParsedArtifact(kind=kind, title=title, content=body))
            spans.append((match.start(), match.end()))

    if not artifacts:
        return ExtractionResult(text=reply, artifacts=[])

    # Remove the blocks from the chat text — the artifact renders in its own
    # pane, and repeating hundreds of lines of HTML in the transcript is noise.
    cleaned = reply
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]

    # Removing an inner envelope leaves its wrapper's fence lines behind.
    cleaned = _ORPHAN_FENCE_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if _FENCE_ONLY_RE.match(cleaned):
        cleaned = ""

    # Models frequently write the document twice — once as plain chat, then
    # again inside the envelope. Only the fenced copy is removed above, so the
    # unfenced one sits in the transcript and the document appears twice.
    if _duplicates_artifact(cleaned, artifacts):
        log.info("artifact_duplicate_chat_text_dropped", chars=len(cleaned))
        cleaned = ""
    log.info(
        "artifacts_extracted",
        count=len(artifacts),
        kinds=[a.kind for a in artifacts],
    )
    return ExtractionResult(text=cleaned, artifacts=artifacts)


# A trailing "Sources:" section the model wrote itself, containing nothing but
# bare markers. It duplicates the viewer's source cards and carries none of
# their information — no title, no guest, no timestamp. Stripped so a real one
# can be appended in its place.
_BARE_SOURCES_RE = re.compile(
    r"\n[#*\s]*(?:sources?|references?|citations?)\s*:?\s*\n"
    r"(?:[-*\d.\s]*\[?S\d+\]?[^\n]*\n?)+\s*$",
    re.I,
)


def strip_bare_sources(body: str) -> str:
    """Remove a self-written sources list made only of markers."""
    return _BARE_SOURCES_RE.sub("", body).rstrip()


def _normalise(text: str) -> str:
    """Lowercase, strip markup, collapse whitespace. For comparison only."""
    return " ".join(re.sub(r"[#*_`>\-\[\]]|<[^>]+>", " ", text).lower().split())


def _duplicates_artifact(chat_text: str, artifacts: List[ParsedArtifact]) -> bool:
    """Is the leftover chat text just a second copy of the document?

    Compared on normalised text, so a markdown copy and an HTML copy of the same
    content still match. The 60-character prefix identifies a repeat without
    tripping on a one-line summary that legitimately quotes the opening, and the
    200-character floor keeps genuine covering sentences.
    """
    stripped = _normalise(chat_text)
    if len(stripped) < 200:
        return False
    for artifact in artifacts:
        body = _normalise(artifact.content)
        if not body:
            continue
        if stripped[:60] in body or body[:60] in stripped:
            return True
    return False


def _parse_meta(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        # Near-JSON is common from small models: {kind: html, title: Foo}
        return {k.lower(): v.strip() for k, v in _KV_RE.findall(raw)}


# Markdown constructs, counted per line so a document is weighed rather than
# sniffed.
_MD_SIGNALS = (
    re.compile(r"^#{1,6}\s+\S", re.M),          # ## heading
    re.compile(r"^\s*[-*+]\s+\S", re.M),        # - bullet
    re.compile(r"^\s*\d+\.\s+\S", re.M),        # 1. numbered
    re.compile(r"\*\*[^*\n]+\*\*"),             # **bold**
    re.compile(r"\[[^\]\n]+\]\([^)\n]+\)"),     # [link](url)
    re.compile(r"^>\s+\S", re.M),               # > quote
    re.compile(r"^\|.+\|$", re.M),              # | table |
)
_HTML_TAG_RE = re.compile(r"<(/?)([a-z][a-z0-9]*)\b[^>]*>", re.I)


def _resolve_kind(declared: Optional[str], fence_kind: str, body: str) -> str:
    """Decide html vs markdown by weighing the body, not by sniffing one tag.

    An earlier version returned "html" the moment it saw any block tag. A model
    that opens with `<h1>Title</h1>` and then writes the entire document in
    markdown — which qwen2.5:3b does routinely — was therefore rendered as HTML,
    so every `##`, `- ` and `**bold**` appeared as literal characters in the
    viewer. That is the "it rendered plain html" bug.

    The declaration is the weakest signal and comes last: models mislabel these
    constantly in both directions.
    """
    md_score = sum(len(pattern.findall(body)) for pattern in _MD_SIGNALS)
    html_tags = _HTML_TAG_RE.findall(body)
    html_score = len(html_tags)

    # A real HTML document has many tags and structural ones at that.
    structural = sum(1 for _, tag in html_tags if tag.lower() in {
        "div", "section", "article", "table", "tbody", "tr", "td", "th", "style", "header"
    })
    if structural >= 2 or html_score >= 8:
        return "html"
    # Markdown syntax that outweighs the tags means markdown, whatever the
    # document opened with.
    if md_score >= 3 and md_score > html_score:
        return "markdown"
    if html_score >= 3:
        return "html"

    declared_lower = str(declared or "").lower()
    if declared_lower in {"html", "markdown", "md"}:
        return "html" if declared_lower == "html" else "markdown"
    if fence_kind == "html":
        return "html"
    return "markdown"


# Block tags a model sprinkles into otherwise-markdown output. Converted rather
# than stripped, because react-markdown runs with raw HTML disabled and would
# silently delete the text inside them.
_H_TAG_RE = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.S | re.I)
_SIMPLE_TAGS = (
    (re.compile(r"</?(?:p|div|section|article|br\s*/?)\s*[^>]*>", re.I), "\n"),
    (re.compile(r"<(?:strong|b)>(.*?)</(?:strong|b)>", re.S | re.I), r"**\1**"),
    (re.compile(r"<(?:em|i)>(.*?)</(?:em|i)>", re.S | re.I), r"*\1*"),
    (re.compile(r"<li[^>]*>(.*?)</li>", re.S | re.I), r"- \1"),
    (re.compile(r"</?[uo]l[^>]*>", re.I), "\n"),
    (re.compile(r"<code>(.*?)</code>", re.S | re.I), r"`\1`"),
)


def normalise_markdown(body: str) -> str:
    """Fold stray HTML tags in a markdown document into markdown."""
    body = _H_TAG_RE.sub(lambda m: f"\n{'#' * int(m.group(1))} {m.group(2).strip()}\n", body)
    for pattern, replacement in _SIMPLE_TAGS:
        body = pattern.sub(replacement, body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _clean_title(title: Optional[str]) -> str:
    if not title:
        return ""
    return " ".join(str(title).split())[:MAX_TITLE]


def _infer_title(body: str, kind: str) -> str:
    """Fall back to the document's own first heading."""
    match = _TITLE_RE.search(body)
    if match:
        raw = match.group(1) or match.group(2) or ""
        stripped = re.sub(r"<[^>]+>", "", raw)
        cleaned = _clean_title(stripped)
        if cleaned:
            return cleaned
    return "Untitled document" if kind == "markdown" else "Untitled page"
