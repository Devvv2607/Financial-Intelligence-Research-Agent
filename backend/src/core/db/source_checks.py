"""When each company's sources were last checked upstream (DECISIONS D-049).

Freshness is about when we last *asked* EDGAR / NewsAPI, not about the newest item: a
company that filed its last 8-K five days ago is fully up to date if EDGAR was checked
today. The check times live in `ingestion_jobs` (no schema change) under two extra
stages, with `updated_at` = the time of the last successful fetch. Writers pass the
response's cache-file time, so a run that reuses a cached response doesn't claim a newer
check than the one that really happened.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.db.schema import ingestion_jobs

SourceCheck = Literal["sec_check", "news_check"]


def file_time(path: Path) -> datetime | None:
    """A cache file's modification time (= when its response arrived), or None."""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC) if path.exists() else None


async def record_check(
    engine: AsyncEngine, cik: int, source: SourceCheck, checked_at: datetime
) -> None:
    """Upsert the check time; never moves it backwards."""
    stmt = insert(ingestion_jobs).values(
        cik=cik,
        stage=source,
        status="done",
        details={"checked_at": checked_at.isoformat()},
        updated_at=checked_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["cik", "stage"],
        set_={
            "status": "done",
            "details": stmt.excluded.details,
            "updated_at": stmt.excluded.updated_at,
        },
        where=ingestion_jobs.c.updated_at < stmt.excluded.updated_at,
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)


async def last_checks(
    conn: AsyncConnection, ciks: list[int], source: SourceCheck
) -> dict[int, datetime]:
    rows = await conn.execute(
        select(ingestion_jobs.c.cik, func.max(ingestion_jobs.c.updated_at))
        .where(
            ingestion_jobs.c.cik.in_(ciks),
            ingestion_jobs.c.stage == source,
            ingestion_jobs.c.status == "done",
        )
        .group_by(ingestion_jobs.c.cik)
    )
    return {cik: ts for cik, ts in rows if ts is not None}
