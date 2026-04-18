from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI

from app.api.routes import router
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous = limiter.total_tokens
    limiter.total_tokens = max(previous, settings.sync_thread_pool_capacity)
    yield
    limiter.total_tokens = previous


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(router)
