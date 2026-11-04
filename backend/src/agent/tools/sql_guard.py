"""Validation for model-written SQL (text-to-SQL screener), layer 1 of 2.

Layer 1 (here): parse with sqlglot and accept only a single read query over allowlisted
tables/columns, with a capped LIMIT. Layer 2 (database): the query runs as the
`fira_readonly` role (column-level SELECT grants only) inside a READ ONLY transaction with
a statement timeout, so even a validator bug can't write or read beyond the allowlist.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

MAX_ROWS = 100

# table -> {column: description}. The descriptions also feed the text-to-SQL prompt, so
# the model sees exactly what it is allowed to use. Long free text (filing bodies, risk
# factor bodies) is deliberately excluded: the screener answers structured questions.
ALLOWLIST: dict[str, dict[str, str]] = {
    "companies": {
        "cik": "SEC company id (join key)",
        "ticker": "stock ticker, e.g. 'NVDA'",
        "name": "registered company name",
        "sic": "SIC industry code",
        "sic_description": "SIC industry description",
        "sic_sector": "sector from SIC-code rules (GICS-style name)",
        "llm_sector": "sector from an LLM reading the business description (GICS-style name)",
        "llm_sub_industry": "LLM sub-industry, e.g. 'Semiconductors'",
        "sector_conflict": "true when sic_sector and llm_sector disagree",
        "fiscal_year_end": "fiscal year end as MMDD, e.g. '0926'",
    },
    "filings": {
        "accession_no": "SEC accession number (join key)",
        "cik": "company id",
        "form_type": "'10-K' or '8-K'",
        "filing_date": "date filed",
        "fiscal_year": "10-K only: fiscal year of the report",
        "url": "link to the filing on sec.gov",
    },
    "risk_factors": {
        "id": "risk factor id",
        "cik": "company id",
        "fiscal_year": "fiscal year of the 10-K it came from",
        "heading": "risk factor heading (one sentence)",
        "category": (
            "one of: regulatory, competition, supply_chain, cybersecurity, macroeconomic, "
            "geopolitical, litigation, technology, talent, financial_liquidity, climate_esg, "
            "customer_concentration, ip, other"
        ),
        "summary": "one-line summary",
        "needs_review": "true if the category could not be assigned reliably",
    },
    "eight_k_events": {
        "accession_no": "accession number",
        "cik": "company id",
        "filing_date": "date filed",
        "items": "array of 8-K item codes, e.g. {'2.02','9.01'}",
    },
    "financial_facts": {
        "cik": "company id",
        "metric": (
            "one of: revenue, net_income, operating_income, total_assets, total_liabilities, "
            "cash, rd_expense, eps_diluted"
        ),
        "fiscal_year": "fiscal year (year of the period end date)",
        "value": "numeric value in `unit`",
        "unit": "'USD' or 'USD/shares'",
        "period_end": "period end date",
    },
    "news_articles": {
        "id": "article id",
        "cik": "company id",
        "title": "headline",
        "source_name": "outlet",
        "published_at": "publication time",
        "url": "article link",
    },
}

_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Into, exp.Copy, exp.Set, exp.Use, exp.TruncateTable, exp.Grant,
)  # fmt: skip


class SQLRejected(ValueError):  # noqa: N818 - surfaced to the model as feedback
    pass


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str
    tables: tuple[str, ...]
    limit_applied: bool


def validate_sql(sql: str, max_rows: int = MAX_ROWS) -> ValidatedSQL:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except ParseError as exc:
        raise SQLRejected(f"could not parse SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise SQLRejected("exactly one SQL statement is allowed")
    root = statements[0]
    if not isinstance(root, exp.Query):
        raise SQLRejected(f"only SELECT queries are allowed (got {root.key.upper()})")
    forbidden = root.find(*_FORBIDDEN)
    if forbidden is not None:
        raise SQLRejected(f"{forbidden.key.upper()} is not allowed")

    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    tables: set[str] = set()
    for t in root.find_all(exp.Table):
        name = t.name.lower()
        if name in ctes:
            continue
        if t.db and t.db.lower() != "public":
            raise SQLRejected(f"schema {t.db!r} is not allowed")
        if name not in ALLOWLIST:
            raise SQLRejected(f"table {name!r} is not allowed; use: {', '.join(ALLOWLIST)}")
        tables.add(name)

    # Names the query defines itself: select-list aliases, CTE names and CTE column lists.
    defined = {a.alias.lower() for a in root.find_all(exp.Alias) if a.alias} | ctes
    for cte in root.find_all(exp.CTE):
        cte_alias = cte.args.get("alias")
        if cte_alias is not None:
            defined |= {c.name.lower() for c in cte_alias.columns}
    _check_columns(root, defined)

    for star in root.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise SQLRejected("SELECT * is not allowed; list the columns you need")
    for fn in root.find_all(exp.Anonymous):
        raise SQLRejected(f"function {fn.name}() is not allowed")

    limit_node = root.args.get("limit")
    current = _limit_value(limit_node)
    applied = current is None or current > max_rows
    if applied:
        root = root.limit(max_rows)
    return ValidatedSQL(root.sql(dialect="postgres"), tuple(sorted(tables)), applied)


def _resolve(scope: Scope, alias: str) -> exp.Table | Scope | None:
    """What an alias means in this scope, looking outwards for correlated references."""
    current: Scope | None = scope
    while current is not None:
        if alias in current.sources:
            source = current.sources[alias]
            return source if isinstance(source, exp.Table | Scope) else None
        current = current.parent
    return None


def _check_columns(root: exp.Query, defined: set[str]) -> None:
    """Every column must exist in the allowlist of the real table it comes from.

    Aliases are resolved per scope (sqlglot's scope analysis), because the same alias can
    name a table in a CTE and a CTE in the outer query. Columns of CTEs and subqueries are
    checked inside their own scope, where they are defined from real tables.
    """
    for scope in traverse_scope(root):
        real = [v for v in scope.sources.values() if isinstance(v, exp.Table)]
        derived = any(isinstance(v, Scope) for v in scope.sources.values())
        for col in scope.columns:
            if isinstance(col.this, exp.Star):
                continue  # t.* is rejected by the star check
            name, qual = col.name.lower(), col.table.lower()
            if qual:
                source = _resolve(scope, qual)
                if source is None:
                    raise SQLRejected(f"unknown table alias {qual!r}")
                if isinstance(source, exp.Table) and name not in ALLOWLIST[source.name.lower()]:
                    raise SQLRejected(f"column {qual}.{name} is not allowed")
            elif not (
                name in defined
                or any(name in ALLOWLIST[t.name.lower()] for t in real)
                or derived  # may come from a CTE/subquery, itself checked in its own scope
            ):
                raise SQLRejected(f"unknown or disallowed column {name!r}")


def _limit_value(node: exp.Expression | None) -> int | None:
    if node is None:
        return None
    value = node.args.get("expression")
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.this)
    return 10**9  # non-literal LIMIT: treat as unbounded so the cap applies


def schema_description() -> str:
    """Allowlisted schema as text for the text-to-SQL prompt (static -> cacheable)."""
    lines = []
    for table, cols in ALLOWLIST.items():
        lines.append(f"TABLE {table}")
        lines += [f"  - {c}: {d}" for c, d in cols.items()]
    return "\n".join(lines)
