"""Export a Claude Code session transcript for the handoff.

    python -m scripts.export_transcript ~/.claude/projects/<proj>/<id>.jsonl \
        -o agent-transcripts/01-session.md

The raw JSONL is ~3 MB of tool payloads and file contents — unreadable, and it
carries whatever passed through the session. This produces a readable markdown
transcript with secrets redacted, keeping the prompts, the reasoning, the tool
calls, and crucially the **failures**, which the brief asks for by name.

Redaction is conservative: anything shaped like a key, token, password, or
connection string with credentials is replaced. Run outside a container, since
the transcripts live in the host's home directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

# Conservative: over-redaction costs readability, under-redaction leaks.
_REDACTIONS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), "sk-ant-REDACTED"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-REDACTED"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "AIza-REDACTED"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "gsk-REDACTED"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp-REDACTED"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?([^\s'\"]{8,})"),
     r"\1=REDACTED"),
    (re.compile(r"postgres(?:ql)?://[^:]+:[^@]+@"), "postgresql://user:REDACTED@"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer REDACTED"),
]

MAX_BLOCK_CHARS = 2400


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit:,} more characters]"


def _text_of(content: Any) -> str:
    """Message content is either a string or a list of typed blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "thinking":
            parts.append("_[thinking]_\n" + block.get("thinking", ""))
        elif kind == "tool_use":
            args = json.dumps(block.get("input", {}), indent=2)[:800]
            parts.append(f"**→ {block.get('name', 'tool')}**\n```json\n{args}\n```")
        elif kind == "tool_result":
            body = block.get("content")
            body = _text_of(body) if not isinstance(body, str) else body
            flag = " (error)" if block.get("is_error") else ""
            parts.append(f"**← result{flag}**\n```\n{truncate(body, 900)}\n```")
    return "\n\n".join(p for p in parts if p.strip())


def render(rows: Iterable[Dict[str, Any]], title: str) -> str:
    out = [f"# {title}", "", "> Exported from a Claude Code session. Secrets redacted.", ""]
    turn = 0
    for row in rows:
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        body = _text_of(message.get("content"))
        if not body.strip():
            continue
        if role == "user":
            turn += 1
            out += [f"\n---\n\n## Turn {turn} — prompt", "", truncate(redact(body))]
        elif role == "assistant":
            out += ["", "### Response", "", truncate(redact(body))]
    return "\n".join(out) + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.export_transcript")
    parser.add_argument("source", type=Path, help="path to the .jsonl session file")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--title", default="Coding agent transcript")
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"No such file: {args.source}", file=sys.stderr)
        return 1

    rows = []
    for line in args.source.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a partial trailing line while the session is live

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(rows, args.title), encoding="utf-8")
    print(f"Wrote {args.output} ({args.output.stat().st_size:,} bytes) from {len(rows)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
