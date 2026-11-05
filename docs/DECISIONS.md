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

**Retry cap and unresolved tags (owner decision, Phase 1).** Groq enforces strict-mode
schemas on its side and rejects a generation that breaks them (HTTP 400
`json_validate_failed`) without reporting token usage. Every structured call therefore gets
at most **2 attempts** (first try + one retry with the error fed back). If a risk-tag batch
still fails, the valid tags in the last output are kept; any risk without a valid category
is stored as `other` with `needs_review = true`, and the report counts these and the schema
rejections. Retries can't quietly burn the daily quota, and nothing is silently mislabelled.

**Headings-only comparison scope (owner decision).** The headings-only mode runs on three
companies with different 10-K layouts (AAPL, INTC, XOM), not the whole sample: running
both modes everywhere would roughly double tagging tokens for little extra signal.

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

## D-018 · Company universe: 205 large caps, validated against SEC tickers
**Status:** Implemented (Phase 1, `data/universe.csv`)

205 US large caps across all 11 GICS sectors (12–27 per sector), picked by hand to cover
every sector, every demo-query company, and a range of 10-K formats. The sample includes
banks, which may fail extraction (D-014). Every ticker was checked against SEC's
`company_tickers.json` on 2026-11-04, and the `name` column is SEC's registered title,
not a hand-typed name. That check found that MMC now trades as **MRSH** (same CIK 62709),
and that EA, WBD, AVB and EQR had no entry in the SEC ticker file. Those four were
replaced with same-sector large caps (FOXA, RDDT, IRM, EXR). We did not investigate why
they were absent; without a ticker → CIK mapping the pipeline can't resolve them.

Beyond the spec's `ticker,name`, the file has a `universe_sector` column. This is the
**coverage bucket used to pick the universe**, labelled from general GICS knowledge. It
is not a pipeline output. The stored sector fields come only from the SIC mapping and the
LLM classifier (§3.4). The bucket is kept as an independent reference for sanity-checking
those two signals, and for reporting coverage by sector.

## D-019 · One Groq client for structured output, talking to the HTTP API directly
**Status:** Implemented (Phase 1, `core/llm.py`)

Every structured LLM call (risk tags, sector, section boundaries; later the planner and
critic) goes through `GroqClient.structured()`, which returns a validated Pydantic object.
It uses Groq's OpenAI-compatible endpoint directly instead of `langchain-groq`, because the
free tier forces three things the wrapper hides: pacing from the `x-ratelimit-*` response
headers, telling a per-minute 429 (wait and retry) from a per-day 429 (stop, resume
tomorrow), and recording exact `usage` per call (including gpt-oss's hidden reasoning
tokens) into `llm_calls`. Requests use strict `json_schema` (schemas are rewritten for strict
mode: `$defs` inlined, every property required, `additionalProperties: false`), retry up to
2 times on validation failure with the error fed back, run one at a time per model (with an
8K TPM budget, parallel calls only race each other into 429s), and default to
`reasoning_effort: "low"`. Responses are cached by a hash of model, schema, messages and
effort; prompts are versioned `.md` files, so editing a prompt invalidates its cache. The
agent's tool-calling loops (Phase 3) will use `langchain-groq` for LangGraph integration,
and token accounting there will write to the same ledger.

## D-020 · Successor registrants: merge the predecessor CIK's history
**Status:** Implemented (Phase 1, `pipeline.PREDECESSOR_CIKS`)

SEC's ticker map points XOM at CIK 2115436, "ExxonMobil Holdings Corp", a new registrant
created on 2026-07-01 (its first filing is an `8-K12B`, successor issuer). It has no 10-K
yet; both 10-Ks and the XBRL history are under Exxon Mobil Corp (CIK 34088). We keep a
small, explicit successor -> predecessor map and merge the predecessor's filings and
companyfacts. A company with no 10-K and no mapping fails with a reason that names this
case, rather than silently ingesting nothing. An explicit map was chosen over automatic
detection because guessing a predecessor (e.g. by name search) could attach the wrong
company's history; each mapping is checked by hand.

## D-021 · Risk-factor splitting from styling, validated on the sample 10-Ks
**Status:** Implemented (Phase 1, `extract/risk_splitter.py`, `extract/html_clean.py`)

10-Ks mark risk-factor headings with *styling*, and filers do it differently, so the HTML
is first flattened into blocks that remember emphasis (bold/italic/underline from tags or
inline CSS), the leading emphasized run, and font size. A heading is a block that is
mostly emphasized, or set clearly larger than the section's body text (Intel: 12pt
non-bold headings over 9pt body). Paragraphs that open with an emphasized sentence are
"inline" headings (ExxonMobil's "Economic conditions. ..."), except on bullets, which are
points inside a risk (Intel, AMD). Short unpunctuated headings followed by another heading
are subsection labels. The "summary of risk factors" bullet list that many filers put first
is skipped, but only before the first real risk factor, since later risks can have bulleted
bodies. Page furniture is removed only when it is a repeated line with a page number or plain
small text ("Table of Contents"): Intel repeats its 18pt "Risk Factors" title as a running
header, and dropping all repeated lines also dropped the real heading. Each rule came from a
concrete failure on a sample filing, and each has a test, either on the real fixtures (Apple,
ExxonMobil, JPMorgan, Intel) or on synthetic blocks.

## D-022 · `--sample N` means the agreed sample, not the first N rows
**Status:** Implemented (Phase 1)

The spec's `--sample 10` doesn't say *which* 10. Taking the first rows of `universe.csv`
would give ten IT companies. `--sample N` takes the first N of the agreed list
(AAPL, NVDA, AMD, INTC, BA, TSLA, JNJ, UNH, XOM, JPM): demo-query companies, five sectors,
four 10-K layouts, and one bank. `--tickers A,B` selects any subset.

## D-023 · Sector stored on vector objects: LLM sector, falling back to SIC
**Status:** Implemented (Phase 1)

Weaviate objects carry one `sector` filter field. We use the LLM classification when present
(it reflects GICS-style reasoning about the business), falling back to the SIC rule. Both
signals and the conflict flag stay in Postgres for the screener and the reports.

## D-024 · Groq prompt caching: measured, hit-and-miss, not something to plan around
**Status:** Measured on gpt-oss-120b (2026-11-04); gpt-oss-20b measurement pending its quota

Groq's docs say prompt caching is supported for gpt-oss-20b/120b, works on exact prefix
matches, and that "cached tokens do not count towards your rate limits", which would make
a long static prompt nearly free. Tagging requests already put the static part first (system
prompt with instructions + taxonomy, then the schema) and the variable risk factors last.
Probing on 120b with the real tagging prompt (~1.1-1.3K static tokens):

- Hits arrive in **1,024-token blocks** (`usage.prompt_tokens_details.cached_tokens`), and
  only on some calls, even for byte-identical consecutive requests.
- `json_schema` (strict): 1 hit in ~11 calls. `json_object`: 3 hits in ~10 calls. Plain: 1
  in ~5. A first small probe suggested strict mode never caches; a later run got a hit, so
  that conclusion was withdrawn.
- On real batches (XOM, 22 risks) cached tokens were 0-21% of prompt tokens.

So caching is a bonus, not a basis for the projection. The ledger records
`cached_tokens`, and the daily budget counts only *counted* tokens (prompt - cached +
completion), matching how Groq applies its limits. The response mode is selectable
(`json_object` validates locally and avoids server-side schema rejections) and is chosen
from the 20b batch-size/mode experiment. Groq limits are per organization and model, so
extra API keys don't add capacity.

## D-025 · Taxonomy prompt v2 and versioned tags
**Status:** Implemented (Phase 1)

The 14 categories are fixed by the spec; v2 sharpens their *boundaries*, aimed at the
confusions the sample exposed: an overloaded `financial_liquidity` (tax rates, stock price,
M&A), trade policy split between regulatory and geopolitical, demand pressure split between
macroeconomic and competition, and M&A with no home. Each category now has include/exclude
notes, there are explicit tie-break rules (classify by the *source* of harm, not the
consequence), and there are eight one-line examples. All of it is static, so it belongs to
the cacheable prefix. Every tag stores `tag_version` = prompt version + model (for example
`risk_tagging:v2@openai/gpt-oss-20b`). Tagging redoes only missing or stale tags, so moving
to a new prompt (or a dev run on another model) never needs a destructive wipe, and an
interrupted re-tag resumes. Accuracy is measured against the owner's labels on a 30-row
stratified review sheet (`make review-export` / `review-score`), not against another model run.

## D-026 · Run-level token accounting and the daily full-run mode
**Status:** Implemented (Phase 1)

Every LLM call and every job carries a `run_id`. A response-cache hit records the counted
tokens the original call cost (`saved_tokens`). The report prices "one complete pass" per
company from the runs that completed its stages, so retries, killed runs and re-extractions
no longer inflate per-company cost (the problem hand-corrected in the first sample report).
The full run is a daily, resumable job: `make ingest-daily` runs the whole universe, stops
cleanly at `GROQ_DAILY_TOKEN_BUDGET` (default 170K counted tokens per model per rolling 24h,
below Groq's 200K, for headroom) and at `NEWSAPI_REQUESTS_PER_DAY` (90 of 100). It also
prints a progress line before and after: companies done/remaining, tokens used in the last
24h, NewsAPI requests today. Paused stages resume on the next day's run. The rolling 24h
window is our own conservative choice: Groq's docs don't say whether the daily limit is
rolling or reset at a fixed time.

## D-027 · Agent and dev testing use a different model than ingestion
**Status:** Implemented (config); used from Phase 2

Groq's daily limits are per model, so the agent and dev testing use `AGENT_MODEL_*`
(set to gpt-oss-120b) while ingestion uses gpt-oss-20b. They never compete for the same
daily quota during the multi-day full run. `GroqClient(agent=True)` resolves the agent models.

## D-028 · Tool contract: typed I/O, evidence for citations, never raise
**Status:** Implemented (Phase 2, `agent/tools/base.py`)

Every tool is an async function with a Pydantic input model, registered with `@tool`
(the docstring is the description the LLM sees). `call_tool` validates input, enforces a
per-tool timeout, and turns *any* failure (bad input, not found, timeout, upstream error,
an unexpected exception) into `ToolResult(ok=False, error={code, message})`, so a failing
tool can never crash the graph (BUILD_SPEC 4.4). Successful results carry typed `Evidence`
(stable `source_id`, URL, exact supporting snippet, form/year/section or publish date).
That is what the Phase 3 synthesizer will cite as `[n]`. Tools resolve tickers against
whatever companies are ingested: unknown tickers produce a warning on partial requests,
or a `not_found` error that lists the available tickers, so nothing assumes the full
205-company universe.

## D-029 · Text-to-SQL safety in two independent layers
**Status:** Implemented (Phase 2, `agent/tools/sql_guard.py`, `screener.py`, migration)

*Layer 1, sqlglot:* exactly one statement; it must be a query; no DML/DDL/COPY/SET/INTO
anywhere in the tree; only allowlisted tables (public schema) and columns; no `SELECT *`;
no unrecognized functions (sqlglot parses unknown calls such as `pg_sleep` as `Anonymous`,
and those are rejected, so nothing relies on a denylist); LIMIT capped at 100. Column
checks resolve aliases **per scope** with sqlglot's scope analysis. A first version used one
global alias map and rejected a correct model query that reused `pr` for a table inside a
CTE and for the CTE outside it. *Layer 2, Postgres:* the query runs inside a READ ONLY
transaction after `SET LOCAL ROLE fira_readonly`, a NOLOGIN role with column-level SELECT
grants equal to the allowlist (a test keeps the migration and the allowlist in sync), plus
`statement_timeout` = 5s. Verified directly: DELETE and reading `risk_factors.body` are
denied, and `pg_sleep(3)` is cancelled. Using `SET ROLE` avoids a second database password.
The model gets one retry with the validator's or database's error as feedback (the same
2-attempt cap as D-003). The prompt (v2) adds one-row-per-company and "latest 10-K means
each company's MAX(fiscal_year)" rules, plus two worked examples, after the first model
query joined risk_factors and repeated companies. The allowlist leaves out free-text
bodies: the screener answers structured questions; text questions go to `search_filings`.

## D-030 · News: store first, guarded live refresh
**Status:** Implemented (Phase 2, `agent/tools/news.py`)

`search_news` always searches the store (hybrid search over ingested articles). It calls
NewsAPI only for companies whose newest stored article is older than 3 days, only when
live fallback is enabled (never in production), at most 3 companies per call, and only
under the shared 90-requests/day cap that ingestion also respects. Fetched articles are
stored with the same ids ingestion would use, so they're idempotent. Any NewsAPI failure
returns store results plus a warning, so news being unavailable never fails the
question. The NewsAPI client and the prompt loader moved from `ingestion` to `core`, so
the agent doesn't import the ingestion package.

## D-031 · compare_risk_factors: embeddings decide identity, a word diff decides change
**Status:** Implemented (Phase 2; revised after owner review)

*Identity.* Risks are matched across two 10-Ks one-to-one, greedily by descending bge cosine
(heading + first 150 words); at least 0.90 means the same risk. On Apple FY2025 vs FY2026 a
genuinely new risk (AI) scored 0.84 against its nearest old risk, and reworded ones scored
0.94-0.97. That matches the 0.90 ingestion uses to reuse a tag.

*Change.* The first version also used cosine for "changed vs unchanged" (below 0.985 =
changed). The owner flagged Apple's "third-party developers" risk as wrongly "changed":
identical heading, cosine 0.976. A word-level diff of heading + full body shows it **did**
change materially: +261 words of new App Store commission and alternative-distribution
language (diff ratio 0.664). The embedding of the opening words can't see a long addition.
So change is now decided by the word diff (`difflib`, ratio of matching words): at least
**0.95** with the same category is *unchanged*; anything else is *changed*, labelled
`reworded`, `recategorized` or both. Calibration pairs: Apple "carriers and other resellers"
0.992 (+2/-1 words) and J&J "counterfeit versions" 0.987 (-5) are unchanged; J&J "tax laws"
0.931 (+6/-35) and Apple "third-party developers" 0.664 (+261) are changed. Changed items
report words added/removed and the largest added passage, so the agent can say *what*
changed. Counts are returned for new / removed / changed / unchanged (Apple: 1/0/20/7, a
heavily revised 10-K; J&J: 3/0/2/18, the 3 new risks being the planned Orthopaedics
separation).

## D-032 · News relevance: entity gate, absolute score, semantic floor
**Status:** Implemented (Phase 2, `agent/tools/news_relevance.py`; revised after owner review)

The first `search_news` ranked by Weaviate's hybrid score and returned off-topic hits for
TSLA (a Rivian article, a tax opinion piece, an MG car review), two of them scoring exactly
0.5. That's not a default value: Weaviate's relative-score fusion min-max normalises the
BM25 and vector scores within each query's result list and averages them, so an article
that is top on one signal and absent from the other scores exactly 0.5. Those scores are
relative and can't be thresholded. The stored news is also noisy (43% Biztoc re-posts,
plus package-index and deal-site entries). Now, hybrid search only collects 50 candidates,
and then:

1. **Entity gate:** the company's name, a distinctive first word ("Ford", "Jpmorgan"; not
   "General", "Advanced", "Home"), or its ticker (3+ letters) must appear in the title, or
   for non-aggregator outlets in the description. Biztoc descriptions list trending
   tickers, so only its titles count.
2. **Sources:** package indexes and deal sites are excluded; Biztoc is down-weighted (x0.8).
3. **Score** = (0.75 x semantic cosine + 0.25 x keyword coverage) x source weight. It's
   absolute and returned per hit with its parts.
4. **Floor on semantic similarity, 0.58.** On stored NVDA/TSLA articles relevant hits scored
   0.61-0.63 and irrelevant ones 0.44-0.56, while the combined score separated them less
   cleanly (0.435 relevant vs 0.427 irrelevant). So the floor uses semantic similarity
   and the combined score only ranks.

Before -> after: TSLA "earnings guidance" went from 5 off-topic hits to none; the store
holds no Tesla earnings coverage, so "no relevant news" is the correct answer. NVDA "export
restrictions China" now returns only the three China-business stories. Known limit: a
borderline hit can still pass (an India EV-sales story mentioning Tesla scored 0.583 vs
the 0.58 floor). The floor was calibrated on a small set and will be re-checked on full-run
data. NewsAPI requests now ask for 100 results (the free-plan maximum, same cost) instead
of 50, because 50 covered only a few days for heavily covered companies.

---

## D-033 · Agent LLM layer: one budget, a dev cache, tokens per node
**Status:** Implemented (Phase 3, `agent/llm.py`, `agent/runlog.py`)

The agent uses gpt-oss-120b only (`AGENT_MODEL_*`), whose daily quota is separate from the
ingestion model's. Every agent call goes through `GroqAgentLLM`:
- **Structured output** (analyzer, planner, critic) reuses Phase 1's `GroqClient` (strict
  `json_schema`, at most 2 attempts). **Tool-calling and streaming** (specialists,
  synthesizer) go through langchain-groq `ChatGroq` so LangGraph's `messages` stream mode
  sees the tokens.
- **Budget guard:** before each call, the 24-hour total for the agent model is checked
  against `AGENT_DAILY_TOKEN_BUDGET` (150K). Over budget, nodes degrade instead of failing
  (the critic answers "ok", a failed replan answers from what was collected).
- **Dev response cache** (`AGENT_DEV_CACHE`), keyed by model, effort, messages and tools:
  re-running a demo after changing one prompt only pays for the nodes whose input changed
  (demo 1's re-run cost 6.7K instead of 11.8K).
- **Ledger:** every call, including cache hits with the tokens they saved, is recorded
  with the run id, so `node_usage(run_id)` gives tokens per node; the CLI prints it.
- **Reasoning effort per node:** low for the analyzer and specialist loops, medium for the
  planner, critic and synthesizer. Measured on the live demos: reasoning tokens were
  26-114 per specialist step and 47-317 per analyzer turn (low), against 203-864 per
  planner call (medium). There is no same-query comparison of the two effort levels yet.

## D-034 · Tests make zero Groq calls
**Status:** Implemented (Phase 3, `tests/agent/fakes.py`, `tests/conftest.py`)

Every node reaches the outside world through `AgentDeps` (LLM, tool executor, company
list, clock, run recorder, fiscal periods). The tests build the same compiled graph with a
scripted `FakeLLM` and fake tools, and an autouse respx guard fails any test that tries to
reach api.groq.com. The checkpoint test runs interrupt/resume against the real Postgres
saver, over a fresh connection.

## D-035 · Synthesis: shared source cap, exact fiscal periods, checked citations
**Status:** Implemented (Phase 3, `agent/nodes/answer.py`; fixes from the live demos)

- **Sources are numbered round-robin across steps** (cap 30, 300-character snippets).
  In step order, AMD's 53 risk factors took every slot in demo 1 and left NVIDIA and the
  news uncitable.
- **Fiscal periods come from the database.** The synthesizer is given each company's stored
  10-K fiscal years and period-end dates. Given only the MMDD fiscal-year end, it wrote
  Apple's FY end as 09-30 (actual: 2026-09-26).
- **Citations are checked deterministically.** gpt-oss often cites in its native style
  (`【3†source】`) despite the prompt. `finalize` maps those to `[3]`, removes other bracket
  references (step ids) and any number that isn't a source, then adds the footer.
- **Prompt rules from the demos:** news supports a risk only when the headline is directly
  about it; no verdicts of its own ("moderate-to-high risk", or "high" leverage for a bank
  whose ratio is normal for banks).
- **Wrong-but-existing sources** (demo 7 quoted one Intel risk factor and cited another)
  are caught by the citation check in D-038.

## D-036 · Replans: bounded, additive, and judged at the question's level
**Status:** Implemented (Phase 3, `agent/nodes/planning.py`, critic prompt v2)

Replans add steps with new ids (`r2s1`…), never touch completed results, and are capped
at 2. In demo 7 the critic read the 1,200-character review digests as missing data and
asked for full lists, so two rounds re-fetched the same comparison (27.7K tokens). Digests
are now marked as shortened. Critic prompt v2 judges at the question's level of detail and
never asks for a tool call that was already made. The planner (v2) also leaves optional
years out unless the user names them, because guessing them produced a wrong comparison.
`compare_risk_factors` now normalises the year order itself, so "new" always means
"in the later filing".

## D-037 · Entity resolution is deterministic; ambiguity asks
**Status:** Implemented (Phase 3, `agent/nodes/intake.py`)

Company mentions are matched against the ingested companies with names, aliases and
uppercase tickers (a 1-letter ticker only as `$F`), using rapidfuzz's strict `ratio`. Its
`WRatio` partial matching matched "Intel" to "Johnson Controls International". A shared
short name ("Johnson": JNJ and JCI) interrupts with the candidates. On resume only an exact
full-name or ticker match is kept, and the run returns to the node that asked. Companies
the dataset doesn't cover are named in the answer with close matches. They are never
answered from model knowledge.

## D-038 · Citation verification with the local embedding model
**Status:** Implemented (Phase 3, `agent/citecheck.py`, used in `finalize`; zero LLM tokens)

`resolve_citations` only guarantees that each `[n]` exists. Demo 7 showed that a real
source can still be the wrong one: a quote ("We have ceased to return capital to
stockholders") was cited to a different risk factor. `finalize` now checks every citation
before the footer is added:

- **Claim:** each citation group closes a claim, which is the text since the previous group
  or sentence/table-cell boundary. When that is under 4 words, the claim is the cell or the
  line, and for a citation alone in a table's "Source" column it is the rest of the row.
  Claims still under 4 words ("None [1]") are not checked.
- **Score:** the cosine similarity of claim and source (title + full snippet) with the local
  bge-small model, the one already loaded for news relevance. No tokens are spent.
- **Action:**
  - A source scoring ≥ **0.585** supports the claim.
  - A citation below that is moved only if another source clearly matches (≥ **0.75**).
  - Otherwise the citation is dropped. A claim left with no citation is marked
    `[unverified]`, and the footer explains the marker.
- **Logged per run:** checked / supported / re-attached / dropped / unverified claims and
  precision (supported ÷ checked). These go to `runs.citation_check`, a JSON log line, the
  final stream event and the CLI.

**Calibration** used the 68 (claim, cited source) pairs in the 2026-11-04 demo answers,
rebuilt from saved run state and labelled by hand:
- The one confirmed wrong citation (demo 7 [15]) scored **0.567**. The lowest correct one
  was 0.604, a "Removed: None" row citing the comparison summary. 0.585 is the midpoint.
- Correct quote-to-source pairs scored 0.745-0.915. Short aggregate claims citing a summary
  or an XBRL fact scored 0.60-0.72.
- The wrong citation's best alternative (0.614) was an unrelated business-overview chunk;
  the right risk factor wasn't among the numbered sources. That is why re-attaching needs
  0.75 and the claim is marked unverified instead.

Replayed with the real model over the saved answers, the check flags exactly that citation:
precision 1.0 on five runs and 0.933 on demo 7 (14/15). A run with no checkable
citations has no precision.

**Limits.**
- The margin rests on one known error and is thin (0.567 vs 0.604), so this is a
  calibrated heuristic, not a measured error rate. The Phase 6 citation-accuracy eval
  should re-calibrate it on a labelled set.
- Similarity shows that a source is about the claim, not that the claim's reasoning holds.
  Demo 1's loose news-to-risk links cite the right articles and pass.

**Incident while adding `runs.citation_check`.** `alembic revision --autogenerate` compared
the database with our metadata. LangGraph's checkpoint tables aren't in that metadata, so it
proposed dropping all four. The migration was applied before that was caught, and the
saved thread state of the 2026-11-04 demo runs was lost (no backup or WAL archive was
configured).
- **Not affected:** the `runs` log (plans, steps, answers, tokens), all ingested data, the LLM
  ledger and the transcripts in `docs/demo-runs/`.
- **Effect:** the demo 7 follow-up needs demo 2's thread, so both are re-run.
- **Fix:** the migration now only adds the column, and the saver recreated empty tables.
  `alembic/env.py` passes `include_object` so autogenerate ignores tables that exist in the
  database but not in our metadata; `alembic check` is clean with them present.
- **Practice:** autogenerated migrations are applied in a separate step after review, never
  in the same command.

## D-039 · Synthesizer input: tool-ranked top 12 per step, shorter snippets
**Status:** Implemented (Phase 3, `agent/nodes/answer.py`; measured without LLM calls)

The synthesizer was the largest or second-largest node in every run (25-63% of its tokens). Its prompts were
rebuilt from the saved state of the 2026-11-04 runs and counted with `o200k_base`; the
rebuilt sizes match the ledger's prompt tokens within 1-5%.
- **Where the tokens go:** evidence text (the numbered sources) was **70%** of the user
  message and step results 28%. The answers cited only 4-15 of the 30 sources each.
- **Which items to keep:** ranking each step's items by embedding similarity to the
  question kept fewer of the actually cited sources than the tools' own order (58% vs 88%
  at 10 per step). Tools already rank: news by relevance, comparisons summary-first.
- **What changed:**
  - Each step contributes its first **12** items, still round-robin and capped at 30.
  - Snippets are cut to **200** characters at a word boundary, without the heading that
    risk factors and news repeat from the title. The citation check still uses the full
    snippet.
  - `compare_risk_factors` lists each changed risk's later and earlier wording side by
    side. It used to list all later versions first, so a cap kept a change's new wording
    without its old wording.

Replay result: 26,735 -> 22,256 synthesizer prompt tokens over 6 runs (**-17%**). Apple
fell 54% and the multi-step runs 5-13%. All 52 citations of runs on current code still
had their source. The only 3 not kept come from demo 1's first run, whose numbering was
the bug fixed in D-035. The next lever is the step-results section (28%).

## D-040 · Backups: pg_dump of Postgres, restore proven, data-safety rules
**Status:** Implemented (`make.ps1` / `Makefile` targets `backup`, `restore`; CLAUDE.md)

This follows the checkpoint-table loss (D-038). The ingested data costs days of Groq and
NewsAPI quota to rebuild, so the owner set hard rules in CLAUDE.md:
- back up before any migration, re-extraction, re-tag or bulk delete;
- generate and apply migrations in separate steps, with the owner's OK in between;
- no destructive command without asking, in any mode.

- **What is backed up:** Postgres only, as a full `pg_dump -Fc` into
  `data/backups/<db>-<timestamp>.dump` (gitignored). Weaviate is derived: the `index`
  stage reads only Postgres and embeds locally, so `ingestion run --only index --force`
  rebuilds it without quota. Raw EDGAR/NewsAPI downloads in `data/raw/` are a cache.
- **Restore:** `restore <file> -Database <new>` creates a new database and never
  overwrites one. Restoring into the live database asks for its name to be typed, and
  uses `--clean --if-exists`.
- **Proof:** the first backup (`fira-20261105-131536.dump`, 1.4 MB, 17 tables with data)
  was restored into the scratch database `fira_restore_test`. Every table's row count
  matched the live database, the Alembic revision matched, and an md5 over all
  `risk_factors` rows was identical.
- **Windows pitfall:** in PowerShell 5.1, piping a native command's binary output
  (`pg_dump > file`) re-encodes it as text and corrupts the dump. The PowerShell target
  dumps inside the container and copies the file out with `docker compose cp`. PS 5.1
  also doesn't escape embedded double quotes in native arguments, so the container
  commands avoid them. In Git Bash and on Linux the plain pipe works (checked: the piped
  dump lists the same 17 tables).

## D-041 · API: native FastAPI SSE, runs in background tasks, typed events
**Status:** Implemented (Phase 4, `backend/src/api/`)

- **SSE without a new dependency.** FastAPI 0.162 ships SSE (`fastapi.sse`:
  `EventSourceResponse`, `format_sse_event`, keep-alive comments), so `sse-starlette`
  isn't needed. Its generator-style routes start the response before the handler body
  runs, so a 404/409/422/429 can't be raised there. The run endpoints therefore validate
  first and then return an `EventSourceResponse` over our own generator, which sends a
  keep-alive comment every 15 s.
- **Runs are background tasks.** A run is driven by a task that pushes events into a
  queue, and the SSE response only drains that queue. A closed tab or dropped connection
  doesn't cancel a run that is spending tokens: it finishes, is recorded, and its thread
  can be read back. A thread allows one active run (409 otherwise). A new question on a
  thread waiting for a clarification is refused with 409 until `/resume` answers it.
- **Typed events.** Every graph event is validated against a Pydantic discriminated
  union (`api/schemas.AgentEvent`) before it's sent: the spec's types plus `run_started`,
  `event:` = type, `id:` = a gapless sequence. Tokens come from LangGraph's `messages`
  stream mode for the synthesizer node. An unexpected exception ends the stream with an
  `error` event whose message doesn't leak internals.
- **Graph event fixes found while building the API:**
  - `clarification_needed` used to be emitted before `interrupt()` in
    `ask_clarification`. LangGraph re-runs the interrupted node on resume, so every
    resume repeated the question. It's now emitted by the node that creates the question
    (resolver or analyzer), which doesn't re-run.
  - `evidence_added` carries full source cards (the UI's Sources tab), not just ids.
  - `step_completed` carries summary, error and duration.
- **Threads table.** The conversation lives in the checkpointer. A small `threads` table
  (id, title, status, last run) lists threads for the UI and tracks
  `running` / `awaiting_clarification` / `error`. `GET /api/threads/{id}` combines it with
  the checkpointer's state (messages, pending question, latest plan, evidence and
  citations) and the run ids from `runs`.
  - A CHECK constraint (`ck_threads_status`) limits `status` to those four values; a
    test keeps the code's `ThreadStatus` in sync with it.
  - `updated_at`'s server default fires only on INSERT, so every update in
    `core/threads.py` goes through one helper that sets it, from the database clock
    (D-042). Tests run against both the in-memory and the Postgres store.
  - The agent CLI registers its threads too (`ensure()` + status around each run), so
    CLI demo conversations appear in the UI's thread list.
- **Public-demo protection.** Agent runs and resumes are limited per client IP
  (`API_RUNS_PER_MINUTE`, default 6, in-memory sliding window) and questions are capped
  at `API_MAX_QUESTION_CHARS`. CORS is locked to the frontend origin.
- **Tests.** The real app and lifespan with a scripted runtime cover: a full SSE run
  (event order, schema, gapless ids, live tokens), interrupt -> history -> resume over
  HTTP, 404/409/422/429, busy threads, a run with no listener, the error event and the
  graph endpoint. The fake LLM streams through LangChain's `GenericFakeChatModel`, so
  token events are exercised without Groq. The data endpoints have integration tests
  against the local stack.

## D-042 · Single source of truth for time: the database clock
**Status:** Implemented (Phase 4; `core/threads.py`, `core/db/llm_store.py`, `ingestion/report.py`)

**Rule:** any timestamp stored in Postgres, and any time window compared against stored
timestamps, comes from the database's clock (`now()` / server defaults). The host's
clock is used only within one process: step durations, recency scoring against
external publish dates, and file names.

**Why:** the new thread-store test caught `threads.updated_at` going *backwards*. Inserts
took it from Postgres (`server_default=now()`), updates from Python (`datetime.now()`),
and the Docker VM's clock ran about 9 ms ahead of the Windows host. Two writers with two
clocks cannot keep an order. In production the API host and the managed database are
different machines, so the skew would be larger and not under our control.

**Applied:**
- **Thread store:** sets `updated_at = now()` in SQL on every update. The in-memory test
  store uses the process clock, its only clock.
- **Token-budget window:** the daily guard (`tokens_last_24h`) and the ingestion report's
  "last 24 h" figure are now `created_at >= now() - interval '24 hours'`. Before, they
  compared host time with database-set `created_at`. Same totals at today's skew,
  correct at any skew.
- **Reviewed, unchanged:**
  - `started_at` / `finished_at` of agent steps: one process, durations only.
  - News recency scoring: compared with external publish times.
  - Ingestion run ids and file stamps.

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
- **bge tokenizer truncation, verified (Phase 1).** The tokenizer fastembed loads for
  bge-small is configured `truncation: {max_length: 512, strategy: longest_first}`, which
  confirms D-001 against the real artifact: longer inputs would be cut silently. fastembed
  serves a quantized ONNX build (`Qdrant/bge-small-en-v1.5-onnx-Q`, 65 MB). Embeddings are
  close to, but not bit-identical with, the FP32 model; all our vectors come from this one
  build, so they are consistent.
- **Inline XBRL encoding (Phase 1).** lxml mis-guessed the encoding of some filings when
  given bytes. Filings are now decoded explicitly (UTF-8, falling back to cp1252), and the
  `<?xml ... encoding=...?>` declaration is stripped, because lxml rejects `str` input that
  still carries one.
- **Workspace member installed empty (Phase 1).** `uv sync` ran before
  `ingestion/src/ingestion/` had files, so the editable install had nothing in it, and
  `python -m ingestion` resolved the repo's top-level `ingestion/` folder as a namespace
  package ("No module named ingestion.__main__"). `uv sync --reinstall-package
  fira-ingestion` fixed it. Fresh clones are unaffected.
- **lxml has no type information (Phase 1).** mypy is told to ignore missing imports for
  `lxml.*` only; lxml use is confined to `extract/html_clean.py`.
- **Alembic autogenerate made `companies.cik` SERIAL (Phase 1).** An integer primary key
  defaults to autoincrement in SQLAlchemy, which is wrong for a natural key like the CIK.
  The column is `autoincrement=False`, and the initial migration was regenerated before
  anything depended on it.
- **Column lengths caught by a dev smoke test (Phase 1).** Before spending gpt-oss-20b
  quota, the new path was run end-to-end on 120b. It hit two `VARCHAR` overflows:
  `filing_sections.section` (16) vs `business_overview` (17), and `run_id` (36) vs experiment
  run ids (44). The second happened *after* the Groq call returned, so that call's ~4K
  tokens (120b) were never written to the ledger; on the ingestion model this would have
  undercounted the daily budget. Both columns were widened, and every text column was
  checked against its longest real value.
- **"Table of Contents" links split risk factors (Phase 1, found in Phase 2).** Running
  the new `get_risk_factors` tool on real data showed NVIDIA risk factors whose heading was
  "Table of Contents": 111 such rows across 6 of the 10 sample companies. Those filers
  underline the navigation link repeated at every page break; the furniture filter kept
  emphasized repeated lines (to protect Intel's large-font section title), so the splitter
  read each link as a heading and cut risk factors in two at page breaks. Navigation links
  are now dropped regardless of styling. Per-filing risk counts fell accordingly (e.g.
  NVIDIA 38 -> 23, AMD 53 -> 44) and none of these headings remain; a regression test covers
  it. The stored sample is re-extracted (no LLM cost) before the 20b re-tag.
- **SQLAlchemy array column named `items` (Phase 2).** `eight_k_events.c.items` resolves to
  the column collection's `.items()` method, not the column (mypy caught it), so it is
  indexed as `c["items"]`. The generic `sqlalchemy.ARRAY` also has no `.overlap()`; the
  schema now uses `postgresql.ARRAY` (same database type, no migration).
- **pytest-asyncio loop policy (Phase 2).** DB-backed integration tests need a selector loop
  on Windows. Overriding the `event_loop_policy` fixture is deprecated in pytest-asyncio
  1.4; the repo `conftest.py` implements the `pytest_asyncio_loop_factories` hook with
  `core.aio.selector_loop`. Integration tests therefore run on the Windows dev machine and
  skip themselves where no stack is running (CI).
- **authlib deprecation warning (Phase 0, resolved in Phase 2).** weaviate-client imports
  authlib, and authlib >= 1.8.0 (released 2026-08-30; 1.7.x is silent, measured) warns on
  import that its httpx integration moves to `httpx2`. It forces the warning visible with
  `simplefilter("always")` in `authlib.deprecate`, which is why a plain pytest filter
  didn't work. `core/__init__.py` imports `authlib.deprecate` first and then ignores
  exactly that message, which puts our filter in front; `agent`, `ingestion` and `api` import
  `core` first. pytest re-enables DeprecationWarnings per test, so the same single message
  is also listed in its `filterwarnings`. Pinning authlib < 1.8 would have silenced it too,
  but holds back an auth library's releases just to hide a notice.
- **LangGraph reserves `configurable.run_id` (Phase 3).** We first passed our run id as
  `configurable.run_id`. LangGraph also reads that key as the run's own id, and a second
  turn in the same thread with a new value ran as a no-op (no new checkpoint, the old
  answer returned). Our id is now `configurable.agent_run_id`; resumes reuse the paused
  run's id.
- **Checkpoint serializer allow-list (Phase 3).** langgraph-checkpoint 3.x warns
  "Deserializing unregistered type" for Pydantic objects in state that aren't on its
  allow-list. `agent/checkpoint.py` registers our state models with
  `JsonPlusSerializer(allowed_msgpack_modules=...)` for both the Postgres and in-memory
  savers.
- **ChatGroq's HTTP client (Phase 3).** `ChatGroq` creates its own async client and
  nothing closes it, so the CLI ended with an "unclosed transport" warning. The agent
  now passes one `http_async_client` it owns and closes it in `aclose()`.
- **Windows reports `NUL` as a terminal (Phase 3).** `sys.stdin.isatty()` is true when
  stdin is redirected from `NUL`, so the CLI's clarification prompt hit EOF. EOF now
  means "no answer": the run stays paused, to be continued with `python -m agent resume`.
