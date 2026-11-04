"""screener_agent tool: text-to-SQL over a read-only, allowlisted view of Postgres.

The agent model writes SQL from the question; `sql_guard` validates it (layer 1); it then
runs as the `fira_readonly` role in a READ ONLY transaction with a statement timeout
(layer 2). A rejected or failing query is retried once with the error as feedback.
"""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from agent import prompts
from agent.tools.base import Evidence, Output, ToolContext, ToolFailure, tool
from agent.tools.sql_guard import SQLRejected, ValidatedSQL, schema_description, validate_sql
from core.llm import InvalidOutputError

READONLY_ROLE = "fira_readonly"
STATEMENT_TIMEOUT_MS = 5_000
MAX_ATTEMPTS = 2  # same cap as structured outputs (D-003): each retry spends quota


class QueryDatabaseInput(BaseModel):
    question: str = Field(
        min_length=5,
        description=(
            "A cross-company question answerable from structured data: sectors, risk "
            "categories, financial metrics, 8-K item counts, news counts"
        ),
    )


class SQLDraft(BaseModel):
    sql: str
    explanation: str


class QueryDatabaseOutput(BaseModel):
    question: str
    sql: str
    explanation: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    limit_applied: bool
    attempts: int


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


Executor = Callable[[str], Awaitable[QueryResult]]


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


async def run_readonly(engine: AsyncEngine, sql: str) -> QueryResult:
    """Run validated SQL under the read-only role. Postgres enforces column grants,
    READ ONLY and the timeout even if the validator missed something."""
    async with engine.connect() as conn, conn.begin() as tx:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        await conn.execute(text(f"SET LOCAL ROLE {READONLY_ROLE}"))
        await conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
        result = await conn.execute(text(sql))
        columns = list(result.keys())
        rows = [[_jsonable(v) for v in row] for row in result.fetchall()]
        await tx.rollback()  # nothing to commit; never leave the role set
    return QueryResult(columns=columns, rows=rows)


async def text_to_sql(
    ctx: ToolContext, question: str, execute: Executor
) -> tuple[SQLDraft, ValidatedSQL, QueryResult, int]:
    if ctx.llm is None:
        raise ToolFailure("internal", "no LLM configured for text-to-SQL")
    system = prompts.load("text_to_sql") + "\n" + schema_description()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            draft = await ctx.llm.structured("reasoning", SQLDraft, messages, stage="text_to_sql")
        except InvalidOutputError as exc:
            raise ToolFailure("upstream", f"model did not return a usable query: {exc}") from exc
        try:
            checked = validate_sql(draft.sql)
            result = await execute(checked.sql)
            return draft, checked, result, attempt
        except SQLRejected as exc:
            last_error = f"rejected: {exc}"
        except DBAPIError as exc:
            last_error = f"database error: {str(exc.orig).splitlines()[0]}"
        messages = [
            *messages[:2],
            {"role": "assistant", "content": draft.sql},
            {"role": "user", "content": f"That query failed ({last_error}). Fix it."},
        ]
    raise ToolFailure(
        "rejected",
        f"no valid query after {MAX_ATTEMPTS} attempts; {last_error}; last SQL: {draft.sql[:1500]}",
    )


@tool("query_database", QueryDatabaseInput, timeout_s=60)
async def query_database(ctx: ToolContext, inp: QueryDatabaseInput) -> Output:
    """Answer cross-company questions with SQL over structured data, e.g. 'Which
    semiconductor companies flag supply chain risks and grew revenue more than 20%?'.
    Read-only, limited to companies, filings, risk factor tags, 8-K item codes, XBRL
    financial facts and news headlines. Returns the SQL used and the rows."""

    async def execute(sql: str) -> QueryResult:
        return await run_readonly(ctx.engine, sql)

    draft, checked, result, attempts = await text_to_sql(ctx, inp.question, execute)
    out = QueryDatabaseOutput(
        question=inp.question,
        sql=checked.sql,
        explanation=draft.explanation,
        columns=result.columns,
        rows=result.rows,
        row_count=len(result.rows),
        limit_applied=checked.limit_applied,
        attempts=attempts,
    )
    preview = "\n".join(
        " | ".join(str(v) for v in row) for row in [result.columns, *result.rows[:10]]
    )
    evidence = [
        Evidence(
            source_id=f"sql:{hashlib.sha256(checked.sql.encode()).hexdigest()[:12]}",
            source_type="sql",
            title=f"Database query: {inp.question}"[:300],
            snippet=f"{checked.sql}\n{preview}"[:1500],
        )
    ]
    warnings = [] if result.rows else ["the query returned no rows"]
    return Output(out, evidence, warnings)
