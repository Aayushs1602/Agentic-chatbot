"""Intent routing and the relevance gate.

Both are structured-output calls rather than tool calls. On qwen2.5:3b, JSON
schema-constrained decoding classified intent 4/4 on the golden cases, while
function calling at that size is not dependable — so the local path never asks
the model to *choose* a tool, only to *answer a question* whose answer the
orchestrator acts on. Same code runs on the cloud providers.

Routing happens **before retrieval, on user text only**. That ordering is a
security property, not an accident: transcript content can never influence which
tool or skill runs, so a guest saying "ignore your instructions" on-air cannot
redirect the agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from app.logging import get_logger
from app.providers.base import LLMProvider, Message

log = get_logger("agent.router")


class Intent(str, Enum):
    KNOWLEDGE = "knowledge_question"   # answer from the transcripts
    ESSAY = "write_essay"              # Ship 30 for 30 essay
    ARTIFACT = "create_artifact"       # markdown or HTML document
    CHITCHAT = "chitchat"              # greetings, thanks, meta-questions

    @property
    def needs_retrieval(self) -> bool:
        return self is not Intent.CHITCHAT


_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [i.value for i in Intent],
        },
        "search_query": {
            "type": "string",
            "description": "A self-contained search query resolving pronouns from context.",
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["intent", "search_query", "confidence"],
}

_ROUTER_SYSTEM = """You classify a user's message in a product-and-growth research assistant.

Intents:
- knowledge_question: asks something answerable from podcast transcripts about
  product, growth, careers, hiring, pricing, metrics, or strategy.
- write_essay: asks for an essay, blog post, article, or written piece — often
  referring to the previous answer ("turn that into an essay").
- create_artifact: asks for a document, table, checklist, template, dashboard,
  one-pager, or HTML/markdown output to look at rather than read as chat.
- chitchat: greetings, thanks, and questions about you and your capabilities.
  ONLY these. A request for information of any kind is never chitchat — even
  when the archive plainly cannot answer it. "What is the weather tomorrow",
  "what is my account balance", and "write me some code" are all
  knowledge_question, and the retrieval step will correctly find nothing. This
  matters because chitchat skips retrieval and grounding entirely, so misrouting
  an information request there is the one way an ungrounded claim reaches the
  user.

Also produce search_query: a standalone search query for the transcript corpus.
Resolve pronouns using the conversation so far — if the user says "expand on
that", search_query must name what "that" was. For chitchat, use an empty string.

Respond with JSON only."""


@dataclass
class Route:
    intent: Intent
    search_query: str
    confidence: float
    fallback_used: bool = False


# Cheap pre-classifier. A short greeting shouldn't cost a model call plus a
# retrieval round-trip — that is most of the latency budget for zero benefit.
_CHITCHAT_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|thanks|thank you|ty|ok|okay|cool|got it|nice|"
    r"good morning|good afternoon|good evening|bye|goodbye)[\s!.?]*$",
    re.I,
)
_ESSAY_RE = re.compile(r"\b(ship\s*30|atomic essay|write .{0,20}essay|blog post)\b", re.I)


async def route(
    provider: LLMProvider,
    message: str,
    *,
    history: Optional[List[Message]] = None,
) -> Route:
    """Classify `message`. Never raises — a routing failure must not lose the turn."""
    text = message.strip()

    if _CHITCHAT_RE.match(text):
        return Route(Intent.CHITCHAT, "", 1.0)

    # Recent turns only: the router needs pronoun antecedents, not the whole
    # transcript, and a long history on a 3B degrades classification.
    convo: List[Message] = []
    for m in (history or [])[-4:]:
        convo.append(Message(role=m.role, content=m.content[:600]))
    convo.append(Message(role="user", content=text))

    try:
        parsed = await provider.complete_json(
            convo, schema=_INTENT_SCHEMA, system=_ROUTER_SYSTEM, temperature=0.0
        )
        intent = Intent(parsed["intent"])
        query = (parsed.get("search_query") or "").strip() or text
        confidence = float(parsed.get("confidence", 0.5))
        log.info("routed", intent=intent.value, confidence=round(confidence, 2))
        return Route(intent, query, confidence)

    except Exception as exc:  # noqa: BLE001
        # Degrade to a heuristic rather than failing the turn. Defaulting to
        # KNOWLEDGE is the safe direction: it retrieves and grounds, where
        # guessing ESSAY would produce 1,250 confident ungrounded words.
        intent = Intent.ESSAY if _ESSAY_RE.search(text) else Intent.KNOWLEDGE
        log.warning("router_fallback", error=str(exc), intent=intent.value)
        return Route(intent, text, 0.0, fallback_used=True)


# ── Relevance gate ──────────────────────────────────────────────────────

# One judgement, not two. An earlier version asked for `answerable` *and*
# `relevant_sources`, which a small model routinely answered inconsistently
# (answerable=false alongside a populated source list). Asking only which
# sources are useful, and deriving answerability from that, removes the
# contradiction entirely.
_RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "useful_sources": {"type": "array", "items": {"type": "string"}},
        "missing": {"type": "string"},
    },
    "required": ["useful_sources"],
}

# Framing matters more than strictness here, and this was measured.
#
# Asking "do these excerpts ANSWER the question" refused 5 of 6 questions whose
# top retrieval hit was the exactly-matching episode — because podcast
# transcripts almost never contain a passage that neatly answers anything. They
# contain relevant discussion. The model was applying the test it was given,
# correctly, and the test was wrong.
#
# Asking "would someone find this USEFUL, and remember these are conversational"
# scores 5/6 on covered questions while still refusing 5/5 out-of-corpus ones.
# Precision is preserved by naming the out-of-domain categories explicitly
# rather than by demanding a higher bar of relevance.
_RELEVANCE_SYSTEM = """You are filtering search results from a podcast transcript archive.

For each excerpt, decide whether it contains material a person asking this
question would find USEFUL. These are conversational transcripts: a useful
excerpt discusses the topic, gives an example, or shares experience. It will
almost never restate the question or give a tidy, complete answer — do not
require that.

useful_sources: the ids (like "S1") of every excerpt on the question's topic.

Leave useful_sources EMPTY only when the excerpts are about a genuinely
different subject. A question about weather, medicine, code, personal account
data, or academic physics has no useful excerpt in a product-and-growth archive.

missing: when useful_sources is empty, one short clause naming the subject the
excerpts are actually about.

Respond with JSON only."""


_SOURCE_RE = re.compile(r'<source id="(S\d+)">\s*(.*?)\s*</source>', re.S)
_DIGEST_CHARS = 420


def _digest(context: str) -> str:
    """Shrink the formatted context to the opening of each source block.

    Prompt evaluation dominates latency on a 3B: judging relevance over the full
    five chunks measured ~30s of a 34s turn. Topical relevance is decidable from
    the opening of each passage, so this is the largest single latency win in
    the turn — and it does not touch the context used for the actual answer.
    """
    parts = []
    for marker, body in _SOURCE_RE.findall(context):
        snippet = " ".join(body.split())[:_DIGEST_CHARS]
        parts.append(f"[{marker}] {snippet}")
    return "\n\n".join(parts) if parts else context[:2000]


@dataclass
class Relevance:
    answerable: bool
    relevant_sources: List[str]
    missing: str = ""
    checked: bool = True


async def check_relevance(
    provider: LLMProvider, question: str, context: str
) -> Relevance:
    """The real abstain gate.

    Embedding similarity cannot do this job — measured on the golden set,
    in-corpus and out-of-corpus questions overlap across a third of the cosine
    range, and a threshold rejecting every out-of-corpus question would also
    refuse 11 of 15 real ones (docs/retrieval-calibration.md). A bi-encoder
    answers "is this text similar"; only the model answers "does this text
    answer the question".
    """
    if not context.strip():
        return Relevance(False, [], "no transcript excerpts were retrieved")

    digest = _digest(context)

    try:
        parsed = await provider.complete_json(
            [Message(role="user", content=f"Question: {question}\n\nExcerpts:\n{digest}")],
            schema=_RELEVANCE_SCHEMA,
            system=_RELEVANCE_SYSTEM,
            temperature=0.0,
        )
        useful = [str(s) for s in parsed.get("useful_sources", []) if str(s).strip()]
        relevance = Relevance(
            # Derived, never separately asserted — see the schema comment.
            answerable=bool(useful),
            relevant_sources=useful,
            missing=str(parsed.get("missing", "")),
        )
        log.info(
            "relevance_checked",
            answerable=relevance.answerable,
            sources=relevance.relevant_sources,
        )
        return relevance

    except Exception as exc:  # noqa: BLE001
        # Fail *open*: proceed to generation, where post-generation citation
        # resolution is the second line of defence. Failing closed would turn
        # every provider hiccup into a refusal, which is worse for a corpus the
        # user knows covers their question.
        log.warning("relevance_check_failed", error=str(exc))
        return Relevance(True, [], checked=False)
