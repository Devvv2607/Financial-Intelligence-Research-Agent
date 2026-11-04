"""Liveness (`/health`) and readiness (`/ready`) probes.

/health  — the process is up. Never touches dependencies, so orchestrators don't
           restart a healthy API just because Postgres blipped.
/ready   — every dependency the agent needs is usable. Returns 503 otherwise, with
           a per-check breakdown so the failure is obvious.
"""

import asyncio
import logging
from collections.abc import Awaitable
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from core import db, vectorstore
from core.config import Settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class CheckResult(BaseModel):
    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


async def _run_check(name: str, check: Awaitable[object], timeout_s: float) -> CheckResult:
    try:
        result = await asyncio.wait_for(check, timeout=timeout_s)
    except TimeoutError:
        return CheckResult(ok=False, detail=f"timed out after {timeout_s}s")
    except Exception as exc:  # any failure = not ready; report it, don't raise
        logger.warning("readiness check failed", extra={"check": name, "error": repr(exc)})
        return CheckResult(ok=False, detail=type(exc).__name__)
    if result is False:
        return CheckResult(ok=False, detail="reported not ready")
    return CheckResult(ok=True)


def _groq_key_check(settings: Settings) -> CheckResult:
    # Presence only: /ready is polled often and must not spend Groq quota.
    if settings.groq_api_key and settings.groq_api_key.get_secret_value().strip():
        return CheckResult(ok=True)
    return CheckResult(ok=False, detail="GROQ_API_KEY is not set")


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request, response: Response) -> ReadyResponse:
    state = request.app.state
    settings: Settings = state.settings
    timeout = settings.readiness_timeout_s

    postgres, weaviate_ = await asyncio.gather(
        _run_check("postgres", db.ping(state.engine), timeout),
        _run_check("weaviate", vectorstore.ensure_ready(state.weaviate), timeout),
    )
    checks = {"postgres": postgres, "weaviate": weaviate_, "groq_key": _groq_key_check(settings)}

    all_ok = all(c.ok for c in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(status="ready" if all_ok else "not_ready", checks=checks)
