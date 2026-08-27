# Coding agent transcripts

The full build session, exported from Claude Code and redacted with
`backend/scripts/export_transcript.py`.

- [`01-build-session.md`](01-build-session.md) — complete session: prompts, tool
  calls, results, and every correction.

The brief asks for failed attempts and how they were corrected, so this page
indexes the ones that mattered. **Six of the eight below produced no error, no
warning, and no failing test.** They were found by reading what the system
actually returned rather than trusting that it worked, which is the part of the
process worth showing.

---

## 1. The default embedding model was the worst available option

**Assumed:** `BAAI/bge-small-en-v1.5` is the standard recommendation, so use it.

**Measured:** 1.8 chunks/s — roughly **10× slower** than models it is supposed to
beat. fastembed ships it as an int8-quantized ONNX build, and the target CPU is
Zen 2, which has no AVX512-VNNI, so int8 GEMM is emulated.

**Corrected:** benchmarked five candidates on the real machine and switched to
`snowflake/snowflake-arctic-embed-xs` — 17.6 chunks/s at equivalent retrieval
quality. Full-corpus ingest went from ~90 minutes to ~23.

**Generalises:** quantized ONNX is a pessimisation on pre-VNNI hardware. "Smaller
file" is not "faster."

## 2. Every chunk was silently truncated by a third

**Symptom:** none. Ingestion succeeded, retrieval returned results, no error.

**Cause:** chunks were 400→800 tokens; every candidate encoder caps at 512 and
truncates **silently**. A third of each chunk was never embedded — findable by
keyword search, invisible to vector search.

**Corrected:** chunks reduced to 400 tokens, and a config validator now rejects
`CHUNK_TOKENS > 0.8 × max_seq_len` at startup so it cannot recur.

## 3. "Hybrid" retrieval was dense-only for every query

**Symptom:** none. The pipeline logged success and answered from dense results.

**Cause:** `websearch_to_tsquery` joins terms with **AND**. A natural-language
question required all seven words to co-occur in one passage. Measured on the
real corpus: **0 matches** for *"How do I know when I've actually found
product-market fit?"*, versus 725 with OR semantics.

**Corrected:** `build_sparse_query` emits an OR clause over non-stopword terms.
Sparse candidates across the golden set went 0 → 748.

## 4. The abstain gate could not work as designed

**Planned:** refuse when top-1 cosine similarity falls below a threshold.

**Measured:** in-corpus questions span 0.616–0.756, out-of-corpus 0.548–0.671.
*"Write me a Python function that reverses a linked list"* scores **0.671** —
higher than 11 of 15 real questions. Any threshold rejecting all five
out-of-corpus questions would refuse 11 of 15 legitimate ones.

**Corrected:** demoted the threshold to a safety floor and replaced the gate with
a model-based relevance judgement plus mechanical citation resolution. Evidence
in [`docs/retrieval-calibration.md`](../docs/retrieval-calibration.md).

**The lesson:** a bi-encoder answers "is this text similar", never "does this
text answer the question". No threshold on that signal can encode a question it
was never asked.

## 5. Chunks began mid-word

**Symptom:** citations quoting text like *"ith external stakeholders"*.

**Cause:** the chunker snapped the chunk **end** to a boundary but advanced the
**start** by raw arithmetic, so every chunk after the first began mid-token —
corrupting both the embedding and the quoted text.

**Corrected:** both ends snap to boundaries, with a regression test asserting no
chunk starts mid-word.

## 6. An ad filter that caught nothing

**Symptom:** a Vanta sponsor read scored **0.710** as the top source for *"hiring
your first product manager"*.

**First attempt:** require an opener ("brought to you by") **and** a
call-to-action in the same chunk. Matched **0 of 277** real sponsor passages — at
400 tokens an ad read spans several chunks, so the two signals never co-occur.

**Corrected:** derived the rules by measuring against the live index instead of
guessing — sponsor vanity URLs (`vanta.com/lenny`) or two independent CTA
signals. Flags 303 of 9,112 chunks with clean separation on manual inspection.

## 7. Chasing the wrong bug

**Symptom:** the assistant refused questions the corpus clearly covered.

**Nearly did:** loosen the relevance-check prompt, which looked over-strict.

**Actually:** inspecting the retrieved passages showed the judge was *right* and
retrieval was wrong — an ad, a Stripe interview aside, and an OKR tangent.
"Fixing" the judge would have masked both real defects (#5 and #6) and made the
system confidently wrong instead of correctly cautious.

**The lesson:** when a checker rejects something it should accept, verify what it
was shown before adjusting the checker.

## 8. Skills silently never loaded

**Symptom:** the model replied *"I'm unable to generate HTML content"* — despite
a skill that specifies exactly how.

**Cause:** the skill loader derived its path from `__file__` and resolved to
`/skills` inside the container. The Docker build context was `./backend`, so the
repo-root `skills/` directory was never in the image. Load failure was logged at
`error` and the agent quietly fell back to a generic prompt.

**Corrected:** build context moved to the repo root, skills copied in and
bind-mounted for editing, and `SKILLS_DIR` makes the location explicit rather
than inferred.

---

## Also corrected, more briefly

- **ammonia keeps stripped tags' text content** by default, so
  `<script>alert(1)</script>` left `alert(1)` as visible text. Fixed with
  `clean_content_tags`.
- **CSS danger patterns matched only their opening token**, so scrubbing
  `url('https:` from `url('https://evil.test/x.png')` left the hostname behind.
- **A dangerous-URL regex required a second colon** after `data:text/html`, so
  that payload was cleaned but never *reported* — the report is what the viewer
  shows the user.
- **The artifact parser lost documents** when the model wrapped the envelope in a
  second fence, which qwen2.5:3b does often.
- **Zero citations in the first essay** — the instruction was last in a rule
  list, where a 3B ignores it. Moved first, with a worked example.
- **First essay ran 839 words**, short of the rubric floor, because the closing
  TL;DR was never generated as its own step.
- **`/readyz` took 6 seconds** because dependency probes ran serially, each
  paying its own timeout. Now concurrent.
- **Cold model load cost ~77 s** on the first request. Now `keep_alive` plus a
  background warmup at startup.
- **The relevance check cost ~30 s of a 34 s turn** by evaluating full context.
  Running it on a digest of each passage's opening cut a turn to 6.3 s.
