"""Human review of risk tags: export a labelling sheet, then score the model against it.

The export is a stratified sample (every category represented where possible, spread
across companies) of current-version tags from each company's latest 10-K. The owner
fills `your_category`; `score` reports accuracy and the most common confusions. This
gives a ground-truth accuracy number, which agreement between model configurations
cannot.
"""

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import companies, risk_factors
from core.taxonomy import RiskCategory

CATEGORIES: tuple[str, ...] = get_args(RiskCategory)
COLUMNS = [
    "risk_id", "ticker", "fiscal_year", "heading", "body_excerpt", "model_category",
    "model_summary", "needs_review", "your_category", "notes",
]  # fmt: skip
EXCERPT_WORDS = 80


async def export_review_sheet(
    engine: AsyncEngine, path: Path, tag_version: str, n: int = 30
) -> int:
    version = tag_version
    latest = (
        select(risk_factors.c.cik, func.max(risk_factors.c.fiscal_year).label("fy"))
        .group_by(risk_factors.c.cik)
        .subquery()
    )
    async with engine.connect() as conn:
        rows = [
            dict(r)
            for r in (
                await conn.execute(
                    select(risk_factors, companies.c.ticker)
                    .join(companies, companies.c.cik == risk_factors.c.cik)
                    .join(
                        latest,
                        (latest.c.cik == risk_factors.c.cik)
                        & (latest.c.fy == risk_factors.c.fiscal_year),
                    )
                    .where(risk_factors.c.tag_version == version)
                    .order_by(func.md5(risk_factors.c.id))  # deterministic pseudo-random
                )
            ).mappings()
        ]
    picked = stratified_sample(rows, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: Excel on Windows opens it with the right encoding.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in picked:
            w.writerow(
                {
                    "risk_id": r["id"],
                    "ticker": r["ticker"],
                    "fiscal_year": r["fiscal_year"],
                    "heading": r["heading"],
                    "body_excerpt": " ".join(r["body"].split()[:EXCERPT_WORDS]) + " ...",
                    "model_category": r["category"],
                    "model_summary": r["summary"],
                    "needs_review": r["needs_review"],
                    "your_category": "",
                    "notes": "",
                }
            )
    return len(picked)


def stratified_sample(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Round-robin over categories (so rare ones appear), preferring unseen companies."""
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    picked: list[dict[str, Any]] = []
    tickers: Counter[str] = Counter()
    while len(picked) < n and any(by_cat.values()):
        for cat in sorted(by_cat, key=lambda c: len(by_cat[c])):
            pool = by_cat[cat]
            if not pool or len(picked) >= n:
                continue
            best = min(range(len(pool)), key=lambda i: tickers[pool[i]["ticker"]])
            r = pool.pop(best)
            picked.append(r)
            tickers[r["ticker"]] += 1
    return picked


@dataclass
class ReviewScore:
    labelled: int
    correct: int
    invalid_labels: list[str]
    confusions: list[tuple[tuple[str, str], int]]
    per_category: dict[str, tuple[int, int]]  # owner's category -> (model correct, total)

    @property
    def accuracy(self) -> float:
        return self.correct / self.labelled if self.labelled else 0.0


def read_labels(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Labelled sheet rows (your_category set and valid) and the invalid labels."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    labelled, invalid = [], []
    for r in rows:
        mine = (r.get("your_category") or "").strip().lower()
        if not mine:
            continue
        if mine not in CATEGORIES:
            invalid.append(f"{r['risk_id']}: {mine!r}")
            continue
        labelled.append(r | {"your_category": mine})
    return labelled, invalid


def score_labels(
    rows: list[dict[str, str]], model_of: dict[str, str], invalid: list[str] | None = None
) -> ReviewScore:
    """Owner labels vs a model's category per risk id (rows without one are skipped)."""
    labelled = correct = 0
    confusions: Counter[tuple[str, str]] = Counter()
    per_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        mine, model = r["your_category"], model_of.get(r["risk_id"])
        if model is None:
            continue
        labelled += 1
        per_cat[mine][1] += 1
        if model == mine:
            correct += 1
            per_cat[mine][0] += 1
        else:
            confusions[(mine, model)] += 1
    return ReviewScore(
        labelled,
        correct,
        invalid or [],
        confusions.most_common(),
        {k: (v[0], v[1]) for k, v in sorted(per_cat.items())},
    )


def score_review_sheet(path: Path) -> ReviewScore:
    """Owner labels vs the tags frozen in the sheet when it was exported."""
    rows, invalid = read_labels(path)
    return score_labels(rows, {r["risk_id"]: r["model_category"] for r in rows}, invalid)


async def current_tags(engine: AsyncEngine, ids: list[str]) -> dict[str, tuple[str, str]]:
    """risk id -> (category, tag_version) as stored now. Ids are content hashes, so a
    risk whose text changed or that was removed by a re-extraction is simply absent."""
    stmt = select(risk_factors.c.id, risk_factors.c.category, risk_factors.c.tag_version).where(
        risk_factors.c.id.in_(ids)
    )
    async with engine.connect() as conn:
        return {
            r.id: (r.category or "", r.tag_version or "") for r in (await conn.execute(stmt)).all()
        }


@dataclass(frozen=True)
class ComparedScore:
    sheet_version: str
    current_version: str
    sheet: ReviewScore  # the exported (frozen) tags, on the rows that still exist
    current: ReviewScore  # today's stored tags, same rows
    gone: list[str]  # "TICKER FYyyyy: heading" of labelled rows no longer stored


async def score_sheet_and_current(
    engine: AsyncEngine, path: Path, sheet_version: str
) -> ComparedScore:
    rows, invalid = read_labels(path)
    now = await current_tags(engine, [r["risk_id"] for r in rows])
    kept = [r for r in rows if r["risk_id"] in now]
    gone = [
        f"{r['ticker']} FY{r['fiscal_year']}: {r['heading'][:80]}"
        for r in rows
        if r["risk_id"] not in now
    ]
    versions = sorted({v for _, v in now.values()})
    return ComparedScore(
        sheet_version,
        ", ".join(versions) or "none",
        score_labels(kept, {r["risk_id"]: r["model_category"] for r in kept}, invalid),
        score_labels(kept, {k: c for k, (c, _) in now.items()}),
        gone,
    )


def compared_markdown(c: ComparedScore) -> str:
    lines = [
        "## Risk-tag review score: exported vs current tags\n",
        "Same labelled risk factors, scored against both tag sets.\n",
        "| Tags | Accuracy vs owner labels |\n|---|---|",
        f"| exported ({c.sheet_version}) | {_pct(c.sheet)} |",
        f"| current ({c.current_version}) | {_pct(c.current)} |",
    ]
    if c.gone:
        lines.append(
            f"\nExcluded, risk text changed or removed since export ({len(c.gone)}):\n"
            + "\n".join(f"- {g}" for g in c.gone)
        )
    lines.append("\n### Current tags in detail\n")
    lines.append(score_markdown(c.current).split("\n", 2)[2])
    return "\n".join(lines)


def _pct(s: ReviewScore) -> str:
    return f"**{100 * s.accuracy:.0f}%** ({s.correct}/{s.labelled})"


def score_markdown(s: ReviewScore) -> str:
    lines = [
        "## Risk-tag review score\n",
        f"Model accuracy vs owner labels: **{100 * s.accuracy:.0f}% ({s.correct}/{s.labelled})**",
    ]
    if s.invalid_labels:
        lines.append(f"\nIgnored (not a taxonomy category): {', '.join(s.invalid_labels)}")
    if s.confusions:
        lines.append(
            "\nDisagreements (owner -> model): "
            + ", ".join(f"{a} -> {b} (x{n})" for (a, b), n in s.confusions)
        )
    lines.append("\n| Owner's category | Model correct |\n|---|---|")
    lines += [f"| {cat} | {ok}/{tot} |" for cat, (ok, tot) in s.per_category.items()]
    return "\n".join(lines)
