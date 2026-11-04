# BUILD SPEC — Financial Intelligence Research Agent (LangGraph · Groq · Weaviate)

> Paste this whole file into Claude Code (or save it in the repo root as `BUILD_SPEC.md` and tell Claude Code: "Read BUILD_SPEC.md and start with Phase 0").

---

## 0. Your role

You are a senior AI engineer building a production-grade, portfolio-quality **agentic research system** for financial questions. The owner (Dev) will be interviewed on this project for an AI Engineer role, so:

- Every architectural choice must be **explainable** — add short "why" comments at key decision points and an `docs/DECISIONS.md` log (one paragraph per decision).
- Prefer clear, idiomatic code over clever code. No dead code, no placeholder `TODO` stubs left in "done" phases.
- **Work phase by phase** (Section 9). At the end of each phase: run lint + tests, print a short summary of what was built and how to verify it, then **stop and wait for my go-ahead**.

---

## 1. What this project must prove (resume claims — all must be literally true when done)

1. An agentic research system using **LangGraph, Groq, and Weaviate** that **decomposes natural-language financial questions into multi-step execution plans**, automating research over **SEC filings and financial news**.
2. A data pipeline that **extracts risk factors and sector classifications from 10-K / 8-K filings across 190+ companies** via **SEC EDGAR and NewsAPI**.
3. **Tool-based reasoning** that turns ambiguous natural-language queries into concrete, multi-step retrieval workflows over financial datasets.

The headline the demo should make obvious: **"This is not a RAG chatbot. It plans, runs tools in parallel, checks its own work, asks when it's unsure, and cites everything."**

---

## 2. Tech stack (use these unless you hit a hard blocker — if so, stop and tell me)

| Layer | Choice |
|---|---|
| Language | Python 3.11+, managed with `uv` |
| Agent framework | **LangGraph** (latest stable) + `langchain-groq` |
| LLM | **Groq**. Two configurable model slots in `.env`: `GROQ_MODEL_REASONING` (planner, critic, synthesizer) and `GROQ_MODEL_FAST` (routing, tagging, extraction). Check Groq's current model list; don't hardcode model names in code. |
| Vector DB | **Weaviate** (v4 Python client), local via Docker; Weaviate Cloud for deploy. Hybrid search (BM25 + vector). |
| Embeddings | Local `BAAI/bge-small-en-v1.5` via `sentence-transformers` or `fastembed` (bring-your-own vectors into Weaviate). Optional cross-encoder reranker behind a flag. |
| Structured store | **PostgreSQL** (companies, filings, risk factors, XBRL facts, runs) — also used as the **LangGraph checkpointer** (`PostgresSaver`). |
| API | **FastAPI**, async, **SSE streaming** of agent events |
| Frontend | **Next.js (App Router) + TypeScript + Tailwind + shadcn/ui**, **React Flow** for the live graph view, Recharts for charts |
| Data sources | SEC EDGAR (`data.sec.gov` submissions + XBRL companyfacts + Archives), NewsAPI |
| Testing | `pytest`, `pytest-asyncio`, `respx` (HTTP mocks), Playwright (e2e) |
| Observability | Structured JSON logging; optional LangSmith tracing via env flag |
| Infra | `docker-compose` (postgres, weaviate, api, web), GitHub Actions CI, `Makefile` |

---

## 3. Data layer & ingestion pipeline

### 3.1 Company universe
- ~200 US large caps spread across all 11 GICS-style sectors (store the list in `data/universe.csv`: ticker, name). Must end with **≥190 companies successfully ingested**.
- Resolve ticker → CIK using SEC's `company_tickers.json`.

### 3.2 SEC EDGAR client (`ingestion/edgar/client.py`)
- Mandatory `User-Agent: "Dev Mittal devmittal2607@gmail.com"` header (from env).
- Global rate limiter **≤ 8 req/s** (SEC limit is 10), exponential backoff on 429/5xx, on-disk cache of raw responses in `data/raw/` so re-runs don't hit SEC.
- Endpoints:
  - `data.sec.gov/submissions/CIK##########.json` → company metadata (SIC code, SIC description, exchanges) and recent filings (form type, accession number, filing date, primary document, **8-K item numbers**).
  - `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` → financial facts.
  - `sec.gov/Archives/edgar/data/{cik}/{accession}/{primary_doc}` → filing HTML.

### 3.3 What to ingest per company
- **Latest 10-K** (plus the previous one, so the agent can answer "how did risks change YoY").
- **8-Ks from the last 12 months** — keep the item codes (e.g. 2.02 results, 5.02 exec changes, 1.01 material agreements, 8.01 other events) and the body text.
- **XBRL facts** for a curated set of metrics: revenue, net income, operating income, total assets, total liabilities, cash, R&D expense, EPS (diluted). Revenue tags vary across companies — build a concept-mapping table (e.g. `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet`) and pick the best available per company/year. Keep annual (10-K, FY) values only.
- **News**: NewsAPI articles per company for a recent window. The free tier is limited — cache aggressively, store articles in Postgres + Weaviate, and design the news tool to read from the store first and only call NewsAPI live when data is stale.

### 3.4 Extraction
- **Section extraction (10-K):** HTML → clean text. Locate Item 1 (Business) and Item 1A (Risk Factors). The first match is usually the table of contents — pick the occurrence whose span to the next item (1B / 1C / 2) is longest. If regex fails, fall back to an LLM boundary-detection call on a windowed chunk. You may evaluate `edgartools` for parsing, but the fallback must be ours and tested.
- **Risk factor splitting:** split Item 1A into individual risk factors using their headings (bold/italic paragraph headings in the HTML). Each risk factor = `{heading, body}`.
- **Risk taxonomy tagging (Groq, FAST model, structured output):** tag every risk factor with one primary category from a fixed taxonomy — `regulatory, competition, supply_chain, cybersecurity, macroeconomic, geopolitical, litigation, technology, talent, financial_liquidity, climate_esg, customer_concentration, ip, other` — plus a one-line summary. Batch requests, retry on validation failure, cache results by content hash so re-runs are free.
- **Sector classification:** two signals, both stored:
  1. Rule-based: SIC code → sector mapping table.
  2. LLM-based: classify Item 1 business description into sector + sub-industry with a confidence score.
  Flag disagreements (`sector_conflict = true`) — this is a nice talking point.

### 3.5 Storage
**Postgres tables:** `companies`, `filings`, `filing_sections`, `risk_factors`, `eight_k_events`, `financial_facts`, `news_articles`, `runs` (agent run log: query, plan, steps, tokens, latency, cost), `ingestion_jobs`.

**Weaviate collections** (all with rich filterable metadata: ticker, cik, sector, form_type, fiscal_year, filing_date, item_code, risk_category, source_url):
- `FilingChunk` — ~800-token chunks with overlap from 10-K sections and 8-Ks.
- `RiskFactor` — one object per tagged risk factor.
- `NewsArticle` — title + description + content snippet.

### 3.6 Pipeline ergonomics
- CLI: `python -m ingestion run --universe data/universe.csv [--sample 10] [--only edgar|news|tagging|index]`.
- **Idempotent** (upserts keyed on accession number / content hash) and **resumable** (track per-company stage in `ingestion_jobs`).
- Concurrency with `asyncio` + the rate limiter. Progress bar. Final report: companies OK / failed (with reasons), filings, risk factors, chunks, facts, articles.
- `make ingest-sample` (10 companies) for fast dev loops; `make ingest-full` for the real run.

---

## 4. The agent (the core of the project)

### 4.1 Design principles
- **Plan-and-execute with a critic loop**, not a single ReAct agent. The plan is a first-class, visible object.
- Use these LangGraph features explicitly, and list them in the README with the file/line where each is used:
  1. Typed `StateGraph` with **reducers** (`Annotated[list, operator.add]`) for accumulating evidence from parallel branches
  2. **Conditional edges** for routing
  3. **`Send` API** for parallel fan-out of independent plan steps
  4. **Subgraphs** — each specialist agent is its own compiled subgraph with its own tool loop
  5. **`interrupt()` + `Command(resume=...)`** for human-in-the-loop clarification
  6. **`PostgresSaver` checkpointer** keyed by `thread_id` → multi-turn memory and resumable runs
  7. **Streaming** (`stream_mode=["updates", "messages", "custom"]` + `get_stream_writer()` for step-progress events)
  8. **Bounded loops**: max replans and a recursion limit, so it can never spin forever
  9. **Replay from a checkpoint** (bonus): re-run a thread from step N via checkpoint history

### 4.2 State (sketch — refine as needed)
```python
class PlanStep(BaseModel):
    id: str
    goal: str                      # natural-language sub-question
    agent: Literal["filings", "financials", "news", "screener"]
    tool_hints: list[str]
    entities: list[str]            # resolved tickers
    depends_on: list[str] = []     # step ids → defines the DAG
    status: Literal["pending", "running", "done", "failed"] = "pending"

class ResearchState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    query: str
    resolved_entities: list[Entity]              # ticker, cik, name, confidence
    clarification: str | None
    plan: list[PlanStep]
    evidence: Annotated[list[Evidence], operator.add]   # each with source_id, url, snippet, step_id
    critique: Critique | None
    replan_count: int
    answer: str | None
    citations: list[Citation]
```

### 4.3 Graph
```
START
  → guardrail          (block "should I buy X?" style requests → polite refusal; not investment advice)
  → entity_resolver    (fuzzy match company names/tickers against `companies`; low confidence or multiple matches → interrupt)
  → query_analyzer     (is the question underspecified? e.g. "how risky is the bank?" → interrupt with 2–4 concrete options)
  → planner            (REASONING model, structured output → list[PlanStep] as a DAG; 1–6 steps; no fake steps for simple questions)
  → dispatcher         (Send() every step whose dependencies are done → specialist subgraphs run in parallel)
      ├─ filings_agent     subgraph
      ├─ financials_agent  subgraph
      ├─ news_agent        subgraph
      └─ screener_agent    subgraph
  → join               (loop back to dispatcher until all steps done/failed)
  → critic             (REASONING model: does the evidence actually answer every part of the question? any unsupported claims? missing entity/year?)
        ├─ gaps & replan_count < 2 → planner (replan only the missing parts)
        └─ ok → synthesizer
  → synthesizer        (final answer in markdown, every factual sentence carries [n] citations mapping to evidence; comparison tables when comparing companies)
  → END
```

### 4.4 Specialist subgraphs & tools
Each specialist is a small tool-calling loop (max ~4 tool calls) that returns typed `Evidence`.

| Agent | Tools |
|---|---|
| **filings_agent** | `search_filings(query, tickers, form_types, fiscal_years, sections)` — Weaviate hybrid search with metadata filters · `get_risk_factors(ticker, fiscal_year, category?)` · `compare_risk_factors(ticker, year_a, year_b)` — new / removed / changed risks via embedding similarity · `get_8k_events(ticker, item_codes?, since?)` |
| **financials_agent** | `get_financial_metrics(tickers, metrics, years)` from `financial_facts` · `calculate(expression)` — safe arithmetic for growth %, margins, ratios (no `eval`; use a restricted AST evaluator) |
| **news_agent** | `search_news(query, tickers, since)` — store first, NewsAPI live fallback, deduplicated |
| **screener_agent** | `query_database(question)` — **text-to-SQL** over a read-only Postgres role, allowlisted tables/columns only, SQL validated with `sqlglot` (SELECT-only, enforced `LIMIT`, statement timeout). For cross-company questions like "Which semiconductor companies flag supply chain as a risk and grew revenue >20%?" |

All tools: Pydantic input/output schemas, docstrings good enough for the LLM to pick correctly, timeouts, and errors returned as structured tool results (never crash the graph).

### 4.5 LLM layer (`agent/llm.py`)
- Single factory for both model slots. Structured output via Pydantic with retry-on-validation-error (max 2).
- Retry with backoff on Groq rate limits. Per-run token + latency accounting written to `runs`.
- All prompts live in `agent/prompts/*.md`, versioned — not inline strings.

### 4.6 Must-work demo queries (use these as the e2e + eval seed set)
1. "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks. Does recent news support any of them?" → parallel filings + news for 2 companies, comparison table.
2. "How did Apple's risk factors change between its last two 10-Ks?" → `compare_risk_factors`.
3. "Which healthcare companies had the fastest revenue growth last fiscal year, and what are their main regulatory risks?" → screener → financials → filings (dependent steps).
4. "What material events has Boeing reported in 8-Ks this year?" → `get_8k_events`.
5. "How risky is the bank?" → **must trigger a clarification interrupt** (which bank? which kind of risk?).
6. "Should I buy Tesla?" → guardrail refusal with an offer to research its risks/financials instead.
7. A follow-up in the same thread: "Now do the same for Intel." → uses checkpointed memory.

---

## 5. API (FastAPI)

- `POST /api/threads` → create thread
- `POST /api/threads/{id}/runs` → start a run, returns **SSE stream** of typed events: `plan_created`, `step_started`, `step_completed`, `tool_called`, `evidence_added`, `critique`, `replan`, `clarification_needed`, `token` (answer streaming), `final`, `error`
- `POST /api/threads/{id}/resume` → answer a clarification (`Command(resume=...)`)
- `GET /api/threads/{id}` → history (from checkpointer)
- `GET /api/graph` → graph structure as JSON (from `graph.get_graph()`) for the UI
- `GET /api/companies`, `GET /api/companies/{ticker}` (profile, sector, risk breakdown, recent 8-Ks, key metrics)
- `GET /api/insights/risk-heatmap` (sector × risk category counts)
- `GET /api/runs/{id}` (plan, steps, timings, tokens)
- `GET /health`, `GET /ready` (checks Postgres, Weaviate, Groq key presence)
- Pydantic schemas for everything; CORS locked to the frontend origin; simple per-IP rate limiting for the public demo.

---

## 6. Frontend (Next.js) — make the agent's thinking visible

Design: clean, dark-first, "research terminal" feel but not cluttered. Good typography, monospace for tickers and numbers, responsive down to mobile.

**`/` Research workspace (main screen)**
- Left: thread list + "New research".
- Center: chat. Answer streams token by token; inline `[n]` citations are clickable and highlight the source.
- Right panel with tabs:
  - **Plan** — live step list (goal, agent, status chip, duration). Steps appear the moment the planner emits them and tick through running → done. Replans visibly add steps.
  - **Graph** — React Flow render of the LangGraph graph; the active node pulses, completed nodes are marked, parallel branches light up together. *This is the "how I use LangGraph" screen — make it great.*
  - **Sources** — evidence cards: filing excerpts (company, form, year, section, link to the SEC document) and news (outlet, date, link).
- **Clarification card**: when an interrupt fires, show the question with clickable options + free text; submitting calls `/resume` and the run continues.
- Example-query chips on the empty state (the demo queries from 4.6).

**`/companies`** — searchable, sector-filterable table. **`/companies/[ticker]`** — sector (both classifications, conflict badge), risk factors grouped by category, recent 8-K timeline, key metric sparklines.

**`/insights`** — sector × risk-category heatmap built from the pipeline output + ingestion stats (companies, filings, risk factors, chunks). This page proves the "190+ companies" pipeline exists.

**`/evals`** (optional but strong) — latest eval run results table.

Handle loading, empty, and error states everywhere. No fake data in the UI.

---

## 7. Testing & evaluation

**Unit tests**
- Item 1A / Item 1 extraction on saved fixture HTML from 3+ real 10-Ks with different formatting
- Risk factor splitter, SIC → sector mapping, XBRL concept mapping
- Safe calculator, SQL validator (must reject `DROP`, `UPDATE`, multiple statements, non-allowlisted tables)
- EDGAR client: rate limiting, retries, caching (mocked with `respx`)

**Agent tests (deterministic)** — use a fake/scripted LLM so tests don't call Groq:
- Planner output → correct DAG; dispatcher fans out independent steps in parallel and respects `depends_on`
- Critic loop stops at max replans
- Interrupt → resume flow works end to end with the checkpointer
- Guardrail blocks investment-advice requests

**Integration** — `docker-compose` up, sample ingestion of 10 companies, run demo queries against real Groq (marked `@pytest.mark.live`, skipped in CI unless key present).

**E2E** — Playwright: ask a demo query, see plan steps appear, see the answer with citations, answer a clarification.

**Eval harness** (`evals/`) — 30 golden questions (expand from 4.6) with expected entities, expected agents/tools, and key facts. Metrics per run:
- plan validity (expected agents used), tool-selection accuracy
- retrieval hit rate (expected source in evidence)
- citation faithfulness (LLM-as-judge: is each cited sentence supported by its source?)
- clarification precision (asks when it should, doesn't when it shouldn't)
- p50/p95 latency, avg tokens per query

`make eval` writes `evals/results/<timestamp>.json` + a markdown summary. Put the latest numbers in the README — **only real measured numbers.**

**CI** — GitHub Actions: ruff + mypy (backend), eslint + tsc (frontend), unit + agent tests, frontend build.

---

## 8. Production concerns
- Config via `pydantic-settings`; `.env.example` documenting every variable; no secrets in code or git.
- Structured JSON logs with `run_id` / `thread_id` on every line.
- Timeouts on every external call; graceful degradation (if NewsAPI fails, answer from filings and say news was unavailable).
- Answers always include a short "Not investment advice" footer.
- Multi-stage Dockerfiles; `docker-compose up` brings up the whole stack with one command.
- Deploy target (Phase 7): frontend on Vercel, API on Railway or Render, Weaviate Cloud, managed Postgres (Neon or Supabase). Ingestion runs offline/locally and loads into the cloud stores.

---

## 9. Build phases (stop after each one)

| Phase | Deliverable | Done when |
|---|---|---|
| **0. Scaffold** | Monorepo (`backend/`, `frontend/`, `ingestion/`, `evals/`, `docs/`), `docker-compose`, `Makefile`, `CLAUDE.md` (project conventions + commands), health endpoints, CI skeleton | `docker-compose up` → `/ready` is green |
| **1. Ingestion** | EDGAR client, parsers, risk tagging, sector classification, XBRL facts, news, Postgres + Weaviate loading | `make ingest-sample` works; unit tests pass; then full run reports **≥190 companies** ingested |
| **2. Tools** | All tools from 4.4 with schemas + tests | Each tool callable from a script with real data; tests pass |
| **3. Agent graph** | Full graph from 4.3 with checkpointer, interrupts, streaming; CLI `python -m agent ask "..."` that prints the plan, steps, and answer | All 7 demo queries behave correctly from the CLI; agent tests pass |
| **4. API** | FastAPI with SSE streaming, threads, resume | Can run demo queries via `curl` and watch the event stream |
| **5. Frontend** | All pages in Section 6 | Full demo flow works in the browser, including clarification and follow-ups |
| **6. Evals + E2E** | Eval harness, Playwright tests, CI green | `make eval` produces a results report |
| **7. Harden + deploy + docs** | Deployment, README, DECISIONS.md | Public URL works; README complete |

---

## 10. README must include
- One-paragraph pitch + screenshot/GIF of the Plan and Graph panels in action
- Mermaid architecture diagram + the LangGraph graph diagram
- **"How this uses LangGraph"** — the 9 features from 4.1, each with a one-line explanation and a file link
- Data pipeline summary with real counts from the full ingestion run
- Eval results table (real numbers)
- Local setup in ≤5 commands, env vars, deploy notes
- Limitations & next steps (honest)

---

## 11. Working rules for you (Claude Code)
- Start Phase 0 by showing me the planned folder structure and the list of decisions you'll log; then build.
- Commit after each meaningful unit with conventional commit messages.
- Never fake data, metrics, or test results. If an API, model, or library behaves differently than this spec assumes, check the current docs, adapt, and log it in `DECISIONS.md`.
- Keep functions small and typed. Run tests before declaring anything done.
- If something in this spec is wrong or a better approach exists, tell me before deviating.