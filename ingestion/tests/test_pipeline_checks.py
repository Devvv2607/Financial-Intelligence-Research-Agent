"""The full / daily pipeline records when EDGAR and NewsAPI were checked (D-049).

`ingest-daily` is `run --daily` through `Pipeline`; a newly ingested company must not
show "never checked". The check time is the response's cache-file time, recorded only
when the stage that fetched it succeeded. No database, network or LLM: the stage bodies
and the job-table writes are stubbed, the cache files are real.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from core.config import Settings
from core.newsapi import NewsApiClient
from ingestion import pipeline as pl
from ingestion.edgar.client import EdgarClient
from ingestion.edgar.submissions import SUBMISSIONS_URL
from ingestion.pipeline import Pipeline, StageError, UniverseRow

CIK = 320193
APPLE = UniverseRow("AAPL", "Apple Inc.", "Information Technology")
SEC_TIME = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
NEWS_TIME = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)


def _touch(path: Path, when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))


@pytest.fixture
def run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Pipeline, list[tuple[int, str, datetime]], dict[str, str]]:
    edgar = EdgarClient("Test Person test@example.com", tmp_path)
    news = NewsApiClient("unused", "https://newsapi.invalid/v2", tmp_path)
    _touch(edgar.cache_path(SUBMISSIONS_URL.format(cik=CIK)), SEC_TIME)
    _touch(tmp_path / "newsapi" / "AAPL" / f"{datetime.now(UTC).date()}.json", NEWS_TIME)

    recorded: list[tuple[int, str, datetime]] = []
    job_status: dict[str, str] = {}  # stage -> status before the run

    async def record_check(_engine: Any, cik: int, source: str, when: datetime) -> None:
        recorded.append((cik, source, when))

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    async def status(_engine: Any, _cik: int, stage: str) -> str | None:
        return job_status.get(stage)

    monkeypatch.setattr(pl, "record_check", record_check)
    monkeypatch.setattr(pl.pg, "ensure_company", noop)
    monkeypatch.setattr(pl.pg, "set_job", noop)
    monkeypatch.setattr(pl.pg, "job_status", status)
    p = Pipeline(
        Settings(_env_file=None, app_env="test", groq_model_fast="test-model"),
        cast(Any, None),
        edgar,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        news,
        run_id="t",
    )
    p._ticker_map = {"AAPL": CIK}
    return p, recorded, job_status


async def test_successful_edgar_and_news_stages_record_their_check_times(
    run: tuple[Pipeline, list[tuple[int, str, datetime]], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, recorded, _ = run

    async def stage(*_a: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(p, "_run_stage", stage)
    out = await p._company(APPLE, ("edgar", "news"), force=False, refresh=False)
    assert out.stages == {"edgar": "done", "news": "done"}
    assert recorded == [(CIK, "sec_check", SEC_TIME), (CIK, "news_check", NEWS_TIME)]


async def test_no_check_is_claimed_for_skipped_or_failed_stages(
    run: tuple[Pipeline, list[tuple[int, str, datetime]], dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, recorded, job_status = run
    job_status["edgar"] = "done"  # done on an earlier day: nothing was fetched now

    async def stage(_stage: str, *_a: Any) -> dict[str, Any]:
        raise StageError("NewsAPI said no")

    monkeypatch.setattr(p, "_run_stage", stage)
    out = await p._company(APPLE, ("edgar", "news"), force=False, refresh=False)
    assert out.stages == {"edgar": "done", "news": "failed"}
    assert recorded == []
