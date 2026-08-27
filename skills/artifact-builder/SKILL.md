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
```artifact {"kind": "markdown", "title": "<a short title for THIS document>"}
<the document>
```
````

- `kind` is `"markdown"` or `"html"`. **Default to `markdown`** — see below.
- `title` describes *this* document. Never copy the placeholder above; a title
  that does not match the content is worse than no title.
- Write one or two sentences of chat before the block saying what you made.
  **Write the document once, inside the fence.** Do not also paste it into the
  chat — it renders in its own panel, and a second copy is pure noise.

## Do not mix the two formats

Pick one and write the whole document in it. The single most common failure here
is opening with `<h1>Some Title</h1>` and then writing everything else in
markdown. The result renders as literal `##` and `- ` characters on screen.

If you choose markdown, use `#` headings — not `<h1>`. If you choose HTML, use
`<h1>` and `<ul><li>` — not `#` and `- `.

## Choosing the format

**Markdown** for anything primarily read in order: briefs, checklists, meeting
notes, summaries, plans, one-pagers. **This is the default.** Choose it unless
visual layout is genuinely the point — it is easier to write correctly and it
renders well without any styling effort.

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
