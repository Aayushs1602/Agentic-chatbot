# Design

UI and UX decisions, and the reasoning behind them.

---

## 1. Principles

**Show the work.** A grounded assistant that declines to answer looks *broken*
unless the user can see why. So the agent's steps stream live — classified the
question, searched 303 transcripts, found five passages, judged them
insufficient. This is the single most important interaction decision in the
product: it converts an apparent failure into a legible decision, and makes the
agent auditable without reading logs.

**Citations are checkable, not decorative.** Every marker deep-links to the
second of the episode where the claim was made. "Trust me" and "watch it
yourself" are different products.

**Refusal is a first-class answer.** It gets its own visual treatment — warm, not
alarming — and explains what was searched and what was missing. It reads as the
system working, because it is.

**Latency is honest.** On a 4 GB GPU an answer takes 5–15 s and an essay ~150 s.
Rather than hide that behind a spinner, the steps and partial text stream so the
wait is *legible* and the user can tell progress from a hang.

**The chat is the product; everything else gets out of the way.** Sources
collapsed by default, sidebar hidden on mobile, artifact pane only when there is
an artifact.

---

## 2. Information architecture

```
┌──────────┬──────────────────────────────────────┬───────────────────┐
│ Sessions │ Header: title · corpus · provider    │                   │
│          ├──────────────────────────────────────┤  Artifact viewer  │
│ New chat │                                      │                   │
│ ───────  │  Agent steps                         │  Preview │ Source │
│ Chat 1   │  Answer with [S1] chips              │                   │
│ Chat 2   │  ▸ 3 sources                         │  🛡 2 removed     │
│          │                                      │                   │
│          ├──────────────────────────────────────┤  ☐ Allow scripts  │
│          │ Composer                             │                   │
└──────────┴──────────────────────────────────────┴───────────────────┘
```

Three regions, in priority order: **history** (navigation, collapsible),
**conversation** (always primary), **artifact** (contextual, only when one exists).

Nothing is more than one click from the conversation. There is no settings page:
the only setting that matters — which model is answering — lives in the header
where its state is always visible.

---

## 3. Interaction states

Every state below is designed, not incidental.

| State | Treatment | Why |
|---|---|---|
| **Empty session** | Headline + one line on what the assistant does + four suggested questions | The suggestions teach the corpus's boundaries faster than any explanation |
| **Streaming** | Steps appear as they complete; text streams with a blinking caret | The caret distinguishes "thinking" from "hung" |
| **Tool running** | Last step pulses; earlier steps go solid green | Progress without a progress bar that would be lying |
| **Essay in progress** | Headline and hook appear immediately, then each section | ~150 s is intolerable as a spinner and fine as visible construction |
| **Refusal** | Warm amber card, "Outside the corpus" label, explanation | Distinct from an error — nothing broke |
| **Answer replaced** | Streamed text is discarded and replaced | Grounding is only knowable after generation; a plausible uncited answer must not stay on screen |
| **Provider down** | Red dot in the badge, reason and fix in the dropdown | A greyed-out option with no reason reads as a broken product |
| **Corpus empty** | Banner with the exact ingest command; composer disabled | The most likely first-run state; guessing is not required |
| **DB down** | Banner, composer disabled | Better than a white screen |
| **Error** | Red card with message, hint, and `request_id` | The id ties the user's report to the logs |
| **Artifact sanitized** | Amber strip, expandable list of what was removed and why | The brief asks for a legible policy |

---

## 4. Key decisions

### Agent steps are visible by default

The alternative — a spinner, then an answer — tested badly against the product's
core promise. When the assistant refuses, a spinner makes it look broken. When it
answers, the steps are the difference between "a chatbot said so" and "it
searched 303 transcripts and found these five passages."

Rendered as plain language ("Checking the sources answer it"), not internal names.

### Citation chips over footnotes

`[S1]` renders as a small clickable chip that scrolls to its source card.
Inline, so the claim and its evidence stay adjacent; compact, so a sentence with
three citations still reads as prose.

### Sources collapsed by default

Five sources of ~400 tokens each would dominate the answer. Collapsed to a one-line
summary, expandable, with the title, guest, and a timestamped link.

### The artifact pane takes over below 1024px

Squeezing a chat column and a document into a phone width makes both unusable.
Under 1024px the pane replaces the conversation, with a document count in the
header to get back.

### Refusal is amber, not red

Red means something broke. A refusal is the system working correctly and saying
so. Amber, with "Outside the corpus" rather than an apology.

### No dark-mode toggle

The page follows the system preference. A toggle is a preference to store, a
control to place, and a state to get wrong; following the OS is right by default
and costs nothing.

---

## 5. Responsive behaviour

| Width | Layout |
|---|---|
| ≥ 1024px | Sidebar + chat + artifact pane side by side |
| 768–1023px | Sidebar visible; artifact pane replaces the chat when open |
| < 768px | Sidebar becomes an overlay; single column; artifact full-screen |

Tables and code blocks scroll inside their own container — the page body never
scrolls horizontally, including inside the artifact frame.

---

## 6. Accessibility

- **Streaming and screen readers.** The answer container is `aria-live="off"`
  while streaming and `polite` once settled. A per-token live region is
  technically "accessible" and practically unusable — it re-announces on every
  token.
- **Focus management.** Focus returns to the composer when a reply completes.
- **Keyboard.** Everything reachable and operable: `Enter` sends, `Shift+Enter`
  newlines, `Escape` closes the provider menu. The delete-chat button appears on
  hover *and* on focus — a hover-only control doesn't exist without a pointer.
- **Contrast.** WCAG AA in both themes. Colour is never the only signal: the
  provider dot pairs with text, refusals carry a label as well as a tint.
- **Motion.** `prefers-reduced-motion` disables the caret blink and transitions.
- **Semantics.** One `<h1>`, `<nav>`/`<main>`/`<aside>` landmarks, `aria-expanded`
  on disclosures, `aria-current` on the active session, labelled controls.
- **Links.** External links carry `rel="noopener noreferrer"` and an
  `sr-only` "(opens in a new tab)".

---

## 7. Visual language

A warm, paper-like palette rather than the default cool grey — this is a reading
tool, and the corpus is conversational.

| Token | Light | Dark |
|---|---|---|
| `--bg` | `#fbfaf9` | `#16150f` |
| `--surface` | `#ffffff` | `#1e1d18` |
| `--text` | `#1c1a18` | `#f0eee9` |
| `--accent` | `#b8552b` | `#e08b5f` |
| `--warn` | `#9a6a12` | `#d9ac52` |
| `--danger` | `#b3261e` | `#f08b83` |

System font stack — no webfont, so no external request, no layout shift, and
nothing to block in the artifact frame's CSP.

Every colour is defined on bare `:root` first, then overridden under
`prefers-color-scheme: dark`, so nothing depends on a media query to *exist*.

---

## 8. Rejected alternatives

| Considered | Rejected because |
|---|---|
| Render artifacts inline in the chat | A 1,200-word document destroys conversation scannability; the brief also asks for a viewer beside the chat |
| Auto-open the artifact pane on session load | Steals attention on a chat the user opened to re-read |
| Footnote-style citations at the bottom | Separates a claim from its evidence — the thing the product exists to keep together |
| Hide agent steps behind a "details" toggle | The steps are most valuable exactly when the user hasn't thought to look |
| Optimistic streaming without `replace` | Would leave a fluent uncited answer on screen — the failure this product exists to prevent |
| Model-generated session titles | A second inference on a 3B for a sidebar label; truncating the first question is predictable and free |
| A settings page | One setting matters; it belongs in the header |
