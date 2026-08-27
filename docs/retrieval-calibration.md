# Retrieval calibration

Evidence behind two decisions in `architecture.md`: which embedding model runs by
default, and why the abstain gate is a model-based relevance check rather than a
similarity threshold.

Regenerate everything here with:

```bash
docker compose exec backend python -m scripts.calibrate_retrieval
```

Measured 27 Aug 2026 against 15 ingested episodes (937 chunks), on the target
machine: Ryzen 5 4600H (6C/12T), 16GB RAM, GTX 1650 4GB, inside the backend
container.

---

## 1. Embedding model selection

Throughput at ~400-token inputs, batch 32:

| Model | Precision | Dim | chunks/s | Full-corpus ingest (est.) |
|---|---|---|---:|---:|
| `BAAI/bge-small-en-v1.5` | int8 | 384 | 1.8 | ~90 min |
| `snowflake/snowflake-arctic-embed-s` | fp32 | 384 | 8.8 | ~35 min |
| `BAAI/bge-small-en` | fp32 | 384 | 11.5 | ~28 min |
| **`snowflake/snowflake-arctic-embed-xs`** | **fp32** | **384** | **17.6** | **~23 min** |
| `sentence-transformers/all-MiniLM-L6-v2` | fp32 | 384 | 44.6 | ~10 min |

**The obvious default was the worst option.** `bge-small-en-v1.5` is the standard
recommendation and fastembed ships it as an int8-quantized ONNX build. This CPU
is Zen 2, which has no AVX512-VNNI, so int8 GEMM is emulated — the quantized
model lands roughly 10× *slower* than the fp32 models it is supposed to beat.
Worth knowing generally: quantized ONNX is a pessimisation on pre-VNNI hardware,
and "smaller file" is not "faster".

`arctic-embed-xs` is the pick: retrieval quality within noise of bge-small, 10×
the throughput. MiniLM is faster still but measurably weaker at retrieval, and
retrieval quality *is* the product here. All candidates are 384-dim, so any of
them can be swapped in with a re-ingest and no schema change.

### A silent truncation bug this surfaced

Chunks were originally 800 tokens. Every candidate encoder has a 512-token
maximum sequence length and truncates **silently** — so roughly a third of every
chunk was never embedded. Those tokens remained findable by keyword search and
invisible to vector search: a recall hole that produces no error and no log line.

Chunks are now 400 tokens with 80 overlap, and `Settings._chunk_fits_the_encoder`
rejects any configuration where `CHUNK_TOKENS > 0.8 × max_seq_len` at startup.

---

## 2. The abstain gate: a measured negative result

**Original design:** abstain when the best dense cosine similarity falls below a
threshold. It was in the plan, and it does not work.

Golden set: 15 in-corpus product/growth questions, 5 deliberately out-of-corpus.

| Statistic | In-corpus range | Out-of-corpus range | Separable? |
|---|---|---|---|
| top-1 cosine | 0.616 – 0.756 | 0.548 – 0.671 | **No** |
| top-1 margin over the mean of the rest | 0.016 – 0.075 | 0.005 – 0.055 | **No** |

The ranges overlap across a third of the scale. Concretely:

- *"Write me a Python function that reverses a linked list."* scores **0.671** —
  higher than **11 of the 15** legitimate product questions.
- A threshold set to reject all five out-of-corpus questions would also refuse
  **11 of 15** real ones. A threshold set to admit every real question admits
  every out-of-corpus one too.

This is not a tuning problem. A bi-encoder trained with a cosine objective
compresses everything into a narrow high band, and it answers *"is this text
similar to that text"* — never *"do these passages answer this question"*. No
threshold on that signal can encode a question it was never asked.

### What replaced it

**`RETRIEVAL_MIN_SIM` is now a safety floor (0.35), not the domain gate.** It
catches degenerate states — empty corpus, broken embedder, a query matching
nothing — and is set low enough never to fire on a real question.

The authoritative gate is a two-stage check in the orchestrator:

1. **Relevance judgement.** After retrieval, a short structured-output call asks
   the model whether the retrieved passages actually answer the question, and
   which source ids carry the answer. Cheap on a 3B model, and it evaluates the
   thing we actually care about.
2. **Citation resolution.** After generation, every `[S#]` marker is resolved
   against the retrieved set. Unresolved markers are stripped and counted; a
   knowledge-intent answer left with zero resolved markers falls back to the
   abstain response.

Reflection rather than arithmetic — and a more honest design than the one it
replaced.

---

## 3. Hybrid health

The `sparse` column in the calibration output is a regression check on a failure
mode that produces no error.

`websearch_to_tsquery` joins its terms with **AND**. Passing a natural-language
question straight through means every term must co-occur in one chunk, so on the
real corpus *"How do I know when I've actually found product-market fit?"*
matched **0** chunks — while the pipeline logged success and answered from dense
results alone. "Hybrid" retrieval was quietly dense-only for every query.

`build_sparse_query` now converts the question into an OR clause over
non-stopword terms; `ts_rank_cd` still rewards chunks matching more terms more
densely, so ranking survives the looser matching.

| | Before | After |
|---|---:|---:|
| Sparse candidates across the golden set | 0 | 748 |
| Chunks retrieved by *both* retrievers | 0 | 134 |
| Queries using both retrievers | 0/20 | 20/20 |

The calibration script prints a `WARNING dense-only queries` line if this ever
regresses, and `TestSparseQueryBuilder` covers the term-building rules.

---

## 4. Tuning the retrieval funnel

The golden set was run end to end (`python -m scripts.evaluate`) after each
change. The metric is the PRD's: in-corpus questions returning at least one
*resolved* citation, and out-of-corpus questions refused.

| Change | Grounded | Refusal | Median latency |
|---|---:|---:|---:|
| Baseline | 9/15 (60%) | 4/5 | 23 s |
| + chitchat routing fix, citation repair pass | 10/15 (67%) | **5/5** | 23 s |
| + relevance reframe (§2), search-query cleaning | 11/15 (73%) | 5/5 | 24 s |
| + candidates 40→80, top-k 5→8, rewrite gating | **12/15 (80%)** | 5/5 | 29 s |

Four measured findings behind those rows:

**Out-of-corpus questions were being routed as chitchat**, which skips retrieval
and grounding entirely — the one path by which an ungrounded claim reaches the
user. Fixing the router's chitchat definition took refusal from 4/5 to 5/5.

**The model writes faithful answers and omits the citation markers.** That
accounted for as many failures as genuinely ungrounded output. A narrow repair
pass — "add markers, change nothing else" — recovers them; measured live it
turned a 0-citation answer into 3 resolved citations in 3.1 s.

**The router was padding the search query.** "Should I stay an individual
contributor or move into management" came back as "...management product growth
metrics strategy", and those appended category words pulled the embedding off
the question. The rewrite now runs *only* when the message contains a reference
to resolve ("expand on that"); a self-contained question is already the best
query available.

**Widening the funnel flipped three failures to passes** — `retention-curve`,
`growth-loops` and `career-ic-vs-manager` all recovered. But it flipped two the
other way: `first-pm-hire` and `pm-interview` began failing as fast abstains at
the relevance gate. Eight passages makes the gate's digest ~3,400 characters,
and the 3B judges *worse* with more to read. More context, weaker judgment.

## 5. A cross-encoder reranker was tested and rejected

Reranking was the obvious next move, and named in the PRD as the first thing to
add. It was benchmarked before being built, and the measurement says no.

Throughput was never the problem — 40 query/passage pairs on CPU:

| Model | 40 pairs | Rate |
|---|---:|---:|
| `jinaai/jina-reranker-v1-tiny-en` | 0.95 s | 42 pairs/s |
| `Xenova/ms-marco-MiniLM-L-6-v2` | 1.40 s | 29 pairs/s |
| `jinaai/jina-reranker-v1-turbo-en` | 1.83 s | 22 pairs/s |

**Quality was.** On `career-ic-vs-manager`, RRF correctly surfaced *"Building a
long and meaningful career | Nikhyl Singhal"* — the exactly-right episode, which
discusses staying an IC versus moving to management. Both rerankers **demoted
it** out of the top 3 and promoted Marty Cagan episodes on process and product
theatre instead.

The tell is in the scores: every passage came back negative
(`jina-turbo`: −0.17 to −0.95). These are MS MARCO-trained rerankers, calibrated
on short factoid web passages. A 400-token chunk of two people talking is a
different distribution entirely, and the model has no useful signal on it.

So no reranker ships. This is a *measured* recommendation rather than a deferred
one: the honest next step is a reranker fine-tuned on conversational transcripts,
or an LLM-as-reranker pass — not an off-the-shelf cross-encoder.

## 6. HNSW search width

`hnsw.ef_search` defaults to 40 while the dense query asks for
`RETRIEVAL_CANDIDATES` (80) per retriever, so the default sits below pgvector's
own guidance of `ef_search >= LIMIT`.

Measured against brute-force ground truth at the current corpus size,
**recall@80 is 100% at the default** — the graph is small enough that 40 finds
everything. It is set to `max(64, candidates × 2)` per connection anyway,
because the guidance is explicit, the cost is zero, and the margin narrows as
the corpus grows. This system has already been bitten twice by silent recall
loss (§1 truncation, §3 AND-vs-OR), which is reason enough to close a third
opening before it matters.

## 7. Parent-child chunking was tested and rejected

"Small-to-big" retrieval — match on small chunks for precision, then widen each
hit to include its neighbours before the model reads it — is the standard next
move after a reranker, and it needed no schema change here: `(episode_id, ord)`
already encodes the parent structure, so widening is a neighbour lookup.

A nine-case probe against the relevance gate looked promising:

| Window | Correct |
|---|---:|
| base (no widening) | 7/9 |
| **w = 1** | **8/9** |
| w = 2 | 5/9 |

On the full golden set it **fell from 80% to 60%**.

The interesting part is that it is not a uniform loss. Widening **fixed all
three** cases that were failing — `first-pm-hire`, `pm-interview`,
`roadmap-prioritization` — exactly as the probe predicted. It then **broke
four** that were passing.

**The likely mechanism is a bug in how the two interact, not in the idea.** The
relevance judge reads a digest: the first ~420 characters of each passage. After
widening, those first 420 characters are the *preceding neighbour's* text, not
the chunk that actually matched — so the judge is shown the wrong part of every
source. The failure timings support it: the broken cases refused in 3.8–4.6
seconds, far too fast to have read anything useful.

Making this work would mean carrying two texts per chunk — the matched span for
judging, the widened span for generating — and re-tuning `top_k`, since eight
widened passages exceed the 8,192-token context and Ollama truncates silently
(§1). That is a real piece of work, not a flag flip, and it is the third
plausible improvement this project has rejected on measurement rather than
shipped on intuition.

`RETRIEVAL_PARENT_WINDOW` remains in the code, defaulting to 0, so the finding
can be reproduced with one environment variable.
