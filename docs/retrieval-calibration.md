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
