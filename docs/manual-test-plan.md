# Manual test plan

The automated suite (243 tests, `cd backend && python -m pytest`) covers logic
that can be asserted without a model. This covers what it cannot: the streaming
UI, the artifact viewer, and behaviour that depends on a live LLM.

**Setup:** `docker compose up -d --build`, then
`docker compose exec backend python -m app.rag.ingest --limit 20`.
Open http://localhost:5173. Steps are ordered — later ones assume earlier ones.

Timings assume the default `qwen2.5:3b-instruct-q4_K_M` on a 4 GB GPU.

---

## A — First run and readiness

| # | Step | Expected |
|---|---|---|
| A1 | `curl -s localhost:8000/healthz` | `{"status":"ok",…}` |
| A2 | `curl -s localhost:8000/readyz` | `status: ready`, real `corpus` counts, `degraded: []` |
| A3 | Open http://localhost:5173 | Chat loads; header shows episode and passage counts |
| A4 | Check the provider badge | Green dot, model name shown |
| A5 | Stop Ollama (`ollama stop` / quit), wait ~20 s | Badge turns red; dropdown gives the reason **and** the fix |
| A6 | Restart Ollama, wait ~20 s | Badge returns to green with no page reload |

## B — Grounded answer

| # | Step | Expected |
|---|---|---|
| B1 | Ask *"How do I know when I've found product-market fit?"* | Steps appear in order: understanding → searching → checking sources |
| B2 | Watch the answer | Text streams token by token with a blinking caret |
| B3 | Read the answer | Claims carry `[S1]`-style chips; a guest is named |
| B4 | Click a citation chip | Sources expand and scroll to that card |
| B5 | Click "Listen from …" | Opens YouTube at the right timestamp, new tab |
| B6 | Check the line under the answer | Provider, model, and elapsed time |
| B7 | Reload the page | The conversation persists with citations intact |

## C — Refusal (the core promise)

| # | Step | Expected |
|---|---|---|
| C1 | Ask *"What's the weather in Mumbai tomorrow?"* | Amber card labelled **Outside the corpus** |
| C2 | Read it | Says what it searched and what was missing; no weather answer |
| C3 | Ask *"Write a Python function to reverse a linked list."* | Also refused — the model **can** answer this, and must not |
| C4 | Check the steps | `check_relevance` shows `answerable: false` |
| C5 | Ask *"How should I price a B2B product?"* | Answers normally — the refusal isn't over-triggering |

> C3 is the important one. Refusing something the model could answer from
> parametric knowledge is the product's whole contract.

## D — Sessions

| # | Step | Expected |
|---|---|---|
| D1 | Click **+ New chat** | Empty state with suggestions |
| D2 | Ask *"What makes a good north star metric?"* | Answers |
| D3 | Switch to the earlier chat | Its own history, not the new one's |
| D4 | Ask a follow-up using *"that"* | Resolves against **this** chat's context only |
| D5 | Check the sidebar | Each chat titled from its first question |
| D6 | Delete a chat | Disappears; a remaining chat is selected |

## E — Ship 30 essay

| # | Step | Expected |
|---|---|---|
| E1 | Ask *"Write a Ship 30 for 30 essay about pricing strategy."* | `plan_outline` step shows the headline and 3 section headings |
| E2 | Watch | Headline and hook appear first, then sections one by one |
| E3 | Wait (~2–3 min) | `write_takeaway`, then `check_rubric` with word count and citations |
| E4 | Read the essay | H1, ≥3 `##` sections, a bulleted takeaway, a "Monday" action |
| E5 | Check length | ~1,000–1,500 words |
| E6 | Check citations | Several resolved `[S#]` markers |

> The rubric's **emphasis** check often fails on the 3B — it under-uses bold — so
> the essay ships with a visible warning. That is the rubric working. A larger
> model passes it.

## F — Artifacts and the viewer

| # | Step | Expected |
|---|---|---|
| F1 | Ask *"Make me an HTML one-pager on hiring your first PM."* | `create_artifact` step; pane opens on the right |
| F2 | Check the chat | A short covering sentence — **not** raw HTML |
| F3 | Read the document | Styled headings, lists, citations |
| F4 | Click **Source** | The original markup |
| F5 | Click **Copy** | Copied to clipboard |
| F6 | Ask for a second document | Tab strip appears; both switchable |
| F7 | Reload the page | Artifacts reload with the session |
| F8 | Narrow the window below 1024px | Pane takes over; header shows a document count to get back |

## G — Artifact security

The important one. Requires `psql` and a browser console.

| # | Step | Expected |
|---|---|---|
| G1 | Generate any HTML artifact | Renders |
| G2 | Inspect the `<iframe>` in devtools | `sandbox` present, **no** `allow-same-origin`, **no** `allow-scripts` |
| G3 | Check its `srcdoc` | Contains `default-src 'none'` CSP |
| G4 | Inject a payload directly: <br> `docker compose exec db psql -U lenny -d lenny -c "UPDATE artifacts SET content_sanitized='<h1>x</h1><script>alert(1)</script>' WHERE id=(SELECT id FROM artifacts LIMIT 1);"` then reload | Heading renders; **no alert** — the frame blocks it even with the allowlist bypassed |
| G5 | Tick **Allow scripts**, reload | Still no alert from G4's payload — opaque origin, and layer 1 would have stripped it in the real path |
| G6 | Check the browser console | CSP violations reported, not silent execution |
| G7 | Find an artifact whose banner says "N elements removed" | Expanding it names each construct and why |

> G4 deliberately bypasses layer 1 to prove layer 2 stands alone. Neither layer
> is trusted by itself.

## H — Failure handling

| # | Step | Expected |
|---|---|---|
| H1 | Stop Ollama mid-answer | `error` event; a clear message, not a hang |
| H2 | Send a message with Ollama down | 503 with a hint, or fallback if `PROVIDER_FALLBACK=true` |
| H3 | `docker compose stop db`, reload | Banner: database unreachable; composer disabled; no white screen |
| H4 | `docker compose start db`, wait ~20 s | Recovers without a reload |
| H5 | Start a long answer, click **Stop** | Streaming halts; the partial answer is kept |
| H6 | Start an answer, close the tab, check `docker compose logs backend` | `client_disconnected` — generation aborted, GPU released |
| H7 | Set `CHUNK_TOKENS=800` in `.env`, restart backend | Refuses to start; the log names the variable and the fix |

## I — Accessibility

| # | Step | Expected |
|---|---|---|
| I1 | `Tab` from page load | Visible focus ring on every control, sensible order |
| I2 | Send a message with `Enter`; `Shift+Enter` | Sends / newlines |
| I3 | After a reply completes | Focus is back in the composer |
| I4 | Open the provider menu, press `Escape` | Closes |
| I5 | Tab to a session row | Its delete button becomes visible (not hover-only) |
| I6 | Screen reader on, send a message | Answer announced once when settled, not per token |
| I7 | Switch OS to dark mode | Theme follows; contrast holds |
| I8 | Enable "reduce motion" | Caret stops blinking |
| I9 | Zoom to 200% | No horizontal page scroll; tables scroll inside themselves |

## J — Fresh-clone rehearsal

The brief asks that an evaluator can clone and run using only the documented
steps. Run this in a **clean directory** with `docker compose down -v` first.

| # | Step | Expected |
|---|---|---|
| J1 | `git clone …` into a new folder | |
| J2 | Follow the README verbatim, no other knowledge | |
| J3 | Time from clone to first grounded answer | **< 10 minutes** — the operational success metric |
| J4 | `cd backend && python -m pytest` with Ollama stopped and no keys | 243 passed |
| J5 | `grep -r "sk-\|api_key" --include="*.py" --include="*.ts" .` | No secrets |
| J6 | `git ls-files \| grep -x .env` | No output — `.env` is not tracked |
