#!/usr/bin/env python3
"""Human-readable view of the backend's JSON logs.

    docker compose logs -f backend | python scripts/pretty_logs.py

    # everything, not just the pipeline
    docker compose logs -f backend | python scripts/pretty_logs.py --all

    # one request, after the fact
    docker compose logs backend | python scripts/pretty_logs.py --request 01J4F

Structured JSON is the right format for machines and unreadable while you are
watching a request go through. This renders each turn as a tree, so the agent's
work is legible in real time:

    14:32:01  POST /api/sessions/8f2a…/messages
    14:32:03   ├─ classify_intent     knowledge_question (0.95)
    14:32:04   ├─ search_transcripts  8 passages · 5 episodes · cos 0.71
    14:32:09   ├─ check_relevance     answerable · S1 S2 S4
    14:32:10   ├─ apply_skill         grounded-answer
    14:32:38   ├─ generate            131 tokens · 28.1s
    14:32:38   └─ citations           S1 S2 · grounded
    14:32:38  200 · 37.2s

Reads stdin, so it works with `docker compose logs`, a file, or a pipe. Pure
stdlib — no install, and it runs on the host rather than in the container.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

# ANSI, disabled when stdout is not a terminal so piping to a file stays clean.
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


DIM = lambda s: _c("2", s)        # noqa: E731
BOLD = lambda s: _c("1", s)       # noqa: E731
RED = lambda s: _c("31", s)       # noqa: E731
GREEN = lambda s: _c("32", s)     # noqa: E731
YELLOW = lambda s: _c("33", s)    # noqa: E731
BLUE = lambda s: _c("34", s)      # noqa: E731
CYAN = lambda s: _c("36", s)      # noqa: E731

# Events that make up the visible life of a request, in the order they occur.
PIPELINE = {
    "routed",
    "injection_attempt_routed_to_knowledge",
    "chitchat_reclassified_as_knowledge",
    "catalog_query",
    "retrieval_ok",
    "retrieval_abstain",
    "retrieval_empty",
    "relevance_checked",
    "skills_loaded",
    "ollama_complete",
    "cloud_complete",
    "anthropic_complete",
    "ollama_json",
    "citations_resolved",
    "citations_repaired",
    "ungrounded_answer_replaced",
    "abstained",
    "artifacts_extracted",
    "artifact_sanitized",
    "ship30_outline",
    "ship30_evaluated",
    "provider_fallback",
    "followup_rewrite_unresolved",
    "followup_rewrite_invented_terms",
    "client_disconnected",
}

PROBLEMS = {
    "provider_fallback",
    "ungrounded_answer_replaced",
    "abstained",
    "retrieval_abstain",
    "injection_attempt_routed_to_knowledge",
    "app_error",
    "unhandled_error",
    "db_ping_failed",
    "ollama_timeout",
    "ollama_unreachable",
}


def _time(row: Dict[str, Any]) -> str:
    stamp = str(row.get("timestamp", ""))
    return stamp[11:19] if len(stamp) > 19 else stamp[:8].ljust(8)


def _describe(row: Dict[str, Any]) -> Optional[str]:
    """One line for one event, or None to skip it."""
    event = row.get("event", "")
    g = row.get  # noqa: E741

    if event == "request":
        code = int(g("status", 0))
        colour = GREEN if code < 400 else (YELLOW if code < 500 else RED)
        return f"{colour(str(code))} · {g('duration_ms', 0)/1000:.1f}s  {DIM(g('path', ''))}"

    if event == "routed":
        return f"{'classify_intent':22s}{g('intent', '?')} ({g('confidence', '?')})"
    if event == "injection_attempt_routed_to_knowledge":
        return RED(f"{'injection blocked':22s}forced to knowledge · retrieval will find nothing")
    if event == "chitchat_reclassified_as_knowledge":
        return YELLOW(f"{'reclassified':22s}chitchat -> knowledge ({g('words')} words)")
    if event == "catalog_query":
        return f"{'query_catalog':22s}{g('kind', '?')}" + (f" · {g('subject')}" if g("subject") else "")
    if event == "retrieval_ok":
        counts = DIM(f"(dense {g('dense', 0)} / sparse {g('sparse', 0)})")
        return (
            f"{'search_transcripts':22s}{g('selected', '?')} passages · "
            f"{g('episodes', '?')} episodes · cos {g('best_cosine', '?')} {counts}"
        )
    if event == "retrieval_abstain":
        return YELLOW(f"{'search_transcripts':22s}below floor · cos {g('best_cosine', '?')}")
    if event == "relevance_checked":
        ok = g("answerable")
        sources = " ".join(g("sources") or []) or "none"
        line = f"{'check_relevance':22s}{'answerable' if ok else 'not answerable'} · {sources}"
        return line if ok else YELLOW(line)
    if event in {"ollama_complete", "cloud_complete", "anthropic_complete"}:
        return (
            f"{'generate':22s}{g('tokens_out', '?')} tokens · "
            f"{int(g('duration_ms', 0))/1000:.1f}s {DIM(str(g('model', '')))}"
        )
    if event == "ollama_json":
        return DIM(f"{'  structured call':22s}{int(g('duration_ms', 0))/1000:.1f}s")
    if event == "citations_resolved":
        resolved = " ".join(g("resolved") or []) or "none"
        invented = g("invented") or []
        line = f"{'citations':22s}{resolved}"
        if invented:
            line += YELLOW(f" · dropped invented {' '.join(invented)}")
        return line if g("grounded") else YELLOW(line + " · NOT grounded")
    if event == "citations_repaired":
        return GREEN(f"{'citations repaired':22s}{' '.join(g('resolved') or [])}")
    if event == "ungrounded_answer_replaced":
        return RED(f"{'answer replaced':22s}nothing resolvable was cited")
    if event == "abstained":
        return YELLOW(f"{'abstained':22s}{g('reason', '')}")
    if event == "artifacts_extracted":
        return f"{'create_artifact':22s}{g('count')} · {', '.join(g('kinds') or [])}"
    if event == "artifact_sanitized":
        return YELLOW(f"{'sanitized':22s}removed {g('removed')} · {g('tags')}")
    if event == "ship30_outline":
        return f"{'plan_outline':22s}{g('sections')} sections · {str(g('headline', ''))[:44]}"
    if event == "ship30_evaluated":
        passed = g("passed")
        failed = [c["name"] for c in (g("checks") or []) if not c.get("passed")]
        line = f"{'check_rubric':22s}{g('word_count')} words · {g('citation_count')} citations"
        if not passed:
            line += YELLOW(f" · failed {', '.join(failed)}")
        return line
    if event == "provider_fallback":
        return YELLOW(f"{'provider fallback':22s}{g('requested')} -> {g('using')} · {g('reason', '')}")
    if event in {"followup_rewrite_unresolved", "followup_rewrite_invented_terms"}:
        return YELLOW(f"{'followup repaired':22s}{event.replace('followup_rewrite_', '')}")
    if event == "client_disconnected":
        return DIM(f"{'client disconnected':22s}generation aborted")
    if event in {"app_error", "unhandled_error"}:
        return RED(f"{'ERROR':22s}{g('code', '')} {str(g('message', g('error', '')))[:70]}")
    if event in {"db_ping_failed", "ollama_unreachable", "ollama_timeout"}:
        return RED(f"{'DEPENDENCY':22s}{event} {str(g('error', ''))[:60]}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--all", action="store_true", help="show every event, not just the pipeline")
    parser.add_argument("--request", help="only lines for this request_id prefix")
    parser.add_argument("--problems", action="store_true", help="only fallbacks, refusals and errors")
    args = parser.parse_args()

    current = None
    for raw in sys.stdin:
        # `docker compose logs` prefixes each line with the service name.
        line = raw.rstrip("\n")
        brace = line.find("{")
        if brace == -1:
            continue
        try:
            row = json.loads(line[brace:])
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or "event" not in row:
            continue

        event = row["event"]
        request_id = str(row.get("request_id", "-"))

        # The UI polls /readyz every 20 seconds. Left in, it interleaves with
        # the turn being watched and splits the tree in half.
        if not args.all and event == "request":
            path = str(row.get("path", ""))
            if path.startswith(("/readyz", "/healthz")) or path.endswith("/artifacts"):
                continue

        if args.request and not request_id.startswith(args.request):
            continue
        if args.problems and event not in PROBLEMS:
            continue
        if not args.all and not args.problems and event not in PIPELINE and event != "request":
            continue

        text = _describe(row)
        if text is None:
            if not args.all:
                continue
            text = DIM(f"{event:22s}{json.dumps({k: v for k, v in row.items() if k not in {'event', 'timestamp', 'request_id', 'level'}})[:80]}")

        # A new request id starts a new tree.
        if request_id not in {"-", current} and event != "request":
            current = request_id
            print(f"\n{DIM(_time(row))} {BOLD(CYAN('turn ' + request_id[:8]))}")

        prefix = " └─" if event == "request" else " ├─"
        if event == "request":
            print(f"{DIM(_time(row))} {prefix} {text}")
            current = None
        else:
            print(f"{DIM(_time(row))} {prefix} {text}")

        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, BrokenPipeError):
        sys.exit(0)
