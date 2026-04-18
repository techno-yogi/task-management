from __future__ import annotations

from typing import Any

from celery.signals import worker_process_init, worker_process_shutdown
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

_sync_engine: Engine | None = None
SyncSessionLocal: sessionmaker[Session] | None = None


def _build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    sync_engine_kwargs: dict[str, Any] = {
        "echo": settings.sql_echo,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

    if url.drivername.startswith("sqlite"):
        sync_engine_kwargs["connect_args"] = {
            "check_same_thread": False,
            "timeout": settings.sqlite_timeout_seconds,
        }
    else:
        sync_engine_kwargs["pool_size"] = settings.sync_db_pool_size
        sync_engine_kwargs["max_overflow"] = settings.sync_db_max_overflow
        sync_engine_kwargs["pool_timeout"] = settings.sync_db_pool_timeout

    engine = create_engine(database_url, **sync_engine_kwargs)

    if url.drivername.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # type: ignore[no-redef]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_timeout_seconds * 1000}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def _configure_session_factory(database_url: str) -> sessionmaker[Session]:
    global _sync_engine, SyncSessionLocal
    if _sync_engine is not None:
        _sync_engine.dispose(close=False)
    _sync_engine = _build_engine(database_url)
    SyncSessionLocal = sessionmaker(
        bind=_sync_engine,
        expire_on_commit=False,
        autoflush=False,
        class_=Session,
    )
    return SyncSessionLocal


def get_sync_session_factory() -> sessionmaker[Session]:
    global SyncSessionLocal
    if SyncSessionLocal is None:
        return _configure_session_factory(settings.sync_database_url)
    return SyncSessionLocal


def dispose_sync_engine() -> None:
    global _sync_engine, SyncSessionLocal
    if _sync_engine is not None:
        _sync_engine.dispose()
    _sync_engine = None
    SyncSessionLocal = None


@worker_process_init.connect
def _init_worker_db(**_: Any) -> None:
    _configure_session_factory(settings.sync_database_url)


@worker_process_shutdown.connect
def _shutdown_worker_db(**_: Any) -> None:
    dispose_sync_engine()


get_sync_session_factory()


def get_sync_engine() -> Engine:
    global _sync_engine
    if _sync_engine is None:
        _configure_session_factory(settings.sync_database_url)
    assert _sync_engine is not None
    return _sync_engine


def get_pool_snapshot() -> dict[str, int | str | None]:
    engine = get_sync_engine()
    pool = engine.pool
    snapshot: dict[str, int | str | None] = {
        "pool_class": pool.__class__.__name__,
        "checkedin": getattr(pool, "checkedin", lambda: None)(),
        "checkedout": getattr(pool, "checkedout", lambda: None)(),
        "overflow": getattr(pool, "overflow", lambda: None)(),
        "size": getattr(pool, "size", lambda: None)(),
        "database_url": str(engine.url).split("@")[-1],
    }
    return snapshot


def get_pg_stat_activity_snapshot(session: Session) -> list[dict[str, str | int | None]]:
    engine = session.get_bind()
    assert engine is not None
    if engine.dialect.name != "postgresql":
        return []
    rows = session.execute(text(
        """
        select application_name, state, wait_event_type, count(*) as connections
        from pg_stat_activity
        where datname = current_database()
        group by application_name, state, wait_event_type
        order by application_name nulls last, state nulls last
        """
    ))
    return [dict(row._mapping) for row in rows]
