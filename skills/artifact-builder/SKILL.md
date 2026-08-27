---
name: artifact-builder
description: Produce a Markdown or HTML/CSS document from the conversation, rendered beside the chat in the artifact viewer.
when_to_use: The user asks for a document, table, checklist, template, one-pager, dashboard, or anything they want to look at and reuse rather than read as a chat reply.
---

# Artifact builder

Produce a self-contained document from the conversation and the transcript
sources, wrapped in an artifact envelope so it renders in the viewer beside the
chat instead of appearing as a wall of code.

## The envelope

Emit exactly this, with nothing after the closing fence:

````
```artifact {"kind": "html", "title": "Q3 Growth Review"}
<h1>Q3 Growth Review</h1>
...
```
````

- `kind` is `"html"` or `"markdown"`.
- `title` is a short noun phrase — it labels the viewer tab.
- Write one or two sentences of chat before the block saying what you made.
  Do not repeat the document's contents in the chat.

## Choosing the format

**Markdown** for anything primarily read in order: briefs, checklists, meeting
notes, summaries, plans. It is the default — pick it unless layout is the point.

**HTML** when structure carries meaning: comparison tables, dashboards, scorecards,
side-by-side layouts, anything with visual hierarchy.

## HTML rules

The viewer renders artifacts in a sandboxed frame with no network access and no
scripting. Write to that reality rather than against it:

- **No `<script>`.** Anything interactive is stripped and the user is told it was.
- **No external requests** — no `<link>`, no remote `<img src>`, no web fonts.
  Inline `<style>` only. Images must be `data:` URIs.
- **No forms** — no `<form>`, `<input>`, or `<button>`.
- **A fragment, not a page.** Start at `<h1>`; no `<!doctype>`, `<html>`, `<head>`,
  or `<body>`.
- **Style it deliberately.** A `<style>` block with system fonts, real spacing,
  and readable type. Plain unstyled HTML looks unfinished.
- **Use system font stacks:** `font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- **Colours must work on light and dark backgrounds.** Set an explicit
  `background` and `color` on the outer container rather than inheriting.
- **Tables must scroll**, not overflow: wrap them in
  `<div style="overflow-x:auto">`.

Anything violating these is removed by the sanitizer, and the viewer shows the
user exactly what was removed and why. Cooperating produces a better document
than being cleaned up.

## Grounding

An artifact is held to the same standard as an answer:

- Every factual claim carries its `[S#]` marker, in the artifact itself.
- Never invent a marker; only the provided sources exist.
- Close with a **Sources** section listing each cited episode and guest.
- When the sources do not support the document, say so in chat and do not emit
  an artifact. A confident, well-formatted, ungrounded document is worse than no
  document — the formatting makes it more persuasive, not more true.

## Quality bar

- Lead with the most decision-useful content. No throat-clearing.
- Concrete over generic: real examples, numbers, and named frameworks from the
  transcripts.
- Skimmable — headings, short paragraphs, bullets where the content is a list.
- If a table has more than about six columns, it wants to be a list instead.
