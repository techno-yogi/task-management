from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings


def _async_engine_url_and_connect_args(database_url: str) -> tuple[Any, dict[str, Any]]:
    """asyncpg rejects libpq-style sslmode/sslrootcert query params; normalize URL and SSL connect_args."""
    url = make_url(database_url)
    connect_args: dict[str, Any] = {}

    if url.drivername.startswith("postgresql+asyncpg"):
        q = dict(url.query)
        q.pop("sslmode", None)
        q.pop("sslrootcert", None)
        ssl_param = q.pop("ssl", None)
        url = url.set(query=q)
        ca = settings.async_pg_ssl_ca_file
        if ca and Path(ca).is_file():
            ctx = ssl.create_default_context(cafile=ca)
            connect_args["ssl"] = ctx
        elif ssl_param in ("require", "verify-ca", "verify-full", "prefer", "allow", "true", "1"):
            connect_args["ssl"] = True
        elif ssl_param in ("disable", "false", "0"):
            connect_args["ssl"] = False

    return url, connect_args


url = make_url(settings.async_database_url)
async_engine_kwargs: dict[str, Any] = {
    "echo": settings.sql_echo,
    "pool_pre_ping": True,
}

if url.drivername.startswith("sqlite+"):
    async_engine_kwargs["connect_args"] = {"timeout": settings.sqlite_timeout_seconds}
    engine_url: Any = settings.async_database_url
else:
    engine_url, ssl_connect_args = _async_engine_url_and_connect_args(settings.async_database_url)
    async_engine_kwargs["pool_size"] = settings.async_db_pool_size
    async_engine_kwargs["max_overflow"] = settings.async_db_max_overflow
    async_engine_kwargs["pool_timeout"] = settings.async_db_pool_timeout
    async_engine_kwargs["pool_recycle"] = 1800
    if ssl_connect_args:
        async_engine_kwargs["connect_args"] = ssl_connect_args

async_engine: AsyncEngine = create_async_engine(engine_url, **async_engine_kwargs)

if url.drivername.startswith("sqlite+"):
    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_timeout_seconds * 1000}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
