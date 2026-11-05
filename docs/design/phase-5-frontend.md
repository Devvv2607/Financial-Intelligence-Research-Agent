# Phase 5 frontend design (draft for review)

Status: **draft, no code yet**. Stack as scaffolded: Next.js 16 App Router, React 19, TypeScript,
Tailwind v4. The API is Phase 4 (`docs/DECISIONS.md` D-041). Items marked **[backend]** need
a small API change and your OK.

---

## 1. Pages

### Shell (every page)
Top bar, 48 px: product mark · nav (**Research**, **Companies**, **Insights**) · a status dot
fed by `/ready` (green / amber / red, with the per-check breakdown on hover). No sidebar
outside the workspace, so the data pages get the full width.

### `/` Research workspace
Three columns. The run is the main object on the page, so its live state gets a dedicated
panel instead of being mixed into the chat.

| Region | Width | Content |
|---|---|---|
| **Threads** (left) | 248 px, collapsible | "New research" button; threads from `GET /api/threads`, grouped Today / Earlier. Each row: title, status chip (`running` amber pulse, `awaiting_clarification` violet, `error` red), relative time. |
| **Conversation** (center) | fluid, prose max 760 px | Turns: the question, then the assistant block (below). The composer sits at the bottom; it's disabled while a run is active and while a clarification is pending (answer it in the card). The empty state shows the 7 demo queries as chips. |
| **Run panel** (right) | 420 px, resizable | Tabs **Plan · Graph · Sources**. Each tab shows a badge (plan `3/5`, sources `12`). The panel follows the selected turn, latest by default. |

**Assistant block**, top to bottom:
1. **Status line** (mono, muted): `Planning…` → `Running 3 steps (2 parallel)` → `Reviewing` →
   `Gaps found, replanning (round 2)` → `Writing`. It's derived from events, so it's always
   honest.
2. **Answer**: streamed markdown with GFM tables. `[n]` renders as a citation chip. Clicking
   it opens Sources and highlights card *n*. `[unverified]` renders as a muted outlined chip
   with a tooltip.
3. **Footer row**:
   - the citation-check badge (`citations 14/15 verified`);
   - total tokens and duration;
   - a "Run details" link that opens a drawer over the run panel. The drawer, from
     `GET /api/runs/{id}`, shows tokens per node as horizontal bars, step timings as a
     mini Gantt, and the recorded plan.
4. **Clarification card** (when paused): the question, 2-4 option buttons, a free-text
   field, and Submit, which calls `/resume`. It's styled as a paused state (violet edge),
   not an error.

**Mobile (< 900 px)**: the conversation only. Threads open in a drawer, and the run panel
becomes a bottom sheet with the same tabs.

**Plan tab**: step cards grouped by round. A replan round is headed by the critic's gaps
("Round 2, added because: AMD FY2025 revenue missing"). Each card shows:
- id (mono), the agent with its color, and the goal;
- `direct` / `agentic` chip, status chip and a live duration;
- "after r1s1" when the step has dependencies.

Expanding a card shows its tool calls (`get_risk_factors(ticker=AMD)` · ok · 53 sources) and
the step's summary, error or skip reason. The critic's verdict closes the list.

**Sources tab**: evidence cards streamed in as `evidence_added` arrives, unnumbered and in
arrival order. On `final` they re-sort into the answer's `[n]` order: cited cards first and
numbered, the rest under "Retrieved, not cited". Each card shows:
- a type icon (10-K risk factor / 8-K / XBRL fact / news / filing excerpt);
- ticker, form, FY, section and date;
- the title, a 3-line snippet, and a link to SEC or the outlet.

Filter chips: type, ticker.

### `/companies`
A page title with a count ("10 companies · 701 risk factors"), a search box (ticker or name)
and sector filter chips. Then a dense table:
- **Ticker** (mono), **Name**, **Sector**;
- an amber "conflict" badge when the SIC and LLM sectors disagree, with both shown on hover;
- **FY end**, **Latest 10-K**, **Risk factors**.

The table is sortable and a row click opens the detail page. With 10 companies now and
about 205 later, client-side filtering is fine. Server Component; `loading.tsx` / `error.tsx`
cover loading and failure, and "no matches" is the empty state.

### `/companies/[ticker]`
- **Header:** ticker (large, mono), name, sector badges (universe · SIC · LLM, with a
  conflict badge and the LLM rationale on hover), FY end, CIK linking to EDGAR, and a
  **"Research this company"** button that opens the workspace with a prefilled question.
- **Row 1, key metrics:** a tile per metric (revenue, net income, diluted EPS, total assets,
  operating cash flow…). Each tile has the latest value with its FY, the change vs the prior
  year, and a sparkline over all years. The data is from `metrics`, which has one value per
  fiscal year.
- **Row 2, two columns:**
  - **Risk factors by category** (60%): collapsible groups sorted by count, each with a
    count bar. Each item is a heading plus a one-line summary, with a `needs review` marker
    when tagging fell back.
  - **8-K timeline** (40%): date, item chips (`2.02 Results`, `5.02 Officers`), link.

### `/insights`
This page proves the pipeline exists (spec §6).
- **Stat tiles:** companies, filings, risk factors, 8-K events, filing chunks, news articles.
- **Heatmap:** sectors as rows, risk categories as columns.
  - The color shows the category's share of that sector's risk factors. Absolute counts
    would just track how many companies a sector has, so a toggle switches to counts.
  - Hovering a cell shows the count, the number of companies and the share.
  - Clicking a cell opens `/companies?sector=…`.
- **Footer note:** "Latest 10-K per company · N companies", so the scope is explicit.

---

## 2. SSE events → UI

| Event | Conversation | Plan | Graph | Sources | Other |
|---|---|---|---|---|---|
| `run_started` | create the assistant block, status `Starting` | reset for this turn | intake group active | reset for this turn | thread → `running`; keep `run_id`, last event id |
| `plan_created` | status `Planning done: N steps` | round 1 cards (`pending`) | planner done → dispatcher | | |
| `replan` | status `Replanning (round k)` | append round k with the critic's gaps | critic→planner loop edge pulses | | |
| `step_started` | status `Running N steps` | card → `running`, timer starts | dispatcher→agent edge animates; agent pulses; ×N badge for parallel steps | | |
| `tool_called` | | append to the card's tool list | expanded agent: `direct`/`tools` node flashes | | |
| `evidence_added` | | card source count | | append cards (dedupe by `source_id`) | |
| `step_completed` | | card → done/failed/skipped + duration, summary/error/reason | agent → done when it has no running steps; failed = red ring | | |
| `critique` | status `Reviewing` → `Gaps found…` or `Writing` | verdict row | critic done | | |
| `clarification_needed` | clarification card; composer locked | | `ask_clarification` violet (paused) | | thread → `awaiting_clarification` |
| `token` | append to the answer (markdown rendered incrementally) | | synthesizer active | | |
| `final` | **replace** the streamed text with `answer` (finalize may have fixed citations, added `[unverified]` and the footer); render citation chips; check badge | | finalize → end, all done | number and re-sort by `citations` | thread → `idle`; refetch thread list |
| `error` | error banner with **Retry** | running cards → `failed` | active node red | | thread → `error` |

Two rules:
- **`final` is authoritative.** Streamed tokens are a preview, because `finalize` rewrites
  citations.
- **Events are the only writer of run state.** The UI never infers progress from timers.

**[backend] `node` event (proposed).** Today the graph view would have to infer the active
node from the events above. That works for planner, agents, critic and synthesizer, but not
for entity resolver, query analyzer or join. LangGraph's `tasks` stream mode reports each
node's start and finish, with the subgraph namespace. A thin
`{event:"node", node, namespace, phase:"start"|"end"}` would make the graph exact, including
parallel specialists. It would be about 30 lines in `RunManager`, plus tests. Without it,
the table's mapping is the fallback.

---

## 3. Graph tab: keeping 33 nodes readable

`/api/graph` returns 13 top-level nodes plus 4 specialist subgraphs × 5 nodes (prepare,
direct, llm, tools, finish). Shown raw, the 4 identical inner loops make up 60% of the
picture and say the same thing four times.

**Collapsed by default:**
1. **Specialists:** each subgraph becomes one node ("Filings agent", "Financials",
   "News", "Screener") in a fan-out lane between `dispatcher` and `join`. A ×N badge
   shows parallel steps on the same agent. The subgraph is identified from the `subgraph`
   field, not hard-coded, so new agents appear automatically.
2. **Intake group:** `start_turn`, `guardrail`, `entity_resolver`, `query_analyzer` and
   `ask_clarification` sit in a labelled group box ("Intake"). It's expanded because the
   clarification paths are a selling point.
3. **Hidden:** `__start__` / `__end__`, which become a small entry and exit marker.

The result is 9 main nodes plus 4 agent nodes, 13 boxes in total, left to right:
`Intake → planner → dispatcher → [agents] → join → (dispatcher) → critic → synthesizer → finalize`.

**Expand on click:** clicking an agent node opens its inner loop
(`prepare → direct | llm ⇄ tools → finish`) as an inset card below the main graph, with
that agent's live state. It's not inlined, so the main layout never reflows during a run.

**Edges:**
- forward edges are solid;
- conditional edges are dashed;
- loop edges are curved, labelled, and animate when taken:
  - critic → planner ("replan");
  - join → dispatcher ("next wave");
  - ask_clarification → resolver/analyzer ("resume").

**Layout:** `@dagrejs/dagre` on the collapsed graph, computed once per `/api/graph` fetch.
The structure is static, so positions never jump. Rendered with React Flow (`@xyflow/react`),
with zoom and pan but no editing. A legend gives node states (idle · active · done · paused
· failed) and edge types.

**Run replay:** when a past turn is selected, the graph shows its final state, with every
visited node marked done and the taken loops highlighted.

---

## 4. Components, state, reconnects

**Components** (`frontend/components/`):
- `shell/`: TopBar, NavLinks, StatusDot.
- `workspace/`:
  - ThreadList, ThreadRow, Conversation, Turn, AssistantBlock, StatusLine;
  - AnswerMarkdown (react-markdown + remark-gfm, citation-chip renderer);
  - CitationChip, CitationCheckBadge, ClarificationCard, Composer, ExampleChips;
  - RunPanel, RunDetailsDrawer.
- `plan/`: PlanTab, RoundHeader, StepCard, ToolCallRow, StatusChip, CritiqueRow.
- `graph/`: GraphTab, collapse.ts (pure: API graph → collapsed graph), layout.ts (dagre),
  AgentNode, StepNode, GroupNode, LoopEdge, AgentInset, Legend.
- `sources/`: SourcesTab, SourceCard, SourceFilters.
- `companies/`: CompanyTable, SectorChips, SectorBadges, MetricTile, Sparkline (hand-rolled
  SVG, no chart library), RiskCategoryList, EightKTimeline.
- `insights/`: StatTiles, RiskHeatmap (CSS grid), HeatmapToggle.
- `ui/`: Tabs, Chip, Badge, Tooltip, Drawer, Skeleton, ErrorState, EmptyState.

**State:**
- **Data pages** (companies, detail, insights) are Server Components fetching from the API
  at request time. They have no client state.
- **Server state in the workspace** (threads, thread detail, graph structure, run details):
  TanStack Query, for caching, refetch after `final`, and standard loading/error states.
- **Live run state:** one pure reducer, `applyEvent(run, event) → run`, with a `useReducer`
  per active run. The state holds plan rounds, steps by id, tool calls, sources by id,
  critique, clarification, the answer buffer, the final answer, the error, active nodes and
  the last event id. Being pure, it's unit-tested (Vitest) by replaying recorded SSE
  sequences, including the ones the backend tests produce. No global store library is
  needed.
- **Types:**
  - REST types are generated from FastAPI's OpenAPI (`openapi-typescript`).
  - The event union isn't in OpenAPI (the SSE route returns a response object), so
    **[backend]** a small script exports `AgentEvent` as JSON Schema and
    `json-schema-to-typescript` generates the TS union.
  - CI fails if the generated files are stale, so frontend and backend can't drift.
- **SSE client:** `EventSource` can't POST, so the client uses `fetch` with a stream reader
  and `eventsource-parser`. An `AbortController` cancels only the reader. The run continues
  on the server by design (D-041).

**Reconnects.** A run outlives its connection (D-041), so a dropped stream never loses
the run. The question is how the UI catches up. The proposal has two layers:

1. **[backend] Resumable event log (recommended).** `RunManager` keeps each active or
   just-finished run's events in an in-memory buffer, ids already gapless, kept 10 minutes
   after the end. A new endpoint, `GET /api/threads/{id}/events`, accepts the
   `Last-Event-ID` header or `?after=n`. It replays what was missed, then continues live.
   The client reconnects with backoff (0.5 s → 1 → 2 → 4, max 5 tries) and shows a quiet
   "Reconnecting…" pill; the reducer ignores ids it has already applied. This also covers
   a **page reload mid-run**: thread detail says `running`, so the UI subscribes with
   `after=0` and rebuilds the whole run, tokens included.
2. **Fallback, no stream available** (API restarted and buffer gone, or retries
   exhausted): poll `GET /api/threads/{id}` every 2 s until the status leaves `running`.
   Then render the turn from `latest` (plan, step results, sources, citations) and the
   message history. Token-by-token streaming is lost; the content isn't.

On load, a thread in `awaiting_clarification` shows its card from `pending_clarification`.
A thread in `error` shows the banner with Retry.

**[backend] Scripted E2E mode (proposed).** Playwright (spec §7) needs runs without Groq. A
runtime factory selected by `AGENT_RUNTIME=scripted` (refused when `APP_ENV=production`)
would replay recorded runs through the same fakes the API tests use. E2E would then cover
the full UI (plan, graph, sources, clarification) at zero tokens.

---

## 5. Visual direction

Dark-first, dense and calm, like a terminal that reads well: few colors, each with a
meaning, numbers in mono, no decoration that doesn't carry information.

**Palette** (extends the tokens already in `globals.css`):

| Token | Hex | Use |
|---|---|---|
| `bg` | `#0B0D10` | page |
| `surface` | `#12161C` | panels, cards |
| `surface-2` | `#181D25` | hover, inset, table header |
| `border` | `#232A34` | 1 px rules (no shadows) |
| `text` | `#E6E8EB` | primary |
| `muted` | `#8A919C` | secondary, status line |
| `faint` | `#5C6370` | tertiary, disabled |
| `accent` | `#F0B429` | amber: running / active / focus (the terminal accent) |
| `link` | `#6CB6FF` | citations, links |
| `ok` | `#3FB950` | done, verified |
| `bad` | `#F85149` | failed, error |
| `paused` | `#A78BFA` | violet: clarification / awaiting input (distinct from running) |

- **Agent identity:** filings `#6CB6FF`, financials `#3FB950`, news `#F778BA`, screener
  `#39C5CF`. The colors are used only for the agent's dot and graph node border.
- **Heatmap:** one sequential amber ramp from `surface-2` to `accent`, not a rainbow, so
  intensity reads as magnitude.
- **Contrast:** status is never conveyed by color alone; every chip has an icon and a label.
  `text` and `muted` meet WCAG AA on `bg`/`surface`. `faint` (about 3.3:1) is only for
  disabled or decorative elements, never for content.

**Type:**
- **Geist Sans** (already loaded) for UI and prose; **Geist Mono** for tickers, numbers,
  ids, tool names, args and timestamps.
- `tabular-nums` everywhere numbers line up.
- Sizes: 13 px in panels and tables (dense), 15 px with line-height 1.6 for answer prose,
  20/24 px for page titles. Weights 400/500/600 only.

**Shape and motion:**
- 6 px radius, 1 px borders, 8 px spacing grid.
- Motion only signals live state: a 1.2 s pulse on active nodes and running chips, a 150 ms
  fade for streamed cards.
- Everything respects `prefers-reduced-motion`.

**Libraries to add** (exact versions pinned at implementation, after checking current
releases): `@xyflow/react`, `@dagrejs/dagre`, `@tanstack/react-query`, `react-markdown`,
`remark-gfm`, `eventsource-parser`, `lucide-react`, plus `openapi-typescript` and
`json-schema-to-typescript` (dev), and Vitest and Playwright (dev).

---

## Decisions needed from you

1. **[backend] `node` event** from LangGraph's `tasks` stream: an exact live graph,
   including intake nodes and join.
2. **[backend] Resumable event log** with `GET /api/threads/{id}/events` (reconnect and
   reload mid-run), with polling as the fallback.
3. **[backend] Event JSON Schema export** for generated TS types, with a CI staleness check.
4. **[backend] Scripted runtime** for zero-token Playwright E2E.
5. **Palette:** amber accent with violet for "paused". Or would you prefer a cooler cyan
   accent?
