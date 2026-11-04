"""Tagging-configuration experiment: tokens per risk factor vs reliability.

Tags the same latest-year risk factors under each (response mode, batch size) config and
reports, per config: counted tokens per risk factor, Groq prompt-cache hit rate, schema
rejections, risks left as `needs_review`, and agreement with the first (reference) config.

Batches are built exactly as production builds them (per company, in ordinal order), so
the configuration finally chosen for production gets response-cache hits for the
companies measured here instead of paying for them twice.
"""

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.config import Settings
from core.db.schema import companies, risk_factors
from core.llm import DailyLimitError, GroqClient, ResponseMode
from ingestion import prompts
from ingestion.enrich.risk_tagger import RiskInput, TagResult, tag_with_llm
from ingestion.load import postgres as pg


@dataclass(frozen=True)
class Config:
    mode: ResponseMode
    batch_size: int

    @property
    def label(self) -> str:
        return f"{self.mode}:b{self.batch_size}"

    @classmethod
    def parse(cls, spec: str) -> "Config":
        mode, size = spec.split(":")
        if mode not in ("json_schema", "json_object"):
            raise ValueError(f"unknown mode {mode!r}")
        return cls(mode, int(size))  # type: ignore[arg-type]


@dataclass
class ConfigResult:
    config: Config
    risks: int = 0
    tagged: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    rejections: int = 0
    needs_review: int = 0
    agreement: tuple[int, int] | None = None  # (agree, compared) vs the reference config
    paused: str | None = None

    @property
    def counted(self) -> int:
        return self.prompt_tokens - self.cached_tokens + self.completion_tokens

    @property
    def per_risk(self) -> float:
        return self.counted / self.tagged if self.tagged else 0.0

    @property
    def cache_rate(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


async def _latest_risks(engine: AsyncEngine, tickers: list[str]) -> dict[int, list[RiskInput]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    risk_factors.c.cik,
                    risk_factors.c.id,
                    risk_factors.c.heading,
                    risk_factors.c.body,
                    risk_factors.c.fiscal_year,
                )
                .join(companies, companies.c.cik == risk_factors.c.cik)
                .where(companies.c.ticker.in_(tickers))
                .order_by(
                    risk_factors.c.cik, risk_factors.c.fiscal_year.desc(), risk_factors.c.ordinal
                )
            )
        ).all()
    latest_year: dict[int, int] = {}
    out: dict[int, list[RiskInput]] = {}
    for r in rows:
        year = latest_year.setdefault(r.cik, r.fiscal_year)
        if r.fiscal_year == year:
            out.setdefault(r.cik, []).append(RiskInput(r.id, r.heading, r.body))
    return out


async def _usage(engine: AsyncEngine, run_id: str) -> dict[str, int]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "select count(*) filter (where not cache_hit and ok) calls, "
                    "coalesce(sum(prompt_tokens),0) p, coalesce(sum(cached_tokens),0) c, "
                    "coalesce(sum(completion_tokens),0) o, "
                    "count(*) filter (where error = 'schema_rejected') rej "
                    "from llm_calls where run_id = :r"
                ),
                {"r": run_id},
            )
        ).one()
    return {"calls": row.calls, "p": row.p, "c": row.c, "o": row.o, "rej": row.rej}


async def run_tagging_experiment(
    engine: AsyncEngine,
    llm: GroqClient,
    settings: Settings,
    tickers: list[str],
    configs: list[Config],
    base_run_id: str,
) -> list[ConfigResult]:
    risks = await _latest_risks(engine, tickers)
    version = f"{prompts.version('risk_tagging')}@{llm.model_for('fast')}"
    results: list[ConfigResult] = []
    tags_by_config: dict[str, dict[str, TagResult]] = {}
    for cfg in configs:
        res = ConfigResult(cfg, risks=sum(len(v) for v in risks.values()))
        llm.run_id = f"{base_run_id}:{cfg.label}"
        tags: list[TagResult] = []
        try:
            for cik, items in risks.items():
                tags += await tag_with_llm(
                    llm,
                    items,
                    cik=cik,
                    stage="experiment_tagging",
                    batch_size=cfg.batch_size,
                    body_words=settings.risk_tag_body_words,
                    mode=cfg.mode,
                )
        except DailyLimitError as exc:
            res.paused = str(exc)
        await pg.save_experiment_tags(engine, f"{version}|{cfg.label}", tags)
        usage = await _usage(engine, llm.run_id)
        res.tagged, res.needs_review = len(tags), sum(t.needs_review for t in tags)
        res.calls, res.rejections = usage["calls"], usage["rej"]
        res.prompt_tokens, res.cached_tokens, res.completion_tokens = (
            usage["p"],
            usage["c"],
            usage["o"],
        )
        tags_by_config[cfg.label] = {t.key: t for t in tags}
        results.append(res)
        if res.paused:
            break
    ref = tags_by_config.get(configs[0].label, {})
    for res in results[1:]:
        mine = tags_by_config[res.config.label]
        shared = [
            k for k in mine if k in ref and not ref[k].needs_review and not mine[k].needs_review
        ]
        res.agreement = (sum(mine[k].category == ref[k].category for k in shared), len(shared))
    return results


def results_markdown(results: list[ConfigResult], tickers: list[str]) -> str:
    rows = []
    for r in results:
        agree = (
            "reference"
            if r.agreement is None
            else f"{100 * r.agreement[0] / r.agreement[1]:.0f}% ({r.agreement[0]}/{r.agreement[1]})"
            if r.agreement[1]
            else "n/a"
        )
        rows.append(
            f"| `{r.config.label}` | {r.tagged}/{r.risks} | {r.calls} | {r.counted:,} | "
            f"**{r.per_risk:,.0f}** | {r.cache_rate:.0%} | {r.rejections} | {r.needs_review} | "
            f"{agree}{' · paused' if r.paused else ''} |"
        )
    return (
        f"## Tagging configuration experiment ({', '.join(tickers)})\n\n"
        "| Config | Tagged | Calls | Counted tokens | Tokens / risk | Prompt cached | "
        "Schema rejections | needs_review | Agreement vs reference |\n"
        "|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "Counted tokens = prompt - cached + completion (what Groq's rate limits see). "
        "Rejected generations return no usage, so their tokens are not included."
    )
