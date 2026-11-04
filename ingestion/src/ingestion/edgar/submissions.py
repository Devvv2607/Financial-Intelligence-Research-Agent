"""Company metadata and filing lists from data.sec.gov submissions JSON."""

from dataclasses import dataclass
from datetime import date
from typing import Any

from ingestion.edgar.client import EdgarClient

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"


@dataclass(frozen=True)
class FilingRef:
    accession_no: str
    form_type: str
    filing_date: date
    report_date: date | None
    primary_doc: str
    items: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class CompanyMeta:
    cik: int
    name: str
    sic: int | None
    sic_description: str | None
    exchanges: tuple[str, ...]
    fiscal_year_end: str | None


async def load_ticker_map(client: EdgarClient, *, refresh: bool = False) -> dict[str, int]:
    data = await client.get_json(TICKERS_URL, refresh=refresh)
    return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def _rows(block: dict[str, Any], cik: int) -> list[FilingRef]:
    out = []
    for i, acc in enumerate(block["accessionNumber"]):
        doc = block["primaryDocument"][i]
        if not doc:
            continue
        items_raw = block.get("items", [""] * len(block["accessionNumber"]))[i] or ""
        filing_date = _parse_date(block["filingDate"][i])
        if filing_date is None:
            continue
        out.append(
            FilingRef(
                accession_no=acc,
                form_type=block["form"][i],
                filing_date=filing_date,
                report_date=_parse_date(block.get("reportDate", [""] * len(block["form"]))[i]),
                primary_doc=doc,
                items=tuple(x.strip() for x in items_raw.split(",") if x.strip()),
                url=ARCHIVE_URL.format(cik=cik, acc=acc.replace("-", ""), doc=doc),
            )
        )
    return out


def parse_company(data: dict[str, Any]) -> CompanyMeta:
    sic = data.get("sic")
    return CompanyMeta(
        cik=int(data["cik"]),
        name=data["name"],
        sic=int(sic) if sic else None,
        sic_description=data.get("sicDescription") or None,
        exchanges=tuple(e for e in data.get("exchanges", []) if e),
        fiscal_year_end=data.get("fiscalYearEnd") or None,
    )


async def load_filings(
    client: EdgarClient,
    cik: int,
    *,
    need_10k: int = 2,
    since_8k: date,
    refresh: bool = False,
) -> tuple[CompanyMeta, list[FilingRef]]:
    """Return company metadata plus every filing we need.

    `recent` holds roughly the last year / 1000 filings. Large banks file thousands of
    prospectus supplements a year, which can push the prior 10-K out of `recent`, so we
    page through `files` until we have `need_10k` 10-Ks.
    """
    data = await client.get_json(SUBMISSIONS_URL.format(cik=cik), refresh=refresh)
    meta = parse_company(data)
    refs = _rows(data["filings"]["recent"], cik)
    for page in data["filings"].get("files", []):
        if sum(r.form_type == "10-K" for r in refs) >= need_10k:
            break
        older = await client.get_json(SUBMISSIONS_PAGE_URL.format(name=page["name"]))
        refs.extend(_rows(older, cik))

    ten_ks = sorted(
        (r for r in refs if r.form_type == "10-K"), key=lambda r: r.filing_date, reverse=True
    )[:need_10k]
    eight_ks = [r for r in refs if r.form_type == "8-K" and r.filing_date >= since_8k]
    return meta, ten_ks + sorted(eight_ks, key=lambda r: r.filing_date, reverse=True)
