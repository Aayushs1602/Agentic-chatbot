"""The Ship 30 for 30 rubric, and the section-by-section writer.

Two ideas here, and they are the point of the whole skill.

**A skill is instructions plus a machine-checkable rubric plus a repair loop.**
Only the instructions survive being written as a one-off prompt. `evaluate()`
turns the checkable half of `reference/principles.md` into assertions, so
"skimmable formatting" and "grounded claims" stop being aspirations and become
pass/fail — and a failure names the fix rather than the symptom.

**Long essays are written in sections, not in one shot.** A 1,250-word essay is
~1,700 tokens; on qwen2.5:3b that measured ~73 seconds, and quality falls apart
well before the end — the model loses the thread, repeats itself, and drifts off
its citations. Writing an outline first and then each section against it keeps
every generation inside the range where a 3B is reliable, streams visible
progress, and never approaches the request timeout. It also happens to be what
the source material prescribes for long pieces: stacked 1/3/1 sequences rather
than one essay five times the size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.agent.citations import extract_markers
from app.logging import get_logger
from app.providers.base import LLMProvider, Message

log = get_logger("agent.ship30")

# ── Targets, from reference/principles.md ───────────────────────────────

TARGET_WORDS = 1250
MIN_WORDS = 1000
MAX_WORDS = 1500
MIN_SECTIONS = 3
MIN_CITATIONS = 4
MAX_SECTION_WORDS = 420
# Bold is a signal. Past roughly a tenth of the text it stops being one.
MAX_BOLD_RATIO = 0.10

_HEADING_RE = re.compile(r"^##\s+(.+)$", re.M)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+\S|^\s*\d+\.\s+\S", re.M)
_H1_RE = re.compile(r"^#\s+(.+)$", re.M)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    # Fed to the repair pass. A check that cannot say how to fix itself is a
    # complaint, not a rubric.
    fix: str = ""


@dataclass
class Rubric:
    checks: List[Check] = field(default_factory=list)
    word_count: int = 0
    section_count: int = 0
    citation_count: int = 0

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "word_count": self.word_count,
            "section_count": self.section_count,
            "citation_count": self.citation_count,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


def word_count(text: str) -> int:
    """Words of prose, excluding markdown syntax."""
    stripped = re.sub(r"[#*_`>\-]", " ", text)
    stripped = re.sub(r"\[S\d+(?:\s*,\s*S\d+)*\]", " ", stripped)
    return len([w for w in stripped.split() if any(ch.isalnum() for ch in w)])


def split_sections(essay: str) -> List[tuple]:
    """`(heading, body)` for each `##` section."""
    matches = list(_HEADING_RE.finditer(essay))
    out = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(essay)
        out.append((match.group(1).strip(), essay[match.end() : end].strip()))
    return out


def evaluate(essay: str, *, available_markers: Optional[List[str]] = None) -> Rubric:
    """Score an essay against the rubric."""
    words = word_count(essay)
    sections = split_sections(essay)
    markers = extract_markers(essay)
    if available_markers is not None:
        valid = {m.upper() for m in available_markers}
        markers = [m for m in markers if m in valid]

    rubric = Rubric(word_count=words, section_count=len(sections), citation_count=len(markers))

    rubric.checks.append(
        Check(
            "length",
            MIN_WORDS <= words <= MAX_WORDS,
            f"{words} words (target {TARGET_WORDS}, allowed {MIN_WORDS}-{MAX_WORDS})",
            fix=(
                f"Expand to about {TARGET_WORDS} words with more concrete detail from the sources"
                if words < MIN_WORDS
                else f"Cut to about {TARGET_WORDS} words; remove restatement and filler"
            ),
        )
    )

    rubric.checks.append(
        Check(
            "headline",
            bool(_H1_RE.search(essay)),
            "has an H1 headline" if _H1_RE.search(essay) else "no `# ` headline",
            fix="Add a single `# ` headline that answers WHO, WHAT and WHY. Clear beats clever.",
        )
    )

    rubric.checks.append(
        Check(
            "sections",
            len(sections) >= MIN_SECTIONS,
            f"{len(sections)} `##` sections (need {MIN_SECTIONS})",
            fix=f"Break the body into at least {MIN_SECTIONS} `## ` sections, one idea each.",
        )
    )

    has_bullets = bool(_BULLET_RE.search(essay))
    rubric.checks.append(
        Check(
            "bullets",
            has_bullets,
            "has a list" if has_bullets else "no bulleted or numbered list",
            fix="Any paragraph carrying three or more parallel points becomes a bulleted list.",
        )
    )

    bold_words = sum(word_count(m) for m in _BOLD_RE.findall(essay))
    ratio = bold_words / max(1, words)
    rubric.checks.append(
        Check(
            "emphasis",
            0 < ratio <= MAX_BOLD_RATIO,
            f"{ratio:.0%} of words bolded"
            + ("" if ratio > 0 else " — none")
            + (f" (max {MAX_BOLD_RATIO:.0%})" if ratio > MAX_BOLD_RATIO else ""),
            fix=(
                "Bold exactly one key sentence per section — the section's argument."
                if ratio == 0
                else "Reduce bold to one key sentence per section; used everywhere it signals nothing."
            ),
        )
    )

    rubric.checks.append(
        Check(
            "grounding",
            len(markers) >= MIN_CITATIONS,
            f"{len(markers)} resolved citations (need {MIN_CITATIONS})",
            fix=f"Attach `[S#]` markers to factual claims; at least {MIN_CITATIONS} must resolve.",
        )
    )

    long_sections = [h for h, b in sections if word_count(b) > MAX_SECTION_WORDS]
    rubric.checks.append(
        Check(
            "section_balance",
            not long_sections,
            "sections balanced" if not long_sections else f"over-long: {', '.join(long_sections)}",
            fix=f"Split or trim any section past {MAX_SECTION_WORDS} words.",
        )
    )

    log.info("ship30_evaluated", **rubric.to_dict())
    return rubric


# ── Outline-driven writing ──────────────────────────────────────────────

OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "hook": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "argument": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "argument", "sources"],
            },
        },
    },
    "required": ["headline", "hook", "sections"],
}

_OUTLINE_SYSTEM = """Plan a ~1,250-word essay in the Ship 30 for 30 style, grounded
strictly in the transcript excerpts provided.

- headline: answers WHO, WHAT and WHY. Clear beats clever. Prefer naming a
  credible guest, or leading with a concrete outcome.
- hook: one sentence stating the essay's central claim. Open at the END of the
  story — the outcome or the surprising result. No throat-clearing.
- sections: exactly 3. Each has a heading, a one-sentence argument, and the
  source ids (like "S1") that support it. One idea per section.

Use only the source ids that appear in the excerpts. Respond with JSON only."""


@dataclass
class Outline:
    headline: str
    hook: str
    sections: List[Dict[str, Any]]


async def plan_outline(
    provider: LLMProvider, topic: str, context: str
) -> Optional[Outline]:
    """Plan the essay before writing any of it."""
    try:
        parsed = await provider.complete_json(
            [Message(role="user", content=f"Topic: {topic}\n\nExcerpts:\n{context}")],
            schema=OUTLINE_SCHEMA,
            system=_OUTLINE_SYSTEM,
            temperature=0.4,
        )
    except Exception as exc:  # noqa: BLE001
        # Fall back to one-shot generation rather than losing the turn. Slower
        # and weaker, but it still produces an essay.
        log.warning("ship30_outline_failed", error=str(exc))
        return None

    sections = [s for s in parsed.get("sections", []) if s.get("heading")][:3]
    if not sections:
        return None

    outline = Outline(
        headline=str(parsed.get("headline") or topic).strip(),
        hook=str(parsed.get("hook") or "").strip(),
        sections=sections,
    )
    log.info("ship30_outline", headline=outline.headline, sections=len(outline.sections))
    return outline


# ~300 words x 3 sections, plus a hook and a takeaway, lands near the 1,250
# target. An earlier 230 finished at 839 — short of the rubric's floor, which
# then burned a repair pass on padding rather than on substance.
SECTION_WORDS = 300
TAKEAWAY_WORDS = 160


def section_prompt(outline: Outline, index: int, context: str) -> str:
    section = outline.sections[index]
    others = [s["heading"] for i, s in enumerate(outline.sections) if i != index]
    sources = ", ".join(section.get("sources") or []) or "any relevant source"
    return (
        f"Essay headline: {outline.headline}\n"
        f"Central claim: {outline.hook}\n\n"
        f"Write ONLY this section, {SECTION_WORDS} words:\n"
        f"## {section['heading']}\n"
        f"Argument to make: {section['argument']}\n"
        f"Supporting sources: {sources}\n\n"
        f"Other sections (do not write these, do not repeat their content): "
        f"{'; '.join(others) or 'none'}\n\n"
        # Citation instruction first and by example. Buried at the end of a rule
        # list, a 3B ignores it — the first run produced zero markers across the
        # whole essay.
        f"CITATIONS ARE MANDATORY. End every factual sentence with a source "
        f"marker in square brackets, exactly like this:\n"
        f'  Superhuman priced at $30 a month to justify four hours saved [{(section.get("sources") or ["S1"])[0]}].\n'
        f"Use only these ids: {sources}. Never invent one.\n\n"
        f"Also: 1/3/1 rhythm — one-sentence opener, three-sentence development, "
        f"one-sentence close. Name the guest. Bold exactly one key sentence with "
        f"**double asterisks**. Write {SECTION_WORDS} words — do not stop short.\n"
        f"Start with the `## ` heading and write nothing else."
    )


def takeaway_prompt(outline: Outline) -> str:
    """The closing TL;DR the guide prescribes.

    Generated separately rather than folded into the last section: the source
    material treats the close as its own move, and a 3B asked to write a section
    *and* wrap up the essay does neither well.
    """
    headings = "; ".join(s["heading"] for s in outline.sections)
    return (
        f"Essay headline: {outline.headline}\n"
        f"Central claim: {outline.hook}\n"
        f"Sections covered: {headings}\n\n"
        f"Write the closing section, {TAKEAWAY_WORDS} words, exactly as:\n"
        f"## The takeaway\n"
        f"- three or four bullets, each one compressed lesson with its [S#] marker\n"
        f"- then one final sentence naming what the reader should do differently "
        f"on Monday. Specific enough to act on without rereading.\n\n"
        f"Reuse only source markers that already appear in the essay. "
        f"Start with the `## ` heading and write nothing else."
    )


def repair_prompt(essay: str, rubric: Rubric, context: str) -> str:
    """One targeted revision naming the failures and their fixes."""
    problems = "\n".join(f"- {c.name}: {c.detail}. FIX: {c.fix}" for c in rubric.failures)
    return (
        "Revise the essay below so it passes every check. Change only what the "
        "problems require — keep the voice, the structure, and every existing "
        "citation.\n\n"
        f"Problems:\n{problems}\n\n"
        f"Excerpts (for citations):\n{context}\n\n"
        f"Essay:\n{essay}\n\n"
        "Return the complete revised essay in markdown, and nothing else."
    )
