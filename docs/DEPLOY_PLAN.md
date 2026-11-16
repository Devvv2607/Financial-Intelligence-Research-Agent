# DEPLOY PLAN: ship a public demo of the Financial Intelligence Research Agent

> Save this file as `docs/DEPLOY_PLAN.md` and tell Claude Code: "Read docs/DEPLOY_PLAN.md, CLAUDE.md and docs/DECISIONS.md, then start Phase 0."

---

## 0. Context (verify each item yourself before relying on it)

- Repo: github.com/Devvv2607/Financial-Intelligence-Research-Agent, `main` at `411811e` or newer.
- Built and merged: ingestion, tools, the LangGraph agent, the FastAPI SSE API, the Next.js UI (Research workspace, Companies, Company detail, Insights), the eval harness, Playwright E2E, data-freshness disclosure, incremental refresh, and the tag-repair fix (`fix/retag-empty-summary`).
- Data: a 10-company sample in local Docker Postgres and Weaviate. The full 205-company run has not started.
- **Known open issue:** after merging the tag repair, `empty_summary` in `risk_factors` is still not 0. Diagnose this first (Phase 0).
- Constraints:
  - **$0 budget.** Free tiers only. Dev will not pay or add a paid plan.
  - Groq free tier: 200K tokens/day per model, 8K tokens/minute, 1K requests/day. The agent uses `openai/gpt-oss-120b`; ingestion uses `openai/gpt-oss-20b`.
  - NewsAPI free (developer) plan.
  - Windows plus PowerShell, Docker Desktop, project ports 5442 (Postgres), 8095 and 50055 (Weaviate), 8000 (API), 3000 (web), 3005 and 8011 (dev and E2E).
- Dev conserves Claude usage. Keep every report short (15 lines at most at each stop), and don't re-read large files you've already read.

## 1. Goal

A public URL a recruiter can open on a phone, tap an example question, and within a minute see a real, cited answer with the live Plan and Graph panels. It must not exhaust the free budget or break within a month. The project must be described honestly everywhere: the dataset size, recorded runs versus live runs, and cold starts.

## 2. Ground rules (all phases)

1. Use one branch per phase off `main`. Show a short plan before coding. Then run `.\make.ps1 check` (with the Docker stack up) and push. Dev opens a PR, waits for all four CI checks, and approves the merge. Never merge without Dev's OK.
2. Run `.\make.ps1 backup` before any data operation. Generate and apply migrations in separate steps, with Dev's OK in between.
3. No destructive commands without asking: `DROP`, `TRUNCATE`, `DELETE` without `WHERE`, `docker volume rm`, `docker compose down -v`, `git reset --hard`, a force push, or a restore over a live database. The index stage `--only index --force` is allowed, because it rebuilds Weaviate from Postgres and makes no LLM calls.
4. **Never read, print, commit or paste secrets.** Dev creates every account and types keys into host dashboards himself. You may give exact click paths and variable names, never values.
5. Ask before anything that spends Groq tokens. State the estimate first. Never run a live eval or a re-tag on your own initiative.
6. Anything that could cost money is out of scope. If a host needs a card, say so and offer a no-card alternative.
7. For any fact about a hosting provider (free-tier limits, sleep behavior, expiry, supported versions, terms), check the current official docs, then cite the URL and the date you checked. Don't rely on memory.
8. Never fake data, metrics or results. Every number in the README or the resume must come from a saved report, with its source.
9. No `Co-authored-by` trailer in commits.

---

## Phase 0: fix the tag data (blocker)

1. Read-only state check. Report:
   ```sql
   SELECT count(*) FILTER (WHERE coalesce(summary,'')='') AS empty_summary,
          count(*) FILTER (WHERE category='other')      AS other_total,
          count(*) FILTER (WHERE needs_review)          AS flagged
   FROM risk_factors;
   ```
   Also report the breakdown of the empty-summary rows by `tag_source`, company and fiscal year, and the last 48 hours of `llm_calls` for the `risk_tagging%` stages.
2. Was the repair run (`uv run python -m ingestion run --needs-review --only tagging`)?
   - **Not run:** back up, run the `--dry-run` (it must say `re-inheritable for free: 96; rows going to LLM: 0`), then ask Dev before the real run.
   - **Run but still above 0:** diagnose read-only, using the code path, the SQL and the inheritance matches. Report the root cause with evidence and the smallest fix. Wait.
3. Target: `empty_summary = 0`, `other_total` around 30 (genuine `other` tags only), and every flagged row has a `review_reason`.
4. Rebuild the vectors: `uv run python -m ingestion run --only index --sample 10 --force` (no LLM calls). Confirm the Weaviate `RiskFactor.risk_category` values match Postgres for 5 spot-checked rows.
5. **Stop.** Report the before and after numbers.

## Phase 1: fresh data and the freshness fix proven live

1. `.\make.ps1 backup`, then `uv run python -m ingestion refresh --sample 10 --dry-run`. Show the per-company counts and wait for Dev's OK, then run it for real. Confirm that the new 8-Ks (including NVIDIA's Item 5.02 for the 12 Nov 2026 CEO change) and the fresh news rows exist and are indexed.
2. Live test, with Dev's OK first (about 15K agent-model tokens). In the real runtime, ask: "Did the NVIDIA CEO resign?"
   - Pass: the answer reports the resignation with citations to the 8-K or news, and states the data date.
   - Save the transcript to `docs/demo-runs/<date>/`.
3. **Stop.** Report pass or fail and the tokens spent.

## Phase 2: make it safe to be public (code only, zero tokens)

Branch: `feat/public-demo-mode`.

1. **Budget and abuse guards** (configurable through env, safe defaults):
   - Per-IP live questions per day (default 3).
   - A global daily agent-token cap (default 120K, below the existing 150K guard).
   - A maximum question length.
   - **A global concurrency limit of one live run at a time,** with a queue position shown in the UI. The 8K tokens/minute limit means parallel runs will hit 429 errors.
   - Friendly, specific messages when any limit is hit.
2. **Recorded runs:**
   - Capture the complete SSE event stream of real live runs to JSON fixtures in the repo. They must contain no secrets and no IPs.
   - Serve them through a replay endpoint that plays the events with compressed timing.
   - The example chips play recorded runs instantly at zero tokens. The UI shows an unmissable badge: "Recorded live run · <date> · not live".
   - A "Run it live" button uses the guarded live path. When a limit is hit, offer the recorded runs instead.
   - Never present a replay as live.
   - Write a short recording script that captures a run from the live API. Don't record anything in this phase.
3. **Cold-start UX:** if the API doesn't answer within 5 seconds, show "Waking the free-tier server, about 30–60s" with an automatic retry.
4. **Production hardening:**
   - Startup fails if `APP_ENV=production` and `AGENT_RUNTIME=scripted`.
   - Live NewsAPI is off in production.
   - CORS allows the exact web origin only.
   - No stack traces in error responses.
   - Startup verifies that text-to-SQL runs under the read-only role.
   - The demo footer shows the dataset scope ("10-company sample").
5. **NewsAPI terms:** check the current terms of the free developer plan. If public display of NewsAPI-sourced headlines isn't allowed, propose options (hide news in public mode, or an allowed free source) and wait for Dev's decision.
6. **Tests:** cap-hit behavior, concurrency queueing, replay badge, production startup guards. Also add Playwright specs for the replay and for the cap-hit message, in scripted mode.
7. `.\make.ps1 check`, the E2E suite, push. **Stop.**

## Phase 3: choose hosting (research only, no code)

1. **Measure locally first:**
   - API container RAM at idle and during a live run, with the embedding model loaded.
   - Weaviate RAM with the sample data.
   - Image sizes and cold-start time.
2. **Compatibility checks:**
   - The local Postgres major version is 18. Will a dump restore into the host's version? Can the host run our migrations, including creating `fira_readonly` and its GRANTs?
   - Do the Weaviate client and server versions match ours (1.40.3)?
   - Can the LangGraph checkpointer tables be created on the host?
3. **Compare current free options,** checking and citing each (see rule 7):
   - Frontend: Vercel Hobby.
   - API: Render, Railway, Fly.io, Koyeb, Hugging Face Spaces (Docker), Oracle Cloud Always Free VM, Google Cloud free e2-micro.
   - Postgres: Neon, Supabase.
   - Weaviate: Weaviate Cloud sandbox (expiry and renewal) versus self-hosting on a VM.
4. **Output:** a table of 2–3 complete stacks with columns for: $0 confirmed, card required, RAM fit, sleep and cold start, expiry risk, "will the link still work in 60 days", and setup effort on Windows.
   - Recommend one, with reasons. If the best option needs a card, say so and give the best no-card stack.
5. **Stop and wait for Dev's choice.**

## Phase 4: deploy configs and runbook

Branch: `chore/deploy`, for the chosen stack.

1. **Dockerfiles:**
   - Bake the embedding model into the API image, so it isn't downloaded at startup.
   - Run as a non-root user.
   - Add a healthcheck.
   - Keep the size small.
2. **Host config files** as needed (`vercel.json`, `render.yaml`, `fly.toml`, or a production compose file for a VM). Don't invent URLs; use placeholders like `<API_URL>`.
3. **`docs/DEPLOY.md`:** a numbered Windows runbook, including:
   - an env var table (names, purpose, which service; no values);
   - `INTERNAL_API_URL`, `NEXT_PUBLIC_API_URL` (set before the Vercel build) and `CORS_ORIGINS`;
   - `APP_ENV=production`, with `AGENT_RUNTIME` unset.
4. **Data load scripts:**
   - A version-safe dump of local Postgres, then a restore into the hosted Postgres. Run the migrations, create the checkpointer tables and verify the row counts.
   - Rebuild the hosted Weaviate from the hosted Postgres with the index stage pointed at the remote URLs (no LLM calls). Never point any local command at production without Dev's explicit OK.
5. **`scripts/smoke.ps1 <API_URL> <WEB_URL>`:**
   - Checks `/health`, `/ready` (and that `X-Agent-Runtime` is not `scripted`), `/api/companies`, `/api/insights/risk-heatmap`, one replay, and the CORS headers.
   - Prints pass or fail for each check.
6. **Maintenance section:**
   - How to push new data after `ingest-daily`.
   - Weaviate renewal steps, if relevant.
   - What to do when the daily caps hit.
   - No keep-alive pinging of free tiers.
7. `.\make.ps1 check`, push. **Stop.**

## Phase 5: deploy (Dev and Claude Code together)

1. Give Dev one step at a time: the account to create, the exact click path, and the variable names to enter. After each step, verify it before moving on.
2. Use host CLIs only after Dev has logged in himself.
3. Load the data (Phase 4 scripts), then run `scripts/smoke.ps1` against production.
4. **Record 4–5 replays** from live runs on production, with Dev's OK first (about 15K agent tokens each). Use the demo questions:
   - the NVIDIA vs AMD comparison;
   - Apple's risk changes;
   - the healthcare growth question;
   - "How risky is the bank?" (the clarification flow);
   - "Did the NVIDIA CEO resign?"
   Commit the fixtures and redeploy.
5. **Acceptance checklist.** Report each item as pass or fail:
   - The site opens on a phone. The cold load shows the waking message.
   - An example chip replays in under 5 seconds, with the "recorded" badge visible.
   - One "Run it live" question completes with citations and a freshness line.
   - Hitting the per-IP cap shows the friendly message and offers replays.
   - `/companies`, `/companies/NVDA` and `/insights` render real data.
   - There are no secrets in the repo, the scripted runtime is refused in production, CI is green on `main`, and the smoke test passes.
6. **Stop.** Report the live URL.

## Phase 6: README, video, resume

Branch: `docs/live-demo`.

1. **README top section:**
   - The live demo link.
   - "Demo runs on a 10-company sample; full 205-company ingestion in progress (free tier)."
   - How recorded and live runs work, and the cold-start note.
   - A screenshot or GIF slot.
   - Real numbers only, each citing its source report. Leave a placeholder for the tagging accuracy until Dev's labeled review score exists, and for the eval results until a live eval has run.
2. Write a 60–90 second demo video script for Dev to record (shot list plus narration).
3. Draft resume bullets with brackets for numbers that aren't real yet. Do not write "190+ companies" until the ingest report shows it.
4. Push. **Stop.**

---

## Dev's own tasks (no Claude needed)

- Label `data/review/risk_tags_review.csv` (hide the model-tag column), then run `.\make.ps1 review-score`. That gives the tagging accuracy number for the README.
- Later: run `.\make.ps1 ingest-daily` once a day until at least 190 companies are ingested, then push the new data to production using the DEPLOY.md maintenance steps.

## Stop points summary

| After | Wait for |
|---|---|
| Phase 0 | Dev OK on the repair numbers |
| Phase 1 | Dev OK on the refresh and the live test |
| Phase 2 | PR green, then merge |
| Phase 3 | Dev's hosting choice |
| Phase 4 | PR green, then merge |
| Phase 5 | Each account and secret step, the replay recording, the acceptance checklist |
| Phase 6 | PR green, then merge |
