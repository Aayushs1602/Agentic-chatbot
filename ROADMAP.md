# AI Platform Roadmap

**Working document.** Tracks the evolution of the Lenny Growth Assistant into an
AI infrastructure platform. `main` holds the frozen FDE take-home submission;
platform work lands on `LLM_gateway` and its descendants.

Companion to `PLAN.md` (which describes the take-home as built). Where the two
disagree, this file is newer.

---

## 0. Ground rules

| Decision | Choice | Why |
|---|---|---|
| **Repo** | Evolve in place, extract later | `LLMProvider` is already a port. Extraction against a real interface at P12 beats guessing at one now. |
| **Work split** | Mixed by phase | Plumbing gets implemented for me; genuinely new distributed-systems concepts are handed over as guided exercises. Marked **[you]** below. |
| **Durable state** | Postgres | Job queue via `FOR UPDATE SKIP LOCKED`, usage ledger, prompt registry. No new service, no hosting cost. |
| **Ephemeral state** | Redis (compose) | Rate-limit buckets, provider health, pub/sub. Free locally; kept behind an interface so a Postgres-only deploy stays possible. |
| **Isolation** | API keys + Postgres RLS | Database-enforced, not `WHERE`-clause-enforced. |
| **Budget** | $0 | Everything runs in `docker compose`. Cloud provider keys optional. |

---

## 0.1 The exercise template

Every **[you]** item is an exercise, not a ticket. "Implement a circuit breaker"
teaches you what a circuit breaker is. Implementing one, then watching your own
implementation stampede a recovering provider under concurrency, teaches you why
the naive version is wrong — and that second thing is the one that survives.

So each exercise runs all ten steps, grouped into five movements:

**LEARN**
1. **Concept** — what the thing is, in one sentence you could say out loud.
2. **Why it exists** — the class of problem it solves.
3. **Failure scenario** — what breaks *in this codebase* without it. Specific
   file, specific numbers, not "the system could become unstable."

**DESIGN**
4. **Design it yourself** — answer the listed questions in writing *before*
   opening an editor. The design questions are where the real decisions live;
   if you skip to code you will make them by accident.

**BUILD**
5. **Implement**
6. **Write tests** — for the behaviour you designed, not for the code you wrote.

**BREAK**
7. **Break it intentionally** — a specific, scripted experiment.
8. **Observe the failure** — write down what actually happened. Half the value
   is in being surprised.
9. **Fix it**

**DOCUMENT**
10. **Explain the tradeoff** — a short section in `docs/`. What did you give up?
    Under what conditions is the opposite choice correct?

Step 7 is not optional and not theoretical. An exercise where nothing broke means
the experiment was too gentle, not that the implementation was perfect.

Steps 3 and 7 look similar and are not: 3 is the motivating story you reason
about beforehand, 7 is the reproduction you run afterwards against your own code.
When 7 disagrees with 3, 7 is right.

**Where the write-ups go:** `docs/exercises/<phase>-<name>.md`. Each is short —
the design answers from step 4, what you observed at step 8, and the tradeoff
from step 10. Twelve of these at the end is a more convincing artifact than the
code, because they show reasoning rather than output.

Exercises are marked 🧪 below.

---

## 1. Phase list

Each phase maps to one or more of the 15 target systems. All 15 are covered.
P14 adds no new system — it validates every one of them.

| Phase | Name | Systems | Exercises | Size | Status |
|---|---|---|---|---|---|
| **P0** | Baseline & known-issue cleanup | #4, #10, #14 (existing) | — | ~0.5d | **done** |
| **P1** | Tenancy, auth, rate limit, usage | #1a, #2, #8 | 2 | ~3d | RLS live; ledger + Ex.2 left |
| **P2** | Routing & resilience | #15 | 1 | ~2d | [ ] |
| **P3** | Prompt & config registry | #9 | 1 | ~2d | [ ] |
| **P4** | Context assembly & token budget | #13 | 1 | ~2d | [ ] |
| **P5** | Semantic cache | #5 | 1 | ~2d | [ ] |
| **P6** | Guardrails middleware | #14 | — | ~1.5d | [ ] |
| **P7** | Eval in CI | #10 | — | ~2d | [ ] |
| **P8** | Observability: spans, cost, dashboard | #11 | — | ~3d | [ ] |
| **P9** | Async agent jobs | #6 | 2 | ~3d | [ ] |
| **P10** | Webhook & event fan-out | #12 | 1 | ~2d | [ ] |
| **P11** | Streaming hardening | #3 | 1 | ~1.5d | [ ] |
| **P12** | Gateway extraction | #1b | 1 | ~3d | [ ] |
| **P13** | Tool execution sandbox | #7 | 1 | ~4d | [ ] |
| **P14** | Integration, chaos & demo | validates all | — | ~3d | [ ] |

Twelve exercises total. Sizes assume evenings/weekends and the mixed split, and
include the exercise overhead — steps 7 through 10 are roughly a third of each
phase. They are estimates, not commitments.

P6, P7 and P8 carry no exercise on purpose: P6 is a refactor of code you already
wrote, P7 is CI plumbing, P8 is aggregation queries and a dashboard. Nothing in
them will surprise you. Adding a manufactured exercise there would be busywork.

### Why this order, not the original

Four changes from the roadmap this was derived from, each for a dependency reason:

1. **Tenancy moved from 6th to 1st.** Rate-limit buckets, the usage ledger, and
   the cache key all need `tenant_id`. Building them first means retrofitting a
   column through three subsystems.
2. **Semantic cache moved from 5th to after the registry and context assembly.**
   The answer is not a function of the query alone — it depends on retrieved
   chunks, history, active skill, and prompt version. A cache keyed on the query
   embedding serves confidently wrong answers. The key needs P3's prompt version
   and P4's context fingerprint to exist first.
3. **The standalone gateway service moved from 1st to 12th.** In-process gateway
   concerns land in P1/P2; the network boundary gets drawn at P12, when there is
   a second consumer and a proven interface. See §3.
4. **The sandbox stays last and gains a prerequisite.** There is no code
   execution in this codebase to sandbox. P13 must first add a tool that
   genuinely needs execution.

---

## 2. Phases in detail

### P0 — Baseline & known-issue cleanup  **done**

Nothing new gets built. This closes live defects found while auditing, so later
phases start from a clean baseline.

- [x] TTL-cache `provider.status()` (`PROVIDER_HEALTH_TTL_S`, default 10s).
      `resolve()` called it on **every chat request**, and `ollama.py:75` makes a
      live `GET /api/tags` — so every turn paid a network round-trip before
      generation started. `/providers` passes `force=True` so a manual refresh
      still tells the truth; `/readyz` was already independent via `probe.py`.
- [x] Probe fallback candidates concurrently, not serially. Selection still
      follows the configured order, so concurrency changed how long it takes to
      find out and never which provider wins — asserted by its own test.
- [x] **Single-flight probes** (not in the original scope, added because the
      cache is wrong without it). A cold cache under load had every in-flight
      request start its own probe, which is the same stampede moved to the
      moment the TTL expires. Concurrent callers now join one task.
- [x] `invalidate()` for when reality contradicts the cache. Unused until P2,
      where the breaker needs it.
- [x] Baseline: **336 passed, 4 skipped**. After P0: **347 passed, 4 skipped**.
- [x] Verified the new tests fail against the old behaviour: serial probing
      measured 0.33s against a 0.25s threshold, and defeating single-flight
      produced 10 probes where 1 was asserted. A test that has never failed has
      never been tested.

**Already done, carried forward — do not rebuild:**

- **#4 RAG serving** — HNSW + generated `tsvector` + RRF + per-episode diversity
  cap, in `rag/retrieve.py`. Plus `docs/retrieval-calibration.md`, which records
  parent-chunk retrieval *regressing* the golden set 80% to 60% and keeps it
  behind a default-off flag. Lead with that; a documented negative result is
  rarer than a working feature, and it is the template every exercise write-up
  in this roadmap should follow.
- **#10 eval harness** — `scripts/evaluate.py`, 20 cases, grounded + refusal rates.
- **#14 guardrails** — `security/sanitize.py` (nh3), the relevance gate, and the
  `replace` retract path in the orchestrator.
- **#3 streaming, most of it** — `api/chat.py:142`, a closed 8-event SSE
  contract with disconnect cancellation at `:180`.

**Exit:** one fewer round-trip on the hot path; suite green.

---

### P1 — Tenancy, auth, rate limit, usage  [ ]

*Systems #1 (in-process gateway concerns), #2 (metering), #8 (multi-tenancy).*
Merged because in this codebase they are one change.

**Migration `002_tenancy.sql`**

- [x] `tenants` (+ `rpm_limit`/`rpd_limit` overrides, NULL = use plan default)
- [x] `api_keys` (id, tenant_id, key_prefix, key_hash, name, created_at,
      last_used_at, revoked_at)
- [x] `usage_events` table created; the write path lands with the ledger,
      after RLS. `cost_micros` as `bigint` — money in floats is a bug you do not
      find in one row, you find it in the monthly total.
- [x] `tenant_id uuid` on `episodes`, `chunks`, `sessions`, `messages`,
      `artifacts`, `tool_calls`, each with an FK and a tenant-leading index
- [x] Seed a `default` tenant, backfill, then `SET NOT NULL`. Written as
      `ADD COLUMN NOT NULL DEFAULT <literal>` then `DROP DEFAULT`: catalog-only
      on PG11+, so ~19k chunks backfill with no table rewrite, and afterwards an
      INSERT that forgets a tenant fails loudly instead of landing in `default`.
- [x] Seed a `dev` tenant. **Not** a fixed key — a key hash in a migration is a
      credential in git, and "it's only for dev" is what gets it into staging.
      `DEV_API_KEY` is seeded from the environment at startup instead.

`chunks` carries its own `tenant_id` rather than inheriting through `episodes`:
one column on ~40k rows buys an RLS policy and a vector query that filter
without a join.

**Auth & keys**

- [x] Key generation + hashing (`security/api_keys.py`). SHA-256, not bcrypt:
      32 CSPRNG bytes have no guessing attack to slow down, so a slow KDF would
      add ~100ms to every authenticated request and buy nothing.
- [x] `require_tenant` / `optional_tenant` (`api/deps.py`). Every rejection is
      byte-identical — separating unknown from revoked from suspended hands an
      unauthenticated caller a free enumeration oracle.
- [x] `tenant_id` contextvar in `logging.py`, injected into every log line and
      **reset per request** in middleware, not merely set at auth time.
- [x] Key CRUD in `api/keys.py` plus `GET /whoami`. Not `admin.py`, which
      documents itself as read-only corpus inspection.

**Write paths & enforcement** — done after RLS landed

- [x] `repository.py` sets `tenant_id` on all four INSERTs (sessions, messages,
      tool_calls, artifacts). `002` had made the column NOT NULL and dropped its
      default, so every write had been raising `NotNullViolationError` — the app
      could not start a conversation while 400 tests passed.
- [x] `app/tenancy.py:current_tenant()` — raises `TenantRequiredError` (500)
      rather than guessing. A silent default here writes one tenant's data into
      another's account and nothing downstream ever flags it.
- [x] `ingest.py` binds `INGEST_TENANT_SLUG` for the run, since a CLI has no
      request to inherit from, and fails loudly on an unknown slug.
- [x] `require_tenant` enforced at the **router** level in `main.py` for chat,
      search, artifacts and admin — so a new endpoint in those files is
      protected by default. Health and providers stay open: no tenant data, and
      /readyz must answer while the database is down.
- [x] `tests/test_repository_writes.py` (5 tests) — the gap that let the NOT NULL
      break through. Verified non-vacuous: reverting `create_session` to its
      broken form fails all 5.

**Usage ledger**

- [ ] Provider price map (per-model, input/output, in micros)
- [ ] Write `usage_events` on the `done` path in `api/chat.py`, where
      `tokens_in`/`tokens_out` already land
- [ ] `GET /usage`, `GET /usage/{tenant}` with date-range aggregation

> ✅ **Verified against live Postgres.** `002_tenancy.sql` applied onto the
> existing populated database (303 episodes, 18,503 chunks) — so the real path,
> not an empty schema. All six `tenant_id` columns `NOT NULL` with the default
> dropped, six FKs present, every row backfilled to `default`. Auth proved end
> to end over HTTP: 401 for missing / malformed / unknown keys, 200 for a real
> one, key minting works, health endpoints stay open, and zero secret tails
> appear in the logs.
>
> Two defects the pure-unit suite could not have caught, both found by running it:
>
> 1. `authenticate` bound its staleness window as the string `"60 seconds"`.
>    asyncpg types `$n::interval` as a real interval and raised `DataError` on
>    the first call. Every unit test passed, because none reach Postgres. Now a
>    `timedelta`, with `db`-marked regression tests.
> 2. **The `db`-marked tests had never all passed together.** asyncpg binds a
>    pool to its creating event loop; pytest-asyncio gives each test a fresh
>    loop; so from the second db test onward everything inherited a pool from a
>    closed loop (`RuntimeError: Event loop is closed`). Three `test_catalog.py`
>    tests were failing this way and it was invisible while Postgres was down.
>    Fixed with an `app_db` fixture in conftest.py — **use it for the RLS tests**,
>    or you will spend an evening debugging Postgres instead of your policies.

#### 🧪 Exercise 1 — Postgres Row-Level Security  **[you]**

> ### ✅ Done — four traps, all hit on real hardware
>
> 1. **Owners bypass RLS.** Policy correct, `ENABLE` on, and ACME's row came
>    back anyway: `lenny` owns the tables. Fixed by `FORCE`.
> 2. **Superusers bypass it even with FORCE.** The Docker image makes
>    `POSTGRES_USER` a superuser (`rolbypassrls = t`), so no table-level setting
>    could ever have worked. Fixed by a dedicated `lenny_app` role — DML only,
>    no `CREATE` on the schema, so it cannot redefine the accessor the policies
>    call.
> 3. **`SET` leaks across pooled connections.** Measured: a second request that
>    set *nothing* still counted ACME's row. Fixed by `SET LOCAL` inside an
>    explicit transaction (`_tenant_scope()` in `db/pool.py`).
> 4. **`SET LOCAL` resets to `''`, not NULL** — and `''::uuid` *raises*. The
>    policy returned a 500 from the security layer instead of zero rows. Fixed
>    by `app_current_tenant()`, which catches `invalid_text_representation` and
>    returns NULL. A security boundary answers "none"; it never throws.
>
> Traps 1 and 2 were predicted below. **Traps 3 and 4 were not** — they were
> found by running the thing, which is the entire argument for step 7.
>
> Scope: seven tables (the six plus `usage_events`), `ENABLE` + `FORCE` + one
> policy each. `tenants` and `api_keys` are deliberately excluded — auth must
> read a key row *before* it knows which tenant is asking, so a policy there
> makes every key look invalid.
>
> `tests/test_rls.py` (14 tests) runs as `lenny_app` via `SET LOCAL ROLE`.
> Without that it would pass against a database with no policies at all.
> Verified non-vacuous: dropping `FORCE` fails 1 test; making the accessor raise
> instead of failing closed fails 3. Suite: **400 passed** with the DB up.
>
> **Last mile closed.** Migrations run on owner credentials via a dedicated
> connection in `migrate.py`; the pool authenticates as `lenny_app` through
> `APP_DATABASE_URL`. Verified end to end: `current_user = lenny_app`,
> `rolsuper = false`, default tenant sees 32 sessions, acme sees 1, a
> cross-tenant fetch by explicit id returns 404 rather than a filtered row, and
> `CREATE TABLE` from the app is refused.
>
> `APP_DATABASE_URL` blank still falls back to the owner URL so an existing
> checkout boots — but startup then logs `rls_not_enforced`, because that
> configuration has every policy in place and none of them applying.
>

**LEARN.** *(1) Concept* — the database decides which rows a session can see,
using a policy evaluated per row against a connection-local variable. *(2) Why*
— `WHERE tenant_id = $1` is a convention, and a convention is one forgotten
clause away from a cross-tenant leak. RLS makes the leak impossible even from a
raw `psql` session holding the app's own credentials. *(3) Without it* — your
hybrid query in `rag/retrieve.py` is ~50 lines mixing a pgvector CTE with a
tsvector CTE before RRF fusion. Add the tenant filter to the dense side, forget
the sparse side, and tenant A's keyword hits enter tenant B's fusion. Nothing
errors. The answer reads normally. You find out from a customer.

**DESIGN.** *(4)* Answer in writing first: Which role does the app connect as,
and does that role own the tables? What is the GUC named, and why must it be
`SET LOCAL` rather than `SET`? Which tables need `FORCE ROW LEVEL SECURITY` and
why is `ENABLE` alone insufficient here? How does `db/migrate.py` keep working —
it must bypass RLS to run DDL. What happens to a request with no tenant (health
checks, `/readyz`)?

**BUILD.** *(5)* Policies on all six tenant-scoped tables, plus the `SET LOCAL`
transaction wrapper in `db/pool.py` — note that this forces read paths to open a
transaction they do not currently have. *(6) Tests* — set tenant A, query a row
you know belongs to B, assert zero rows. Then assert the *same* query as a
bypassing role returns one row, proving the row exists and RLS hid it. Without
that second assertion you are only testing that your fixtures are empty.

**BREAK.** *(7)* Two experiments. **(a)** Drop `FORCE ROW LEVEL SECURITY` and
re-run the suite. **(b)** Change `SET LOCAL` to `SET`, set `DB_POOL_MAX=1`, and
issue two requests as different tenants back to back. *(8) You should see* —
**(a)** every test still passes, because the app role owns the tables and owners
bypass RLS; your policies were decorative and nothing told you. That silence is
the lesson. **(b)** the second request reads the first request's tenant: the
pooled-connection GUC leak, reproduced on demand. *(9) Fix* — restore both, and
add a test that would have caught (a).

**DOCUMENT.** *(10)* RLS costs a transaction on every read and applies a
predicate per row, which changes some plans. Measure the hit on the hybrid
retrieval query specifically — it is your most expensive read. Then write down
where you would draw the line: when is app-level filtering the right call, and
what would have to be true for you to accept it?

#### 🧪 Exercise 2 — Token-bucket rate limiter  **[you]**

The `RateLimiter` interface, DI wiring and test harness are mine; both
implementations are yours.

**LEARN.** *(1) Concept* — a bucket of capacity N refills at R tokens/second; a
request costs one token and is rejected when the bucket is empty. *(2) Why* — it
permits a burst up to N while bounding the sustained rate, which matches how
people actually use an API. *(3) Without it* — a fixed window admits 2× the limit
across a boundary: 100 requests at 11:59:59 and 100 at 12:00:00 is 200 in one
second, and on a 4GB GPU serving a 3B model that is a queue nobody drains.

**DESIGN.** *(4)* Where does refill happen — a background job, or lazily on read
from `(tokens, last_refill_at)`? Lazy is correct; be able to say why. How do you
make check-and-decrement a single atomic operation in each backend? What does
`Retry-After` contain, and can you compute it exactly rather than guessing? Do
the per-minute and per-day buckets belong in the same store?

**BUILD.** *(5)* Redis implementation and Postgres implementation.
*(6) Tests* — burst to capacity then assert 429; advance time and assert
recovery; assert `Retry-After` matches the real wait.

**BREAK.** *(7)* Fire 50 concurrent requests at a bucket of capacity 10, using a
deliberately non-atomic read-then-write. *(8) You should see* — well over 10
admitted. Every one of the 50 read `tokens=10` before any of them wrote back:
the lost-update race, which is the entire reason this exercise exists. *(9) Fix*
— collapse it to one atomic operation. In Redis that means a Lua script (or
`INCR` with an expiry, if you can argue the semantics); in Postgres it means a
single `UPDATE ... SET tokens = ... RETURNING`, never `SELECT` then `UPDATE`.

**DOCUMENT.** *(10)* Redis is fast and loses every bucket on restart, handing
each tenant one free burst. Postgres is durable and puts a write on the hot path
of every request. Which failure do you prefer — and does your answer change
between the per-minute bucket and the per-day one? It should.

**Order within the phase**, to keep the suite green throughout:
schema + backfill, then auth, then RLS, then rate limit, then usage.

The existing `_user_id` cookie survives, demoted to a *within-tenant* user
identity. It was never a security boundary and now it no longer needs to be.

**Exit:** two tenants cannot see each other's sessions, messages, or chunks —
proven by a test that sets the wrong `tenant_id` and gets zero rows, not by code
review. Every request is metered. Exceeding a quota returns `429`.

---

### P2 — Routing & resilience  [ ]  *(#15)*

The registry already resolves providers with an ordered, opt-in fallback chain
and surfaces `fell_back_from` to the UI. This makes it a router.

- [ ] Promote P0's status cache into a real health checker with background refresh
- [ ] Routing policy: cheap model for simple intents, strong model for complex,
      driven by the router's existing intent classification
- [ ] Cost-aware routing using P1's price map
- [ ] Latency-aware routing (rolling p95 per provider)
- [ ] Per-provider timeout and retry budgets
- [ ] Record every routing decision, so `/usage` can answer "what did fallback
      cost me?"

#### 🧪 Exercise 3 — Circuit breaker  **[you]**

**LEARN.** *(1) Concept* — a state machine wrapping a dependency that stops
calling it after repeated failures, so you fail fast instead of paying the
timeout every time. Three states: CLOSED (traffic flows, failures counted), OPEN
(all calls rejected immediately, no traffic reaches the dependency), HALF-OPEN
(a limited probe decides whether to close or re-open). *(2) Why* — a slow
dependency is more dangerous than a dead one, because it consumes your resources
while failing. *(3) Without it* — `ollama_timeout_s` is 120. Ollama dies; twenty
users send a message; each request holds a worker for two minutes waiting on a
socket that will never answer. Your uvicorn workers are exhausted, `/healthz`
stops responding, and the entire platform looks down because one provider is.
The failure spread from the thing that broke to the thing that didn't.

**DESIGN.** *(4)* Write the state table before any code — states down the side,
events across the top, transitions in the cells. Then answer: What counts as a
failure? A connection timeout, clearly; a 400 from a malformed prompt, clearly
not — that is your bug, not the provider's, and counting it opens the breaker on
a dependency that is perfectly healthy. Does the breaker open on N consecutive
failures or on a failure *rate* over a window, and which behaves better under
low traffic? How long does OPEN last, and does the cooldown back off on repeated
failed probes? In HALF-OPEN, how many probes are allowed, and what do all the
other concurrent requests get?

**BUILD.** *(5)* One breaker per provider, wired into `registry.resolve()`.
*(6) Tests* — CLOSED→OPEN at the threshold; stays OPEN through the cooldown;
OPEN→HALF-OPEN on expiry; HALF-OPEN→CLOSED on probe success; HALF-OPEN→OPEN on
probe failure.

**BREAK.** *(7)* **The concurrency test.** Drive the breaker to OPEN, wait out
the cooldown, then fire ten simultaneous requests at the exact instant it enters
HALF-OPEN. *(8) You should see* — all ten reach the provider. Each read the state
before any of them wrote it, so each concluded it was the probe. You have just
re-stampeded a dependency at the precise moment it was weakest, which is worse
than having no breaker at all: you queued the load *and then* released it in one
burst. Every naive implementation has this bug, including the one you are about
to write. *(9) Fix* — entering HALF-OPEN must atomically issue exactly one probe
token. Every other caller fails fast as though the breaker were still OPEN. If
your fix uses a lock, check what it does to the CLOSED path throughput.

**DOCUMENT.** *(10)* A breaker that opens fast protects you and converts a
two-second blip into a full cooldown of hard failure for every user. Justify your
threshold and cooldown against your actual failure profile, which is not generic:
local Ollama has a ~77s cold start after `ollama_keep_alive` expires — so a
too-eager breaker will open on a *warming* provider and refuse traffic to
something that was about to work. A cloud provider's 429 has a completely
different recovery shape. Should the two providers share a policy?

**Exit:** kill Ollama mid-demo; requests fail over within one request, the
breaker opens, and traffic stops probing a dead provider on every call.

---

### P3 — Prompt & config registry  [ ]  *(#9)*

Prompts currently live in `SKILL.md` files and Python string constants. This
makes them versioned data.

- [ ] `prompts` / `prompt_versions` tables (id, version, body, model,
      temperature, max_tokens, metadata, created_by, created_at)
- [ ] Immutable versions; a mutable `production` pointer per prompt
- [ ] Resolution API + cache, so the hot path is not a DB read per turn
- [ ] Rollback: repoint `production`, no deploy
- [ ] Ingest the existing `SKILL.md` files as v1 without changing behaviour
- [ ] Stamp `prompt_version` on `messages` — this is what makes P5's cache key
      correct and P7's evals attributable

#### 🧪 Exercise 4 — Deterministic A/B assignment  **[you]**

**LEARN.** *(1) Concept* — bucket a subject into a variant by hashing a stable
identifier, so the assignment is a pure function rather than a coin flip.
*(2) Why* — experiments need each subject to see one variant consistently, and
need the assignment to be reproducible after the fact. *(3) Without it* —
`random() < 0.1` evaluated per turn. In a six-turn conversation the user gets v2
for turns 1–3 and v3 for turn 4, with accumulated history written under a
different prompt. The answers stop cohering, and the user reports "it got worse
halfway through," which is true and unreproducible. Meanwhile your eval
attributes turns to variants at random, so the measured difference between v2 and
v3 is noise you will confidently interpret.

**DESIGN.** *(4)* What is the hash input — tenant, user, session, or prompt id,
and in what combination? What property do you need when the split changes from
90/10 to 80/20 (hint: only the intended 10% should move buckets; if everyone
reshuffles, your in-flight experiment is destroyed). Should the same user land in
the same bucket for two *different* prompts? If yes, you have correlated your
experiments and cannot attribute an effect to either — is that what you want?

**BUILD.** *(5)* Stable hash → integer → bucket. *(6) Tests* — same input yields
the same bucket across 10,000 calls; distribution is within tolerance of the
configured split; moving 90/10 to 80/20 reassigns only the intended slice.

**BREAK.** *(7)* Implement it with Python's built-in `hash()` on a string, then
restart the process and re-run the assignment for the same user. Then run two
uvicorn workers and hit the same user through both. *(8) You should see* — the
bucket changes across restarts and differs between workers, because
`PYTHONHASHSEED` is randomised per process. Your "deterministic" split is
deterministic only within one process lifetime, which is the worst kind of
nondeterminism: it passes every local test. *(9) Fix* — a stable digest
(`hashlib.sha256`) or an explicitly seeded algorithm.

**DOCUMENT.** *(10)* Hashing on session gives coherent conversations and noisier
aggregates; hashing on tenant gives clean aggregates and means a whole tenant
sees the experimental prompt. Pick one, and state the conditions under which you
would switch.

**Exit:** change a prompt and roll it back without a deploy; every stored message
records which version produced it.

---

### P4 — Context assembly & token budget  [ ]  *(#13)*

The orchestrator assembles context implicitly. This makes it explicit and bounded.

- [ ] `ContextAssembler` with a declared budget per section (system, history,
      retrieved, tool results, reserved output)
- [ ] Real token counting, not word-count estimates — the chunker's approximation
      is fine for chunking and not fine for a budget
- [ ] History compaction — summarise older turns rather than truncating
- [ ] Emit a **context fingerprint** (stable hash of the assembled context).
      P5 depends on this.
- [ ] Record per-section token counts in the trace

#### 🧪 Exercise 5 — Eviction policy under budget pressure  **[you]**

**LEARN.** *(1) Concept* — when assembled context exceeds the window, something
must be dropped, and *what* you drop is a product decision wearing an
infrastructure costume. *(2) Why* — the window is a hard physical limit and
inputs are unbounded. *(3) Without it* — `ollama_num_ctx` is 8192.
`retrieval_top_k` is 8 at `chunk_tokens` 400, so retrieval alone can reach ~3200,
plus a skill body, plus six turns of history, plus reserved output. Overflow, and
Ollama silently drops the *tail* of the prompt. Depending on your assembly order
that tail may be the user's actual question — so the model answers a question it
was never shown, fluently, using context it was.

**DESIGN.** *(4)* Rank the sections by droppability and defend the ranking: is
the 8th retrieved chunk worth more or less than turn 1 of the conversation? Does
each section get a floor below which you refuse rather than degrade? Do you drop
a whole chunk or truncate it — noting that a truncated chunk still carries its
citation marker, so you would be citing a source for text the model never saw.
What happens when even the floors do not fit?

**BUILD.** *(5)* The policy. *(6) Tests* — several over-budget shapes; assert the
final size and that no section falls below its floor.

**BREAK.** *(7)* Run the full golden set with the budget deliberately tightened
until eviction fires on every case. *(8) You should see* — which cases fail, and
more importantly *how*. A grounded rate that drops because the model started
abstaining is a system degrading correctly. A grounded rate that holds while
answers get subtly wrong is a system fabricating from partial context, and it is
much worse than the first because your existing metric cannot see it. *(9) Fix*
— eviction that cuts into the retrieval floor should trigger abstention, not a
confident answer over a partial corpus.

**DOCUMENT.** *(10)* You already have the methodology for this in
`docs/retrieval-calibration.md` — produce the same kind of table: budget vs
grounded rate vs abstention rate vs mean latency. Aggressive eviction is cheaper
and less grounded; name the exchange rate you are willing to accept.

**Exit:** no turn exceeds its budget; the trace shows where every token went.

---

### P5 — Semantic cache  [ ]  *(#5)*

Deliberately after P3 and P4, because the cache key needs both.

- [ ] Cache table: query embedding, response, citations, tenant_id, model,
      temperature, prompt_version, context_fingerprint
- [ ] Hard tenant scoping in the lookup — enforced by RLS, not by a `WHERE`
- [ ] TTL + invalidation on re-ingest (a cached answer citing a deleted chunk is
      worse than a cache miss)
- [ ] Hit/miss/saved-cost metrics into P1's ledger

#### 🧪 Exercise 6 — Similarity threshold calibration  **[you]**

**LEARN.** *(1) Concept* — a cache hit is a nearest-neighbour lookup over query
embeddings with a cosine threshold, not an exact key match. *(2) Why* — natural
language never repeats verbatim, so an exact-match cache on a chat product hits
approximately never. *(3) Without a calibrated threshold* — 0.85 sounds sensible
and will match *"how should I price my product"* to *"how should I price my
product for enterprise"*. Those have different answers and different citations,
so the cache serves a confident, well-cited, wrong response — and because it is
cached, it serves the same wrong response every time, deterministically. This is
strictly worse than a hallucination, which at least varies enough to get noticed.

**DESIGN.** *(4)* What is in the key besides the embedding, and why does each
element belong there — tenant, model, temperature, `prompt_version` from P3,
`context_fingerprint` from P4? Should the threshold vary by intent? Your router
already classifies, and a catalogue lookup tolerates a much looser match than a
grounded-answer question. What is the failure mode of caching an *abstention*?

**BUILD.** *(5)* Lookup and write paths. *(6) Tests* — curated similar pairs hit,
curated different pairs miss, and a differing `prompt_version` always misses.

**BREAK.** *(7)* Run the full golden set at thresholds 0.80, 0.85, 0.90, 0.95 —
twice each, cold then warm, so the second pass can actually hit. *(8) You should
see* — a threshold where the hit rate looks excellent and the grounded rate has
quietly fallen. Find it. That point is the entire exercise; everything above it
is a cache and everything below it is a wrong-answer generator with good latency.
*(9) Fix* — select the threshold maximising hit rate **subject to** no grounded-
rate regression, not the one maximising hit rate.

**DOCUMENT.** *(10)* Write it up like §7 of `retrieval-calibration.md`, table
included: threshold vs hit rate vs grounded rate vs cost saved. If the honest
conclusion is "no threshold beats not caching on this corpus," publish that. It
is the same shape as your parent-chunk finding and just as valuable.

**Exit:** a measured hit rate and a measured cost saving, with **no drop** in
grounded rate.

---

### P6 — Guardrails middleware  [ ]  *(#14)*

Mostly a refactor — the pieces exist, scattered. Cheap, and it makes the
security story legible. No exercise: this is code you already wrote, moved.

- [ ] Extract sanitisation, the relevance gate, and the retract path into an
      ordered, declared middleware chain
- [ ] Inbound: PII detection, prompt-injection heuristics, policy checks
- [ ] Outbound: grounding verification (exists), PII redaction, schema validation
- [ ] Per-tenant policy configuration, stored via P3's registry
- [ ] Record every guardrail decision in the trace
- [ ] A test corpus of injection attempts, run in CI

**Exit:** guardrails are a declared, inspectable, per-tenant chain rather than
behaviour distributed across four modules.

---

### P7 — Eval in CI  [ ]  *(#10)*

`scripts/evaluate.py` exists and needs a live model, so it sits outside pytest by
design. This makes it a gate. No exercise: this is CI plumbing.

- [ ] Per-dimension metrics, not just grounded/refusal: retrieval recall@k,
      citation accuracy, refusal precision, p95 latency
- [ ] Expand the golden set well beyond 20 cases
- [ ] GitHub Actions job running against a containerised small model
- [ ] Store results per commit; fail the build on regression against baseline
- [ ] Report per prompt version (from P3), so an eval result is attributable
- [ ] Trend view — the interesting artifact is the graph over time

**Exit:** a prompt change that lowers the grounded rate fails CI instead of
reaching main.

---

### P8 — Observability  [ ]  *(#11)*

`tool_calls` + `/messages/{id}/trace` give per-turn traces. Missing: hierarchy,
cost, and aggregation. No exercise: aggregation queries and a dashboard.

- [ ] Spans with parent/child, not a flat call list
- [ ] OpenTelemetry semantics (even if the exporter stays Postgres — the point is
      the vocabulary)
- [ ] Cost per trace, from P1's ledger
- [ ] Aggregation: p50/p95/p99 latency, error rate, fallback rate, cache hit rate,
      cost per tenant per day
- [ ] Dashboard: extend `AdminDashboard.tsx` rather than adding Grafana
- [ ] Slow-turn and cost-spike detection

This phase is the instrument panel for P14. Build it knowing that every chaos
scenario has a "Metrics" column, and that column has to be fillable from here.

**Exit:** answer "why was yesterday 3x more expensive?" from the dashboard, in
under a minute, without grepping logs.

---

### P9 — Async agent jobs  [ ]  *(#6)*

Moves the agent off the HTTP request lifecycle. Postgres-backed;
`FOR UPDATE SKIP LOCKED` is a real queue and costs nothing to run.

- [ ] `jobs` table with status, attempts, checkpoint payload, tenant_id
- [ ] `POST /agent/jobs`, `GET /agent/jobs/{id}`, `DELETE` for cancellation
- [ ] Checkpoint after each orchestrator step, so a crash resumes mid-turn
- [ ] Retries with exponential backoff + jitter
- [ ] Dead-letter queue with a replay path
- [ ] Cooperative cancellation

#### 🧪 Exercise 7 — Job claiming with SKIP LOCKED  **[you]**

**LEARN.** *(1) Concept* — `SELECT ... FOR UPDATE SKIP LOCKED` lets each worker
claim rows no other worker holds, turning a table into a work queue without a
broker. *(2) Why* — the alternative is a message broker you would have to run,
and Postgres is already here. *(3) Without it* — `SELECT ... LIMIT 1` followed by
`UPDATE ... SET status='running'` races: two workers read the same row, both
claim it, both run the same agent turn. The tenant is billed twice for one job
and two assistant messages land in one session, which the UI renders as the
assistant answering itself.

**DESIGN.** *(4)* One statement or two? What is the visibility timeout — how do
you reclaim a job whose worker was `kill -9`'d while holding it, given the lock
died with the connection but `status='running'` did not? Is `status` sufficient,
or do you need `claimed_at` plus a reaper? How many jobs should a worker claim
per poll, and what does batching cost you when one job in the batch is slow?

**BUILD.** *(5)* Claim, heartbeat, reap. *(6) Tests* — single-worker happy path,
then reclaim-after-death.

**BREAK.** *(7)* Four workers, 100 jobs, all started simultaneously. Run it three
ways: with `SKIP LOCKED`, with `FOR UPDATE` alone, and with no locking at all.
*(8) You should see* — exactly 100 executions in the first; correct-but-serialised
in the second, with throughput collapsing as workers queue behind each other's
locks; duplicates in the third. Record the throughput numbers, not just the
correctness verdict — the middle case is the one people ship by accident because
it is *correct*. *(9) Fix* — if the first case did not give you exactly 100,
find out why before moving on.

**DOCUMENT.** *(10)* `SKIP LOCKED` provides no ordering guarantee across workers:
job 5 can complete before job 1. When does that matter for an agent platform, and
what would ordering cost? Also record where this stops scaling — polling a table
is fine at your volume and is not fine at some volume. Name it.

#### 🧪 Exercise 8 — Idempotency across a mid-call crash  **[you]**

**LEARN.** *(1) Concept* — an operation is idempotent when performing it twice
has the same effect as performing it once; where that is not natural, an
idempotency key makes it so. *(2) Why* — every durable queue is at-least-once,
so retries are guaranteed, so double side effects are guaranteed unless you
prevent them. *(3) Without it* — worker claims a job, calls the LLM (2 seconds,
real money), and is `kill -9`'d before writing the result. The reaper reclaims
the job. The LLM is called again. The tenant is billed twice for one answer. Now
add P10 and the same crash fires the completion webhook twice, so the customer's
system creates two records for one event.

**DESIGN.** *(4)* The general shape is *write intent before acting, then record
the outcome atomically with clearing the intent*. Apply it: walk your
orchestrator's steps and classify each as replayable-for-free or not. Routing
and retrieval are deterministic given the same inputs and cost nothing to redo.
Generation, billing, and webhook delivery are none of those things. That
classification **is** the exercise — the code that follows is mechanical.
Then: what is the key derived from, and where is it stored so that the storing
is in the same transaction as the effect?

**BUILD.** *(5)* Idempotency keys on each non-replayable effect; check-then-act
inside one transaction. *(6) Tests* — simulate a crash at each step boundary and
assert exactly-once side effects at each.

**BREAK.** *(7)* Run 50 jobs and `kill -9` the worker at randomised points inside
each. *(8) You should see* — count `usage_events` rows and webhook deliveries
against jobs actually completed. Any excess is your bug, and the excess will not
be zero on the first attempt. *(9) Fix*, then re-run the same 50.

**DOCUMENT.** *(10)* Exactly-once across a process boundary and a network is
impossible; what you built is effectively-once via keys. Write down which of your
side effects are naturally idempotent, which are made idempotent by a key, and
which remain honestly at-least-once with the contract pushed to the consumer —
the webhook is the last category, and P10 has to say so in its docs.

**Exit:** `kill -9` a worker mid-job; the job resumes from its last checkpoint on
another worker without duplicating side effects.

---

### P10 — Webhook & event fan-out  [ ]  *(#12)*

Needs P9 — there are no events to fan out until jobs exist.

- [ ] `webhook_endpoints` + `webhook_deliveries` tables
- [ ] Event types: `job.completed`, `job.failed`, `usage.threshold_exceeded`
- [ ] HMAC signing with a timestamp, and a documented verification recipe
- [ ] Delivery worker with retry + backoff
- [ ] Per-endpoint circuit breaking (reuse P2's breaker)
- [ ] Delivery log + manual replay

#### 🧪 Exercise 9 — At-least-once delivery and the consumer contract  **[you]**

**LEARN.** *(1) Concept* — you cannot guarantee exactly-once delivery to a remote
endpoint, so you guarantee at-least-once and make the receiver's job possible by
signing, sequencing, and identifying every event. *(2) Why* — the network can
fail after the consumer commits and before the ack reaches you; you cannot tell
that case apart from a genuine failure, so you must retry, so duplicates are a
certainty rather than a risk. *(3) Without it* — you retry a delivery whose 200
was lost in transit, the customer's system provisions the same thing twice, and
because your delivery log says "failed then succeeded" you have no idea it
happened.

**DESIGN.** *(4)* What goes in the signature, and why must the timestamp be
inside the signed payload rather than beside it? What is your replay window and
what attack does it close? Does the event carry a monotonic sequence per
endpoint, and what should a consumer do with an out-of-order event? What is the
retry schedule, when does a delivery dead-letter, and does a dead-lettered event
block subsequent events for that endpoint (ordered) or not (throughput)?

**BUILD.** *(5)* Signing, delivery worker, retry, dead-letter, replay.
*(6) Tests* — signature verification round-trip; retry schedule; dead-letter
after N; replay produces an identical signed payload.

**BREAK.** *(7)* Point at a deliberately hostile endpoint: returns 500 five
times then 200; accepts but never responds (hits your timeout after committing);
returns 200 twice for the same delivery. Also: replay an event from the log and
verify the signature against the original. *(8) You should see* — the middle case
is the interesting one. The consumer committed and you recorded a failure, so you
retried, so it committed twice. That is at-least-once made concrete, and no
amount of retry tuning fixes it. *(9) Fix* — you cannot fix it on your side. The
fix is the contract: a stable event id the consumer deduplicates on, and
documentation that says so in plain language.

**DOCUMENT.** *(10)* Write the consumer-facing page: how to verify a signature,
why you must deduplicate on event id, what your retry schedule is, and what
happens after dead-lettering. Then note what you gave up by not building ordered
delivery per endpoint, and what it would have cost.

**Exit:** point at a deliberately flaky endpoint; deliveries retry, back off,
eventually dead-letter, and replay cleanly.

---

### P11 — Streaming hardening  [ ]  *(#3)*

The SSE contract, disconnect cancellation, and in-stream error events already
exist. This is the remaining 20%.

- [ ] Resume via `Last-Event-ID` — requires buffering recent events per stream
- [ ] Multi-worker fan-out (a stream started on worker A, resumed on worker B) —
      this is where Redis pub/sub earns its place
- [ ] Heartbeat events to survive intermediary idle timeouts
- [ ] Explicit stream timeout and cleanup

#### 🧪 Exercise 10 — Backpressure  **[you]**

**LEARN.** *(1) Concept* — backpressure is what a producer does when the consumer
cannot keep up. *(2) Why* — an unbounded buffer between a fast producer and a slow
consumer is a memory leak with extra steps. *(3) Without it* — a phone on a bad
connection reads your SSE stream at a trickle while Ollama generates at full
speed. Tokens pile up in the server-side buffer for that one stream. Fifty such
clients and the process is killed by the OOM reaper — and note that
`request.is_disconnected()` at `api/chat.py:180` does not help you here at all,
because a *slow* client is not a *disconnected* client. Your existing protection
does not cover this case, which is why it is worth doing.

**DESIGN.** *(4)* Bound the buffer — then what happens when it fills? Three
options and all three are bad: drop events (unacceptable, they are tokens of an
answer), block the producer (correct-looking, but on the Ollama path it holds a
4GB GPU hostage to one slow phone), or terminate the stream with a clear error.
Pick one, and say what changes your mind. Does the answer differ for the local
provider and a cloud one? What is the threshold — buffer size, or time-since-last-
successful-write?

**BUILD.** *(5)* Bounded buffer plus the chosen policy. *(6) Tests* — a
deliberately slow consumer; assert bounded memory and the designed outcome.

**BREAK.** *(7)* A client that reads one byte per second, against a full-length
generation. Watch RSS per stream, then run twenty of them. *(8) You should see* —
how fast memory grows per stream and where the process gives up. Get the actual
number; "it grows" is not an observation. *(9) Fix.*

**DOCUMENT.** *(10)* Blocking the producer protects memory and holds GPU time for
a client that may never read the result. Under what load does your choice become
the wrong one, and what would you watch on P8's dashboard to notice?

**Exit:** kill the network mid-stream, reconnect, and resume without duplicated
or lost tokens.

---

### P12 — Gateway extraction  [ ]  *(#1b)*

Now earned: P1 through P11 produce a real interface, and there is a second
consumer.

- [ ] Extract the gateway into a standalone service
- [ ] `HTTPGatewayProvider` implementing the existing `LLMProvider` protocol —
      the chatbot changes one line of wiring
- [ ] OpenAI-compatible surface as a *second* interface, for external consumers
- [ ] Partial-failure semantics: gateway up, provider down; gateway down mid-stream
- [ ] Deploy as a separate compose service

#### 🧪 Exercise 11 — Retract semantics across a network boundary  **[you]**

**LEARN.** *(1) Concept* — `_ReplaceText` tells the client to discard everything
it has rendered and show something else instead, because grounding can only be
judged after generation completes. *(2) Why* — it is the mechanism that stops
your product shipping a plausible uncited answer, which is the single worst thing
it can do. *(3) Without care* — the gateway streams 200 tokens to the chatbot,
which streams them to the browser, which renders them. Then grounding fails. The
retract now has to travel back through two hops to un-say something already on
screen. The tempting fix — buffer everything at the gateway until grounding is
known — deletes streaming entirely, which is the feature you were extracting.

**DESIGN.** *(4)* Three architectures, and choosing between them *is* the
exercise. **(a)** Gateway streams, app buffers and decides — preserves the
retract, moves the buffering problem to the app, and reintroduces exactly the
backpressure question from P11. **(b)** Grounding stays app-side and `replace`
never crosses the boundary — cleanest, and it constrains what the gateway is
allowed to own forever. **(c)** An explicit retract frame in the wire protocol —
most general, and now every future consumer must implement it correctly or
silently ship ungrounded answers. Cost each one out before writing a line.

**BUILD.** *(5)* The wire protocol and both sides. *(6) Tests* — grounded and
ungrounded turns end to end, through the real boundary, not a mock.

**BREAK.** *(7)* Kill the gateway process mid-stream: after tokens have been
delivered, before the grounding decision has been made. *(8) You should see* —
whether the user is left holding a plausible, ungrounded, un-retracted answer on
screen. If so, your failure mode is *exactly* the outcome the whole retract
mechanism exists to prevent, reintroduced by the extraction. *(9) Fix* — fail
closed: an incomplete stream must resolve to a visible error, never to a
confident partial answer.

**DOCUMENT.** *(10)* This is why extraction is P12 rather than P1. Write down
what the boundary placement decided for you — including that option (b) is only
available because you drew the line late, with the retract mechanism already
built and understood.

**Exit:** two independent consumers on one gateway; the chatbot's behaviour is
unchanged.

---

### P13 — Tool execution sandbox  [ ]  *(#7)*

**Prerequisite:** there is currently no code execution anywhere in this codebase
to sandbox. `security/sanitize.py` is *output* sanitisation, which is an
unrelated problem. So this phase starts by creating the need.

- [ ] Add a tool that genuinely requires execution — e.g. a data-analysis tool
      that writes and runs Python over the corpus to answer quantitative
      questions ("which guests discuss pricing most often?")
- [ ] Container-per-execution with a hard timeout
- [ ] CPU, memory, and PID limits
- [ ] No network by default; explicit allowlist if ever needed
- [ ] Read-only filesystem + a scratch mount
- [ ] Output size caps (a tool printing 2GB is a denial of service)

#### 🧪 Exercise 12 — Container hardening and the threat model  **[you]**

**LEARN.** *(1) Concept* — layered controls (user, capabilities, seccomp,
namespaces, cgroups, network policy) that each independently reduce what
untrusted code can do. *(2) Why* — a single control has a single bypass, and you
are executing text produced by a model, which is to say text influenced by
whatever is in your corpus. *(3) Without it* — a timeout alone stops nothing that
completes quickly. Generated code reads `/proc/self/environ`, finds
`ANTHROPIC_API_KEY` and `CLOUD_API_KEY`, and posts them to an external host in
under a second, well inside any timeout you set.

**DESIGN.** *(4)* Write the threat model before the Dockerfile, and be precise
about the adversary — this matters more here than anywhere else in the roadmap.
Are you defending against a confused model producing destructive code by
accident, or against deliberate injection? Note that `rag/ingest.py:67` clones a
transcript corpus from a public GitHub repository, so your corpus is
attacker-influenceable, which means prompt injection into generated code is a
real path and not a hypothetical one. Those two adversaries justify very
different controls. Then: what does the tool legitimately need, and what is the
smallest grant that satisfies it?

**BUILD.** *(5)* The controls. *(6) Tests* — each control verified
independently, so you know which one is doing the work.

**BREAK.** *(7)* Write the escape attempts yourself, one per control: fork bomb;
read `/proc/self/environ`; outbound HTTP; write outside the scratch mount; emit
2GB to stdout; sleep 30s against a 5s timeout; mount namespace escape. *(8) You
should see* — which are contained and which are not. Expect at least one
surprise; the usual one is that the timeout you trusted does not fire because the
process is unkillable in the state it reached. *(9) Fix*, then re-run all of them.

**DOCUMENT.** *(10)* Each control costs startup latency, and
container-per-execution is roughly 200–500ms before any user code runs. State
when a restricted interpreter would have been sufficient and when only a real
kernel boundary will do — and be honest about which one your actual tool needed.

**Exit:** a documented threat model, plus tests demonstrating containment of each
attack class listed in it.

---

### P14 — Integration, chaos & demo  [ ]

Not another system. After P13, deliberately break the platform and document how
it behaves.

```
                    AI PLATFORM
                         |
        +----------------+----------------+
        |                |                |
     Provider          Redis           Worker
       (x)              (x)              (x)
        |                |                |
        +----------------+----------------+
                         |
                   Does it recover?
```

This phase is what separates "fifteen AI infrastructure features" from "an AI
platform." Anyone can list subsystems. Producing a table that says *here is how
this system fails, here is how it detects the failure, here is what the user
sees, and here is the metric that would have paged me* is a different claim
entirely — and it is the claim that is hard to fake in an interview, because the
follow-up questions have answers only if you actually ran it.

**The chaos list is also a completeness check on the roadmap.** Every scenario
must have an owning phase. A scenario with no owner means the roadmap has a hole.

#### Rules

- [ ] Every scenario is a **script** in `chaos/`, not a manual click-through.
      A failure test you cannot re-run is an anecdote.
- [ ] Every scenario runs against the **full stack** in compose, not against mocks.
- [ ] P8's dashboard must be open while each one runs. If a failure is invisible
      there, that is itself a finding — record it and fix the instrumentation.
- [ ] Results go in `docs/chaos.md`, one row per scenario, columns below.
- [ ] A scenario that passes first try is suspicious. Make it harsher.

#### The matrix

For each scenario, fill six columns:

| Column | The question it answers |
|---|---|
| **Failure** | What was broken, and how |
| **Detection** | How the system noticed — and how long it took |
| **Recovery** | What it did automatically; what needed a human |
| **Data consistency** | What state was left behind. Orphans? Duplicates? Partial writes? |
| **User-visible behaviour** | What the person in the browser actually saw |
| **Metrics** | Which dashboard signal moved, and would it have paged you |

The fourth column is the one people skip and the one that matters. "It recovered"
and "it recovered without leaving a job claimed by a dead worker, a half-written
message row, and a webhook that fires twice tomorrow" are different results.

#### Scenarios

| # | Scenario | Owning phase | What it should prove |
|---|---|---|---|
| 1 | [ ] Kill Ollama mid-generation | P2 | Breaker opens, fallback serves, user sees which model answered |
| 2 | [ ] Kill Redis | P1, P11 | Rate limiter degrades to Postgres; streams survive or fail cleanly |
| 3 | [ ] Kill a worker mid-job | P9 | Job reclaimed and resumed from checkpoint, no duplicate effects |
| 4 | [ ] Restart Postgres | P0, P1 | Pool reconnects; in-flight requests fail cleanly, not silently |
| 5 | [ ] Disconnect SSE mid-stream | P11 | Resume works; no duplicated or lost tokens |
| 6 | [ ] Deliver a duplicate webhook | P10 | Consumer contract holds; delivery log tells the truth |
| 7 | [ ] Exceed a tenant quota | P1 | 429 with accurate `Retry-After`; other tenants unaffected |
| 8 | [ ] Inject a malicious prompt via the corpus | P6, P13 | Guardrails hold; injected instructions do not reach a tool |
| 9 | [ ] Delete a document referenced by the cache | P5 | Invalidation fires; no answer cites a chunk that no longer exists |
| 10 | [ ] Promote a bad prompt version | P3, P7 | CI should have blocked it; if it lands, rollback is one pointer move |
| 11 | [ ] Trigger a failed evaluation | P7 | Build fails, regression is attributable to a commit and a prompt version |
| 12 | [ ] Two workers race one job | P9 | Exactly one execution, proven by counting side effects |

Three more this system specifically needs, which a generic chaos list would miss:

| # | Scenario | Owning phase | What it should prove |
|---|---|---|---|
| 13 | [ ] Ollama cold start after `keep_alive` expires (~77s) | P2 | The breaker does not open on a *warming* provider and lock out a recovery |
| 14 | [ ] Re-ingest the corpus while a stream is live | P5, P9 | Citations resolve or degrade honestly; no answer cites a mid-write chunk |
| 15 | [ ] A long job crosses the per-day rate-bucket boundary | P1 | Quota accounting is correct across the reset; no free window, no double charge |

#### Then: the demo

- [ ] Record a single continuous run: healthy traffic, a failure injected live,
      the dashboard reacting, recovery, and the consistency check afterwards.
- [ ] One page in `README.md` linking the chaos table, the twelve exercise
      write-ups, and the retrieval and cache calibration docs.

The recording is the artifact. A platform that visibly survives having a
dependency killed while someone watches is worth more than any architecture
diagram of it.

**Exit:** `docs/chaos.md` complete for all fifteen scenarios, every finding either
fixed or recorded as a known limitation with a reason.

---

## 3. What "gateway" means in each phase

The word does two jobs, which is what made the original ordering confusing:

- **P1/P2 — gateway as a set of concerns.** Auth, rate limiting, routing,
  metering, fallback. These live *in-process*, behind the `LLMProvider` protocol
  that already exists. This is where the learning is.
- **P12 — gateway as a deployment unit.** A separate service with a wire
  protocol. This is a packaging decision, and it is only interesting once there
  is something real to package and a second consumer to serve.

Building the deployment unit first means designing a wire protocol for one
consumer, guessing at the interface, and adding three network hops per turn to
the local-Ollama critical path — the orchestrator calls the provider three times
per turn (routing, relevance gate, generation).

---

## 4. Coverage of the original 15 systems

| # | System | Phase | Validated by |
|---|---|---|---|
| 1 | LLM Gateway / Proxy | P1, P2 (concerns), then P12 (service) | Chaos 1, 13 |
| 2 | Token Metering & Billing | P1 | Chaos 7, 15 |
| 3 | Streaming Infrastructure | P0 (exists), then P11 (hardening) | Chaos 5 |
| 4 | RAG Serving Pipeline | Done — carried forward | Chaos 9, 14 |
| 5 | Semantic Cache | P5 | Chaos 9, 14 |
| 6 | Async Agent Job Queue | P9 | Chaos 3, 12 |
| 7 | Tool Execution Sandbox | P13 | Chaos 8 |
| 8 | Multi-Tenant Knowledge Base | P1 | Chaos 7 |
| 9 | Prompt & Config Versioning | P3 | Chaos 10 |
| 10 | Eval Pipeline Backend | P0 (exists), then P7 (CI gate) | Chaos 10, 11 |
| 11 | LLM Observability | P0 (traces exist), then P8 (platform) | Every scenario's Metrics column |
| 12 | Webhook & Event Fan-out | P10 | Chaos 6 |
| 13 | Context Assembly Service | P4 | Chaos 14 |
| 14 | Guardrails Middleware | P0 (exists), then P6 (middleware) | Chaos 8 |
| 15 | Model Fallback & Routing | P0 (fallback exists), then P2 (routing) | Chaos 1, 2, 13 |

Every system has an owning phase and at least one scenario that tries to break
it. If a future addition to either column has no counterpart in the other, that
is the signal to look harder.
