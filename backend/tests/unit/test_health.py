"""Health/readiness endpoint behaviour with dependencies stubbed out.

The real end-to-end check (containers up -> /ready green) is `make ready` against
docker-compose; these tests pin down the endpoint's contract.
"""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from api.main import create_app
from core import db, vectorstore
from core.config import Settings


async def _ok(*_: object) -> None:
    return None


async def _true(*_: object) -> bool:
    return True


async def _boom(*_: object) -> None:
    raise ConnectionError("refused")


async def _hang(*_: object) -> None:
    await asyncio.sleep(10)


async def _count(*_: object) -> int:
    return 1568


async def _query_fails(*_: object) -> int:
    raise RuntimeError("FilingChunk: Weaviate returned no count")


async def test_startup_connects_weaviate_outside_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-053: the API connects its client at startup (it used to wait for /ready)."""
    calls: list[object] = []

    async def connect(client: object) -> None:
        calls.append(client)

    monkeypatch.setattr(vectorstore, "connect", connect)
    monkeypatch.setattr(db, "ping", _ok)
    await _get(_settings().model_copy(update={"app_env": "local"}), "/health")
    assert len(calls) == 1
    calls.clear()
    await _get(_settings(), "/health")  # APP_ENV=test: first query connects instead
    assert calls == []


def _settings(groq_key: str | None = "gsk_test") -> Settings:
    return Settings(
        _env_file=None,  # never let a developer's .env leak into tests
        app_env="test",
        groq_api_key=SecretStr(groq_key) if groq_key is not None else None,
        readiness_timeout_s=0.2,
    )


async def _get(settings: Settings, path: str) -> httpx.Response:
    """Run the app's real lifespan, then make one request."""
    app = create_app(settings, runtime_factory=None)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        return await client.get(path)


async def test_health_is_ok_without_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _boom)
    resp = await _get(_settings(), "/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_ready_when_all_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _ok)
    monkeypatch.setattr(vectorstore, "count_objects", _count)
    resp = await _get(_settings(), "/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert all(c["ok"] for c in body["checks"].values())


async def test_ready_503_when_postgres_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _boom)
    monkeypatch.setattr(vectorstore, "count_objects", _count)
    resp = await _get(_settings(), "/ready")
    assert resp.status_code == 503
    checks = resp.json()["checks"]
    assert checks["postgres"] == {"ok": False, "detail": "ConnectionError"}
    assert checks["weaviate"]["ok"] is True


async def test_ready_503_when_weaviate_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _ok)
    monkeypatch.setattr(vectorstore, "count_objects", _hang)
    resp = await _get(_settings(), "/ready")
    assert resp.status_code == 503
    assert "timed out" in resp.json()["checks"]["weaviate"]["detail"]


async def test_ready_503_when_the_weaviate_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connected is not enough: /ready runs a real count, and a failing query is not
    ready (the 2026-11-18 bug: liveness said ok while every query failed)."""
    monkeypatch.setattr(db, "ping", _ok)
    monkeypatch.setattr(vectorstore, "count_objects", _query_fails)
    resp = await _get(_settings(), "/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["weaviate"] == {"ok": False, "detail": "RuntimeError"}


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_ready_503_without_groq_key(monkeypatch: pytest.MonkeyPatch, key: str | None) -> None:
    monkeypatch.setattr(db, "ping", _ok)
    monkeypatch.setattr(vectorstore, "count_objects", _count)
    resp = await _get(_settings(groq_key=key), "/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["groq_key"] == {"ok": False, "detail": "GROQ_API_KEY is not set"}


def test_live_news_fallback_forced_off_in_production() -> None:
    assert Settings(
        _env_file=None, app_env="local", news_live_fallback=True
    ).news_live_fallback_enabled
    assert not Settings(
        _env_file=None, app_env="production", news_live_fallback=True
    ).news_live_fallback_enabled
