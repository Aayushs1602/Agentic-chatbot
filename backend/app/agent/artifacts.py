"""Artifact extraction.

The model emits documents inside a fenced envelope:

    ```artifact {"kind": "html", "title": "Q3 Growth Review"}
    <h1>Q3 Growth Review</h1>
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
# Fence lines orphaned once the block between them is removed.
_ORPHAN_FENCE_RE = re.compile(r"^[ \t]*```[ \t]*\w*[ \t]*$\n?", re.M)
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
        artifacts.append(
            ParsedArtifact(
                kind=kind,
                title=_clean_title(meta.get("title")) or _infer_title(body, kind),
                content=body,
            )
        )
        spans.append((match.start(), match.end()))

    # Pass 2: only if the model never used the envelope at all.
    if not artifacts:
        for match in _GENERIC_FENCE_RE.finditer(reply):
            body = (match.group(3) or "").strip()
            if not body:
                continue
            meta = _parse_meta(match.group(2))
            kind = _resolve_kind(meta.get("kind"), (match.group(1) or "").lower(), body)
            artifacts.append(
                ParsedArtifact(
                    kind=kind,
                    title=_clean_title(meta.get("title")) or _infer_title(body, kind),
                    content=body,
                )
            )
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
    log.info(
        "artifacts_extracted",
        count=len(artifacts),
        kinds=[a.kind for a in artifacts],
    )
    return ExtractionResult(text=cleaned, artifacts=artifacts)


def _parse_meta(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        # Near-JSON is common from small models: {kind: html, title: Foo}
        return {k.lower(): v.strip() for k, v in _KV_RE.findall(raw)}


def _resolve_kind(declared: Optional[str], fence_kind: str, body: str) -> str:
    """Decide html vs markdown.

    The body wins over the declaration. A model that labels a block `markdown`
    and then writes a full HTML document is common, and honouring the label
    would send raw tags down the markdown path where they render as text.
    """
    if _HTML_HINT_RE.search(body):
        return "html"
    if declared and str(declared).lower() in {"html", "markdown", "md"}:
        return "html" if str(declared).lower() == "html" else "markdown"
    if fence_kind == "html":
        return "html"
    return "markdown"


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
