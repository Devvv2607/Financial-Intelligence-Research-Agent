"""Ingestion orchestration: per-company stages, resumable via `ingestion_jobs`.

Stages run in order per company: edgar -> tagging -> news -> index. A stage marked
`done` is skipped on re-runs (unless --force), so an interrupted run — or one paused by
Groq's daily token limit — resumes where it stopped. Companies run concurrently; the
SEC rate limiter and the per-model LLM lock are shared, so concurrency never exceeds
either budget.
"""

import asyncio
import csv
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import weaviate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.db.schema import (
    companies,
    eight_k_events,
    filing_sections,
    filings,
    news_articles,
    risk_factors,
    risk_tag_experiments,
)
from core.embeddings import Embedder
from core.llm import DailyLimitError, GroqClient
from core.vectorstore import FILING_CHUNK, NEWS_ARTICLE, RISK_FACTOR, ensure_collections
from ingestion import prompts
from ingestion.edgar.client import EdgarClient, EdgarError
from ingestion.edgar.submissions import FilingRef, load_filings, load_ticker_map
from ingestion.edgar.xbrl import COMPANYFACTS_URL, extract_annual_facts, metrics_found
from ingestion.enrich.risk_tagger import (
    RiskInput,
    TagResult,
    inherit_tags,
    similarity_text,
    tag_with_llm,
)
from ingestion.enrich.sector_llm import classify_sector
from ingestion.enrich.sector_rules import sic_to_sector
from ingestion.extract.html_clean import blocks_text, html_to_blocks
from ingestion.extract.risk_splitter import split_risk_factors
from ingestion.extract.sections import (
    Section,
    SectionName,
    business_overview,
    find_section_llm,
    find_section_regex,
)
from ingestion.load import postgres as pg
from ingestion.load.chunker import chunk_text, embedding_input, make_header, truncate_tokens
from ingestion.load.weaviate import PendingObjects, replace_company_objects
from ingestion.news.client import NewsApiClient, NewsApiError, build_query, parse_articles

logger = logging.getLogger(__name__)

Stage = Literal["edgar", "tagging", "news", "index"]
STAGES: tuple[Stage, ...] = ("edgar", "tagging", "news", "index")

# The 10 companies agreed for `ingest-sample`: demo-query companies, several sectors and
# 10-K layouts, and one bank (expected-failure path).
SAMPLE_TICKERS = ("AAPL", "NVDA", "AMD", "INTC", "BA", "TSLA", "JNJ", "UNH", "XOM", "JPM")
# Headings-only tagging is measured on three different 10-K layouts only: running both
# modes everywhere would double tagging tokens for no extra signal (owner's call).
EXPERIMENT_TICKERS = ("AAPL", "INTC", "XOM")

# Successor registrant -> predecessor CIK whose history (10-Ks, XBRL) still belongs to the
# company. ExxonMobil reorganized under a new holding-company registrant on 2026-07-01
# (8-K12B); every 10-K before that is under Exxon Mobil Corp (CIK 34088).
PREDECESSOR_CIKS: dict[int, int] = {2115436: 34088}

MIN_RISK_FACTORS = 5
EIGHT_K_MAX_CHARS = 30_000
RISK_EMBED_TOKENS = 500  # heading + body opening, cut to fit the 512-token window (D-001)
EXPERIMENT_HEADINGS_ONLY = "headings_only"


class StagePaused(RuntimeError):  # noqa: N818 - a pause is an expected outcome, not an error
    """A daily limit (ours or a provider's) was reached; the stage resumes on the next run."""


class StageError(RuntimeError):
    """Expected, reportable failure for one company (reason goes in the report)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class UniverseRow:
    ticker: str
    name: str
    universe_sector: str


def read_universe(path: Path) -> list[UniverseRow]:
    with path.open(encoding="utf-8", newline="") as f:
        return [
            UniverseRow(r["ticker"].strip().upper(), r["name"].strip(), r["universe_sector"])
            for r in csv.DictReader(f)
        ]


@dataclass
class CompanyOutcome:
    ticker: str
    cik: int | None = None
    stages: dict[str, str] = field(default_factory=dict)  # stage -> done|failed|skipped|paused
    errors: dict[str, str] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        edgar: EdgarClient,
        llm: GroqClient,
        embedder: Embedder,
        weaviate_client: weaviate.WeaviateAsyncClient,
        news: NewsApiClient | None,
        *,
        run_id: str,
        concurrency: int = 2,
        experiment_tickers: frozenset[str] = frozenset(),
        on_progress: Callable[[str, str], None] | None = None,
    ) -> None:
        self.s = settings
        self.engine = engine
        self.edgar = edgar
        self.llm = llm
        self.embedder = embedder
        self.wv = weaviate_client
        self.news = news
        self.run_id = run_id
        self._sem = asyncio.Semaphore(concurrency)
        # Prompt version + model: tags from another model (e.g. a dev run on the agent
        # model) count as stale and are redone by the production model.
        self._tag_version = f"{prompts.version('risk_tagging')}@{settings.model_for('fast')}"
        self._experiment_tickers = experiment_tickers
        self._on_progress = on_progress or (lambda ticker, msg: None)
        self._ticker_map: dict[str, int] = {}

    async def run(
        self,
        rows: list[UniverseRow],
        stages: tuple[Stage, ...],
        *,
        force: bool = False,
        refresh: bool = False,
    ) -> list[CompanyOutcome]:
        self._ticker_map = await load_ticker_map(self.edgar, refresh=refresh)
        return list(await asyncio.gather(*(self._company(r, stages, force, refresh) for r in rows)))

    async def _company(
        self, row: UniverseRow, stages: tuple[Stage, ...], force: bool, refresh: bool
    ) -> CompanyOutcome:
        out = CompanyOutcome(row.ticker)
        cik = self._ticker_map.get(row.ticker)
        if cik is None:
            out.errors["edgar"] = "ticker not in SEC company_tickers.json"
            out.stages["edgar"] = "failed"
            return out
        out.cik = cik
        await pg.ensure_company(self.engine, cik, row.ticker, row.name, row.universe_sector)
        async with self._sem:
            for stage in stages:
                if not force and await pg.job_status(self.engine, cik, stage) == "done":
                    out.stages[stage] = "done"
                    continue
                if stage == "index" and out.stages.get("tagging") not in (None, "done"):
                    out.stages[stage] = "skipped"
                    out.errors[stage] = "waiting for tagging to finish"
                    continue
                if stage != "edgar" and stage != "news" and out.stages.get("edgar") == "failed":
                    out.stages[stage] = "skipped"
                    continue
                self._on_progress(row.ticker, stage)
                await pg.set_job(self.engine, cik, stage, "running", run_id=self.run_id)
                try:
                    details = await self._run_stage(stage, row, cik, refresh)
                except (DailyLimitError, StagePaused) as exc:
                    out.stages[stage], out.errors[stage] = "paused", str(exc)
                    await pg.set_job(
                        self.engine, cik, stage, "paused", error=str(exc), run_id=self.run_id
                    )
                except (StageError, EdgarError, NewsApiError) as exc:
                    out.stages[stage], out.errors[stage] = "failed", str(exc)
                    await pg.set_job(
                        self.engine,
                        cik,
                        stage,
                        "failed",
                        error=str(exc),
                        details=getattr(exc, "details", None),
                        run_id=self.run_id,
                    )
                except Exception as exc:  # unexpected: record and keep the run going
                    logger.exception("stage crashed", extra={"ticker": row.ticker, "stage": stage})
                    msg = f"{type(exc).__name__}: {exc}"
                    out.stages[stage], out.errors[stage] = "failed", msg
                    await pg.set_job(
                        self.engine, cik, stage, "failed", error=msg, run_id=self.run_id
                    )
                else:
                    out.stages[stage] = "done"
                    await pg.set_job(
                        self.engine, cik, stage, "done", details=details, run_id=self.run_id
                    )
        self._on_progress(row.ticker, "finished")
        return out

    async def _run_stage(
        self, stage: Stage, row: UniverseRow, cik: int, refresh: bool
    ) -> dict[str, Any]:
        if stage == "edgar":
            return await self.stage_edgar(row, cik, refresh)
        if stage == "tagging":
            return await self.stage_tagging(cik, row.ticker)
        if stage == "news":
            return await self.stage_news(row, cik)
        return await self.stage_index(cik)

    # ---------------------------------------------------------------- edgar
    async def stage_edgar(self, row: UniverseRow, cik: int, refresh: bool) -> dict[str, Any]:
        since = datetime.now(UTC).date() - timedelta(days=365)
        meta, refs = await load_filings(self.edgar, cik, since_8k=since, refresh=refresh)
        predecessor = PREDECESSOR_CIKS.get(cik)
        if predecessor is not None:
            _, older = await load_filings(self.edgar, predecessor, since_8k=since, refresh=refresh)
            seen = {r.accession_no for r in refs}
            refs = refs + [r for r in older if r.accession_no not in seen]
        ten_ks = sorted(
            (r for r in refs if r.form_type == "10-K"), key=lambda r: r.filing_date, reverse=True
        )[:2]
        eight_ks = [r for r in refs if r.form_type == "8-K"]
        if not ten_ks:
            raise StageError(
                "no 10-K found (successor registrant without a mapped predecessor?)"
                if predecessor is None
                else "no 10-K found"
            )

        await pg.upsert_company(
            self.engine, meta, row.ticker, row.universe_sector, sic_to_sector(meta.sic)
        )

        details: dict[str, Any] = {"predecessor_cik": predecessor, "ten_ks": []}
        for ref in ten_ks:
            details["ten_ks"].append(await self._ingest_10k(cik, ref))
        latest = details["ten_ks"][0]
        if not latest["item_1a"]["ok"] or latest["risk_factors"] < MIN_RISK_FACTORS:
            details["error"] = "latest 10-K: Item 1A not extracted or too few risk factors"

        ok_8k = 0
        for ref in eight_ks:
            try:
                html = await self.edgar.get_bytes(ref.url)
            except EdgarError:
                continue
            body = blocks_text(html_to_blocks(html))[:EIGHT_K_MAX_CHARS]
            await pg.upsert_filing(self.engine, cik, ref, None)
            await pg.upsert_eight_k(self.engine, cik, ref, body)
            ok_8k += 1
        details["eight_ks"] = {"found": len(eight_ks), "stored": ok_8k}

        facts = await self._facts(cik, predecessor)
        await pg.upsert_facts(self.engine, cik, facts)
        by_year = metrics_found(facts)
        latest_fy = max(by_year) if by_year else None
        details["facts"] = {
            "rows": len(facts),
            "latest_fiscal_year": latest_fy,
            "metrics_latest_year": sorted(by_year.get(latest_fy, set())) if latest_fy else [],
        }
        if "error" in details:
            raise StageError(details["error"], details)
        return details

    async def _ingest_10k(self, cik: int, ref: FilingRef) -> dict[str, Any]:
        fiscal_year = pg.ten_k_fiscal_year(ref)
        await pg.upsert_filing(self.engine, cik, ref, fiscal_year)
        blocks = html_to_blocks(await self.edgar.get_bytes(ref.url))
        info: dict[str, Any] = {"accession_no": ref.accession_no, "fiscal_year": fiscal_year}
        names: tuple[SectionName, ...] = ("item_1", "item_1a")
        for name in names:
            section: Section | None = find_section_regex(blocks, name)
            if section is None:
                section = await find_section_llm(blocks, name, self.llm, cik=cik)
            if section is None:
                info[name] = {"ok": False, "method": None, "chars": 0}
                if name == "item_1":
                    overview = business_overview(blocks)
                    if overview:
                        await pg.upsert_section(
                            self.engine, ref.accession_no, "business_overview", overview, "opening"
                        )
                        info["business_overview"] = {"ok": True, "chars": len(overview)}
                continue
            text = blocks_text(section.body(blocks))
            await pg.upsert_section(self.engine, ref.accession_no, name, text, section.method)
            info[name] = {"ok": True, "method": section.method, "chars": len(text)}
            if name == "item_1a":
                rfs = split_risk_factors(section.body(blocks))
                await pg.replace_risk_factors(self.engine, cik, ref.accession_no, fiscal_year, rfs)
                info["risk_factors"] = len(rfs)
        info.setdefault("risk_factors", 0)
        return info

    async def _facts(self, cik: int, predecessor: int | None) -> list[Any]:
        merged: dict[str, Any] = {"facts": {"us-gaap": {}}}
        for c in (cik, predecessor):
            if c is None:
                continue
            try:
                data = await self.edgar.get_json(COMPANYFACTS_URL.format(cik=c))
            except EdgarError:
                continue  # new registrants may not have companyfacts yet
            for concept, body in data.get("facts", {}).get("us-gaap", {}).items():
                tgt = merged["facts"]["us-gaap"].setdefault(concept, {"units": {}})
                for unit, entries in body.get("units", {}).items():
                    tgt["units"].setdefault(unit, []).extend(entries)
        return extract_annual_facts(merged)

    # ---------------------------------------------------------------- tagging
    async def _risks(self, cik: int) -> dict[int, list[dict[str, Any]]]:
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(risk_factors)
                        .where(risk_factors.c.cik == cik)
                        .order_by(risk_factors.c.fiscal_year.desc(), risk_factors.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
        by_year: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by_year.setdefault(r["fiscal_year"], []).append(dict(r))
        return by_year

    async def stage_tagging(self, cik: int, ticker: str) -> dict[str, Any]:
        by_year = await self._risks(cik)
        if not by_year:
            raise StageError("no risk factors to tag")
        years = sorted(by_year, reverse=True)
        latest, prior = by_year[years[0]], by_year[years[1]] if len(years) > 1 else []
        s = self.s
        details: dict[str, Any] = {
            "latest_year": years[0],
            "latest_count": len(latest),
            "prior_count": len(prior),
        }

        todo = [_risk_input(r) for r in latest if self._needs_tag(r)]
        new_tags = await tag_with_llm(
            self.llm,
            todo,
            cik=cik,
            stage="risk_tagging",
            batch_size=s.risk_tag_batch_size,
            body_words=s.risk_tag_body_words,
            mode=s.risk_tag_response_mode,
        )
        await pg.save_tags(self.engine, new_tags, self._tag_version)
        details["latest_llm_tagged"] = len(new_tags)

        if prior:
            by_year = await self._risks(cik)
            latest = by_year[years[0]]
            latest_tags = [
                TagResult(r["id"], r["category"], r["summary"] or "", "llm")
                for r in latest
                if not self._needs_tag(r)
            ]
            latest_by_key = {r["id"]: r for r in latest}
            prior_todo = [_risk_input(r) for r in prior if self._needs_tag(r)]
            if prior_todo and latest_tags:
                latest_vecs = await self.embedder.embed(
                    [
                        similarity_text(_risk_input(latest_by_key[t.key]), s.risk_tag_body_words)
                        for t in latest_tags
                    ]
                )
                prior_vecs = await self.embedder.embed(
                    [similarity_text(r, s.risk_tag_body_words) for r in prior_todo]
                )
                inherited, remaining = inherit_tags(
                    prior_todo, latest_tags, prior_vecs, latest_vecs, s.risk_tag_inherit_similarity
                )
                await pg.save_tags(self.engine, inherited, self._tag_version)
                prior_tags = await tag_with_llm(
                    self.llm,
                    remaining,
                    cik=cik,
                    stage="risk_tagging_prior",
                    batch_size=s.risk_tag_batch_size,
                    body_words=s.risk_tag_body_words,
                    mode=s.risk_tag_response_mode,
                )
                await pg.save_tags(self.engine, prior_tags, self._tag_version)
                details["prior_inherited"] = len(inherited)
                details["prior_llm_tagged"] = len(prior_tags)

        await self._sector_llm(cik, details)
        if ticker in self._experiment_tickers:
            details["experiment_headings_only"] = await self._headings_experiment(cik, years[0])
        return details

    def _needs_tag(self, r: dict[str, Any]) -> bool:
        """Untagged, or tagged by an older version of the taxonomy prompt."""
        return r["category"] is None or r["tag_version"] != self._tag_version

    async def _sector_llm(self, cik: int, details: dict[str, Any]) -> None:
        async with self.engine.connect() as conn:
            company = (
                (await conn.execute(select(companies).where(companies.c.cik == cik)))
                .mappings()
                .one()
            )
            if company["llm_sector"] is not None:
                details["sector_llm"] = "already classified"
                return
            # Item 1 when extracted; otherwise the filing's opening business overview.
            source = (
                await conn.execute(
                    select(filing_sections.c.text, filing_sections.c.section)
                    .join(filings, filings.c.accession_no == filing_sections.c.accession_no)
                    .where(
                        filings.c.cik == cik,
                        filing_sections.c.section.in_(("item_1", "business_overview")),
                    )
                    .order_by(
                        (filing_sections.c.section == "item_1").desc(),
                        filings.c.filing_date.desc(),
                    )
                    .limit(1)
                )
            ).one_or_none()
        if source is None:
            details["sector_llm"] = "skipped: no Item 1 or business overview text"
            return
        result = await classify_sector(self.llm, company["name"], source.text, cik=cik)
        details["sector_source"] = source.section
        await pg.save_sector_llm(
            self.engine,
            cik,
            result.sector,
            result.sub_industry,
            result.confidence,
            result.rationale,
        )
        details["sector_llm"] = result.sector

    async def _headings_experiment(self, cik: int, latest_year: int) -> int:
        async with self.engine.connect() as conn:
            done = set(
                (
                    await conn.execute(
                        select(risk_tag_experiments.c.risk_factor_id)
                        .join(
                            risk_factors, risk_factors.c.id == risk_tag_experiments.c.risk_factor_id
                        )
                        .where(
                            risk_factors.c.cik == cik,
                            risk_tag_experiments.c.mode == EXPERIMENT_HEADINGS_ONLY,
                        )
                    )
                ).scalars()
            )
        latest = (await self._risks(cik))[latest_year]
        todo = [_risk_input(r) for r in latest if r["id"] not in done]
        tags = await tag_with_llm(
            self.llm,
            todo,
            cik=cik,
            stage="experiment_headings_only",
            batch_size=self.s.risk_tag_batch_size,
            body_words=self.s.risk_tag_body_words,
            headings_only=True,
        )
        await pg.save_experiment_tags(self.engine, EXPERIMENT_HEADINGS_ONLY, tags)
        return len(tags)

    # ---------------------------------------------------------------- news
    async def stage_news(self, row: UniverseRow, cik: int) -> dict[str, Any]:
        if self.news is None:
            raise StageError("NEWSAPI_KEY not set")
        async with self.engine.connect() as conn:
            name = (
                await conn.execute(select(companies.c.name).where(companies.c.cik == cik))
            ).scalar_one_or_none() or row.name
        query = build_query(name, row.ticker)
        if (
            not self.news.cached_today(row.ticker)
            and self.news.requests_today() >= self.s.newsapi_requests_per_day
        ):
            raise StagePaused(
                f"NewsAPI daily cap reached ({self.s.newsapi_requests_per_day} requests)"
            )
        payload = await self.news.everything(row.ticker, query, self.s.news_window_days)
        articles = parse_articles(payload)
        await pg.upsert_news(self.engine, cik, articles)
        return {
            "query": query,
            "total_results": payload.get("totalResults"),
            "stored": len(articles),
        }

    # ---------------------------------------------------------------- index
    async def stage_index(self, cik: int) -> dict[str, Any]:
        await self._ensure_collections()
        tok = self.embedder.tokenizer
        async with self.engine.connect() as conn:
            company = (
                (await conn.execute(select(companies).where(companies.c.cik == cik)))
                .mappings()
                .one()
            )
            sections = (
                (
                    await conn.execute(
                        select(
                            filing_sections,
                            filings.c.fiscal_year,
                            filings.c.filing_date,
                            filings.c.form_type,
                            filings.c.url,
                        )
                        .join(filings, filings.c.accession_no == filing_sections.c.accession_no)
                        .where(filings.c.cik == cik)
                    )
                )
                .mappings()
                .all()
            )
            eight_ks = (
                (
                    await conn.execute(
                        select(eight_k_events, filings.c.url)
                        .join(filings, filings.c.accession_no == eight_k_events.c.accession_no)
                        .where(eight_k_events.c.cik == cik)
                    )
                )
                .mappings()
                .all()
            )
            rfs = (
                (
                    await conn.execute(
                        select(risk_factors, filings.c.filing_date, filings.c.url)
                        .join(filings, filings.c.accession_no == risk_factors.c.accession_no)
                        .where(risk_factors.c.cik == cik)
                    )
                )
                .mappings()
                .all()
            )
            news = (
                (await conn.execute(select(news_articles).where(news_articles.c.cik == cik)))
                .mappings()
                .all()
            )

        ticker = company["ticker"]
        base = {
            "ticker": ticker,
            "cik": cik,
            "company": company["name"],
            "sector": company["llm_sector"] or company["sic_sector"],
        }
        label = {
            "item_1": "Item 1 Business",
            "item_1a": "Item 1A Risk Factors",
            "business_overview": "Business overview",
        }

        chunks = PendingObjects(FILING_CHUNK)
        for sec in sections:
            header = make_header(
                tok, ticker, "10-K", f"FY{sec['fiscal_year']}", label[sec["section"]]
            )
            for c in chunk_text(sec["text"], tok, self.s.chunk_tokens, self.s.chunk_overlap_tokens):
                chunks.add(
                    f"{sec['accession_no']}:{sec['section']}:{c.index}",
                    {
                        **base,
                        "text": c.text,
                        "source_url": sec["url"],
                        "form_type": "10-K",
                        "fiscal_year": sec["fiscal_year"],
                        "filing_date": _dt(sec["filing_date"]),
                        "section": sec["section"],
                        "item_codes": [],
                        "accession_no": sec["accession_no"],
                        "chunk_index": c.index,
                    },
                    embedding_input(header, c.text),
                )
        for ek in eight_ks:
            header = make_header(
                tok, ticker, "8-K", ek["filing_date"].isoformat(), "Items " + ", ".join(ek["items"])
            )
            for c in chunk_text(ek["body"], tok, self.s.chunk_tokens, self.s.chunk_overlap_tokens):
                chunks.add(
                    f"{ek['accession_no']}:8k:{c.index}",
                    {
                        **base,
                        "text": c.text,
                        "source_url": ek["url"],
                        "form_type": "8-K",
                        "fiscal_year": ek["filing_date"].year,
                        "filing_date": _dt(ek["filing_date"]),
                        "section": "8k_body",
                        "item_codes": list(ek["items"]),
                        "accession_no": ek["accession_no"],
                        "chunk_index": c.index,
                    },
                    embedding_input(header, c.text),
                )

        risks = PendingObjects(RISK_FACTOR)
        for r in rfs:
            risks.add(
                r["id"],
                {
                    **base,
                    "heading": r["heading"],
                    "body": r["body"],
                    "summary": r["summary"] or "",
                    "risk_category": r["category"] or "untagged",
                    "fiscal_year": r["fiscal_year"],
                    "filing_date": _dt(r["filing_date"]),
                    "form_type": "10-K",
                    "accession_no": r["accession_no"],
                    "source_url": r["url"],
                    "risk_id": r["id"],
                },
                truncate_tokens(tok, f"{r['heading']}\n{r['body']}", RISK_EMBED_TOKENS),
            )

        arts = PendingObjects(NEWS_ARTICLE)
        for a in news:
            arts.add(
                a["id"],
                {
                    **base,
                    "title": a["title"],
                    "description": a["description"] or "",
                    "content": a["content_snippet"] or "",
                    "source_name": a["source_name"] or "",
                    "published_at": a["published_at"],
                    "source_url": a["url"],
                    "article_id": a["id"],
                },
                f"{a['title']}\n{a['description'] or ''}",
            )

        counts: dict[str, int] = {}
        for pending in (chunks, risks, arts):
            vectors = await self.embedder.embed(pending.texts)
            counts[pending.collection] = await replace_company_objects(
                self.wv, pending, vectors, cik
            )
        counts["max_embedding_tokens"] = max(
            (self.embedder.count_tokens(t) for t in chunks.texts + risks.texts + arts.texts),
            default=0,
        )
        return counts

    async def _ensure_collections(self) -> None:
        await ensure_collections(self.wv)


def _risk_input(r: dict[str, Any]) -> RiskInput:
    return RiskInput(r["id"], r["heading"], r["body"])


def _dt(d: Any) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)
