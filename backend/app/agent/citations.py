"""Post-generation citation resolution.

The second line of defence on grounding. The skill *asks* for `[S1]`-style
markers; this module *verifies* them. Small models invent markers — `[S7]` when
only five sources were provided — and an unresolvable marker is worse than no
marker at all, because it looks like evidence.

Every marker in the generated text is resolved against the retrieved set.
Unresolved ones are stripped and counted; what survives becomes the citation
payload the UI renders as source cards.

Pure functions over strings and dataclasses, so the rule that decides whether an
answer is adequately grounded is unit-testable without a model or a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

# [S1] · [S1, S3] · [S1][S2] — small models produce all of these.
_MARKER_BLOCK_RE = re.compile(r"\[\s*(S\d+(?:\s*,\s*S\d+)*)\s*\]", re.I)
_MARKER_RE = re.compile(r"S\d+", re.I)


@dataclass
class CitationReport:
    text: str
    citations: List[Dict[str, Any]] = field(default_factory=list)
    resolved: List[str] = field(default_factory=list)
    invented: List[str] = field(default_factory=list)
    unused: List[str] = field(default_factory=list)

    @property
    def is_grounded(self) -> bool:
        return bool(self.resolved)

    def to_log(self) -> Dict[str, Any]:
        return {
            "resolved": self.resolved,
            "invented": self.invented,
            "unused": self.unused,
            "grounded": self.is_grounded,
        }


def extract_markers(text: str) -> List[str]:
    """Every marker referenced in `text`, uppercased, in order of first use."""
    seen: List[str] = []
    for block in _MARKER_BLOCK_RE.finditer(text):
        for marker in _MARKER_RE.findall(block.group(1)):
            upper = marker.upper()
            if upper not in seen:
                seen.append(upper)
    return seen


def resolve_citations(text: str, chunks: Sequence[Any]) -> CitationReport:
    """Strip invented markers and build the citation payload for what remains.

    `chunks` are `RetrievedChunk`s; only `.marker` and `.to_citation()` are used,
    which keeps this module free of a retrieval import.
    """
    available = {c.marker.upper(): c for c in chunks if getattr(c, "marker", "")}
    referenced = extract_markers(text)

    resolved = [m for m in referenced if m in available]
    invented = [m for m in referenced if m not in available]

    cleaned = _strip_markers(text, invented) if invented else text

    return CitationReport(
        text=cleaned.strip(),
        citations=[available[m].to_citation() for m in resolved],
        resolved=resolved,
        invented=invented,
        unused=[m for m in available if m not in resolved],
    )


def _strip_markers(text: str, drop: Sequence[str]) -> str:
    """Remove `drop` markers, preserving any valid ones sharing a bracket group."""
    drop_upper = {d.upper() for d in drop}

    def replace(match: re.Match) -> str:
        keep = [m for m in _MARKER_RE.findall(match.group(1)) if m.upper() not in drop_upper]
        return f"[{', '.join(keep)}]" if keep else ""

    cleaned = _MARKER_BLOCK_RE.sub(replace, text)
    # Stripping a marker can leave " ." or a double space behind.
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def format_sources_footer(citations: Sequence[Dict[str, Any]]) -> str:
    """Human-readable source list, for artifacts and copied text.

    The UI renders interactive source cards from the citation payload; this is
    the plain-text equivalent that survives a copy-paste into a doc.
    """
    if not citations:
        return ""
    lines = ["", "---", "**Sources**", ""]
    for c in citations:
        guests = ", ".join(c.get("guests") or [])
        label = f"{c['title']}" + (f" — {guests}" if guests else "")
        url = c.get("url")
        lines.append(f"- `{c['marker']}` {label}" + (f" — [listen]({url})" if url else ""))
    return "\n".join(lines)
