# Demo video script

Target: **2–3 minutes, camera on.** The brief asks for the problem, the product,
local Ollama running, and one important technical trade-off.

Record with the full corpus ingested (303 episodes) and the model already warm —
ask one throwaway question before you hit record, or the first answer pays the
~77 s cold load.

**Pre-flight**

```bash
docker compose up -d
curl -s localhost:8000/readyz | python -m json.tool   # expect status: ready
ollama ps                                             # model resident
```

Have open: the app at `localhost:5173`, a terminal showing `ollama ps`, and
`docs/retrieval-calibration.md`.

---

### 0:00–0:20 — The problem

> "Lenny's Podcast is 303 episodes — about 400 hours. The knowledge is real and
> the recall isn't. People remember *that* someone said something useful about
> pricing, and can't find *who*, or *where*.
>
> The obvious fix is to ask a chatbot, and that's the trap: it gives you
> confident advice with no idea whether anyone on the podcast ever said it.
> So this product's promise isn't 'answers' — it's **answers you can check, or
> an honest no**."

### 0:20–0:55 — A grounded answer

Ask: *"How do I know when I've found product-market fit?"*

Point at the steps as they appear.

> "It classified the question, searched 18,806 passages, and checked whether
> what came back actually answers it — before writing a word."

When the answer lands, click a `[S1]` chip, then the timestamp link.

> "Every claim carries its source. That link opens the episode at the second the
> guest said it. Not 'trust me' — 'watch it yourself'."

### 0:55–1:20 — The honest no

Ask: *"Write me a Python function that reverses a linked list."*

> "The model can obviously answer this. It refuses — because it isn't in the
> transcripts, and answering would break the only promise the product makes.
> It tells you what it searched and what was missing."

### 1:20–1:40 — Local model

Switch to the terminal, `ollama ps`.

> "All of that ran on a 3-billion-parameter model on a 4 GB laptop GPU. No API
> keys, nothing left the machine, zero cost."

Click the provider badge.

> "Cloud and Claude are one click away — the toggle's here, and unavailable
> providers say exactly why."

### 1:40–2:10 — Artifact + security

Ask: *"Make me an HTML one-pager on hiring your first PM."*

While it generates:

> "It's writing a document, not a chat reply — it renders beside the
> conversation."

Point at the sanitizer banner (or open one that has removals).

> "Generated HTML is untrusted — the model just read hundreds of pages of
> third-party text. It's stripped against an allowlist, then rendered in a
> sandboxed frame with no scripts, no network, and no access to this page. And
> it *tells you* what it removed. A sanitizer that strips silently is a black
> box."

### 2:10–2:50 — The trade-off worth explaining

Pick **one**. The abstain gate is the stronger story.

> "The brief asked for the Claude Agent SDK, and for the demo to run on Ollama.
> Those can't be the same code path — Anthropic doesn't support routing the SDK
> to non-Claude models, and a 3B model can't drive that tool protocol anyway.
>
> So the agent layer is mine, and providers are adapters behind one port. The
> same skill files are loaded natively by the Agent SDK, or rendered into a
> prompt for everything else. One definition, two runtimes.
>
> The other one I'd point at: I planned to detect off-topic questions with a
> similarity threshold. Then I measured it."

Show the calibration doc.

> "In-corpus questions score 0.62 to 0.76. Out-of-corpus, 0.55 to 0.67. They
> overlap. 'Write me a linked list' scores higher than eleven of fifteen real
> product questions. Any threshold that rejects every bad question also refuses
> eleven good ones.
>
> That killed the design. A bi-encoder answers 'is this text similar' — never
> 'does this answer the question'. So the gate became a model judgement plus
> mechanical citation checking. That's the kind of thing you only find by
> measuring, and three of the four real bugs I hit produced no error at all."

### 2:50–3:00 — Close

> "Clone, `docker compose up`, one ingest command, and you're asking questions
> in under ten minutes. 231 tests that run with no model and no keys. Everything
> — the PRD, the architecture, the calibration data, and the agent transcripts
> including what went wrong — is in the repo."

---

## Notes

- **Don't narrate the UI.** Say what it means, not what it is.
- **Let one answer stream in real time.** Cutting the wait hides that latency is
  honest and legible.
- If an answer refuses when you expect it to succeed, keep going and say so —
  a 3B on a 4 GB GPU is a real constraint, and the measured grounded rate is in
  the PRD.
- Upload unlisted, link it in the README.
