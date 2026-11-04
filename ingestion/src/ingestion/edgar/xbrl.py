"""Annual financial facts from XBRL companyfacts (DECISIONS D-004, D-012).

Two traps handled here:
1. `fy` is the fiscal year of the *filing*, not the period. A FY2024 10-K also reports
   FY2023 and FY2022 comparatives, all tagged fy=2024. We key on the period instead:
   duration facts must span ~a year (350-380 days) and are identified by `end` date.
2. Companies use different concepts for the same metric (and switch between years), so
   each metric has a priority-ordered concept list, resolved per fiscal year.
Restatements: when the same period appears in several filings, the latest `filed` wins.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# metric -> (unit, kind, concepts in priority order)
CONCEPT_MAP: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "revenue": (
        "USD",
        "duration",
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "RevenuesNetOfInterestExpense",
        ),
    ),
    "net_income": ("USD", "duration", ("NetIncomeLoss", "ProfitLoss")),
    "operating_income": ("USD", "duration", ("OperatingIncomeLoss",)),
    "total_assets": ("USD", "instant", ("Assets",)),
    "total_liabilities": ("USD", "instant", ("Liabilities",)),
    "cash": (
        "USD",
        "instant",
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "Cash",
        ),
    ),
    "rd_expense": (
        "USD",
        "duration",
        (
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        ),
    ),
    "eps_diluted": ("USD/shares", "duration", ("EarningsPerShareDiluted",)),
}

ANNUAL_FORMS = {"10-K", "10-K/A"}
MIN_DAYS, MAX_DAYS = 350, 380  # covers 52/53-week fiscal years


@dataclass(frozen=True)
class FactRow:
    metric: str
    fiscal_year: int
    value: Decimal
    unit: str
    concept: str
    period_start: date | None
    period_end: date
    accession_no: str
    filed: date


def _annual_points(
    entries: list[dict[str, Any]], kind: str, fy_ends: set[date] | None
) -> dict[date, dict[str, Any]]:
    """Dedupe to one value per period end, keeping the most recently filed."""
    best: dict[date, dict[str, Any]] = {}
    for e in entries:
        if e.get("form") not in ANNUAL_FORMS:
            continue
        end = date.fromisoformat(e["end"])
        if kind == "duration":
            if "start" not in e:
                continue
            days = (end - date.fromisoformat(e["start"])).days
            if not MIN_DAYS <= days <= MAX_DAYS:
                continue
        elif fy_ends is not None and end not in fy_ends:
            # Instants only count on a fiscal-year-end date (not quarter-ends in a 10-K).
            continue
        if end not in best or e["filed"] > best[end]["filed"]:
            best[end] = e
    return best


def fiscal_year_ends(facts: dict[str, Any]) -> set[date]:
    """Period-end dates of annual duration facts: the company's fiscal-year ends."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    ends: set[date] = set()
    for metric in ("revenue", "net_income"):
        unit, _, concepts = CONCEPT_MAP[metric]
        for concept in concepts:
            entries = gaap.get(concept, {}).get("units", {}).get(unit, [])
            ends.update(_annual_points(entries, "duration", None))
    return ends


def extract_annual_facts(facts: dict[str, Any], years: int = 6) -> list[FactRow]:
    gaap = facts.get("facts", {}).get("us-gaap", {})
    fy_ends = fiscal_year_ends(facts)
    rows: list[FactRow] = []
    for metric, (unit, kind, concepts) in CONCEPT_MAP.items():
        # For each fiscal year, the first concept in priority order that has a value.
        chosen: dict[int, FactRow] = {}
        for concept in concepts:
            entries = gaap.get(concept, {}).get("units", {}).get(unit, [])
            for end, e in _annual_points(entries, kind, fy_ends).items():
                fiscal_year = end.year  # D-004: label by period end, not the filing's fy
                if fiscal_year in chosen:
                    continue
                chosen[fiscal_year] = FactRow(
                    metric=metric,
                    fiscal_year=fiscal_year,
                    value=Decimal(str(e["val"])),
                    unit=unit,
                    concept=concept,
                    period_start=date.fromisoformat(e["start"]) if "start" in e else None,
                    period_end=end,
                    accession_no=e["accn"],
                    filed=date.fromisoformat(e["filed"]),
                )
        latest = sorted(chosen, reverse=True)[:years]
        rows.extend(chosen[y] for y in latest)
    return rows


def metrics_found(rows: list[FactRow]) -> dict[int, set[str]]:
    by_year: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        by_year[r.fiscal_year].add(r.metric)
    return dict(by_year)
