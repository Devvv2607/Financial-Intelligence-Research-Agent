# Railway exit plan (day 25 of the trial)

Plan only: nothing here has been executed. Owner decides; every data step backs up first.

## 1. The problem

| Service | Measured RAM (2026-11-17) |
|---|---|
| `api` (FastAPI + baked embedding model) | ~639 MB |
| `weaviate` 1.40.3 | ~253 MB |
| `postgres` 18.6 | ~122 MB |

What the Railway plans say (https://docs.railway.com/reference/pricing/plans, checked 2026-11-17):

| Plan | Price | Usage credit | RAM per service | Volume storage |
|---|---|---|---|---|
| Free | $0/month | $1/month | 0.5 GB | 0.5 GB |
| Trial | one-time $5 grant | — | 1 GB | 0.5 GB |
| Hobby | $5/month | $5/month included | 48 GB | 5 GB |

The same page says that when the credit reaches zero, Railway stops all workloads.
Free/Trial volume data is deleted 30 days after expiry.

What this means:

1. The `api` (639 MB) doesn't fit a Free service (0.5 GB).
2. More importantly, $1/month of credit can't keep three always-on services running
   for a month. Railway Free is no home for this stack even if the API were made smaller.
3. Not verified (my page fetches hit a limit): the exact trial end date and Railway's
   per-GB RAM price. Read both in Railway → Usage before day 25.

## 2. Options

| | A. Railway Hobby | B. Oracle Cloud Always Free VM (all three services) | C. Split free hosts |
|---|---|---|---|
| Cost | $5/month; usage beyond the included $5 is billed (unverified) | $0 | $0 |
| Card | Yes (paid plan) | Probably, for sign-up identity checks (widely reported; **not verified**, Oracle's FAQ refused the fetch) | Depends on the host |
| RAM fit | Yes | Yes: Ampere A1 gives 2 OCPU / 12 GB total ([Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), checked 2026-11-17) | **No good fit for the API found** (see below) |
| Migration effort | None (change the plan) | Medium: VM, Docker, HTTPS, restore | High |
| Main risk | Breaks the $0 rule | Oracle "may reclaim" idle Always Free instances. Idle means, over 7 days, 95th-percentile CPU < 20%, network < 20% and, on A1 only, memory < 20%. A quiet demo VM can look idle. Also "out of capacity" errors at create time. | Several accounts, cold starts |

Option C, checked so far:

- **Hugging Face Spaces:** Docker Spaces now need a paid plan (PRO). Only static Spaces
  are free ([Spaces overview](https://huggingface.co/docs/hub/spaces-overview), checked
  2026-11-17). Ruled out.
- **Neon Postgres Free:** 1 GB storage per project, 100 CU-hours per month, scales to
  zero after 5 minutes idle, no card ([pricing](https://neon.com/pricing), checked
  2026-11-17). It fits Postgres. The supported Postgres versions weren't on the page;
  check that 18 restores before relying on it.
- **Render / Koyeb / Google e2-micro:** not verified this session. From earlier research
  the free web tiers are 512 MB (too small for the API) and e2-micro needs a billing
  account. Re-check before choosing.
- **Weaviate:** no free managed option found that lasts. Weaviate Cloud's sandbox expires
  (check its current terms). Weaviate needs a self-hosted home, which pushes toward B.

## 3. Recommendation

1. **$0 rule kept:** B, the Oracle Always Free A1 VM with the existing images under
   docker compose (api + postgres + weaviate), and Vercel unchanged. It has ample RAM and
   one host. If Oracle demands a card the owner won't add, or keeps refusing capacity,
   there is no verified $0 home for a 639 MB API plus Weaviate.
2. **Simplest:** A, Railway Hobby at $5/month. No migration, nothing to lose. Only if the
   owner accepts a paid plan.
3. **Shrinking the API below 0.5 GB** (e.g. a smaller embedding runtime) helps on 512 MB
   hosts. It doesn't solve Weaviate, and it doesn't make Railway Free viable ($1 credit).
   Revisit only if B fails.

Decide by **day 20**, so the move has five days of slack before day 25.

## 4. Migration steps for B, with no data loss

Postgres is the only store to back up. Weaviate is rebuilt from Postgres with no LLM calls.

**A. Back up production (Railway), before anything else**

1. Postgres has no public proxy. Make a temporary one: Railway → `postgres` → Settings →
   Networking → TCP Proxy (or `railway tcp-proxy create`). Note host and port.
2. From the laptop, dump with a matching client (no local install needed):

   ```powershell
   docker run --rm -e PGPASSWORD=<typed, not saved> -v ${PWD}/data/backups:/out postgres:18.6-alpine `
     pg_dump -h <proxy-host> -p <proxy-port> -U <user> -d <db> -Fc -f /out/railway-<timestamp>.dump
   ```

3. **Remove the TCP proxy again** right after the dump.
4. Prove the dump. Restore it into a *scratch* database on the local stack and compare
   row counts for `companies`, `filings`, `risk_factors`, `eight_k_events`,
   `news_articles`, `threads`, `runs` and `ingestion_jobs` against production. This is
   a restore into a scratch DB, not the live one; still ask before running it.
5. Keep the dump in `data/backups/`, plus one copy off the laptop.

**B. Build the new host**

6. Create the Oracle account, then an A1 VM (Ubuntu, home region). Use 1 OCPU / 6 GB
   rather than the full 12 GB, so normal use stays nearer the 20% memory threshold.
   Open ports 80 and 443 only.
7. Install Docker. Clone the repo at `main`. Add a production compose file (new small
   branch: the api, postgres and weaviate images as on Railway, volumes on the boot
   volume, Weaviate API key on) and an `.env` the owner types on the VM.
8. HTTPS, because the Vercel site is HTTPS and browsers block mixed content. Pick one
   (verify each before choosing):
   - Caddy on the VM with a free hostname (e.g. a DuckDNS name) and automatic Let's
     Encrypt.
   - A Vercel rewrite of `/api/*` to the VM. This also needs testing for SSE streaming
     and for which header carries the client IP (`CLIENT_IP_HEADER`).
9. `docker compose up -d postgres weaviate`, then
   `pg_restore --no-owner -d <db> railway-<timestamp>.dump`, then `alembic upgrade head`
   (a no-op if it's current), then check `fira_readonly` and its GRANTs exist.
10. Rebuild Weaviate from Postgres with no LLM calls:
    `uv run python -m ingestion run --only index --force --sample 10`, pointed at the
    VM's stores. Check that the Weaviate counts match Postgres.
11. Start the API. Smoke test: `/health`, `/ready`, `/api/companies`, one replay, and
    CORS from the Vercel origin.

**C. Switch over**

12. Set Vercel's `NEXT_PUBLIC_API_URL` (and `INTERNAL_API_URL`) to the new API and
    redeploy. Set `CORS_ORIGINS` and `CLIENT_IP_HEADER` on the VM. Run the zero-token
    spoof check from D-050.
13. Keep Railway running, untouched, for 48 hours as a fallback. Then pause its services
    (stop, don't delete) and keep the verified dump. Railway deletes trial volume data
    30 days after expiry, so the dump is the record.

**Rollback** at any point before step 13: point Vercel back at the Railway URL.
