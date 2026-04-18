from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import router
from app.config import settings
from app.logging_setup import configure_logging, request_id_ctx


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = max(previous, settings.sync_thread_pool_capacity)
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
