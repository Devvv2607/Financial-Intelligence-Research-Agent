"""Ingestion reports built only from stored data (Postgres, Weaviate counts, disk).

Every number here is computed from what the pipeline actually recorded — llm_calls for
tokens, ingestion_jobs for outcomes, companies for sector labels — so the README can
cite these reports directly. Re-run any time: `python -m ingestion report`.
"""

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.db.schema import (
    companies,
    eight_k_events,
    filings,
    ingestion_jobs,
    llm_calls,
    news_articles,
    risk_factors,
    risk_tag_experiments,
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
async def token_section(engine: AsyncEngine, ciks: list[int]) -> tuple[str, dict[str, Any]]:
    calls = await _rows(engine, select(llm_calls).where(llm_calls.c.cik.in_(ciks)))
    tickers = {
        r["cik"]: r["ticker"]
        for r in await _rows(
            engine, select(companies.c.cik, companies.c.ticker).where(companies.c.cik.in_(ciks))
        )
    }
    per: dict[tuple[int, str], Counter[str]] = defaultdict(Counter)
    for c in calls:
        k = per[(c["cik"], c["stage"])]
        if c["cache_hit"]:
            k["cache_hits"] += 1
            continue
        k["calls"] += 1
        k["in"] += c["prompt_tokens"]
        k["out"] += c["completion_tokens"]
        k["reasoning"] += c["reasoning_tokens"] or 0
        k["latency_ms"] += c["latency_ms"]

    stages = [*PRODUCTION_STAGES, EXPERIMENT_STAGE]
    rows = []
    for cik in sorted(tickers, key=lambda x: tickers[x]):
        cells = []
        for st in stages:
            k = per.get((cik, st), Counter())
            cells.append(f"{k['in'] + k['out']:,}" if k["calls"] else "0")
        prod = sum(
            per.get((cik, st), Counter())["in"] + per.get((cik, st), Counter())["out"]
            for st in PRODUCTION_STAGES
        )
        hits = sum(per.get((cik, st), Counter())["cache_hits"] for st in stages)
        rows.append([f"`{tickers[cik]}`", *cells, f"**{prod:,}**", hits])
    tot = Counter()  # type: ignore[var-annotated]
    for (_, st), k in per.items():
        if st in PRODUCTION_STAGES:
            tot.update(k)
    reuse = await _rows(
        engine,
        select(risk_factors.c.tag_source, func.count())
        .where(risk_factors.c.cik.in_(ciks))
        .group_by(risk_factors.c.tag_source),
    )
    reuse_map = {r["tag_source"]: r["count_1"] for r in reuse}
    text = (
        "## 2. Groq token usage\n\n"
        + _table(
            [
                "Ticker",
                "Item tagging (latest)",
                "Tagging (prior, new/changed)",
                "Sector LLM",
                "Section fallback",
                "Experiment: headings-only",
                "Production total",
                "Cache hits",
            ],
            rows,
        )
        + f"\n\nProduction totals (excl. experiment): {tot['calls']} calls, "
        f"{tot['in']:,} input + {tot['out']:,} output tokens "
        f"({tot['reasoning']:,} of the output were hidden reasoning tokens).\n\n"
        f"Risk-factor tag sources: LLM {reuse_map.get('llm', 0)}, inherited via similarity "
        f"{reuse_map.get('inherited', 0)}, untagged {reuse_map.get(None, 0)}."
    )
    return text, {"per": per, "tickers": tickers}


# ------------------------------------------------------------------ 3. projection
async def projection_section(
    engine: AsyncEngine,
    settings: Settings,
    ciks: list[int],
    universe_size: int,
    token_data: dict[str, Any],
) -> str:
    per = token_data["per"]
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
    if not done:
        return "## 3. Full-run projection\n\nNo company has finished tagging yet."

    def prod(cik: int, key: str) -> int:
        return sum(per.get((cik, st), Counter())[key] for st in PRODUCTION_STAGES)

    totals = sorted(prod(c, "in") + prod(c, "out") for c in done)
    ins = [prod(c, "in") for c in done]
    outs = [prod(c, "out") for c in done]
    calls = [prod(c, "calls") for c in done]
    lat = [prod(c, "latency_ms") for c in done]
    median, mean = statistics.median(totals), statistics.mean(totals)
    p90 = totals[min(len(totals) - 1, math.ceil(0.9 * len(totals)) - 1)]
    remaining = universe_size - len(done)
    tpd, rpd, tpm = (
        settings.groq_tokens_per_day,
        settings.groq_requests_per_day,
        settings.groq_tokens_per_minute,
    )
    model = settings.groq_model_fast
    price_in, price_out = settings.groq_prices_usd_per_mtok.get(model, (float("nan"), float("nan")))

    def scenario(per_company: float) -> list[str]:
        tokens = per_company * remaining
        req = statistics.mean(calls) * remaining
        days = max(math.ceil(tokens / tpd), math.ceil(req / rpd))
        cost = (
            remaining * (statistics.mean(ins) * price_in + statistics.mean(outs) * price_out) / 1e6
        )
        cost *= per_company / mean
        return [f"{tokens:,.0f}", f"{days}", f"{tokens / tpm / 60:.1f} h", f"${cost:,.2f}"]

    rows = [
        ["Median company", *scenario(median)],
        ["Mean company", *scenario(mean)],
        ["p90 company", *scenario(p90)],
    ]

    # Headings-only experiment: same risk factors, tags from heading text only.
    exp_ciks = [c for c in done if per.get((c, EXPERIMENT_STAGE), Counter())["calls"]]
    full_tok = sum(
        per.get((c, "risk_tagging"), Counter())["in"]
        + per.get((c, "risk_tagging"), Counter())["out"]
        for c in exp_ciks
    )
    head_tok = sum(
        per.get((c, EXPERIMENT_STAGE), Counter())["in"]
        + per.get((c, EXPERIMENT_STAGE), Counter())["out"]
        for c in exp_ciks
    )
    pairs = await _rows(
        engine,
        select(risk_factors.c.category.label("full"), risk_tag_experiments.c.category.label("head"))
        .join(risk_tag_experiments, risk_tag_experiments.c.risk_factor_id == risk_factors.c.id)
        .where(
            risk_factors.c.cik.in_(ciks),
            risk_tag_experiments.c.mode == "headings_only",
            risk_factors.c.tag_source == "llm",
        ),
    )
    agree = sum(p["full"] == p["head"] for p in pairs)
    confusions = Counter(
        (p["full"], p["head"]) for p in pairs if p["full"] != p["head"]
    ).most_common(5)
    saving = 1 - head_tok / full_tok if full_tok else 0.0
    tagging_share = statistics.mean(
        (
            per.get((c, "risk_tagging"), Counter())["in"]
            + per.get((c, "risk_tagging"), Counter())["out"]
            + per.get((c, "risk_tagging_prior"), Counter())["in"]
            + per.get((c, "risk_tagging_prior"), Counter())["out"]
        )
        / max(prod(c, "in") + prod(c, "out"), 1)
        for c in done
    )
    head_mean = mean * (1 - tagging_share * saving)
    lat_h = statistics.mean(lat) * remaining / 3_600_000

    return (
        "## 3. Full-run projection\n\n"
        f"Based on **{len(done)} companies** that finished tagging; **{remaining}** of "
        f"{universe_size} remain. All production LLM calls use `{model}`. Limits used: "
        f"{tpd:,} tokens/day, {rpd:,} requests/day, {tpm:,} tokens/min. Price: "
        f"${price_in}/M input, ${price_out}/M output.\n\n"
        + _table(
            [
                "Per-company tokens",
                "Remaining-run tokens",
                "Free-tier days",
                "Min. time at TPM limit",
                "Paid-tier cost",
            ],
            rows,
        )
        + f"\n\n- Per-company production tokens: median {median:,.0f}, mean {mean:,.0f}, "
        f"p90 {p90:,.0f} "
        f"(min {totals[0]:,}, max {totals[-1]:,}).\n"
        f"- **Paid-tier time**: LLM calls run one at a time per model by design; summed observed "
        f"latency projects to ~{lat_h:.1f} h of LLM time for the remaining run, if paid-tier "
        "rate limits don't bind (not verified: paid limits weren't available to measure). "
        "SEC download time is extra (~25 requests/company at 8 req/s).\n\n"
        "### Headings-only tagging\n\n"
        f"Measured on {len(exp_ciks)} companies, same latest-year risk factors: full mode "
        f"{full_tok:,} tokens vs headings-only {head_tok:,} tokens → **{saving:.0%} fewer "
        f"tagging tokens**. Tagging is {tagging_share:.0%} of a company's production tokens, "
        f"so headings-only would cut the per-company mean from {mean:,.0f} to ~{head_mean:,.0f} "
        f"tokens (free tier: ~{math.ceil(head_mean * remaining / tpd)} days).\n\n"
        f"Agreement with full-mode tags: **{_pct(agree, len(pairs))}**. Full-mode tags are the "
        "reference here, not ground truth, so this measures consistency, not accuracy. "
        "Most common disagreements (full → headings-only): "
        + (", ".join(f"{a}→{b} (x{n})" for (a, b), n in confusions) or "none")
        + "."
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
