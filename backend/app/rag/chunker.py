"""Transcript chunking.

Podcast transcripts are long, conversational, and speaker-segmented. Three
properties matter for grounding quality:

1. **Never split mid-sentence.** A chunk that ends halfway through a claim
   produces a citation that doesn't support what it's cited for.
2. **Prefer speaker-turn boundaries.** A turn is the natural semantic unit; a
   chunk that spans "Lenny asks / guest answers" retrieves far better than one
   that starts mid-answer.
3. **Keep char offsets and timestamps.** `start_char` lets a citation point at
   exact source text; `start_seconds` turns it into a YouTube deep link, which
   is the difference between "trust me" and "watch it yourself".

Token counts use a words × 1.33 approximation rather than a real tokenizer.
That keeps the chunker dependency-free and deterministic, and the only decision
it drives is where to cut — a 5% error moves a boundary by a sentence, which
costs nothing. Actual context budgeting uses the model's own accounting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# Average English token is ~0.75 words. Overestimating is the safe direction:
# it makes chunks slightly smaller and keeps us inside the context window.
_WORDS_TO_TOKENS = 1.33

# "[00:12:34]", "(01:02:03)", "00:12:34" at the start of a line.
_TIMESTAMP_RE = re.compile(
    r"[\[\(]?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[\]\)]?",
)
_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*[\[\(]?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[\]\)]?",
    re.MULTILINE,
)

# "Lenny Rachitsky:" / "**Guest:**" / "SPEAKER 1:" at the start of a line.
_SPEAKER_RE = re.compile(
    r"^\s*(?:\*\*)?([A-Z][\w .'\-]{1,40})(?:\*\*)?\s*:\s",
    re.MULTILINE,
)

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'“])")


@dataclass(frozen=True)
class Chunk:
    ord: int
    text: str
    token_count: int
    start_char: int
    end_char: int
    start_seconds: Optional[int]


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text.split()) * _WORDS_TO_TOKENS))


def parse_timestamp(text: str) -> Optional[int]:
    """First `HH:MM:SS` or `MM:SS` in `text`, as seconds. None if absent."""
    m = _TIMESTAMP_RE.search(text)
    if not m:
        return None
    hours, minutes, seconds = m.group(1), m.group(2), m.group(3)
    if hours is None:
        # "12:34" is MM:SS, not HH:MM — podcast timestamps count from zero.
        return int(minutes) * 60 + int(seconds)
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _segment(text: str) -> List[int]:
    """Offsets of candidate split points, best boundaries first.

    Speaker turns and timestamped lines are strong boundaries; blank lines are
    decent; sentence ends are the fallback. Returns a sorted, deduplicated list
    of character offsets that always includes 0.
    """
    points = {0}
    for pattern in (_SPEAKER_RE, _TIMESTAMP_LINE_RE):
        points.update(m.start() for m in pattern.finditer(text))
    for m in re.finditer(r"\n\s*\n", text):
        points.update({m.end()})
    for m in _SENTENCE_END_RE.finditer(text):
        points.add(m.end())
    return sorted(points)


def chunk_transcript(
    text: str,
    *,
    target_tokens: int = 800,
    overlap_tokens: int = 120,
) -> List[Chunk]:
    """Split `text` into overlapping chunks aligned to natural boundaries."""
    text = text.strip()
    if not text:
        return []
    if overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be smaller than target_tokens")

    boundaries = _segment(text)
    total = len(text)
    # Approximate chars-per-token for this specific text, so the mapping from a
    # token budget to a character window stays accurate for dense or sparse
    # transcripts alike.
    #
    # Clamped, because the estimate is word-based and degenerate input (one
    # enormous "word" — a base64 blob, a run-on URL) would otherwise compute a
    # multi-megabyte window and emit the whole transcript as a single chunk.
    # English averages ~4 chars/token; [2, 8] keeps genuine adaptivity while
    # bounding the damage.
    raw_ratio = total / max(1, estimate_tokens(text))
    chars_per_token = min(8.0, max(2.0, raw_ratio))
    window = int(target_tokens * chars_per_token)
    step = int((target_tokens - overlap_tokens) * chars_per_token)

    chunks: List[Chunk] = []
    start = 0
    ordinal = 0

    while start < total:
        ideal_end = min(total, start + window)
        end = ideal_end if ideal_end >= total else _snap(boundaries, ideal_end, start, window)

        body = text[start:end].strip()
        if body:
            # Search a little before the chunk too: the governing timestamp is
            # often on the line immediately above where the chunk begins.
            lookbehind = text[max(0, start - 200) : end]
            chunks.append(
                Chunk(
                    ord=ordinal,
                    text=body,
                    token_count=estimate_tokens(body),
                    start_char=start,
                    end_char=end,
                    start_seconds=parse_timestamp(lookbehind),
                )
            )
            ordinal += 1

        if end >= total:
            break
        next_start = start + step
        # Guarantee forward progress even if `_snap` pulled the boundary back
        # behind the nominal step — otherwise a pathological transcript loops.
        start = max(next_start, start + 1) if next_start <= start else next_start

    return chunks


def _snap(boundaries: List[int], ideal_end: int, start: int, window: int) -> int:
    """Nearest boundary at or before `ideal_end`, without making a tiny chunk.

    Falls back to `ideal_end` when the nearest boundary would cut the chunk to
    less than half the target — a short chunk retrieves worse than a slightly
    ragged one.
    """
    import bisect

    idx = bisect.bisect_right(boundaries, ideal_end) - 1
    while idx >= 0:
        candidate = boundaries[idx]
        if candidate > start and (candidate - start) >= window * 0.5:
            return candidate
        idx -= 1
    return ideal_end
