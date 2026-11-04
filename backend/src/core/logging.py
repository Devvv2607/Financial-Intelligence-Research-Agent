"""Structured JSON logging with request-scoped context (run_id, thread_id).

Context lives in ContextVars so it follows async tasks automatically: set it once
at the start of a request/run and every log line underneath carries it.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
thread_id_var: ContextVar[str | None] = ContextVar("thread_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": run_id_var.get(),
            "thread_id": thread_id_var.get(),
        }
        # Anything passed via `extra=` becomes a top-level field.
        payload.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
