# Architecture Decision Log

One entry per decision: the problem, what we chose, and why. **Status** says whether
it is already implemented or committed-to for a later phase. Entries marked
*Spec deviation* change something in `BUILD_SPEC.md` and were approved before building.

---

## D-001 · Chunk size: ~450 tokens + 60 overlap, not 800 *(Spec deviation, flag A)*
**Status:** Accepted · implemented in Phase 1 (chunker)

**Problem.** The spec asks for ~800-token chunks, but the embedding model,
`BAAI/bge-small-en-v1.5`, has a hard 512-token input limit (its BERT position
embeddings stop at 512). Longer inputs are not rejected; the tokenizer **silently
truncates** them. With 800-token chunks, about 35% of every chunk would never reach
the vector. Worse, Weaviate's BM25 index *does* see the full text, so the two halves of
hybrid search would disagree about what a chunk contains. A risk discussed in the second
half of a chunk could match on keywords but never on meaning.

**Fix.** Chunk to ~450 tokens with 60 tokens of overlap, counted with **the bge
tokenizer itself** (not tiktoken or word counts, which under-count WordPiece tokens).
That leaves ~60 tokens of headroom for a short contextual header we prepend before
embedding (e.g. `AAPL · 10-K · FY2024 · Item 1A`). A unit test asserts that no
chunk-plus-header exceeds 512 tokens.

**Why not a bigger model?** bge-base and bge-large have the same 512 limit.
Long-context embedders (8k tokens) are several times slower on CPU and much larger,
which hurts both the ingestion run and the deployed API image. Smaller chunks also make
citations more precise: an `[n]` points at a paragraph, not a page.

---

## D-002 · Agent state: immutable plan, keyed step results, resettable evidence *(Spec deviation, flag B)*
**Status:** Accepted · implemented in Phase 3 (`agent/state.py`)

**Problem 1: parallel writes to `plan`.** In LangGraph, a state key *without* a
reducer accepts only one write per super-step. When the dispatcher fans out with
`Send`, several specialist branches finish in the same super-step. If each one marks its
step `done` by writing `plan`, LangGraph raises `InvalidUpdateError` ("can receive only
one value per step"). Even if it didn't, one branch's write would overwrite the others.

**Problem 2: evidence that can't be cleared.** `Annotated[list, operator.add]` can only
grow. With the `PostgresSaver` checkpointer, a thread's state persists across turns, so in
a follow-up ("Now do the same for Intel") the previous turn's NVIDIA/AMD evidence would
still be in `evidence`. The synthesizer could then cite it in the Intel answer.

**Fix.**
- `plan: list[PlanStep]` is **written only by the planner** and treated as immutable
  (a replan writes a new version).
- Step progress lives in `step_results: Annotated[dict[str, StepResult], merge_dicts]`,
  keyed by step id. Parallel branches write *different keys*, so merges never conflict.
  A step's status is derived from whether it has a result.
- `evidence` uses a **custom reducer**: normally it appends and dedupes by `source_id`,
  but a sentinel value (`EvidenceReset`) replaces the list. The first node of each turn
  emits the reset, so every turn starts clean while multi-turn *messages* are still
  remembered.

**Why.** This keeps the spec's intent (accumulate evidence from parallel branches with
reducers) while making the state correct under both concurrency and multi-turn memory.
It also gives each reducer a single, explainable job.

---

## D-003 · Risk tagging that fits Groq's free tier *(Spec deviation, flag C)*
**Status:** Accepted · implemented in Phase 1 (`ingestion/enrich/risk_tagger.py`)

**Problem.** Roughly 200 companies × 2 10-Ks × 40–60 risk factors each is about 20k risk
factors. Sending each full risk factor to the LLM is on the order of 10–15M tokens
(a pre-measurement estimate). Groq's free tier enforces per-model daily token and request
limits far below that, so a naive tagger would stall for days or fail mid-run.

**Fix.**
1. **Tag only what's needed.** The latest 10-K's risk factors go to the LLM. For the
   prior-year 10-K, each risk factor is matched to the latest year by embedding similarity
   (the same machinery `compare_risk_factors` needs anyway). A high-similarity match
   inherits the tag; only new or materially changed risks go to the LLM.
2. **Send less per risk.** Heading plus the first ~150 words carries the category signal;
   the full body isn't needed to classify.
3. **Batch** several risk factors per request with structured output, and retry on
   validation failure.
4. **Cache by content hash** in Postgres, so re-runs and resumed runs cost nothing.
5. **Respect limits.** Read Groq's `x-ratelimit-*` response headers, back off on 429, and
   let the job resume across days through `ingestion_jobs`.

**Process.** `make ingest-sample` (10 companies) records real tokens per company. From
that we project full-run tokens, time on the free tier, and cost on the paid tier, and
report them **before** the full run. The owner then decides whether to upgrade.

**Why.** Cost-aware LLM pipelines are a real engineering concern. Similarity-based tag
inheritance is also more *consistent*: an unchanged risk keeps the same category across
years, so year-over-year comparisons aren't polluted by LLM label noise.

---

## D-004 · XBRL annual values: dedupe by reporting period, not `fy` *(Spec deviation, flag E)*
**Status:** Accepted · implemented in Phase 1 (`ingestion/edgar/xbrl.py`)

**Problem.** In SEC `companyfacts`, each fact carries `fy`, `fp`, `form`, `start`, `end`,
`filed`, `accn` and sometimes `frame`. `fy` is the fiscal year **of the filing that
reported the value**, not of the period the value describes. A FY2024 10-K reports
FY2024, FY2023 and FY2022 revenue as comparatives, and all three are tagged `fy=2024`.
Filtering on `form == "10-K" and fy == 2024` therefore returns three different revenue
numbers for "2024". Picking one at random gives wrong growth rates, and that would
directly break demo query 3.

**Fix.** Treat a value as annual when `form` is 10-K (or 10-K/A) and, for duration facts,
`end - start` is 350–380 days. Identify the period by its **`end` date** and derive the
fiscal-year label from that. Dedupe on `(concept, end)`, keeping the most recently
`filed` value, so restatements win. Instant facts (total assets, cash) have no `start`;
match their `end` to the company's fiscal-year-end date. `frame` (e.g. `CY2023`) is used
as a cross-check, not the primary key, because SEC assigns it to only one fact per
calendar period. The revenue concept-mapping table (D-012) runs *after* this dedupe.

**Why.** It's the difference between plausible-looking and correct numbers. Unit tests use
a saved companyfacts fixture containing comparatives and a restatement.

---

## D-005 · `interrupt()` nodes must be side-effect free before the interrupt *(Design rule, flag G)*
**Status:** Accepted · enforced in Phase 3 (`entity_resolver`, `query_analyzer`)

**Problem.** When a graph resumes with `Command(resume=...)`, LangGraph does **not**
continue from the line where `interrupt()` was called. It **re-runs the whole node from
the top**, and `interrupt()` returns the resume value the second time. Anything before the
interrupt runs twice: LLM calls (double cost and latency), DB writes (duplicates) and
event emission. Worst, an LLM-generated question could come out *different* on the second
run from the one the user answered, so the answer would be applied to options the user
never saw.

**Fix.** Split "decide" from "ask":
- An **analysis node** (e.g. `query_analyzer`) does the LLM work and writes the
  clarification question and options into state. Its result is checkpointed.
- A tiny **ask node** reads that from state and calls `interrupt()`. Re-running it is
  free and deterministic, because it only reads checkpointed state.
- No node writes to external systems before an `interrupt()` call.

An agent test asserts that the LLM is called exactly once across an interrupt → resume
cycle.

**Why.** It makes human-in-the-loop correct, not just working: the user always answers the
exact question that was persisted.

---

## D-006 · uv workspace monorepo with a shared `core` package
**Status:** Implemented (Phase 0)

The API, agent, ingestion pipeline and evals all need the same settings, DB models and
Weaviate schema. A uv workspace (root `pyproject.toml`, members added per phase) gives one
lockfile and one virtualenv, so there's no version drift between components. `core` holds
the shared pieces. Members join the workspace only when they have real code: ingestion in
Phase 1, evals in Phase 6. Python is pinned to 3.12 for the widest wheel support across the
ML and DB dependencies.

## D-007 · psycopg 3 as the only Postgres driver
**Status:** Implemented (Phase 0)

LangGraph's `PostgresSaver`/`AsyncPostgresSaver` requires psycopg 3. SQLAlchemy 2 supports
psycopg 3 natively (`postgresql+psycopg://`), including async. Using it everywhere instead
of adding asyncpg means one driver, one URL format and one set of connection semantics.

## D-008 · Liveness vs readiness, and a lazy Weaviate connection
**Status:** Implemented (Phase 0)

`/health` never touches dependencies, so a Postgres blip doesn't make an orchestrator
restart a healthy API. `/ready` checks Postgres (`SELECT 1`), Weaviate (`is_ready()` over
the v4 client) and Groq key **presence**, all concurrently, each with a timeout. It returns
503 with a per-check breakdown when anything fails. The Groq check doesn't call Groq
because the probe is polled often and must not spend quota. The Weaviate client is created
unconnected and connects on first use, so the API boots and *reports* an outage instead of
crash-looping when Weaviate starts slower than the API.

## D-009 · fastembed (ONNX) instead of sentence-transformers
**Status:** Accepted · implemented in Phase 1

Same `bge-small-en-v1.5` weights, but fastembed runs them on ONNX Runtime with no PyTorch
dependency. That cuts roughly 1.5 GB from the API image, which matters for free-tier
Railway/Render memory and build limits. Embeddings are computed by us and passed to
Weaviate (bring-your-own vectors), so Weaviate needs no vectorizer module.

## D-010 · News: store-first, live NewsAPI disabled in production *(Spec deviation, flag D)*
**Status:** Partly implemented (Phase 0 config flag) · completed in Phase 1/2

NewsAPI's free Developer plan allows 100 requests/day, about one month of history, and
truncated `content`, and its terms restrict it to development use. News is therefore
ingested offline into Postgres and Weaviate. `search_news` reads the store first, and the
live fallback is controlled by `NEWS_LIVE_FALLBACK`, which `Settings` forces off when
`APP_ENV=production` (unit-tested). If news is unavailable, the agent answers from filings
and says so.

## D-011 · Groq model ids live only in `.env`
**Status:** Implemented (Phase 0) · models verified 2026-11-04

Groq's catalogue changes often (models are added and deprecated). Two slots,
`GROQ_MODEL_REASONING` and `GROQ_MODEL_FAST`, are set from the live
`GET /openai/v1/models` list after checking structured-output support. Code never names a
model. A per-model price table (also config) drives cost accounting in `runs`.

**Verified 2026-11-04** against the live API with the project key:
`GROQ_MODEL_REASONING=openai/gpt-oss-120b` and `GROQ_MODEL_FAST=openai/gpt-oss-20b` are
both listed as active (131,072-token context). Both returned valid output for a
`response_format: json_schema` request with `strict: true` (enum constrained) and for a
forced function call (`tool_choice` naming the function). Two findings feed Phase 1:
- **Hidden reasoning tokens.** gpt-oss is a reasoning model, so a one-line classification
  billed 139 completion tokens at the default effort and 52 with `reasoning_effort: "low"`
  (same prompt, 120b). Extraction and tagging calls set low effort explicitly.
- **Rate-limit headers** on this account: `x-ratelimit-limit-tokens: 8000` (per-minute
  token budget) and `x-ratelimit-limit-requests: 1000`. The per-minute token budget, not
  the request count, is the binding constraint for batch tagging. The tagger paces itself
  from `x-ratelimit-remaining-tokens` / `x-ratelimit-reset-tokens`.

## D-012 · XBRL revenue concept mapping
**Status:** Accepted · implemented in Phase 1

Companies report revenue under different us-gaap concepts (`Revenues`,
`RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`, …), and some
switch concepts between years. A priority-ordered mapping table picks the best available
concept **per company per fiscal year**, after period dedupe (D-004), and records which
concept was used so the number is auditable.

## D-013 · Windows support: `make.ps1` twin of the Makefile
**Status:** Implemented (Phase 0)

GNU make isn't on Windows by default. The Makefile stays the canonical task list (CI runs
it on Linux), and `make.ps1` exposes the same targets with the same names. Two Windows
details are handled explicitly. First, native commands don't stop PowerShell on failure,
so each step checks `$LASTEXITCODE`. Second, `npm` resolves to an `npm.ps1` shim that
misreads splatted `--flags`, so the script always invokes the real executable
(`npm.cmd`). `.gitattributes` forces LF line endings (CRLF only for `.ps1`) so shell
scripts and Dockerfiles behave the same when built from a Windows checkout.

## D-014 · Bank 10-K extraction failures are accepted and reported *(flag F)*
**Status:** Accepted · Phase 1

Some large banks file a cross-reference-index 10-K whose risk factors live in the
annual-report exhibit (EX-13), so Item 1A extraction fails. The universe starts at about
205 companies to absorb this. Failures are reported with reasons, and exhibit parsing is
added only if successes drop below 190.

## D-015 · Fiscal-year ends are stated, not hidden *(flag L)*
**Status:** Accepted · Phase 3 (synthesizer prompt)

"Last fiscal year" means different calendar periods for different companies (Apple's
fiscal year ends in September, NVIDIA's in January). Cross-company answers state each
company's fiscal-year end next to the numbers.

## D-016 · Windows host runs use a selector event loop (`core.aio`)
**Status:** Implemented (Phase 0)

Running the API on the Windows host, `/ready` reported Postgres as down with
`InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`.
Windows' default asyncio loop is the Proactor loop, which psycopg 3's async mode doesn't
support. `core.aio.selector_loop` returns a `SelectorEventLoop` on Windows (and a normal
loop elsewhere). `make dev-api` passes it to uvicorn (`--loop core.aio:selector_loop`), and
every CLI entrypoint must use `core.aio.run()` instead of `asyncio.run()`. Inside the
Linux containers nothing changes, and uvicorn keeps uvloop.

## D-017 · Collision-free host ports (Postgres 5442, Weaviate 8095/50055)
**Status:** Implemented (Phase 0)

The dev machine runs two native Postgres servers (5432 and 5433), and another project's
Weaviate stack publishes 8080, 8090, 50051 and 50052. Standard mappings first produced
Apache 404s from "Weaviate", then a `port is already allocated` error once that stack
started. Worse, Docker *did* bind 5433 alongside the native server, so `localhost:5433`
from Windows could reach either database. Host ports are configurable
(`POSTGRES_HOST_PORT`, `WEAVIATE_HOST_HTTP_PORT`, `WEAVIATE_HOST_GRPC_PORT`) and default to
5442 / 8095 / 50055, all verified free. Containers still use standard ports internally.
A host-side check confirmed `select version()` returns the container's PostgreSQL 18.6.

---

## Adaptations: where libraries or tools differed from the spec's assumptions

- **Postgres 18 image data path (Phase 0).** From v18 the official image sets
  `PGDATA=/var/lib/postgresql/18/docker` and declares its `VOLUME` at `/var/lib/postgresql`
  (docker-library/postgres#1259). Most compose examples online still mount
  `/var/lib/postgresql/data`, which no longer matches. `docker-compose.yml` mounts
  `/var/lib/postgresql` as the image docs instruct.
- **Next.js 16 scaffold (Phase 0).** `create-next-app@16.4.5` enables `cacheComponents`
  and `partialPrefetching` by default and uses `@tailwindcss/turbopack` for Tailwind v4.
  We keep those defaults and add `output: "standalone"` for a slim Docker image.
- **Ruff 0.17 formats Markdown code blocks (Phase 0).** `ruff format --check` flagged the
  Python sketch inside `BUILD_SPEC.md`. Markdown is excluded from ruff so specs and docs
  aren't rewritten.
- **Weaviate 1.40 single-node startup under Docker Desktop/WSL2 (Phase 0).** The container
  exited with `could not init cluster state: create memberlist: failed to get final
  advertise address: no private IP address found`, even though eth0 had 172.21.0.2. Its
  interface carried an `M-DOWN` flag, which probably defeats auto-detection (not
  confirmed). Setting `CLUSTER_ADVERTISE_ADDR=127.0.0.1` (documented, and correct for one
  node) fixes it.
- **`DEFAULT_VECTORIZER_MODULE` deprecated in Weaviate 1.40 (Phase 0).** It is ignored and
  logs a warning. Removed from compose; our collections will declare `vectorizer: none`
  explicitly, which is what we want with bring-your-own vectors anyway.
- **`make.ps1` and native stderr (Phase 0).** In Windows PowerShell 5.1, a redirected
  stderr line from a native tool becomes an error record. With
  `$ErrorActionPreference = "Stop"`, docker's progress output aborted `make.ps1 up`.
  `Invoke-Step` now runs native tools under `Continue` and judges them only by
  `$LASTEXITCODE`.
- **First CI run on GitHub (Phase 0).** Two failures that a warm local checkout hid.
  (1) `astral-sh/setup-uv@v10` didn't resolve, because that action publishes no moving
  major tags; it is pinned to the exact release `v10.3.3`. (2) `tsc` failed with `Cannot
  find name 'LayoutProps'`: Next 16 generates route types such as `LayoutProps` into
  `.next/types`, which a clean checkout lacks. `typecheck` now runs `next typegen` first.
  Reproduced locally by deleting `.next/` before applying the fix.
- **authlib deprecation warning (Phase 0).** `weaviate-client` imports authlib, which emits
  a `DeprecationWarning` and forces it visible with its own `simplefilter("always")`, so
  pytest's filters can't silence it. It is harmless and outside our code, so we leave it.
