from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import router
from app.celery_app import celery_app
from app.config import settings
from app.logging_setup import configure_logging, request_id_ctx


configure_logging()
log = logging.getLogger("app.main")


def _prewarm_broker_pool(target: int) -> int:
    """Open `target` Kombu producer connections at startup.

    Without prewarming, the first wave of concurrent POST /launch calls all
    pay the connection-establishment cost (TCP+TLS+AUTH) before they can
    publish. On a cold Redis that's seconds of head-of-line blocking. We
    pre-acquire and immediately release `target` connections so the pool is
    populated for the first real request.

    Returns the number of connections actually warmed (clamped to pool limit).
    """
    pool = celery_app.pool
    limit = getattr(pool, "limit", None) or 0
    n = min(target, limit) if limit else target
    held = []
    try:
        for _ in range(n):
            held.append(pool.acquire(block=False))
    except Exception as exc:
        # Don't crash startup if the broker is briefly unavailable; the app
        # will use lazy connection creation as a fallback.
        log.warning("broker prewarm partial: %s (warmed=%d)", exc, len(held))
    finally:
        for conn in held:
            try:
                conn.release()
            except Exception:
                pass
    return len(held)


@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = max(previous, settings.sync_thread_pool_capacity)

    # Best-effort: prewarm broker connections so the first burst of launches
    # doesn't pay TCP+TLS+AUTH per concurrent caller.
    if settings.broker_prewarm_count > 0:
        warmed = _prewarm_broker_pool(settings.broker_prewarm_count)
        log.info("broker pool prewarmed: %d connections", warmed)

    yield
    limiter.total_tokens = previous


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject a per-request correlation id.

    Honors an inbound `X-Request-ID` header so callers can stitch traces; if
    absent we mint a fresh UUID. The id is exposed back as `X-Request-ID` on
    the response and pushed into a contextvar so all logs emitted inside the
    request handler tag the value automatically.
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(RequestIdMiddleware)
app.include_router(router)
