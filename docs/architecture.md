# Architecture

How the system is put together, and why each boundary is where it is.

---

## 1. Topology

```
┌──────────────────────────────────────────────────────────────────┐
│ docker compose                                                   │
│                                                                  │
│  ┌────────────────┐      ┌──────────────────┐    ┌────────────┐  │
│  │ frontend       │      │ backend          │    │ db         │  │
│  │ nginx + React  │─────▶│ FastAPI          │───▶│ Postgres17 │  │
│  │ :5173          │ /api │ :8000            │    │ + pgvector │  │
│  └────────────────┘      └────────┬─────────┘    └────────────┘  │
└───────────────────────────────────┼──────────────────────────────┘
                                    │ host.docker.internal:11434
                            ┌───────▼────────┐
                            │ Ollama (HOST)  │   GPU-attached
                            │ qwen2.5:3b     │
                            └────────────────┘
```

**Ollama runs on the host, not in Compose.** GPU passthrough into Docker Desktop
on Windows and macOS is unreliable, and the demo needs the GPU. The backend
reaches it via `host.docker.internal`, which `extra_hosts` makes work on Linux
too. A CPU-only `--profile ollama` service exists for evaluators who would
rather not install it.

**nginx proxies `/api` same-origin.** The anonymous session cookie stays
first-party and no CORS preflight is involved. `proxy_buffering off` is
load-bearing: without it nginx buffers the SSE stream and answers arrive in one
lump at the end.

---

## 2. Component boundaries

```
app/
├── api/          HTTP only. Validation, SSE framing, status codes.
├── agent/        The agent loop. Knows nothing about HTTP or providers.
│   ├── orchestrator.py   control flow
│   ├── router.py         intent + relevance (structured output)
│   ├── ship30.py         essay rubric + section writer
│   ├── artifacts.py      envelope parsing
│   ├── citations.py      marker resolution
│   └── skills.py         SKILL.md loading
├── providers/    Adapters behind one port. Swappable, no shared state.
├── rag/          Ingestion, chunking, embeddings, retrieval.
├── security/     Artifact sanitization.
└── db/           asyncpg pool, SQL migrations, repository.
```

The rule that keeps this honest: **`agent/` imports `providers/base` but never a
concrete provider**, and `api/` never imports `rag/` directly. So the agent loop
is testable with a fake provider and no database — which is why 243 tests run
with no Ollama and no keys.

---

## 3. Database schema

Postgres 17 + pgvector. Migrations are plain SQL in
`app/db/migrations/`, applied in filename order by a ~40-line runner and
recorded in `schema_migrations`.

**Why not Alembic or an ORM.** The hybrid retrieval query mixes pgvector
operators with `tsvector` ranking, which reads far better as SQL than as ORM
expressions, and the migration file doubles as this documentation. Alembic buys
autogeneration and downgrades that a forward-only schema doesn't need, at the
cost of a second source of truth.

**Rollback:** forward-only. Restore from a `pg_dump`, or add a compensating
migration.

```
episodes ──1:N──► chunks
    │
sessions ──1:N──► messages ──1:N──► tool_calls
    │                 │
    └──1:N──► artifacts
```

| Table | Purpose | Notes |
|---|---|---|
| `episodes` | one row per transcript | `content_sha256` makes re-ingest idempotent |
| `chunks` | retrievable passages | `vector(384)` + generated `tsvector`; `start_seconds` powers deep links |
| `sessions` | one chat | anonymous `user_id` cookie, no auth |
| `messages` | turns | `citations` jsonb, plus provider/model/latency/tokens per row |
| `artifacts` | generated documents | **both** raw and sanitized, plus the removal report |
| `tool_calls` | agent step trace | written on every provider path |
| `ingest_runs` | ingestion observability | surfaced at `/api/ingest/status` |

**Indexes:**

```sql
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);  -- dense
CREATE INDEX ON chunks USING gin  (tsv);                          -- sparse
```

HNSW rather than IVFFlat: better recall at this size and no training step
before the index is usable.

**Session isolation** is enforced at the query layer — every message read is
scoped by `session_id`, and `ON DELETE CASCADE` cleans up dependents. There is a
test asserting no cross-session leakage rather than trusting it.

---

## 4. Ingestion

```
git clone --depth 1 ──► parse frontmatter ──► chunk ──► drop ads ──► embed ──► upsert
                              │                                                  │
                        content_sha256 ────── unchanged? skip ───────────────────┘
```

`python -m app.rag.ingest [--limit N] [--force] [--refresh]`

- **Idempotent and resumable.** Each episode is content-hashed; unchanged
  episodes are skipped. Interrupting and re-running resumes. This is also the
  refresh mechanism.
- **Per-episode transactions.** Chunks are replaced atomically, so an interrupt
  can never leave an episode half-indexed.
- **Stable ordering.** `--limit 20` selects the same 20 episodes everywhere, so
  an evaluator's results match the documented ones.
- **Throughput:** ~5 s/episode, ~25 min for all 303 (18,806 chunks).

### Chunking

400 tokens, 80 overlap, aligned to speaker turns and sentence boundaries.

**Chunks must fit the encoder.** Sentence-transformer models truncate silently at
512 tokens. The original 800-token chunks were embedded only for their first
~512 — a third of every chunk retrievable by keyword and invisible to vector
search, with no error. A config validator now rejects
`CHUNK_TOKENS > 0.8 × max_seq_len` at startup.

**Both ends snap to boundaries.** Snapping only the end while advancing the start
arithmetically made every chunk after the first begin mid-word, corrupting both
the embedding and the text a citation quotes.

### Ad filtering

Sponsor reads are dense marketing copy in exactly the business vocabulary this
corpus is queried with — a Vanta ad scored **0.710** as the top source for
"hiring your first product manager". Detection uses sponsor vanity URLs
(`vanta.com/lenny`) or two independent call-to-action signals, flagging ~3% of
chunks. One signal alone is deliberately insufficient: a guest naming a company
is real content, and a false positive silently deletes knowledge.

`scripts/prune_ads.py` applies the rules to an existing index without a re-embed.

---

## 5. Retrieval

```
query ──┬─► dense  (pgvector cosine, top 80) ──┐
        └─► sparse (ts_rank_cd, top 80)     ───┴─► RRF ─► cap 3/episode ─► top 8
```

**Why hybrid.** Dense handles paraphrase ("how do I know I've got PMF" →
"product-market fit"); sparse handles rare literal tokens — company names,
frameworks, metrics — which is exactly what people ask a podcast corpus about.
Postgres ships both, so this costs one index rather than another service.

**Why RRF.** Cosine and `ts_rank_cd` are on incomparable scales, so weighted
blending needs a normalisation constant that must be retuned whenever either
retriever changes. RRF uses only ranks: `score = Σ 1/(60 + rank)`. No tuning,
and it degrades gracefully when one retriever returns nothing.

**The sparse query must be OR, not AND.** `websearch_to_tsquery` joins terms with
AND, so a natural-language question required every term to co-occur in one
passage. Measured: **0 matches** for "How do I know when I've actually found
product-market fit?" while the pipeline logged success — hybrid retrieval was
silently dense-only for every query. `build_sparse_query` now emits an OR clause
over non-stopword terms; `ts_rank_cd` still rewards passages matching more terms
more densely.

**HNSW search width.** `hnsw.ef_search` is set per connection to
`max(64, candidates × 2)`. pgvector's guidance is `ef_search >= LIMIT`; measured
recall@80 at the current corpus size is 100% even at the default 40, so this is
insurance against corpus growth rather than a present fix.

**No reranker.** One was benchmarked and rejected on measurement — see
[`retrieval-calibration.md`](retrieval-calibration.md) §5.

**Diversity cap.** Max 3 passages per episode. Without it one long on-topic
episode floods every slot and the answer cites a single guest as though it were
consensus.

**Embedding model** — `snowflake/snowflake-arctic-embed-xs`, 384-dim, CPU/ONNX.
Chosen by measurement, not reputation; see
[`retrieval-calibration.md`](retrieval-calibration.md). CPU is deliberate: on a
4 GB GPU, sharing VRAM with the chat model forces a swap on every query.

---

## 6. The agent loop

```
route ──► retrieve ──► check relevance ──► apply skill ──► generate ──► verify
  │            │              │                  │             │           │
  └─ JSON      └─ hybrid      └─ JSON schema     └─ SKILL.md   └─ stream   └─ resolve
     schema       search         (abstain gate)                              markers
```

Deterministic control flow, model-driven decisions. The model decides *what kind
of request this is*, *whether the corpus answers it*, and *what to say*. The
orchestrator decides *what happens next*.

**No function calling on the local path.** At 4 GB VRAM the ceiling is a 3B Q4
model, and 3B function calling is unreliable. Instead: intent classification via
Ollama's `format: <json-schema>` constrained decoding — measured **4/4** on the
golden cases — and retrieval that always runs rather than being a tool the model
may forget to call. Tool *calls* are still recorded in `tool_calls` on every
path, so observability doesn't depend on which model answered.

**Routing runs before retrieval, on user text only.** This is a security
property: transcript content can never influence which tool or skill fires, so a
guest saying "ignore your instructions" on-air cannot redirect the agent.

**Grounding is enforced twice** — the relevance gate before generation, and
mechanical citation resolution after it. Invented markers are stripped; an
answer left with zero resolvable citations is *replaced*, not shown with a
warning. A fluent uncited answer is the exact failure this product exists to
prevent.

### Skills

A skill is `SKILL.md` (frontmatter + procedure) plus, where it matters, a
programmatic rubric in code. That combination is the point — only the
instructions survive being written as a one-off prompt.

`ship30-essay` is the full form: principles extracted from the source, an
outline-then-sections writer, a rubric checking length, headline, sections,
bullets, bold ratio, resolved citations and section balance, and one targeted
repair pass. Every failing check carries the fix it wants, because the repair
prompt consumes them.

The **same** `SKILL.md` files serve both runtimes: loaded natively by the Claude
Agent SDK from `.claude/skills/`, rendered into a system prompt by everything
else.

---

## 7. Provider layer

```
                 Orchestrator
                      │
              LLMProvider (port)
   ┌──────────────────┼──────────────────┐
Ollama          OpenAI-compatible    Anthropic
(local)         (Gemini/Groq/OpenAI) (+ AgenticProvider)
```

**The brief asks for two things that fight.** Build the agent layer on the Claude
Agent SDK; run the demo on local Ollama. Anthropic's documentation states that
routing the SDK to non-Claude models through a gateway is unsupported, and a 3B
model cannot drive that tool protocol regardless. One code path cannot be both.

**Resolution:** the agent layer is ours, providers are adapters, at two levels.
`LLMProvider` (stream + structured JSON) is what the deterministic pipeline uses,
so it runs unchanged everywhere. `AgenticProvider.run_agent` is an *optional*
capability that only the Anthropic adapter implements, exposing Claude's own
agent loop with real tools and native skill loading.

This is the headline trade-off, stated openly rather than hidden.

**Selection and fallback.** `ProviderRegistry` probes availability concurrently
and never raises — an unreachable provider is a fact to report, which is what
`/readyz` and the UI badge render. With `PROVIDER_FALLBACK=true` an unavailable
provider falls through to the next healthy one, and that is logged, recorded on
the message row, and surfaced in the stream. Silently answering from a different
model than the user selected is its own kind of failure.

---

## 8. API

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | liveness; touches no dependency |
| GET | `/readyz` | per-dependency readiness + `degraded[]` next actions |
| GET | `/api/providers` | availability, reasons, hints |
| POST | `/api/providers/active` | switch at runtime |
| POST | `/api/sessions` | new chat |
| GET | `/api/sessions` | scoped by anonymous cookie |
| GET/PATCH/DELETE | `/api/sessions/{id}` | |
| GET | `/api/sessions/{id}/messages` | history |
| **POST** | **`/api/sessions/{id}/messages`** | **SSE stream** |
| GET | `/api/sessions/{id}/artifacts` | sanitized |
| GET | `/api/artifacts/{id}` | sanitized + report |
| GET | `/api/artifacts/{id}/raw` | `text/plain`, never `text/html` |
| POST | `/api/search` | raw retrieval, no model |
| GET | `/api/ingest/status` | last run |
| GET | `/api/skills` | loaded skills |

**SSE events:** `meta`, `tool`, `token`, `replace`, `citations`, `artifact`,
`done`, `error`.

`replace` exists because grounding can only be judged *after* generation
completes, by which point tokens have already reached the client. Rather than
leave a plausible uncited answer on screen, the server tells the client to
discard it.

**Every non-2xx** uses one envelope:

```json
{"error": {"code": "provider_unavailable",
           "message": "Ollama is not reachable at http://…",
           "detail": {"hint": "Run `ollama serve`"},
           "request_id": "01J…"}}
```

`code` is stable and machine-readable; `detail.hint` is the operator's next
action; `request_id` appears on every log line for that request.

---

## 9. Artifact security

Two independent layers. Neither is trusted alone, because the model producing
this HTML has just read hundreds of pages of third-party text.

**Layer 1 — server-side allowlist** (`nh3`/ammonia), applied on the way *into*
the database, so no read path can serve unsanitized content by forgetting a step.

| Permitted | Removed |
|---|---|
| structure, text, tables, lists | `<script>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, `<link>`, `<base>`, `<meta>`, `<svg>` |
| inline `<style>` and `style=` | all `on*` handlers |
| `data:image/*` images | `javascript:`, `vbscript:`, `data:text/html` |
| `https:` links (`rel` injected) | CSS `expression()`, `@import`, external `url()`, `-moz-binding` |

Stripped tags have their **contents** removed too — ammonia's default is to
unwrap and keep the text, which would leave `alert(1)` sitting in the document.

**Layer 2 — render isolation:**

```html
<iframe sandbox srcdoc="…"></iframe>
<!-- CSP inside: default-src 'none'; style-src 'unsafe-inline'; img-src data: -->
```

No `allow-same-origin` → opaque origin → no access to cookies, `localStorage`,
or the parent DOM. No `allow-scripts` → nothing executes. `default-src 'none'` →
no network egress, so there is no channel to exfiltrate over even if code ran.

The "Allow scripts" toggle defaults **off** and adds `allow-scripts` only —
never together with `allow-same-origin`, the one combination that defeats the
sandbox entirely, because a script could then reach out and remove its own
sandbox attribute.

**The sanitizer explains itself.** Removals are recorded and shown in the viewer
("4 elements removed — details"). 26 payloads are tested, each asserting both
that the construct is gone *and* that its removal is reported.

**Markdown takes a different path** — `react-markdown` with raw HTML disabled.
Keeping the two separate means loosening one can never widen the other.

---

## 10. Observability

Structured JSON logs with a `request_id` bound to every line and propagated
through orchestrator → provider → retrieval.

| Event | Carries |
|---|---|
| `request` | method, path, status, duration |
| `retrieval_ok` / `retrieval_abstain` | query, candidate counts, best cosine, episodes |
| `ollama_complete` | model, tokens in/out, duration, finish reason |
| `citations_resolved` | resolved, invented, unused |
| `artifact_sanitized` | what was removed |
| `ship30_evaluated` | every rubric check |
| `provider_fallback` | requested vs. used, and why |

Every LLM call, retrieval, and artifact is also persisted — `messages` carries
provider, model, latency and token counts per turn; `tool_calls` carries the
full agent trace. So a bad answer can be diagnosed after the fact from the
database alone, without reproducing it.

---

## 11. Failure behaviour

| Failure | Response |
|---|---|
| DB unreachable | `/readyz` red, 503 with hint, UI banner — app still starts, so it can *report* the outage |
| Ollama down | 503 + hint, or fallback if enabled |
| Model not pulled | detected at probe time, not mid-generation |
| Model timeout | `error` event; partial answer persisted |
| Empty retrieval | abstain path |
| Corpus not ingested | `/readyz` reports `chunks: 0`; UI prompts the command |
| Missing API key | provider marked unavailable, hidden from the toggle |
| Malformed artifact fence | recovered — unclosed fences, loose JSON, wrapped envelopes |
| Malicious artifact HTML | sanitized, sandboxed, reported |
| Client disconnects | generation aborted, so an abandoned tab doesn't hold the GPU |
| Invalid config | startup fails loudly, naming the variable and the fix |
