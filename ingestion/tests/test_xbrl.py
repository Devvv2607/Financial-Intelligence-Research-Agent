"""XBRL annual facts: period-based dedupe (D-004) and concept mapping (D-012).

The fixture reproduces the traps seen in real companyfacts JSON: comparatives tagged
with the filing's `fy`, a restatement, quarterly values inside a 10-K, a concept switch
between years, and quarter-end instants.
"""

from decimal import Decimal
from typing import Any

from ingestion.edgar.xbrl import extract_annual_facts, fiscal_year_ends


def _d(
    start: str | None,
    end: str,
    val: float,
    fy: int,
    filed: str,
    accn: str,
    form: str = "10-K",
    fp: str = "FY",
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "end": end,
        "val": val,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
        "accn": accn,
    }
    if start:
        e["start"] = start
    return e


FACTS = {
    "facts": {
        "us-gaap": {
            # FY2023 filing reports FY23 + FY22; FY2024 filing reports FY24 + FY23 (restated)
            # + FY22. All comparatives carry the *filing's* fy.
            "Revenues": {
                "units": {
                    "USD": [
                        _d("2021-10-01", "2022-09-30", 100, 2023, "2023-11-01", "A23"),
                        _d("2022-10-01", "2023-09-30", 120, 2023, "2023-11-01", "A23"),
                        _d("2022-10-01", "2023-09-30", 118, 2024, "2024-11-01", "A24"),  # restated
                        _d("2023-10-01", "2024-09-30", 150, 2024, "2024-11-01", "A24"),
                        _d(
                            "2023-07-01", "2023-09-30", 40, 2024, "2024-11-01", "A24"
                        ),  # Q4 in a 10-K
                        _d(
                            "2024-01-01",
                            "2024-03-31",
                            35,
                            2024,
                            "2024-05-01",
                            "Q24",
                            form="10-Q",
                            fp="Q2",
                        ),
                    ]
                }
            },
            # A company that switched concepts: the older concept only covers FY21.
            "SalesRevenueNet": {
                "units": {
                    "USD": [
                        _d("2020-10-01", "2021-09-30", 90, 2021, "2021-11-01", "A21"),
                    ]
                }
            },
            "Assets": {
                "units": {
                    "USD": [
                        _d(None, "2023-09-30", 500, 2023, "2023-11-01", "A23"),
                        _d(
                            None, "2024-06-30", 520, 2024, "2024-08-01", "Q24", form="10-Q", fp="Q3"
                        ),
                        _d(
                            None, "2024-03-31", 515, 2024, "2024-11-01", "A24"
                        ),  # quarter-end in a 10-K
                        _d(None, "2024-09-30", 530, 2024, "2024-11-01", "A24"),
                    ]
                }
            },
            "EarningsPerShareDiluted": {
                "units": {
                    "USD/shares": [
                        _d("2023-10-01", "2024-09-30", 2.5, 2024, "2024-11-01", "A24"),
                    ]
                }
            },
        }
    }
}


def _by(metric: str) -> dict[int, Any]:
    return {r.fiscal_year: r for r in extract_annual_facts(FACTS) if r.metric == metric}


def test_fiscal_year_ends_come_from_annual_duration_periods() -> None:
    ends = {d.isoformat() for d in fiscal_year_ends(FACTS)}
    assert ends == {"2021-09-30", "2022-09-30", "2023-09-30", "2024-09-30"}


def test_one_value_per_period_labelled_by_period_end_not_filing_fy() -> None:
    rev = _by("revenue")
    # Naively filtering fy == 2024 would return 118, 150 *and* 100 (and the Q4 40).
    assert rev[2024].value == Decimal("150")
    assert rev[2022].value == Decimal("100")


def test_restatement_latest_filing_wins() -> None:
    rev = _by("revenue")
    assert rev[2023].value == Decimal("118")
    assert rev[2023].accession_no == "A24"


def test_quarterly_values_and_10q_are_ignored() -> None:
    assert Decimal("40") not in {r.value for r in _by("revenue").values()}
    assert Decimal("35") not in {r.value for r in _by("revenue").values()}


def test_concept_priority_falls_back_per_year() -> None:
    rev = _by("revenue")
    assert rev[2024].concept == "Revenues"
    assert rev[2021].concept == "SalesRevenueNet"
    assert rev[2021].value == Decimal("90")


def test_instants_only_on_fiscal_year_end() -> None:
    assets = _by("total_assets")
    assert {y: r.value for y, r in assets.items()} == {2023: Decimal("500"), 2024: Decimal("530")}
    assert assets[2024].period_start is None


def test_units_and_missing_metrics() -> None:
    eps = _by("eps_diluted")
    assert eps[2024].unit == "USD/shares"
    assert _by("rd_expense") == {}
