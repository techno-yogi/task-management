from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_DIR = Path(__file__).resolve().parent / ".tmp"
DB_DIR.mkdir(exist_ok=True)
DEFAULT_DB_PATH = DB_DIR / "test_app.db"

os.environ.setdefault("APP_ASYNC_DATABASE_URL", f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}")
os.environ.setdefault("APP_SYNC_DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")
os.environ.setdefault("APP_CELERY_BROKER_URL", "memory://")
os.environ.setdefault("APP_CELERY_RESULT_BACKEND", "cache+memory://")

from app.api.routes import router  # noqa: E402,F401
from app.celery_app import celery_app  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.async_db import get_async_session  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def sync_engine_fixture():
    if DEFAULT_DB_PATH.exists():
        DEFAULT_DB_PATH.unlink()
    engine = create_engine(
        os.environ["APP_SYNC_DATABASE_URL"],
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
async def async_engine_fixture(sync_engine_fixture):
    engine = create_async_engine(os.environ["APP_ASYNC_DATABASE_URL"], future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture()
def sync_session_factory(sync_engine_fixture):
    return sessionmaker(bind=sync_engine_fixture, expire_on_commit=False, autoflush=False)


@pytest.fixture()
async def async_session_factory(async_engine_fixture):
    return async_sessionmaker(async_engine_fixture, expire_on_commit=False, autoflush=False)


@pytest.fixture(autouse=True)
def truncate_tables(sync_session_factory):
    from app.models import Chunk, Job, Sweep, TaskVariant

    with sync_session_factory() as session:
        session.query(TaskVariant).delete()
        session.query(Job).delete()
        session.query(Chunk).delete()
        session.query(Sweep).delete()
        session.commit()
    yield


@pytest.fixture()
def client(async_session_factory):
    async def override_get_async_session() -> AsyncGenerator[AsyncSession, None]:
        async with async_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override_get_async_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def configure_celery_for_tests(request, sync_session_factory):
    is_live = request.node.get_closest_marker("live_worker") is not None

    import app.db.sync_db as sync_db
    import app.tasks as tasks_module

    sync_db.SyncSessionLocal = sync_session_factory
    tasks_module.SyncSessionLocal = sync_session_factory

    if is_live:
        yield
        return

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        celery_app.conf.task_always_eager = False


@pytest.fixture()
def eager_celery():
    yield

@pytest.fixture()
def make_payload():
    def _make_payload(*, chunks: int, jobs_per_chunk: int, tasks_per_job: int, sleep_ms: int = 0, base_seed: int = 100) -> dict:
        payload = {"name": f"sweep-{chunks}-{jobs_per_chunk}-{tasks_per_job}-{base_seed}", "chunks": []}
        base = base_seed
        for c in range(chunks):
            chunk = {"ordinal": c + 1, "jobs": []}
            for j in range(jobs_per_chunk):
                shared_context = {"base_value": base, "step": 3, "offset": 0, "sleep_ms": sleep_ms}
                job = {"name": f"job-{c+1}-{j+1}", "shared_context": shared_context, "tasks": []}
                for t in range(tasks_per_job):
                    expected = base + t * 3
                    job["tasks"].append({"point_idx": t, "name": f"t{t}", "expected_value": expected})
                chunk["jobs"].append(job)
                base += 50
            payload["chunks"].append(chunk)
        return payload

    return _make_payload




@pytest.fixture(autouse=True)
def require_opt_in_for_live_workers(request):
    if request.node.get_closest_marker("live_worker") is not None and os.environ.get("RUN_LIVE_WORKERS") != "1":
        pytest.skip("Set RUN_LIVE_WORKERS=1 to run real multi-worker Celery integration tests.")

@pytest.fixture()
def live_multiworker_harness(tmp_path) -> Iterator[dict]:
    base_dir = tmp_path / "live"
    broker_in = base_dir / "broker" / "in"
    broker_out = base_dir / "broker" / "out"
    broker_processed = base_dir / "broker" / "processed"
    for p in (broker_in, broker_out, broker_processed):
        p.mkdir(parents=True, exist_ok=True)

    db_path = base_dir / "live_app.db"
    results_path = base_dir / "live_results.db"

    async_db_url = f"sqlite+aiosqlite:///{db_path}"
    sync_db_url = f"sqlite:///{db_path}"
    broker_url = "filesystem://"
    result_backend = f"db+sqlite:///{results_path}"

    sync_engine = create_engine(
        sync_db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    live_sync_session_factory = sessionmaker(bind=sync_engine, expire_on_commit=False, autoflush=False)

    async_engine = create_async_engine(async_db_url, future=True)
    live_async_session_factory = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)

    async def override_get_async_session() -> AsyncGenerator[AsyncSession, None]:
        async with live_async_session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override_get_async_session

    import app.db.sync_db as sync_db

    sync_db.SyncSessionLocal = live_sync_session_factory
    import app.tasks as tasks_module

    tasks_module.SyncSessionLocal = live_sync_session_factory

    celery_app.conf.update(
        broker_url=broker_url,
        result_backend=result_backend,
        broker_transport_options={
            "data_folder_in": str(broker_in),
            "data_folder_out": str(broker_out),
            "data_folder_processed": str(broker_processed),
            "store_processed": True,
        },
        task_always_eager=False,
        task_eager_propagates=True,
        task_track_started=True,
        worker_prefetch_multiplier=1,
        task_acks_late=True,
    )

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "APP_ASYNC_DATABASE_URL": async_db_url,
            "APP_SYNC_DATABASE_URL": sync_db_url,
            "APP_CELERY_BROKER_URL": broker_url,
            "APP_CELERY_RESULT_BACKEND": result_backend,
            # filesystem transport needs producer/consumer directories reversed across processes
            "APP_CELERY_FS_IN_DIR": str(broker_out),
            "APP_CELERY_FS_OUT_DIR": str(broker_in),
            "APP_CELERY_FS_PROCESSED_DIR": str(broker_processed),
        }
    )

    worker_specs = [
        ("exec1@%h", settings.celery_queue_execution),
        ("exec2@%h", settings.celery_queue_execution),
        (
            "aggregate@%h",
            f"{settings.celery_queue_job_finalize},{settings.celery_queue_chunk_finalize}",
        ),
        ("sweepfinal@%h", settings.celery_queue_sweep_finalize),
    ]

    workers: list[subprocess.Popen[str]] = []
    log_handles = []
    log_files: list[Path] = []
    try:
        for hostname, queues in worker_specs:
            cmd = [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.celery_app:celery_app",
                "worker",
                "--pool=threads",
                "--concurrency=4",
                "--without-heartbeat",
                "--without-gossip",
                "--without-mingle",
                f"--hostname={hostname}",
                f"--queues={queues}",
                "--loglevel=WARNING",
            ]
            safe_name = hostname.split("@")[0]
            log_path = base_dir / f"{safe_name}.log"
            log_files.append(log_path)
            log_handle = open(log_path, "w")
            log_handles.append(log_handle)
            workers.append(
                subprocess.Popen(
                    cmd,
                    cwd=ROOT,
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        time.sleep(6)

        from app.tasks import (
            identify_chunk_finalize_worker,
            identify_execution_worker,
            identify_job_finalize_worker,
            identify_sweep_finalize_worker,
        )

        def gather_workers(task_func, attempts: int = 4) -> set[str]:
            observed: set[str] = set()
            for _ in range(attempts):
                try:
                    result = task_func.delay().get(timeout=20)
                    if result.get("worker"):
                        observed.add(result["worker"])
                except Exception:
                    time.sleep(1)
            return observed

        exec_workers = gather_workers(identify_execution_worker, attempts=6)
        job_finalize_workers = gather_workers(identify_job_finalize_worker)
        chunk_finalize_workers = gather_workers(identify_chunk_finalize_worker)
        sweep_finalize_workers = gather_workers(identify_sweep_finalize_worker)

        if not exec_workers or not job_finalize_workers or not chunk_finalize_workers or not sweep_finalize_workers:
            combined_logs = "\n".join(lp.read_text() for lp in log_files if lp.exists())
            raise RuntimeError(f"Celery workers failed to start or consume the expected queues. Logs:\n{combined_logs}")

        with TestClient(app) as test_client:
            yield {
                "client": test_client,
                "sync_session_factory": live_sync_session_factory,
                "async_engine": async_engine,
                "db_path": db_path,
                "result_path": results_path,
                "exec_workers": exec_workers,
                "job_finalize_workers": job_finalize_workers,
                "chunk_finalize_workers": chunk_finalize_workers,
                "sweep_finalize_workers": sweep_finalize_workers,
            }
    finally:
        app.dependency_overrides.clear()
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
        for worker in workers:
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
        for handle in log_handles:
            handle.close()
        sync_engine.dispose()
        try:
            import asyncio

            asyncio.get_event_loop().run_until_complete(async_engine.dispose())
        except RuntimeError:
            pass
        shutil.rmtree(base_dir, ignore_errors=True)
