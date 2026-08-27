# The Lenny Growth Assistant — Implementation Plan

**Working document.** Source of truth for scope, architecture, and schedule.
Target: FDE take-home, due **28 Aug 2026 EOD** (~1.5 days from 27 Aug).
Budget target: **$0**, with one optional $5 line item (see §10).

---

## 0. The four decisions that shape everything

Read this section first. Everything downstream follows from it.

### D1. The agent layer is *ours*; the Claude Agent SDK is one adapter behind it

The brief asks for two things that fight each other:

- "Build the agent layer using the Anthropic Claude Agent SDK or Pi Coding Agent"
- "Local LLM — **mandatory for the demo**: run the submitted demo using Ollama"

These cannot be the same code path. Anthropic's own docs state plainly that they do
*not* support routing Claude Code / the Agent SDK to non-Claude models through any
gateway (`code.claude.com/docs/en/llm-gateway`). Bolting Ollama behind a LiteLLM
Anthropic-shim would be unsupported and fragile, and a 3B model will not survive
Claude Code's tool protocol anyway.

**Resolution — a real port/adapter boundary:**

```
                     +----------------------------------+
   FastAPI  ------->  |  Orchestrator (our agent loop)   |
                     |  route -> retrieve -> skill -> emit |
                     +----------------+-----------------+
                                      |  AgentProvider protocol
              +-----------------------+-----------------------+
              v                       v                       v
     AnthropicAgentProvider    OllamaProvider        OpenAICompatProvider
     (claude-agent-sdk,        (deterministic         (Gemini / Groq /
      native tools+skills,      pipeline, JSON-        OpenAI — free tiers)
      full agentic loop)        schema outputs)
```

The **skills are the shared asset**: one set of `SKILL.md` files, loaded natively by
the Agent SDK and rendered into prompts by the other two providers. Same behaviour
contract, two execution strategies. This is the headline trade-off for the PRD and
the demo video.

### D2. Do not rely on model-driven function calling on the local path

At 4GB VRAM the ceiling is a 3B Q4 model, and 3B function calling is unreliable.
So the Ollama path uses:

- a **deterministic router** — intent classification via Ollama's `format: <json-schema>`
  structured output, which is far more reliable than tool calls at 3B; and
- **retrieval that always runs** for knowledge intents, rather than being a tool the
  model may forget to call.

Tool *calls* are still recorded in the `tool_calls` table on both paths, so the
observability story and the UI are identical regardless of provider.

### D3. Local Postgres + pgvector, not a hosted DB

`pgvector/pgvector:pg17` in Docker Compose. Zero cost, works offline, no free-tier
pause, and the evaluator gets a real Postgres with the exact schema in
`architecture.md`. Supabase stays supported via a single `DATABASE_URL` swap and is
documented — but is not the default. The brief says "deploy it locally"; local is
both the requirement and the free option.

### D4. Hybrid retrieval in Postgres, embeddings on CPU

- Dense: `pgvector` HNSW, cosine.
- Sparse: Postgres `tsvector` + `ts_rank_cd` (BM25-ish, free, already in the DB).
- Fuse with **Reciprocal Rank Fusion** — no reranker model to download or host.
- Embeddings via **`fastembed`** (ONNX, CPU, `BAAI/bge-small-en-v1.5`, 384-dim,
  ~130MB). *Not* Ollama embeddings: with 4GB VRAM, sharing the GPU between the chat
  model and an embedding model causes a model swap on every single query. Keeping
  embeddings on CPU means Ollama only ever holds the chat model resident.
  `nomic-embed-text` via Ollama stays available as `EMBEDDINGS_PROVIDER=ollama`.

---

## 1. Hardware reality check (drives model selection)

| | |
|---|---|
| CPU | Ryzen 5 4600H, 6C/12T |
| RAM | 15.4 GB |
| GPU | GTX 1650, **4 GB VRAM** |
| Disk free | 239 GB |

**Demo model: `qwen2.5:3b-instruct-q4_K_M`** (~2.0 GB) — fits fully in VRAM with an
8k KV cache, expect ~20–30 tok/s, and it is the strongest 3B for structured JSON output.

- `llama3.2:3b` as the documented alternative.
- `qwen2.5:7b-instruct-q4_K_M` documented as "better quality, ~6 tok/s via partial
  offload" — offered, not the default.

**Gotcha to handle explicitly:** Ollama's default `num_ctx` is 4096 and it *silently
truncates*. We set `num_ctx=8192` per request and assert it in a startup check.

**Consequence for the 1,250-word essay:** one 1,700-token generation at 25 tok/s is
~70s, and quality degrades badly at that length on a 3B. So the Ship 30 skill
**generates section-by-section** — outline fixed up front, then ~5 calls of ~250 words
each. Better output, no timeouts, and it streams with visible progress.

---

## 2. Scope

### In
1. Ingestion of `ChatPRD/lennys-podcast-transcripts` — 269 episodes, Markdown with
   YAML frontmatter (guest, title, YouTube URL + video ID, publish date, duration, views).
2. Hybrid RAG with per-claim citations and deep links back to YouTube.
3. Session-isolated streaming chat, persisted in Postgres.
4. Three skills: `grounded-answer`, `ship30-essay`, `artifact-builder`.
5. Artifact generation (Markdown + HTML/CSS) with a sandboxed in-app viewer.
6. Provider toggle: Ollama (default) / Anthropic Agent SDK / OpenAI-compatible.
7. Docker Compose one-command startup, structured logs, health + readiness endpoints.
8. All 8 deliverables.

### Out — and stated as such in the PRD
- **Auth / multi-tenancy** — anonymous `user_id` cookie only. Not needed for an
  internal single-team tool; adds a day.
- **Cross-encoder reranking** — RRF is good enough at this corpus size; documented as
  the first thing to add.
- **Streaming tool traces in the UI for the Anthropic path** — persisted, not rendered.
- **Cloud hosting** — the brief says deploy locally, and Ollama cannot run on a free tier.
- **Scheduled corpus refresh** — the ingest CLI is idempotent and resumable by content
  hash; wiring it to a cron is documented, not built.

---

## 3. Repository layout

```
Agentic-chatbot/
├─ docker-compose.yml           # db + backend + frontend (+ optional ollama profile)
├─ Makefile                     # make up / ingest / test / bootstrap / logs
├─ .env.example
├─ README.md
├─ PLAN.md                      # this file
├─ docs/
│  ├─ PRD.md
│  ├─ design.md
│  ├─ architecture.md
│  └─ manual-test-plan.md
├─ agent-transcripts/           # deliverable #6 (redacted Claude Code sessions)
│  ├─ README.md                 # narrative: what went wrong, how it was corrected
│  └─ 0N-<topic>.md
├─ skills/                      # SHARED between all providers
│  ├─ grounded-answer/SKILL.md
│  ├─ ship30-essay/
│  │  ├─ SKILL.md
│  │  └─ reference/principles.md   # extracted from the Ship 30 guide
│  └─ artifact-builder/SKILL.md
├─ backend/
│  ├─ Dockerfile
│  ├─ pyproject.toml
│  ├─ alembic/
│  ├─ app/
│  │  ├─ main.py                # app factory, lifespan, request-id middleware
│  │  ├─ config.py              # pydantic-settings, fail-fast validation
│  │  ├─ logging.py             # structlog -> JSON lines
│  │  ├─ errors.py              # structured error envelope + handlers
│  │  ├─ db/{session,models}.py
│  │  ├─ schemas/               # pydantic request/response contracts
│  │  ├─ api/{health,sessions,chat,artifacts,search,providers}.py
│  │  ├─ providers/{base,ollama,anthropic_sdk,openai_compat,registry}.py
│  │  ├─ agent/{orchestrator,router,tools,skills}.py
│  │  ├─ rag/{ingest,chunker,embeddings,retrieve}.py
│  │  └─ security/sanitize.py
│  └─ tests/
└─ frontend/
   ├─ Dockerfile
   └─ src/
      ├─ components/{ChatPane,MessageList,Composer,SourceCards,
      │              ArtifactPane,ArtifactFrame,ProviderBadge,SessionSidebar}
      ├─ hooks/{useSSE,useSessions}.ts
      └─ lib/api.ts
```

---

## 4. Data model (Postgres 17 + pgvector)

```sql
episodes(
  id uuid pk, slug text unique, title text, guests text[],
  youtube_url text, video_id text, published_on date,
  duration_s int, description text,
  source_path text, content_sha256 text,   -- idempotent re-ingest
  ingested_at timestamptz
)

chunks(
  id uuid pk, episode_id uuid fk on delete cascade,
  ord int, text text, token_count int,
  start_char int, end_char int,
  start_seconds int null,                  -- -> youtube.com/watch?v=ID&t=Ns
  embedding vector(384),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
)
-- CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
-- CREATE INDEX ON chunks USING gin (tsv);
-- UNIQUE (episode_id, ord)

sessions(
  id uuid pk, title text, user_id text,    -- anon cookie
  provider text, model text,
  created_at, updated_at, metadata jsonb   -- ua, locale, tz
)

messages(
  id uuid pk, session_id uuid fk on delete cascade,
  role text check in ('user','assistant','system'),
  content text, created_at timestamptz,
  provider text, model text,
  latency_ms int, tokens_in int, tokens_out int,
  citations jsonb,                          -- [{marker,chunk_id,episode_id,score}]
  finish_reason text, error jsonb
)

artifacts(
  id uuid pk, session_id uuid fk, message_id uuid fk,
  kind text check in ('markdown','html'), title text,
  content_raw text, content_sanitized text,
  sanitizer_report jsonb,                   -- what was stripped, and why
  version int, created_at timestamptz
)

tool_calls(
  id uuid pk, message_id uuid fk, name text, args jsonb,
  result_summary jsonb, duration_ms int, ok bool, error text
)

ingest_runs(
  id uuid pk, started_at, finished_at, episodes_seen int,
  episodes_ingested int, chunks int, status text, error text
)
```

Session isolation is enforced at the query layer: every message read is scoped by
`session_id`, and there is a dedicated test asserting no cross-session leakage.

---

## 5. API contract

```
GET  /healthz                      -> {status:"ok", version, uptime_s}      (liveness)
GET  /readyz                       -> {db, embeddings, provider:{name,model,reachable},
                                       corpus:{episodes,chunks}, degraded:[...]}
GET  /api/providers                -> [{id,label,model,available,reason}] + active
POST /api/providers/active         -> switch at runtime (also settable via env)

POST /api/sessions                 -> {id,title,created_at}
GET  /api/sessions                 -> [...]
GET  /api/sessions/{id}/messages   -> [...]
DELETE /api/sessions/{id}

POST /api/sessions/{id}/messages   -> text/event-stream
     events: meta | token | tool | citations | artifact | done | error

GET  /api/artifacts/{id}           -> {kind,title,content_sanitized,sanitizer_report}
GET  /api/artifacts/{id}/raw       -> original, for the "view source" tab

POST /api/search  {q, k}           -> raw retrieval results (evaluator debug affordance)
GET  /api/ingest/status            -> last ingest_run
```

**Error envelope (every non-2xx):**

```json
{"error":{"code":"provider_unavailable",
          "message":"Ollama is not reachable at http://host.docker.internal:11434",
          "detail":{"hint":"Run `ollama serve`"},
          "request_id":"01J..."}}
```

`/readyz` returning a per-dependency breakdown is the highest-leverage thing we can
build for an evaluator: when something is wrong, one curl tells them what.

---

## 6. Ingestion & retrieval

**Ingest** — `python -m app.rag.ingest --limit N --since YYYY-MM-DD`:

1. `git clone --depth 1` the transcripts repo into `./data/` (cached; `--refresh` pulls).
2. Parse YAML frontmatter into `episodes`. Skip if `content_sha256` is unchanged.
3. Chunk at ~800 tokens with 120 overlap, splitting on speaker-turn / paragraph
   boundaries, never mid-sentence. Capture `start_char` and, if `[HH:MM:SS]` markers
   exist in the transcript, `start_seconds` for YouTube deep links.
4. Embed in batches of 64 with fastembed on CPU. Full corpus is roughly 40k chunks,
   about 6–10 minutes.
5. Upsert and record an `ingest_runs` row. Resumable — interrupt and re-run.

`make ingest LIMIT=20` gives an evaluator a working system in under a minute;
`make ingest` does the full corpus.

**Retrieve** — `retrieve.py`:

1. Dense top-40 (cosine) and sparse top-40 (`ts_rank_cd`), run concurrently.
2. RRF fuse: `score = sum over rankers of 1 / (60 + rank_i)`.
3. Diversity cap: at most 3 chunks per episode.
4. Take the top 5 — about 3k tokens, safely inside an 8k `num_ctx` on a 3B.
5. **Abstain gate**: gate on the best *dense cosine similarity*, not the RRF score. RRF
   is a rank-fusion signal — a nonsense query still yields a rank-1 document, so a fused
   score threshold would never fire. Cosine similarity is calibrated to meaning: with
   `bge-small-en-v1.5`, relevant passages sit around 0.5–0.8 and unrelated ones below
   0.4. If `max(cosine) < RETRIEVAL_MIN_SIM` (default 0.45, tuned against the golden set
   in P1), return zero context and have the orchestrator emit an explicit "the transcripts
   I have don't cover this" response *without* asking the LLM for a freeform answer.

**Grounding enforcement:** the prompt requires `[S1]`…`[S5]` markers. After generation,
every marker is resolved against the retrieved set; unresolved markers are stripped and
counted, and a knowledge-intent response with zero resolved markers triggers one repair
pass, then falls back to the abstain message. Citations are persisted on the message and
rendered as expandable source cards.

**Prompt-injection note (goes in architecture.md):** transcripts are third-party text
entering the context window. Retrieved chunks are wrapped in explicit delimiters and
labelled as *data, not instructions*, and retrieval output never influences tool or route
selection — routing happens before retrieval, on user text only.

---

## 7. Skills

Each skill is a `SKILL.md` with frontmatter (`name`, `description`, `when_to_use`), a
procedure, and a rubric. The Agent SDK loads them natively from `.claude/skills/`
(symlinked from `skills/`); the other providers get them rendered into the system prompt
by `agent/skills.py`.

### `ship30-essay` — the one that is graded hardest

The brief explicitly says: *read the linked source, identify the writing principles, and
encode them in the skill rather than relying on an unstructured one-off prompt.* So:

- `reference/principles.md` — principles extracted from the Ship 30 for 30 guide: one
  idea per essay, proven headline formulas ("How to X without Y", "N ways to X", "Why X
  is wrong about Y"), the 1-3-1 opening, short declarative sentences, concrete proof over
  abstraction, formatting for skim, and a specific closing takeaway.
- **A fixed outline contract**: hook → context → 3 body sections → takeaway. The skill
  generates the outline first as structured JSON, then drafts each section separately
  (see §1) — this is what makes a 3B model produce a usable 1,250 words.
- **A programmatic rubric** (`ship30_validator`): word count within 1,100–1,400, at least
  3 H2s, at least 1 bullet list, bold-emphasis ratio under a ceiling, at least 4 resolved
  citation markers, no section over N words. Failing checks trigger **one** targeted
  repair pass on the offending section only, then ship with a visible warning.

A skill is instructions **plus** a machine-checkable rubric **plus** a repair loop. That
distinction is worth one sentence in the video.

### `grounded-answer`
Concise answer, inline `[S#]` markers, explicit abstain language, follow-up aware.

### `artifact-builder`
Emits a strict envelope the server parses:

<pre>
```artifact {"kind":"html","title":"Q3 Growth Review"}
...
```
</pre>

HTML rules are baked into the skill: self-contained, inline `&lt;style&gt;` only, no
scripts, no external requests, no forms. The sanitizer is the enforcement; the skill is
the cooperation.

---

## 8. Artifact viewer & security

Two independent layers — neither is trusted alone.

**Layer 1 — server-side sanitize** (`nh3`, the Rust/ammonia binding):

- Tag allowlist (structural, text, table, `style`), attribute allowlist.
- Stripped: `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, `<link>`, `<base>`,
  all `on*` handlers, `javascript:` and `data:text/html` URLs, SVG event attributes,
  CSS `expression()`, `@import`, and external `url()`.
- Every removal is recorded in `sanitizer_report` and **shown in the UI** as
  "4 elements removed — details ▸". That is what makes the policy legible to the
  evaluator, which is exactly what the brief asks for.

**Layer 2 — render isolation:**

```html
<iframe sandbox srcdoc="..."></iframe>   <!-- no allow-same-origin, no allow-scripts -->
```

plus a CSP injected into the `srcdoc` head:
`default-src 'none'; style-src 'unsafe-inline'; img-src data:;`

An opaque origin means no access to app cookies, `localStorage`, or the parent DOM; no
network egress means no exfiltration even if a payload survives layer 1.

An **"Allow scripts" toggle defaults to OFF**. Turning it on adds `allow-scripts` only —
still never `allow-same-origin`, so scripts run in a unique opaque origin. Documented in
`design.md` and `architecture.md` as permit / block / why.

**Markdown** goes through `react-markdown` + `remark-gfm` + `rehype-sanitize` with raw
HTML disabled — a different, simpler path, not the HTML pipeline.

Viewer features: Preview / Source tabs, copy, download, version history per artifact.

---

## 9. Frontend

Vite + React 18 + TypeScript + Tailwind + shadcn/ui. Not Next.js: there is no SSR need,
the backend is FastAPI, and Vite's dev/build loop is faster to ship in a day.

- **Split pane** — chat left, artifact right, drag-resizable; collapses to tabs under 768px.
- **Provider badge** in the header showing active provider, model, and a health dot from
  `/readyz`; click to switch. Satisfies "make the selected provider visible in the UI".
- **Source cards** numbered `[S1]`… under each answer; click expands the chunk text and
  links to the YouTube deep link.
- **States, all designed rather than accidental**: empty session, streaming,
  tool-running ("searching 269 transcripts…"), section-by-section essay progress, empty
  retrieval (abstain), provider down, artifact sanitized-with-warnings, network error
  with retry.
- **Accessibility**: `aria-live="polite"` on the stream, focus returns to the composer on
  completion, full keyboard navigation, visible focus rings, WCAG AA contrast in both
  themes, `prefers-reduced-motion` respected.

---

## 10. Cost — the "mostly free" ledger

| Item | Choice | Cost |
|---|---|---|
| Local LLM | Ollama + qwen2.5:3b | $0 |
| Embeddings | fastembed CPU (bge-small) | $0 |
| Database | Postgres + pgvector in Docker | $0 |
| Vector store | pgvector (same DB) | $0 |
| Hosting | local Docker Compose | $0 |
| Frontend / backend / libraries | all OSS | $0 |
| Corpus | public GitHub repo | $0 |
| Repo / video | GitHub + YouTube | $0 |
| **Cloud LLM (required)** | see below | **$0 or $5** |

For the cloud provider requirement, in preference order:

1. **Google Gemini free tier** (`gemini-2.x-flash`) through `OpenAICompatProvider` —
   genuinely free with generous limits. The brief says "such as Anthropic Claude or
   OpenAI", so this qualifies. Groq's free tier is an equivalent drop-in.
2. **Anthropic API with $5 of credit** — needed only if we want the `claude-agent-sdk`
   path *demonstrated* rather than merely implemented. Actual usage would be about $0.20.

Note: powering the app from a Claude Code / claude.ai subscription is **not** permitted
for products built on the Agent SDK — Anthropic's docs are explicit — so the Agent SDK
path is documented as `ANTHROPIC_API_KEY`-only. Worth $5 if it can be spent; if not, ship
the adapter with unit tests against recorded fixtures and say so plainly in the README.
An honest documented gap beats a fake integration.

---

## 11. Deployment & operability

**`docker compose up`** brings up `db` (pgvector), `backend`, and `frontend`. Ollama runs
**on the host** — GPU passthrough into Docker Desktop on Windows is not worth the risk —
and the backend reaches it at `host.docker.internal:11434`. An optional `--profile ollama`
service is provided for Linux/CPU evaluators.

Startup sequence: db healthcheck → alembic migrate → backend readiness → frontend.
`make bootstrap` runs `up`, then `ingest LIMIT=20`, then a smoke query, and prints a green
check.

**Observability**: structlog JSON lines with a `request_id` on every log, propagated
through orchestrator → provider → retrieval. Every LLM call logs provider, model, latency,
token counts, and finish reason. Every retrieval logs the query, k, fused top scores, and
whether the abstain gate fired. Every artifact logs the sanitizer diff.

**Resilience matrix** — each row gets a test:

| Failure | Behaviour |
|---|---|
| `ANTHROPIC_API_KEY` missing | provider marked unavailable in `/readyz` and the UI; not a crash |
| Ollama down | 503 with a structured hint; auto-fallback to the next healthy provider when `PROVIDER_FALLBACK=true` |
| Model timeout | stream closes with an `error` event; partial content is persisted |
| Empty retrieval | abstain path, no hallucinated answer |
| DB down | `/readyz` red, chat returns 503, frontend shows a banner rather than a white screen |
| Corpus not ingested | `/readyz` reports `chunks: 0` and the UI prompts `make ingest` |
| Malicious artifact HTML | sanitized, sandboxed, and reported |

---

## 12. Tests

`pytest` + `httpx.AsyncClient` against a throwaway test schema. A `FakeProvider` fixture
means **the whole suite runs with no Ollama and no API key** — critical, because the
evaluator will run `make test` on a cold machine.

- `test_chunker` — boundaries, overlap, determinism, timestamp extraction
- `test_retrieve` — RRF fusion order, episode diversity cap, abstain threshold, empty corpus
- `test_router` — intent classification over fixtures
- `test_providers` — registry selection, availability probing, fallback chain
- `test_sessions` — **cross-session context isolation** (called out in the rubric)
- `test_persistence` — message + citation + artifact round-trip
- `test_chat_stream` — SSE event order and shape
- `test_sanitizer` — payload table: `<script>`, `onerror=`, `javascript:` href,
  `<svg onload>`, `<iframe>`, `<form action>`, CSS `expression()`, `@import`
- `test_ship30_validator` — rubric pass and fail cases
- `test_errors` — every failure row in §11 returns the structured envelope

Target around 30 tests. Frontend: 2–3 Vitest + RTL specs on the artifact pane, plus
`docs/manual-test-plan.md` with numbered UI steps and expected results.

---

## 13. Schedule

`T0` is the start of work. The deadline is roughly `T0 + 34h` wall clock; this assumes
about 22 working hours.

| Block | Hours | Work | Done when |
|---|---|---|---|
| P0 | 0.0–0.75 | Repo, compose, `.env.example`, config, logging, `/healthz`, `/readyz` skeleton, alembic | `docker compose up` is green |
| P1 | 0.75–3.0 | Ingest CLI, chunker, fastembed, schema, hybrid retrieve, `POST /api/search`, tests | `/api/search` returns sane cited chunks for "how do I find PMF" — **no LLM yet** |
| P2 | 3.0–5.5 | Provider protocol, Ollama provider, router, orchestrator, SSE chat, persistence | curl a question, get a streamed cited answer |
| P3 | 5.5–8.0 | Frontend: sessions sidebar, chat, streaming, source cards, provider badge, states | full loop usable in a browser |
| P4 | 8.0–10.0 | Artifact envelope parsing, sanitizer + report, `ArtifactFrame`, viewer tabs, tests | a malicious payload renders inert with a visible report |
| P5 | 10.0–12.0 | `ship30-essay`: principles extraction, outline→sections generation, validator, repair loop | a 1,250-word grounded essay from the 3B model |
| P6 | 12.0–14.0 | `AnthropicAgentProvider` + `OpenAICompatProvider`, runtime toggle, fallback chain, resilience matrix | provider switch works live in the UI |
| P7 | 14.0–17.0 | `PRD.md`, `architecture.md` with diagrams, `design.md`, `README.md` | a stranger could run it |
| P8 | 17.0–19.0 | Test hardening, **fresh-clone rehearsal in a clean directory**, agent-transcript export + redaction | `git clone` → `make bootstrap` works from scratch |
| P9 | 19.0–20.0 | Demo video (2–3 min, camera on), upload, submit | form submitted |
| — | 20.0–22.0 | Buffer | |

**Cut line, in the order things get dropped if behind:**

1. Frontend unit tests → manual test plan only
2. Artifact version history → latest version only
3. `OpenAICompatProvider` → Anthropic + Ollama only
4. `AnthropicAgentProvider` → shipped with fixture tests, documented as not live-tested
5. Full-corpus ingest → 60 episodes, with the CLI proving the refresh path

Never cut: the sanitizer, session isolation, the abstain gate, `/readyz`, the README, the video.

**P8 is not optional.** The brief says "verify that a fresh evaluator can clone the
repository and run the solution using only your documented steps." Budget the two hours;
a broken cold start undoes everything above it.

---

## 14. Deliverables checklist

| # | Deliverable | Where | Notes |
|---|---|---|---|
| 1 | Public GitHub repo | `Aayushs1602/Agentic-chatbot` | verify it is public; grep for secrets before the final push |
| 2 | `README.md` | root | architecture summary, prerequisites, install, env table, **both** model setups, run, test, troubleshooting table |
| 3 | PRD | `docs/PRD.md` | must include the §2 Forward Deployment Brief: user, problem, success metric, assumptions, scope in/out, risks |
| 4 | `design.md` | `docs/` | principles, IA, interaction states (§9), responsive behaviour, a11y, decisions and rejected alternatives |
| 5 | `architecture.md` | `docs/` | schema, endpoints, component boundaries, ingest/retrieval flow, routing, model toggle, security, topology |
| 6 | Agent transcripts | `agent-transcripts/` | export from `~/.claude/projects/D--oogway-labs-fde/`, redact, **include the failures** |
| 7 | Tests | `backend/tests/`, `docs/manual-test-plan.md` | `make test` must pass with no Ollama and no keys |
| 8 | Demo video | YouTube, linked in README | 2–3 min, camera on, show Ollama running locally, cover the D1 trade-off |

**Success metrics for the PRD** — pick one or two and state them measurably:

- *Product*: at least 90% of answers to a 20-question golden set carry at least one
  resolved citation, and the abstain path fires on 5 of 5 out-of-corpus questions. Build
  the golden set in P1 — it doubles as a regression test.
- *Operational*: time from `git clone` to first grounded answer is under 10 minutes on a
  clean machine.

---

## 15. Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| 3B model output quality on a 1,250-word essay | **High** | outline-then-sections generation, programmatic rubric, one repair pass (§7) |
| Agent SDK vs. Ollama requirement conflict | **High** | adapter boundary (D1), stated openly in the PRD and video — a documented trade-off scores better than a hidden fudge |
| Time — 1.5 days for 8 deliverables | **High** | cut line (§13); docs written from this plan, not from scratch |
| Cold-start failure at evaluation | High | the P8 fresh-clone rehearsal is mandatory |
| Hallucination | High | abstain gate, marker resolution, source cards |
| Untrusted artifact HTML | High | sanitize + sandbox + CSP (§8) |
| Prompt injection via transcripts | Medium | delimited data framing; routing decided pre-retrieval |
| Ingest slower than expected | Medium | `--limit`, resumable by content hash |
| GPU OOM at 8k context | Medium | 3B Q4 default, configurable `num_ctx`, documented CPU fallback |
| Secrets leaking into agent transcripts | Medium | redaction script plus manual review before commit |

---

## 16. Decision record (resolved 27 Aug 2026)

| Question | Answer | Consequence |
|---|---|---|
| Working hours available | **~12–14h** | Cut line (§13) applies from the start, not as a fallback. Revised schedule below. |
| Cloud LLM | **Free tier only — Gemini via `OpenAICompatProvider`** | `$0` total. `AnthropicAgentProvider` ships implemented + fixture-tested, marked `available: false` in `/readyz` without a key, and the gap is stated plainly in the README and PRD. |
| Frontend stack | Vite + React + TS + Tailwind + shadcn/ui | As per §9. |
| Corpus | **Full 269 episodes**, `--limit` on the ingest CLI | Evaluator gets a working system in <1 min via `LIMIT=20`; full corpus is one command away. |

### Revised 13-hour schedule (supersedes §13)

| Block | Hours | Work |
|---|---|---|
| P0 | 0.0–0.75 | Scaffold: compose, config, logging, errors, migrations runner, `/healthz` + `/readyz` |
| P1 | 0.75–3.0 | Ingest CLI, chunker, fastembed, hybrid retrieve, `POST /api/search`, golden set, tests |
| P2 | 3.0–5.0 | Provider protocol, Ollama provider, router, orchestrator, SSE chat, persistence |
| P3 | 5.0–7.0 | Frontend: sessions, streaming chat, source cards, provider badge, states |
| P4 | 7.0–8.5 | Artifact envelope, sanitizer + report, sandboxed viewer, tests |
| P5 | 8.5–10.0 | `ship30-essay` skill: principles, outline→sections, validator, repair loop |
| P6 | 10.0–10.75 | Gemini provider, Anthropic adapter (fixture tests), runtime toggle, fallback chain |
| P7 | 10.75–12.5 | PRD, architecture.md, design.md, README |
| P8 | 12.5–13.5 | Fresh-clone rehearsal, agent-transcript export + redaction |
| P9 | 13.5–14.0 | Demo video, upload, submit |

**Dropped up front under the cut line:** frontend unit tests (manual test plan instead),
artifact version history (latest only), live Anthropic Agent SDK demonstration.

### Environment notes (this machine)
- `make` is **not installed** — the `Makefile` is shipped for the evaluator (who will
  likely have it); the README documents the raw `docker compose` equivalent of every
  target, and `scripts/` carries cross-platform helpers.
- Docker Desktop must be started manually before `docker compose up`.
- Backend code stays **Python 3.10-compatible** so the test suite runs natively on this
  machine without Docker, even though the image is 3.12.
