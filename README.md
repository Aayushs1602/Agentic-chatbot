# The Lenny Growth Assistant

A grounded assistant over [Lenny's Podcast transcripts](https://github.com/ChatPRD/lennys-podcast-transcripts).
Every answer cites the episode and timestamp it came from. When the corpus
doesn't cover a question, it says so instead of guessing.

Runs entirely on your machine — local model, local database, no API keys, **$0**.

```bash
git clone https://github.com/Aayushs1602/Agentic-chatbot.git
cd Agentic-chatbot
cp .env.example .env
docker compose up -d --build
docker compose exec backend python -m app.rag.ingest --limit 20
open http://localhost:5173
```

---

## Contents

- [What it does](#what-it-does)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Verifying it works](#verifying-it-works)
- [Architecture in one page](#architecture-in-one-page)
- [Configuration](#configuration)
- [Switching models](#switching-models)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [What is deliberately not here](#what-is-deliberately-not-here)
- [Documentation](#documentation)

---

## What it does

| | |
|---|---|
| **Grounded answers** | Hybrid retrieval over 303 episodes / 18,806 passages, with per-claim citations that deep-link to the second of the episode. |
| **Honest refusal** | When the transcripts don't answer a question, it says so rather than answering from general knowledge. |
| **Ship 30 for 30 essays** | A ~1,250-word essay skill with the source's principles encoded, plus a machine-checked rubric and a repair pass. |
| **Artifacts** | Markdown and HTML documents rendered beside the chat in a sandboxed viewer that shows what it blocked and why. |
| **Model choice** | Ollama (local, default), any OpenAI-compatible cloud endpoint, or the Claude Agent SDK — switchable in the UI. |
| **Visible reasoning** | The agent's steps stream live: classify, search, check relevance, apply skill, verify citations. |

## Prerequisites

| | | |
|---|---|---|
| **Docker Desktop** | required | Postgres, backend, frontend |
| **Ollama** | required for the local demo | [ollama.com](https://ollama.com) — runs on the **host**, not in Docker |
| ~6 GB disk | | model 2 GB, corpus 250 MB, images ~1.5 GB, index ~1 GB |
| ~8 GB RAM | | 4 GB VRAM is enough; the default model is sized for it |

No API keys are needed. `make`, Python, and Node are **not** required — everything
runs in containers, and every `make` target's raw `docker compose` equivalent is
given below.

## Setup

**1. Pull the model** (on the host, so it can use your GPU):

```bash
ollama pull qwen2.5:3b-instruct-q4_K_M
```

**2. Start the stack:**

```bash
cp .env.example .env          # works unedited
docker compose up -d --build  # make up
```

**3. Ingest transcripts.** The repo is cloned automatically on first run.

```bash
# ~1 minute — enough to try it out
docker compose exec backend python -m app.rag.ingest --limit 20

# ~25 minutes — all 303 episodes, 18,806 passages
docker compose exec backend python -m app.rag.ingest
```

Ingestion is **idempotent and resumable**: each episode is content-hashed, so
re-running skips unchanged work. Interrupt it freely. That is also the refresh
path — `--refresh` pulls the transcripts repo first, then ingests only what changed.

**4. Open http://localhost:5173.**

### Not using Docker for the backend?

```bash
cd backend
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://lenny:lenny@localhost:5432/lenny
export OLLAMA_BASE_URL=http://localhost:11434
uvicorn app.main:app --reload
```

## Verifying it works

**`/readyz` is the endpoint to check first.** It reports every dependency
separately, and `degraded` names the exact next action for anything wrong:

```bash
curl -s http://localhost:8000/readyz | python -m json.tool
```

```json
{
  "status": "ready",
  "database": { "reachable": true },
  "corpus": { "episodes": 303, "chunks": 18806 },
  "providers": [
    { "id": "ollama", "model": "qwen2.5:3b-instruct-q4_K_M", "available": true },
    { "id": "cloud", "available": false, "reason": "CLOUD_API_KEY is not set",
      "hint": "Get a free Gemini key at https://aistudio.google.com/apikey" }
  ],
  "degraded": []
}
```

**Retrieval without a model in the loop.** When an answer looks wrong, this
separates "retrieval found the wrong passages" from "the model misused good
passages" in one request:

```bash
curl -s -X POST http://localhost:8000/api/search \
  -H 'Content-Type: application/json' \
  -d '{"q": "how do I know when I have product-market fit", "k": 5}' | python -m json.tool
```

Interactive API docs: **http://localhost:8000/docs**

### Things worth trying

| Ask | What to watch |
|---|---|
| *"How do I know when I've found product-market fit?"* | agent steps, then citations that deep-link to timestamps |
| *"What is the weather in Mumbai tomorrow?"* | the refusal path — it explains what it searched and why it declined |
| *"Write a Ship 30 for 30 essay about pricing"* | outline → section-by-section streaming → rubric check |
| *"Make me an HTML one-pager about hiring PMs"* | the artifact viewer opens beside the chat |
| Click a `[S1]` chip | jumps to the source card |

## Architecture in one page

```
  React + Vite (nginx)  ──►  FastAPI  ──►  Orchestrator
                                              │
                        route ─► retrieve ─► check relevance ─► skill ─► generate ─► verify
                                              │
                                    LLMProvider port
                        ┌─────────────────────┼─────────────────────┐
                     Ollama              OpenAI-compatible     Claude Agent SDK
                     (local)             (Gemini / Groq)       (own agent loop)

  Postgres 17 + pgvector — episodes, chunks, sessions, messages, artifacts, tool_calls
```

**The agent layer is ours; providers are adapters behind it.** The brief asks
for the Claude Agent SDK *and* for the demo to run on local Ollama. Those cannot
be one code path — Anthropic doesn't support routing the SDK to non-Claude
models, and a 3B model can't drive that tool protocol anyway. So the
orchestrator owns control flow, and the same `SKILL.md` files are loaded
natively by the Agent SDK or rendered into a prompt by everything else. One
skill definition, two runtimes. Full reasoning in
[`docs/architecture.md`](docs/architecture.md).

## Configuration

Every variable is documented in [`.env.example`](.env.example), which works
unedited. The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` \| `cloud` \| `anthropic` |
| `OLLAMA_MODEL` | `qwen2.5:3b-instruct-q4_K_M` | sized for 4 GB VRAM |
| `OLLAMA_NUM_CTX` | `8192` | Ollama defaults to 4096 and truncates **silently** |
| `OLLAMA_KEEP_ALIVE` | `30m` | cold load measured ~77 s; this avoids paying it twice |
| `CLOUD_API_KEY` | *(empty)* | optional — free Gemini key enables the cloud provider |
| `ANTHROPIC_API_KEY` | *(empty)* | optional — enables the Claude Agent SDK path |
| `EMBEDDINGS_MODEL` | `snowflake/snowflake-arctic-embed-xs` | CPU/ONNX, 384-dim |
| `CHUNK_TOKENS` | `400` | must stay under the encoder's 512-token window |
| `RETRIEVAL_MIN_SIM` | `0.35` | a **safety floor**, not the abstain gate — see below |
| `DATABASE_URL` | local Postgres | swap one line for Supabase or Railway |

**Never commit `.env`.** It is gitignored; `.env.example` is the template.

## Switching models

**In the UI** — click the provider badge in the header. Unavailable providers stay
listed with the reason and the fix.

**By configuration** — set `LLM_PROVIDER` and restart.

**Cloud (free):** get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey),
set `CLOUD_API_KEY`, restart. Groq and OpenAI work by changing `CLOUD_BASE_URL`
and `CLOUD_MODEL` — nothing else.

**Fallback:** with `PROVIDER_FALLBACK=true`, an unavailable provider falls
through to the next healthy one. That is surfaced in the UI and recorded on the
message — answering from a different model than you selected is never silent.

**Other local models:**

```bash
ollama pull llama3.2:3b          # then set OLLAMA_MODEL
ollama pull qwen2.5:7b-instruct-q4_K_M   # better, ~6 tok/s on 4 GB VRAM
```

## Tests

**231 tests. No Docker, no Ollama, and no API keys required** — that is the
contract, so `make test-local` works on a cold machine.

```bash
docker compose exec backend python -m pytest    # make test
cd backend && python -m pytest                  # make test-local
```

Covered: chunking, hybrid fusion and the diversity cap, intent routing and the
relevance gate, citation resolution, the Ship 30 rubric, artifact extraction,
provider selection and fallback, config validation, and **26 XSS payloads**
against the artifact sanitizer.

UI steps are in [`docs/manual-test-plan.md`](docs/manual-test-plan.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `/readyz` says `corpus: empty` | Not ingested yet — `docker compose exec backend python -m app.rag.ingest --limit 20` |
| Provider badge is red | Ollama isn't running. `ollama serve` on the **host**, not in Docker. |
| `Model 'qwen2.5:3b…' is not pulled` | `ollama pull qwen2.5:3b-instruct-q4_K_M` |
| Backend restart-loops on boot | A config validator rejected `.env`. `docker compose logs backend` names the variable and the fix. |
| First answer takes ~80 s | Cold model load. `OLLAMA_WARMUP=true` does it at startup instead; subsequent answers are 5–15 s. |
| Answers stream in one lump | A proxy is buffering SSE. The bundled nginx sets `proxy_buffering off`. |
| Assistant refuses an answerable question | Check `/api/search` — usually retrieval, not the model. Widen `--limit` or ingest fully. |
| `docker compose exec` says "container is restarting" | Backend crashed on startup; see the logs line above. |
| Ingest interrupted | Just re-run it. Completed episodes are skipped. |

Every error response carries a `request_id` that appears in the logs:

```bash
docker compose logs backend | grep <request_id>
```

## What is deliberately not here

Each of these was a decision, not an oversight — reasoning in
[`docs/PRD.md`](docs/PRD.md).

- **No authentication.** An anonymous cookie scopes the session list. This is an
  internal single-team tool; accounts would cost a day and change nothing about
  what is being evaluated.
- **No cross-encoder reranking.** RRF fusion is good enough at this corpus size.
  First thing to add.
- **No cloud deployment.** The brief asks for a local deployment, and Ollama
  cannot run on a free tier — so local is both the requirement and the $0 option.
- **The Claude Agent SDK path is implemented and fixture-tested, but has not been
  run against the live API.** This build targets zero cost and no Anthropic
  credit was available. Set `ANTHROPIC_API_KEY` to exercise it.
- **The 3B model under-uses bold**, so the Ship 30 rubric's emphasis check often
  fails and the essay ships with a visible warning. That is the rubric working,
  not the essay failing — a larger model passes it.

## Documentation

| Document | What's in it |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | user, problem, success metrics, assumptions, scope, risks |
| [`docs/architecture.md`](docs/architecture.md) | schema, endpoints, retrieval, agent loop, security, topology |
| [`docs/design.md`](docs/design.md) | UI principles, information architecture, states, accessibility |
| [`docs/retrieval-calibration.md`](docs/retrieval-calibration.md) | the measurements behind the model choice and the abstain gate |
| [`docs/manual-test-plan.md`](docs/manual-test-plan.md) | numbered UI test steps |
| [`agent-transcripts/`](agent-transcripts/) | coding-agent sessions, including what went wrong |
| [`PLAN.md`](PLAN.md) | the implementation plan this was built against |
