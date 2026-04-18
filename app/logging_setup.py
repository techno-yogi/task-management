from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from typing import Any

# --- Correlation context ----------------------------------------------------
# These ContextVars are populated by:
#   * the FastAPI middleware in app/main.py (request_id per HTTP request)
#   * the Celery `task_prerun` signal in app/celery_app.py (celery_task_id +
#     a sweep_id/chunk_id/job_id when known from task args/kwargs)
# Anything logged inside these scopes automatically picks up the values via
# `_CorrelationFilter` so log lines from one workflow are trivially grep-able.

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
celery_task_id_ctx: ContextVar[str | None] = ContextVar("celery_task_id", default=None)
sweep_id_ctx: ContextVar[int | None] = ContextVar("sweep_id", default=None)
chunk_id_ctx: ContextVar[int | None] = ContextVar("chunk_id", default=None)
job_id_ctx: ContextVar[int | None] = ContextVar("job_id", default=None)
task_variant_id_ctx: ContextVar[int | None] = ContextVar("task_variant_id", default=None)


_CORRELATION_KEYS = (
    ("request_id", request_id_ctx),
    ("celery_task_id", celery_task_id_ctx),
    ("sweep_id", sweep_id_ctx),
    ("chunk_id", chunk_id_ctx),
    ("job_id", job_id_ctx),
    ("task_variant_id", task_variant_id_ctx),
)


class _CorrelationFilter(logging.Filter):
    """Attach correlation ids from contextvars onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, var in _CORRELATION_KEYS:
            value = var.get()
            if value is not None:
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter with correlation ids and exc_info inline.

    Output is one JSON object per line — friendly to fluent-bit / loki / etc.
    Falls back to the standard `logging.Formatter` if the record happens to
    contain non-serializable extras.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, _ in _CORRELATION_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str)
        except Exception:
            return super().format(record)


def configure_logging(*, json_logs: bool | None = None, level: str | None = None) -> None:
    """Idempotently configure the root logger.

    `json_logs` defaults to env `APP_JSON_LOGS` (truthy => enabled). `level`
    defaults to `APP_LOG_LEVEL` (e.g. "INFO", "WARNING"). Plain-text mode is
    the default so local dev output stays readable; JSON mode is intended for
    container deployments where logs are shipped to a structured backend.
    """
    if json_logs is None:
        json_logs = os.environ.get("APP_JSON_LOGS", "").lower() in ("1", "true", "yes")
    if level is None:
        level = os.environ.get("APP_LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_CorrelationFilter())
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s "
                "[req=%(request_id)s sweep=%(sweep_id)s ctid=%(celery_task_id)s] "
                "%(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        # The default formatter dies if a referenced field is missing; the filter
        # only sets attrs when not None, so add empty defaults for every key.
        class _DefaultsFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                for key, _ in _CORRELATION_KEYS:
                    if not hasattr(record, key):
                        setattr(record, key, "-")
                return True

        handler.addFilter(_DefaultsFilter())

    root = logging.getLogger()
    # Replace existing stream handlers; otherwise repeated configure_logging()
    # in tests stacks up duplicate output.
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler):
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)
