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


def _settings(groq_key: str | None = "gsk_test") -> Settings:
    return Settings(
        _env_file=None,  # never let a developer's .env leak into tests
        app_env="test",
        groq_api_key=SecretStr(groq_key) if groq_key is not None else None,
        readiness_timeout_s=0.2,
    )


async def _get(settings: Settings, path: str) -> httpx.Response:
    """Run the app's real lifespan, then make one request."""
    app = create_app(settings)
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
    monkeypatch.setattr(vectorstore, "ensure_ready", _true)
    resp = await _get(_settings(), "/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert all(c["ok"] for c in body["checks"].values())


async def test_ready_503_when_postgres_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _boom)
    monkeypatch.setattr(vectorstore, "ensure_ready", _true)
    resp = await _get(_settings(), "/ready")
    assert resp.status_code == 503
    checks = resp.json()["checks"]
    assert checks["postgres"] == {"ok": False, "detail": "ConnectionError"}
    assert checks["weaviate"]["ok"] is True


async def test_ready_503_when_weaviate_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _ok)
    monkeypatch.setattr(vectorstore, "ensure_ready", _hang)
    resp = await _get(_settings(), "/ready")
    assert resp.status_code == 503
    assert "timed out" in resp.json()["checks"]["weaviate"]["detail"]


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_ready_503_without_groq_key(monkeypatch: pytest.MonkeyPatch, key: str | None) -> None:
    monkeypatch.setattr(db, "ping", _ok)
    monkeypatch.setattr(vectorstore, "ensure_ready", _true)
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
