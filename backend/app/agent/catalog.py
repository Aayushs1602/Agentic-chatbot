"""Questions about the corpus itself, answered from SQL.

"What is the longest episode?", "How many episodes are there?", "Which episodes
feature Shreyas Doshi?" are all answerable — the `episodes` table has titles,
guests, durations and dates — but none of them are answerable by *semantic
search over transcript chunks*, which is what every other question does. So they
were all refused, on data the system holds.

The fix is not more retrieval. These are aggregate questions about metadata, and
the honest way to answer them is a query, not an embedding. The model's only job
here is to say which question is being asked and extract a parameter; the answer
itself comes from Postgres and is therefore exact by construction — no
hallucination surface, and no citation needed because the corpus *is* the source.

Deliberately narrow. It handles a fixed set of shapes rather than generating SQL,
because a model writing SQL against a live database is a much larger risk than
the feature is worth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.db import pool as db
from app.logging import get_logger
from app.providers.base import LLMProvider, Message

log = get_logger("agent.catalog")

CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "query_type": {
            "type": "string",
            "enum": [
                "longest", "shortest", "newest", "oldest",
                "count", "by_guest", "by_topic", "list_recent", "unsupported",
            ],
        },
        "subject": {
            "type": "string",
            "description": "Guest name or topic, when the question names one. Otherwise empty.",
        },
    },
    "required": ["query_type", "subject"],
}

CATALOG_SYSTEM = """Classify a question about a podcast archive's catalogue.

- longest / shortest: about episode duration
- newest / oldest: about publication date
- count: how many episodes exist
- by_guest: names a specific person ("episodes with Shreyas Doshi")
- by_topic: asks which episodes cover a subject
- list_recent: asks what is in the archive generally
- unsupported: anything the catalogue cannot answer

subject: the guest name or topic if one is named, otherwise an empty string.

Respond with JSON only."""

# Below this, an entry is a clip rather than a full episode. The corpus mixes
# both, and five entries sit between 1 and 3 minutes.
_CLIP_SECONDS = 300

# Detection is operation-first, not noun-first.
#
# The original required an exact noun ("episode|podcast|show|interview") next to
# the superlative, and missed every real phrasing a person actually used:
# "give me the lonngest lenny ep", "what is the longest video", "the longest
# one". Each fell through to semantic search, and one of them produced the worst
# output this system has generated — a fabricated episode duration, read off the
# `[00:50:53]` timestamp markers inside a chunk and presented as fact, with a
# real citation attached.
#
# So: find the *operation* (with typo tolerance), then accept a much wider set
# of nouns, or a short question where no other subject is present.
_OPERATIONS = {
    "longest", "shortest", "briefest", "biggest", "smallest",
    "newest", "latest", "oldest", "earliest", "recent",
    "first", "last", "many", "count", "total", "number",
}

_ARCHIVE_NOUNS = {
    "episode", "episodes", "ep", "eps", "epsiode",
    "video", "videos", "vid", "podcast", "podcasts", "pod",
    "show", "shows", "interview", "interviews", "conversation",
    "transcript", "transcripts", "lenny", "lennys", "one", "ones", "guest", "guests",
}

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_EPISODE_NUMBER_RE = re.compile(r"\bep(?:isode)?\s*(?:number\s*|#\s*|no\.?\s*)?\d+\b", re.I)
# Short questions rarely carry a topic as well as a superlative, so a
# superlative in a short question is almost always about the archive.
_SHORT_QUESTION_WORDS = 7


def _fuzzy_in(word: str, vocabulary: set) -> bool:
    """Exact match, or a near-miss — people typo "lonngest"."""
    if word in vocabulary:
        return True
    if len(word) < 5:
        return False
    import difflib

    return bool(difflib.get_close_matches(word, vocabulary, n=1, cutoff=0.82))


# Asking what someone *said* is always a content question, however many archive
# nouns it contains. Without this veto, "What did guests say about hiring a
# first PM?" matched on "first" + "guests" and would have been sent to SQL,
# which cannot answer it — a worse failure than the gap being closed.
_CONTENT_VERBS = {
    "say", "says", "said", "talk", "talks", "talked", "discuss", "discusses",
    "discussed", "mention", "mentions", "mentioned", "explain", "explains",
    "explained", "recommend", "recommends", "recommended", "think", "thinks",
    "advice", "suggest", "suggests", "suggested", "argue", "argues",
}

# Phrasings that ask what the archive *contains* rather than what was said in
# it. These carry no superlative, so the operation check alone misses them.
_CATALOG_PHRASE_RE = re.compile(
    r"\b(?:which|what|list|show me)\s+(?:\w+\s+){0,2}"
    r"(?:episode|episodes|eps?|video|videos|podcasts?)\b"
    r"|\bepisodes?\b[^.?!]{0,25}\b(?:do you have|are there|available|exist)\b"
    r"|\bhow many\b",
    re.I,
)


def looks_like_catalog_question(message: str) -> bool:
    # "episode 128" / "ep 12" is a catalogue question regardless of shape.
    if _EPISODE_NUMBER_RE.search(message):
        return True

    words = _TOKEN_RE.findall(message.lower())
    if not words:
        return False

    # The veto comes first: a question about what someone said is never
    # answerable from metadata, whatever else it contains.
    if any(w in _CONTENT_VERBS for w in words):
        return False

    if _CATALOG_PHRASE_RE.search(message):
        return True

    has_operation = any(_fuzzy_in(w, _OPERATIONS) for w in words)
    if not has_operation:
        return False

    # A superlative needs either an archive noun, or a question short enough
    # that the superlative is plainly the subject.
    has_noun = any(w in _ARCHIVE_NOUNS for w in words)
    return has_noun or len(words) <= _SHORT_QUESTION_WORDS


@dataclass
class CatalogAnswer:
    text: str
    handled: bool = True


def _hms(seconds: Optional[int]) -> str:
    if not seconds:
        return "unknown length"
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _line(row: Dict[str, Any]) -> str:
    guests = ", ".join(row.get("guests") or [])
    bits = [f"**{row['title']}**"]
    if guests:
        bits.append(f"with {guests}")
    if row.get("duration_s"):
        bits.append(f"({_hms(row['duration_s'])})")
    if row.get("published_on"):
        bits.append(f"— {row['published_on']}")
    if row.get("youtube_url"):
        bits.append(f"\n  [Listen]({row['youtube_url']})")
    return " ".join(bits)


# Deterministic classification, in priority order.
#
# The model was asked to do this and could not: it labelled "which is the
# SHORTEST episode" as `longest`, and gave up entirely on "give the longest
# lenny episode" because the word "lenny" was in it. Getting a superlative
# backwards produces a confidently wrong factual answer, which is the failure
# this whole product is built to avoid.
#
# These are keywords. Keywords do not need a language model, and the same
# reasoning that keeps tool selection away from a 3B applies here.
_KIND_PATTERNS = [
    ("count", re.compile(r"\bhow many\b|\bnumber of (?:episodes|guests)\b|\btotal\b", re.I)),
    ("shortest", re.compile(r"\bshortest\b|\bbriefest\b", re.I)),
    ("longest", re.compile(r"\blongest\b", re.I)),
    ("newest", re.compile(r"\b(?:newest|latest|most recent|recent)\b", re.I)),
    ("oldest", re.compile(r"\b(?:oldest|earliest|first)\b", re.I)),
    (
        "by_guest",
        re.compile(r"\b(?:with|featur(?:e|es|ed|ing)|interview(?:ed|s)?|guests?)\b", re.I),
    ),
]


# Typo-tolerant word lists for the same operations, so "lonngest" classifies
# as well as it detects. Order matters: shortest before longest, so a question
# containing both resolves to the more specific ask.
_KIND_WORDS = [
    ("count", {"many", "count", "total", "number"}),
    ("shortest", {"shortest", "briefest", "smallest"}),
    ("longest", {"longest", "biggest"}),
    ("newest", {"newest", "latest", "recent"}),
    ("oldest", {"oldest", "earliest", "first"}),
]


def classify(question: str) -> str:
    words = _TOKEN_RE.findall(question.lower())
    for kind, vocabulary in _KIND_WORDS:
        if any(_fuzzy_in(w, vocabulary) for w in words):
            return kind
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(question):
            return kind
    return "unsupported"


async def answer(provider: LLMProvider, question: str) -> CatalogAnswer:
    """Answer a catalogue question, or hand back unhandled."""
    kind = classify(question)
    subject = ""

    # The model is used only for what keywords cannot do: pulling a name or a
    # topic out of the sentence. A wrong answer there degrades to "no match",
    # which is recoverable; a wrong superlative is not.
    if kind in {"by_guest", "unsupported"}:
        try:
            parsed = await provider.complete_json(
                [Message(role="user", content=question)],
                schema=CATALOG_SCHEMA,
                system=CATALOG_SYSTEM,
                temperature=0.0,
            )
            subject = (parsed.get("subject") or "").strip()
            if kind == "unsupported":
                model_kind = parsed.get("query_type", "unsupported")
                # Only trust the model for the lookup kinds, never the
                # superlatives it gets backwards.
                if model_kind in {"by_guest", "by_topic", "count", "list_recent"}:
                    kind = model_kind
        except Exception as exc:  # noqa: BLE001
            log.warning("catalog_subject_extraction_failed", error=str(exc))

    log.info("catalog_query", kind=kind, subject=subject[:40])

    # Episode numbers do not exist in this corpus, and saying so is a better
    # answer than a generic refusal — the user is asking for something coherent
    # that simply is not in the data.
    if re.search(r"\bepisode\s+(?:number\s+)?\d+\b", question, re.I):
        total = await db.fetchval("SELECT count(*) FROM episodes")
        return CatalogAnswer(
            "The transcripts aren't numbered — this corpus identifies episodes by "
            f"guest and title rather than by episode number, so there's no way for "
            f"me to look up a specific number.\n\nI have **{total} episodes**. If you "
            "name the guest or the topic, I can find it."
        )

    if kind == "count":
        row = await db.fetchrow(
            "SELECT count(*) AS episodes, count(DISTINCT g) AS guests, "
            "(SELECT count(*) FROM chunks) AS chunks "
            "FROM episodes, unnest(guests) AS g"
        )
        return CatalogAnswer(
            f"The archive holds **{row['episodes']} episodes** featuring "
            f"{row['guests']} guests, indexed as {row['chunks']:,} searchable passages."
        )

    if kind in {"longest", "shortest"}:
        order = "DESC" if kind == "longest" else "ASC"
        # `> 0`, not `IS NOT NULL`: two episodes carry a duration of 0, and
        # returning those as "the shortest episode" presents missing metadata
        # as a fact — the same class of error the whole product avoids.
        rows = await db.fetch(
            f"SELECT title, guests, duration_s, published_on, youtube_url FROM episodes "
            f"WHERE duration_s > 0 ORDER BY duration_s {order} LIMIT 3"
        )
        if not rows:
            return CatalogAnswer("", handled=False)
        head = "longest" if kind == "longest" else "shortest"
        body = "\n\n".join(f"{i}. {_line(dict(r))}" for i, r in enumerate(rows, 1))
        note = ""
        if kind == "shortest" and rows[0]["duration_s"] < _CLIP_SECONDS:
            # The archive mixes short clips in with full episodes. Saying so
            # beats returning a two-minute clip as though it were an episode.
            note = (
                "\n\n_The archive includes some short clips alongside full "
                "episodes, which is what the top of this list is._"
            )
        return CatalogAnswer(f"The {head} episodes in the archive:\n\n{body}{note}")

    if kind in {"newest", "oldest", "list_recent"}:
        order = "ASC" if kind == "oldest" else "DESC"
        rows = await db.fetch(
            f"SELECT title, guests, duration_s, published_on, youtube_url FROM episodes "
            f"WHERE published_on IS NOT NULL ORDER BY published_on {order} LIMIT 5"
        )
        if not rows:
            # Honest about a data gap rather than pretending the question is bad.
            return CatalogAnswer(
                "I can't answer that one: publication dates aren't populated in my "
                "index, so I have no way to order episodes by date. I can tell you "
                "the longest episodes, the total count, or find episodes by guest."
            )
        label = {"newest": "most recent", "oldest": "earliest", "list_recent": "most recent"}[kind]
        body = "\n\n".join(f"{i}. {_line(dict(r))}" for i, r in enumerate(rows, 1))
        return CatalogAnswer(f"The {label} episodes:\n\n{body}")

    if kind == "by_guest" and subject:
        rows = await db.fetch(
            "SELECT title, guests, duration_s, published_on, youtube_url FROM episodes "
            "WHERE EXISTS (SELECT 1 FROM unnest(guests) g WHERE g ILIKE $1) "
            "OR title ILIKE $1 LIMIT 5",
            f"%{subject}%",
        )
        if not rows:
            return CatalogAnswer(
                f"I don't have an episode with **{subject}** in the archive. "
                "Names are matched as they appear in the episode metadata, so a "
                "different spelling may help."
            )
        body = "\n\n".join(f"{i}. {_line(dict(r))}" for i, r in enumerate(rows, 1))
        return CatalogAnswer(f"Episodes featuring **{subject}**:\n\n{body}")

    if kind == "by_topic" and subject:
        rows = await db.fetch(
            "SELECT title, guests, duration_s, published_on, youtube_url FROM episodes "
            "WHERE title ILIKE $1 OR description ILIKE $1 LIMIT 5",
            f"%{subject}%",
        )
        if rows:
            body = "\n\n".join(f"{i}. {_line(dict(r))}" for i, r in enumerate(rows, 1))
            return CatalogAnswer(f"Episodes about **{subject}**:\n\n{body}")
        # A topic with no title match is a normal retrieval question, not a
        # catalogue one — hand it back rather than refusing.
        return CatalogAnswer("", handled=False)

    return CatalogAnswer("", handled=False)
