"""Ingestion reports built only from stored data (Postgres, Weaviate counts, disk).

Every number here is computed from what the pipeline actually recorded — llm_calls for
tokens, ingestion_jobs for outcomes, companies for sector labels — so the README can
cite these reports directly. Re-run any time: `python -m ingestion report`.
"""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.db.llm_store import COUNTED_TOKENS
from core.db.schema import (
    companies,
    eight_k_events,
    filings,
    ingestion_jobs,
    llm_calls,
    news_articles,
    risk_factors,
)

PRODUCTION_STAGES = ("risk_tagging", "risk_tagging_prior", "sector_llm", "extract_fallback")
EXPERIMENT_STAGE = "experiment_headings_only"
ALL_METRICS = 8


@dataclass
class Report:
    sections: dict[str, str]

    def markdown(self) -> str:
        return "\n\n".join(self.sections.values())


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a (0)"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


async def _rows(engine: AsyncEngine, stmt: Any) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(stmt)).mappings().all()]


# ------------------------------------------------------------------ 1. per company
async def company_section(engine: AsyncEngine, ciks: list[int]) -> str:
    comps = await _rows(engine, select(companies).where(companies.c.cik.in_(ciks)))
    jobs = await _rows(engine, select(ingestion_jobs).where(ingestion_jobs.c.cik.in_(ciks)))
    job = {(j["cik"], j["stage"]): j for j in jobs}
    counts: dict[str, dict[int, int]] = {}
    for name, table, extra in (
        ("10k", filings, filings.c.form_type == "10-K"),
        ("8k", eight_k_events, None),
        ("news", news_articles, None),
    ):
        stmt = select(table.c.cik, func.count()).where(table.c.cik.in_(ciks))
        if extra is not None:
            stmt = stmt.where(extra)
        counts[name] = {
            r["cik"]: r["count_1"] for r in await _rows(engine, stmt.group_by(table.c.cik))
        }

    rows = []
    for c in sorted(comps, key=lambda c: c["ticker"]):
        edgar = (job.get((c["cik"], "edgar")) or {}).get("details") or {}
        tks = edgar.get("ten_ks", [])

        def sec(name: str, tks: list[dict[str, Any]] = tks) -> str:
            parts = []
            for t in tks:
                s = t.get(name, {})
                parts.append(f"{s.get('method')}" if s.get("ok") else "FAIL")
            return " / ".join(parts) or "-"

        facts = edgar.get("facts", {})
        index = (job.get((c["cik"], "index")) or {}).get("details") or {}
        statuses = {
            st: (job.get((c["cik"], st)) or {}).get("status", "-")
            for st in ("edgar", "tagging", "news", "index")
        }
        ok = all(v == "done" for k, v in statuses.items() if k != "news")
        rows.append(
            [
                f"`{c['ticker']}`",
                counts["10k"].get(c["cik"], 0),
                counts["8k"].get(c["cik"], 0),
                sec("item_1"),
                sec("item_1a"),
                " / ".join(str(t.get("risk_factors", 0)) for t in tks) or "-",
                f"{len(facts.get('metrics_latest_year', []))}/{ALL_METRICS} "
                f"(FY{facts.get('latest_fiscal_year')})"
                if facts
                else "-",
                counts["news"].get(c["cik"], 0),
                index.get("FilingChunk", "-"),
                index.get("RiskFactor", "-"),
                "OK" if ok else ", ".join(f"{k}:{v}" for k, v in statuses.items() if v != "done"),
            ]
        )
    return (
        "## 1. Per company\n\n"
        + _table(
            [
                "Ticker",
                "10-Ks",
                "8-Ks",
                "Item 1 (latest / prior)",
                "Item 1A (latest / prior)",
                "Risk factors (latest / prior)",
                "XBRL metrics",
                "News",
                "Chunks",
                "RF objects",
                "Status",
            ],
            rows,
        )
        + "\n\nItem columns show the extraction method per 10-K: `regex` or `llm` (fallback)."
    )


# ------------------------------------------------------------------ 2. tokens
@dataclass
class PassCost:
    """What one complete ingestion pass cost a company, in tokens Groq's limits count.

    Calls are attributed to the run that last completed each stage (`ingestion_jobs.
    run_id`); response-cache hits in that run are priced at what the original call cost
    (`saved_tokens`). Rows from before run ids existed fall back to cumulative totals and
    are flagged `legacy`, since they may include work that was later redone.
    """

    counted: int = 0
    prompt: int = 0
    cached: int = 0
    completion: int = 0
    calls: int = 0
    cache_hits: int = 0
    latency_ms: int = 0
    legacy: bool = False


STAGE_JOB = {
    "risk_tagging": "tagging",
    "risk_tagging_prior": "tagging",
    "sector_llm": "tagging",
    "extract_fallback": "edgar",
}


async def pass_costs(engine: AsyncEngine, ciks: list[int]) -> dict[int, PassCost]:
    jobs = await _rows(engine, select(ingestion_jobs).where(ingestion_jobs.c.cik.in_(ciks)))
    run_of = {(j["cik"], j["stage"]): j["run_id"] for j in jobs}
    calls = await _rows(
        engine,
        select(llm_calls).where(
            llm_calls.c.cik.in_(ciks), llm_calls.c.stage.in_(PRODUCTION_STAGES)
        ),
    )
    out: dict[int, PassCost] = defaultdict(PassCost)
    for c in calls:
        run = run_of.get((c["cik"], STAGE_JOB[c["stage"]]))
        cost = out[c["cik"]]
        if run is None:
            cost.legacy = True
        elif c["run_id"] != run:
            continue
        if c["cache_hit"]:
            cost.cache_hits += 1
            cost.counted += c["saved_tokens"]
            continue
        counted = c["prompt_tokens"] - c["cached_tokens"] + c["completion_tokens"]
        cost.counted += counted
        cost.prompt += c["prompt_tokens"]
        cost.cached += c["cached_tokens"]
        cost.completion += c["completion_tokens"]
        cost.calls += int(c["ok"])
        cost.latency_ms += c["latency_ms"]
    return dict(out)


async def token_section(engine: AsyncEngine, ciks: list[int]) -> tuple[str, dict[str, Any]]:
    tickers = {
        r["cik"]: r["ticker"]
        for r in await _rows(
            engine, select(companies.c.cik, companies.c.ticker).where(companies.c.cik.in_(ciks))
        )
    }
    costs = await pass_costs(engine, ciks)
    rows = []
    for cik in sorted(tickers, key=lambda x: tickers[x]):
        c = costs.get(cik, PassCost())
        rows.append(
            [
                f"`{tickers[cik]}`" + (" (legacy)" if c.legacy else ""),
                f"**{c.counted:,}**",
                c.calls,
                f"{c.prompt:,}",
                f"{c.cached / c.prompt:.0%}" if c.prompt else "-",
                f"{c.completion:,}",
                c.cache_hits,
            ]
        )
    calls = await _rows(engine, select(llm_calls).where(llm_calls.c.cik.in_(ciks)))
    rejected = sum(1 for c in calls if c["error"] == "schema_rejected")
    reuse = await _rows(
        engine,
        select(risk_factors.c.tag_source, risk_factors.c.tag_version, func.count())
        .where(risk_factors.c.cik.in_(ciks))
        .group_by(risk_factors.c.tag_source, risk_factors.c.tag_version),
    )
    reuse_txt = ", ".join(
        f"{r['tag_source'] or 'untagged'} ({r['tag_version'] or 'no version'}): {r['count_1']}"
        for r in reuse
    )
    needs_review = (
        await _rows(
            engine,
            select(func.count())
            .select_from(risk_factors)
            .where(risk_factors.c.cik.in_(ciks), risk_factors.c.needs_review.is_(True)),
        )
    )[0]["count_1"]
    text = (
        "## 2. Groq token usage (one complete pass per company)\n\n"
        + _table(
            [
                "Ticker",
                "Counted tokens",
                "Calls",
                "Prompt",
                "Prompt cached",
                "Completion",
                "Response-cache hits",
            ],
            rows,
        )
        + "\n\nCounted = prompt - Groq-cached + completion: what the rate limits see. "
        "`legacy` rows predate run tracking and include all historical calls.\n\n"
        f"Tag sources: {reuse_txt}.\n\n"
        f"Quality flags: **{needs_review} risk factors tagged `other` + needs_review**; "
        f"{rejected} Groq schema rejections overall (no usage is returned for these)."
    )
    return text, {"costs": costs, "tickers": tickers}


# ------------------------------------------------------------------ 3. projection
async def projection_section(
    engine: AsyncEngine,
    settings: Settings,
    ciks: list[int],
    universe_size: int,
    token_data: dict[str, Any],
) -> str:
    costs: dict[int, PassCost] = token_data["costs"]
    done = [
        r["cik"]
        for r in await _rows(
            engine,
            select(ingestion_jobs.c.cik).where(
                ingestion_jobs.c.cik.in_(ciks),
                ingestion_jobs.c.stage == "tagging",
                ingestion_jobs.c.status == "done",
            ),
        )
    ]
    measured = [c for c in done if c in costs and not costs[c].legacy]
    basis = measured or [c for c in done if c in costs]
    if not basis:
        return "## 3. Full-run projection\n\nNo company has finished tagging yet."
    totals = sorted(costs[c].counted for c in basis)
    calls = [costs[c].calls for c in basis]
    median, mean = statistics.median(totals), statistics.mean(totals)
    p90 = totals[min(len(totals) - 1, math.ceil(0.9 * len(totals)) - 1)]
    remaining = universe_size - len(done)
    budget, rpd, tpm = (
        settings.groq_daily_token_budget,
        settings.groq_requests_per_day,
        settings.groq_tokens_per_minute,
    )

    def scenario(per_company: float) -> list[str]:
        tokens = per_company * remaining
        days = max(math.ceil(tokens / budget), math.ceil(statistics.mean(calls) * remaining / rpd))
        return [
            f"{per_company:,.0f}",
            f"{tokens:,.0f}",
            f"**{days}**",
            f"{tokens / tpm / 60:.1f} h",
        ]

    return (
        "## 3. Full-run projection (free tier)\n\n"
        f"Based on **{len(basis)} companies**"
        + ("" if measured else " (legacy cost data only)")
        + f"; **{remaining}** of {universe_size} still need tagging. Daily budget "
        f"{budget:,} counted tokens on `{settings.groq_model_fast}` (Groq limit "
        f"{settings.groq_tokens_per_day:,}), {rpd:,} requests/day, {tpm:,} tokens/min.\n\n"
        + _table(
            [
                "Scenario",
                "Per-company tokens",
                "Remaining tokens",
                "Days at budget",
                "Pure TPM time",
            ],
            [
                ["Median company", *scenario(median)],
                ["Mean company", *scenario(mean)],
                ["p90 company", *scenario(p90)],
            ],
        )
        + f"\n\nRange across companies: {totals[0]:,} to {totals[-1]:,} counted tokens."
    )


async def progress_line(engine: AsyncEngine, settings: Settings, universe: int) -> str:
    """One line for the daily run: how far along the full run is, and today's spend."""
    rows = await _rows(
        engine,
        select(ingestion_jobs.c.cik, ingestion_jobs.c.stage, ingestion_jobs.c.status),
    )
    status: dict[int, dict[str, str]] = defaultdict(dict)
    for r in rows:
        status[r["cik"]][r["stage"]] = r["status"]
    done = sum(
        1
        for st in status.values()
        if all(st.get(s) == "done" for s in ("edgar", "tagging", "index"))
    )
    since = func.now() - timedelta(hours=24)  # the database's clock, like created_at (D-042)
    used = (
        await _rows(
            engine,
            select(func.coalesce(func.sum(COUNTED_TOKENS), 0).label("t")).where(
                llm_calls.c.model == settings.groq_model_fast, llm_calls.c.created_at >= since
            ),
        )
    )[0]["t"]
    news_today = sum(
        1
        for _ in (settings.raw_dir / "newsapi").glob(
            f"*/{datetime.now(UTC).date().isoformat()}.json"
        )
    )
    return (
        f"Companies done {done}/{universe} ({universe - done} remaining) | "
        f"{settings.groq_model_fast} tokens used (24h): {int(used):,} / "
        f"{settings.groq_daily_token_budget:,} | NewsAPI today: {news_today}/"
        f"{settings.newsapi_requests_per_day}"
    )


# ------------------------------------------------------------------ 4. sectors
async def sector_section(engine: AsyncEngine, ciks: list[int] | None = None) -> str:
    stmt = select(companies).where(companies.c.llm_sector.is_not(None))
    if ciks is not None:
        stmt = stmt.where(companies.c.cik.in_(ciks))
    comps = await _rows(engine, stmt)
    n = len(comps)
    sic_u = sum(c["sic_sector"] == c["universe_sector"] for c in comps)
    llm_u = sum(c["llm_sector"] == c["universe_sector"] for c in comps)
    sic_llm = sum(c["sic_sector"] == c["llm_sector"] for c in comps)
    conflicts = [
        [
            f"`{c['ticker']}`",
            c["universe_sector"],
            f"{c['sic']} {c['sic_description']}",
            c["sic_sector"] or "unmapped",
            f"{c['llm_sector']} ({c['llm_sub_industry']}, conf {c['llm_sector_confidence']:.2f})",
            c["llm_sector_rationale"],
        ]
        for c in sorted(comps, key=lambda c: c["ticker"])
        if c["sic_sector"] != c["llm_sector"]
    ]
    return (
        "## 4. Sector classification agreement\n\n"
        f"Companies with both signals: {n}. `universe_sector` is the hand-picked reference "
        "label from data/universe.csv (D-018).\n\n"
        + _table(
            ["Comparison", "Agreement"],
            [
                ["SIC-rule sector vs universe_sector", _pct(sic_u, n)],
                ["LLM sector vs universe_sector", _pct(llm_u, n)],
                ["SIC-rule vs LLM (sector_conflict = false)", _pct(sic_llm, n)],
            ],
        )
        + "\n\n**SIC vs LLM conflicts**\n\n"
        + (
            _table(
                [
                    "Ticker",
                    "Reference",
                    "SIC code",
                    "SIC-rule sector",
                    "LLM sector",
                    "LLM rationale",
                ],
                conflicts,
            )
            if conflicts
            else "None."
        )
    )


# ------------------------------------------------------------------ 5. failures + cache
def _dir_size(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()] if path.exists() else []
    return sum(p.stat().st_size for p in files), len(files)


async def failure_section(engine: AsyncEngine, settings: Settings, ciks: list[int]) -> str:
    rows = await _rows(
        engine,
        select(ingestion_jobs, companies.c.ticker)
        .join(companies, companies.c.cik == ingestion_jobs.c.cik)
        .where(ingestion_jobs.c.cik.in_(ciks), ingestion_jobs.c.status != "done"),
    )
    fails = [[f"`{r['ticker']}`", r["stage"], r["status"], (r["error"] or "")[:200]] for r in rows]
    sec_b, sec_n = _dir_size(settings.raw_dir / "sec")
    news_b, news_n = _dir_size(settings.raw_dir / "newsapi")
    model_b, _ = _dir_size(settings.models_dir)
    return (
        "## 5. Failures and disk usage\n\n"
        + (_table(["Ticker", "Stage", "Status", "Reason"], fails) if fails else "No failed stages.")
        + f"\n\nRaw cache on disk: SEC {sec_b / 1e6:,.1f} MB ({sec_n} files), "
        f"NewsAPI {news_b / 1e6:,.2f} MB ({news_n} files). "
        f"Embedding model: {model_b / 1e6:,.0f} MB."
    )


# ------------------------------------------------------------------ 6. samples
async def sample_section(engine: AsyncEngine, ciks: list[int], n: int = 4) -> str:
    rows = await _rows(
        engine,
        select(risk_factors, companies.c.ticker)
        .join(companies, companies.c.cik == risk_factors.c.cik)
        .where(risk_factors.c.cik.in_(ciks), risk_factors.c.tag_source == "llm")
        .order_by(func.md5(risk_factors.c.id)),
    )
    picked, seen_t, seen_c = [], set(), set()
    for r in rows:  # deterministic pseudo-random pick: distinct companies and categories
        if r["ticker"] in seen_t or r["category"] in seen_c:
            continue
        picked.append(r)
        seen_t.add(r["ticker"])
        seen_c.add(r["category"])
        if len(picked) == n:
            break
    parts = []
    for r in picked:
        body = " ".join(r["body"].split()[:60])
        parts.append(
            f"**`{r['ticker']}` FY{r['fiscal_year']} → `{r['category']}`**  \n"
            f"*Heading:* {r['heading']}  \n*Body (first 60 words):* {body}…  \n"
            f"*LLM summary:* {r['summary']}"
        )
    return "## 6. Sample tagged risk factors\n\n" + "\n\n".join(parts)


async def build_report(
    engine: AsyncEngine, settings: Settings, ciks: list[int], universe_size: int
) -> Report:
    tok_text, tok_data = await token_section(engine, ciks)
    return Report(
        {
            "companies": await company_section(engine, ciks),
            "tokens": tok_text,
            "projection": await projection_section(engine, settings, ciks, universe_size, tok_data),
            "sectors": await sector_section(engine, ciks),
            "failures": await failure_section(engine, settings, ciks),
            "samples": await sample_section(engine, ciks),
        }
    )
