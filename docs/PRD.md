# PRD — The Lenny Growth Assistant

**Status:** shipped, v0.1 · **Date:** 27 August 2026

---

## 1. Forward deployment brief

### The user and the job

**Primary user:** a product or growth practitioner on a small team — PM, growth
lead, or founder — who already trusts Lenny's Podcast as a source and treats it
as a reference library rather than entertainment.

**The job:** *"Someone credible has already answered this question on the
podcast. Find me what they said, in a form I can use in the next twenty
minutes."*

The output is rarely just an answer. It is a section of a strategy doc, an
argument for a decision review, a first draft of a post, or a one-pager for a
teammate who wasn't in the room.

**The pain being removed.** 303 episodes is roughly 400 hours. The knowledge is
real and the recall is not: people remember *that* a guest said something useful
about pricing and cannot find *which* guest, *which* episode, or *what exactly*.
The alternatives today are all bad —

- Search YouTube, then scrub through a two-hour video.
- Ask a general chatbot, which produces plausible advice with no idea whether
  anyone on the podcast ever said it.
- Give up and repeat generic advice in a meeting.

The second is the dangerous one. Ungrounded advice that *sounds* like it came
from a named operator is worse than no answer, because it carries borrowed
authority it hasn't earned. **The product's core promise is therefore not "answers"
— it is "answers you can check, or an honest no."**

### Success metrics

**Primary (product): grounded-answer rate.**
Target: ≥ 90% of in-corpus questions return at least one *resolved* citation,
and 5/5 deliberately out-of-corpus questions are refused rather than answered.

**Measured: 12/15 (80%) grounded, 5/5 refused, median latency 29 s.**
Run it yourself with `make evaluate` — it takes about 13 minutes.

The refusal target is met. The grounded target is **not**, and the gap is
reported rather than tuned away. Progression across the build was 60% → 67% →
73% → 80%, each step driven by a measurement in
[`retrieval-calibration.md`](retrieval-calibration.md) §4. The three remaining
failures are relevance-gate refusals on questions the corpus does cover, and the
diagnosis is specific: the 3B judge degrades as its input grows. That is a real
constraint of running a 3-billion-parameter model on a 4 GB GPU, not a
mystery — and naming it is more useful to whoever picks this up than a number
that cannot be reproduced.

Measured against a 20-question golden set (`backend/tests/data/golden_set.json`),
which doubles as a regression fixture. "Resolved" means the marker maps to a
passage actually retrieved for that turn — an invented `[S7]` counts against us,
because it looks like evidence and isn't.

**Secondary (operational): time from `git clone` to first grounded answer < 10 minutes**
on a clean machine, with no API keys.

This is the metric that makes the thing usable by anyone other than its author,
and it is what `make bootstrap` and `/readyz` exist to protect.

**Deliberately not a metric:** answer latency. On a 4 GB GPU a grounded answer
takes ~30 seconds (median, measured) and an essay ~150. Optimising that would mean a smaller model
or less retrieval, both of which trade away the primary metric.

### Assumptions

The brief was incomplete in several places. Each of these is a decision made to
keep moving, and each is cheap to revisit.

| # | Assumption | If wrong |
|---|---|---|
| 1 | Internal tool, trusted users, no authentication needed | Add auth — a day, no architectural change |
| 2 | Read-only corpus; users don't upload their own documents | Ingestion is already a pluggable pipeline |
| 3 | Freshness measured in weeks, not minutes — a weekly re-ingest is fine | The ingest CLI is idempotent; point cron at it |
| 4 | English only | The embedding model and `tsvector` config are both English-specific |
| 5 | Breadth beats depth — better to cite three guests than exhaust one | Tune `RETRIEVAL_MAX_PER_EPISODE` |
| 6 | The evaluator runs this on a laptop, possibly without a GPU | The default model runs on CPU, slowly |
| 7 | Transcripts are third-party text and may contain adversarial content | Already treated as untrusted — see §6 |

### Scope

**In:**

1. Ingestion of all 303 episodes with resumable, content-hashed refresh
2. Hybrid retrieval (dense + sparse, RRF) with timestamped citations
3. Session-isolated streaming chat, persisted
4. Three skills: grounded answers, Ship 30 essays, artifacts
5. Sandboxed artifact viewer with a visible security report
6. Three model providers with a UI toggle and fallback
7. One-command startup, structured logs, health and readiness endpoints
8. 243 tests that run with no Ollama and no keys

**Out, and why:**

| Excluded | Reasoning |
|---|---|
| Authentication | Assumption 1. A cookie scopes the session list; that is enough for a team tool and doesn't pretend to be a security boundary. |
| Cross-encoder reranking | **Tested and rejected on measurement**, not deferred. Latency was affordable (40 pairs in 0.95–1.8 s on CPU), but both `ms-marco-MiniLM-L-6-v2` and `jina-reranker-v1-turbo-en` *demoted* the correct episode on `career-ic-vs-manager` and promoted unrelated ones. They are MS MARCO-trained on short factoid passages; a 400-token chunk of conversation is a different distribution and they have no calibration for it. See [`retrieval-calibration.md`](retrieval-calibration.md) §5. |
| Cloud deployment | The brief asks for a local deployment, and Ollama cannot run on a free tier. Local is both the requirement and the $0 answer. |
| Live Claude Agent SDK verification | Zero-cost constraint; no Anthropic credit. Implemented and fixture-tested, flagged as unverified in the README. |
| Multi-turn artifact editing | Doubles artifact scope for a feature not asked for. Versioning exists in the schema. |
| Streaming tool traces on the Anthropic path | Persisted, not rendered. Cosmetic gap. |

---

## 2. Flows

### Grounded answer

1. User asks a question.
2. **Route** — intent classified from user text *only*, before retrieval.
3. **Retrieve** — hybrid search, top 8 passages, max 3 per episode.
4. **Check relevance** — the model judges whether those passages answer the
   question. This is the abstain gate; see §4.
5. **Generate** — the `grounded-answer` skill, with passages as delimited data.
6. **Verify** — every `[S#]` marker resolved against what was retrieved.
   Invented markers are stripped; zero resolved markers replaces the answer.

Each step streams to the UI as it happens. That visibility is not decoration: a
grounded assistant that declines to answer looks broken unless the user can see
it searched 303 transcripts and judged the results insufficient.

### Ship 30 essay

Route → retrieve → relevance → **plan outline** → write 3 sections + takeaway →
**score against the rubric** → one targeted repair → verify citations.

Sections are written separately because one-shot 1,700-token generation on a 3B
measured ~73 s and lost coherence well before the end. It is also what the source
material prescribes for long pieces — stacked 1/3/1 sequences.

### Artifact

Route → retrieve → relevance → `artifact-builder` skill → parse the fenced
envelope → **sanitize** → store both raw and sanitized plus a removal report →
render in a sandboxed frame.

### Refusal

Retrieval returns nothing above the floor, *or* the relevance check says the
passages don't answer it → an explicit refusal naming what was searched and what
was missing. No LLM freelancing.

---

## 3. Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | `docker compose up` + one ingest command produces a working system | ✅ |
| 2 | Answers cite episodes with working timestamp deep links | ✅ 12/15 measured |
| 3 | Out-of-corpus questions are refused, not answered | ✅ 5/5 measured |
| 4 | Sessions keep independent context | ✅ tested |
| 5 | Provider switchable in the UI without code changes | ✅ |
| 6 | Unavailable provider degrades with a reason and a fix, no crash | ✅ |
| 7 | ~1,250-word essay with the source's principles applied | ✅ 1,142 words, 4 citations |
| 8 | Artifacts render beside the chat, not as raw code | ✅ |
| 9 | Malicious HTML neutralised **and** the removal explained | ✅ 26 payloads |
| 10 | Tests pass with no Ollama and no keys | ✅ 243 |
| 11 | Every failure returns a structured error with a `request_id` | ✅ |

---

## 4. The decision worth arguing about

**The abstain gate is a model judgement, not a similarity threshold — and the
original design was wrong.**

The plan called for abstaining when the best cosine similarity fell below a
threshold. Measured against the golden set:

| | In-corpus | Out-of-corpus |
|---|---|---|
| top-1 cosine | 0.616 – 0.756 | 0.548 – **0.671** |

*"Write me a Python function that reverses a linked list"* scores **0.671** —
higher than 11 of the 15 real product questions. A threshold that rejects all
five out-of-corpus questions would also refuse **11 of 15** legitimate ones.

This is not tuning. A bi-encoder trained on cosine similarity answers *"is this
text similar"*, never *"does this text answer the question"*. No threshold on
that signal can encode a question it was never asked.

So `RETRIEVAL_MIN_SIM` was demoted to a safety floor for degenerate states, and
the gate became a short structured-output call asking the model whether the
passages actually answer the question, backed by mechanical citation resolution
after generation. Reflection, not arithmetic.

Full evidence: [`retrieval-calibration.md`](retrieval-calibration.md).

---

## 5. Risks

| Risk | Severity | Mitigation | Residual |
|---|---|---|---|
| **Hallucination** | High | Relevance gate + citation resolution + answer replaced if nothing resolves | A 3B can still misread a passage it correctly cites. Deep links let the user check. |
| **3B output quality** | High | Outline-then-sections, rubric, citation repair pass | Emphasis check often fails; ships with a visible warning. The relevance judge also degrades as its input grows — the cause of the remaining 3 failures. |
| **Untrusted artifact HTML** | High | Allowlist + sandboxed frame + CSP, 26 payloads tested | Accepted |
| **Prompt injection via transcripts** | Medium | Passages delimited as data; routing decided *before* retrieval so content can't select tools | A guest could still influence answer *content*; citations make it traceable |
| **Retrieval misses** | Medium | Hybrid dense+sparse, widened funnel, `/api/search` to diagnose | 3/15 golden questions still refused; off-the-shelf reranking made it worse, not better |
| **Ad copy polluting the index** | Medium | Sponsor detection, measured at 303/9,112 chunks | Rules are heuristic; `prune_ads.py --dry-run` to re-tune |
| **Cold start latency** | Low | `keep_alive` + startup warmup | First boot still loads the model |
| **Corpus drift** | Low | Content-hashed idempotent re-ingest | Not scheduled; manual or cron |

---

## 6. Security posture

- **Generated HTML is untrusted.** Two independent layers: a server-side
  allowlist, then an iframe with no `allow-same-origin`, no `allow-scripts`, and
  `default-src 'none'`. Granting scripts adds `allow-scripts` *only* — never
  alongside `allow-same-origin`, the one pairing that defeats the sandbox.
- **The sanitizer explains itself.** Removals are recorded and shown in the
  viewer. A silent strip is a black box; the brief asks for a policy an
  evaluator can understand.
- **Transcripts are untrusted input.** Wrapped in delimiters and labelled as
  data. Routing happens before retrieval, on user text only, so no transcript
  can redirect the agent.
- **No secrets in the repo.** `.env` is gitignored; `.env.example` is the
  template. Database credentials are redacted in `/readyz`.
- **Errors don't leak internals.** Clients get a code, a message, and a
  `request_id`; stack traces stay in the logs.

---

## 7. Implementation plan and what it cost

Built in nine phases against [`PLAN.md`](../PLAN.md), in ~13 working hours.

| Phase | Delivered |
|---|---|
| P0–P1 | Scaffold, ingestion, hybrid retrieval, calibration |
| P2 | Provider port, agent loop, streaming chat, persistence |
| P3 | Frontend, agent step display, source cards |
| P4 | Artifacts, sanitizer, sandboxed viewer |
| P5 | Ship 30 skill, rubric, repair loop |
| P6 | Cloud and Claude Agent SDK adapters, fallback |
| P7–P9 | Documentation, fresh-clone rehearsal, demo |

**Cost: $0.** Local model, CPU embeddings, local Postgres, local deploy, public
corpus, OSS throughout.

### What measurement changed

Four decisions were reversed by data rather than argument, which is the part of
this build worth reading:

1. **The default embedding model was the worst option.** `bge-small-en-v1.5`
   ships as int8 ONNX; the target CPU is Zen 2 with no AVX512-VNNI, so int8 is
   emulated at ~10× slower than fp32. 1.8 → 17.6 chunks/s after switching.
2. **800-token chunks were silently truncated to 512** by the encoder — a third
   of every chunk invisible to vector search, with no error anywhere.
3. **"Hybrid" retrieval was dense-only.** `websearch_to_tsquery` ANDs its terms,
   so a natural-language question matched **zero** passages while the pipeline
   logged success.
4. **The abstain gate could not work as designed** (§4).

Three of the four produced no error, no warning, and no failing test. They were
found by reading what the system actually returned.
