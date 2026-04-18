from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.db.async_db import async_engine, get_async_session
from app.db.sync_db import SyncSessionLocal, get_pg_stat_activity_snapshot, get_pool_snapshot
from app.metrics import CONTENT_TYPE_LATEST, metrics_payload
from app.config import settings
from app.schemas.job import (
    FailedTaskRead,
    SweepCreate,
    SweepFailuresResponse,
    SweepLaunchResponse,
    SweepLaunchStatusResponse,
    SweepListResponse,
    SweepRead,
    SweepStatusResponse,
    SweepSummary,
)
from app.services.job_service import (
    create_sweep_async,
    get_sweep_async,
    get_sweep_status_async,
    list_sweep_failures,
    list_sweeps_async,
)
from app.tasks import launch_sweep_workflow

router = APIRouter(tags=["sweeps"])
log = logging.getLogger("app.api.routes")


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — is the API process up? Always returns 200.

    Use `/health/ready` for orchestrator readiness checks (DB + broker + migrations).
    """
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(response: Response) -> dict[str, object]:
    """Readiness probe — verifies the API can actually serve traffic.

    Checks (in order, all must pass):
    * Postgres reachable: `SELECT 1` against the async pool.
    * Redis broker reachable: ping the broker connection.
    * Alembic schema at head: `alembic_version.version_num` matches the latest
      revision script (Postgres only; sqlite/in-memory test setups skip this
      since they bypass Alembic via Base.metadata.create_all).

    Returns 200 with per-check details on success, 503 with the failing check
    on failure. Designed for k8s/Docker `livenessProbe`/`readinessProbe`.
    """
    checks: dict[str, object] = {}
    overall_ok = True

    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = {"ok": True}
    except Exception as exc:
        checks["db"] = {"ok": False, "error": str(exc)[:200]}
        overall_ok = False

    try:
        with celery_app.connection_for_read() as conn:
            conn.ensure_connection(max_retries=1, timeout=2)
        checks["broker"] = {"ok": True}
    except Exception as exc:
        checks["broker"] = {"ok": False, "error": str(exc)[:200]}
        overall_ok = False

    try:
        head = _alembic_head_revision()
        if head is None:
            checks["alembic"] = {"ok": True, "skipped": "non-postgres backend"}
        else:
            async with async_engine.connect() as conn:
                row = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).first()
            current = row[0] if row else None
            ok = current == head
            checks["alembic"] = {"ok": ok, "current": current, "head": head}
            if not ok:
                overall_ok = False
    except Exception as exc:
        # No alembic_version table = pre-migration or non-managed DB.
        # We don't fail readiness on that — the DB ping above already covered
        # "is the DB usable". Surface the detail for diagnostics.
        checks["alembic"] = {"ok": True, "skipped": f"check failed: {str(exc)[:120]}"}

    if not overall_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if overall_ok else "not_ready", "checks": checks}


def _alembic_head_revision() -> str | None:
    """Return the latest Alembic revision id, or None if not applicable.

    For postgres backends we expect a managed schema. For sqlite (tests) we
    return None so the readiness check skips it.
    """
    from app.config import settings as _settings

    if "sqlite" in _settings.sync_database_url:
        return None
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()
    except Exception:
        return None


@router.get("/sweeps", response_model=SweepListResponse)
async def list_sweeps(
    status_filter: str | None = Query(None, alias="status", description="Exact match on sweep.status (pending|running|done|failed)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),
) -> SweepListResponse:
    """Paginated sweep listing — header rows only (no chunks/jobs/tasks).

    Use `?status=running` to power dashboards. Sort order is id DESC so the
    most-recent sweep is first. `total` reflects the filtered count, not the
    table cardinality.
    """
    items, total = await list_sweeps_async(
        session, status_filter=status_filter, limit=limit, offset=offset
    )
    return SweepListResponse(
        items=[SweepSummary.model_validate(s) for s in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/sweeps", response_model=SweepRead, status_code=status.HTTP_201_CREATED)
async def create_sweep(payload: SweepCreate, session: AsyncSession = Depends(get_async_session)) -> SweepRead:
    sweep = await create_sweep_async(session, payload)
    return SweepRead.model_validate(sweep)


@router.get("/sweeps/{sweep_id}", response_model=SweepRead)
async def get_sweep(sweep_id: int, session: AsyncSession = Depends(get_async_session)) -> SweepRead:
    sweep = await get_sweep_async(session, sweep_id)
    if sweep is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    return SweepRead.model_validate(sweep)


@router.get("/sweeps/{sweep_id}/status", response_model=SweepStatusResponse)
async def get_sweep_status(
    sweep_id: int, session: AsyncSession = Depends(get_async_session)
) -> SweepStatusResponse:
    """Cheap status snapshot — sweep header + per-status counts at every level.

    Polling clients (dashboards, the stress harness) should prefer this over
    `GET /sweeps/{id}`, which eager-loads the full chunk/job/task graph and
    becomes the dominant DB cost on large sweeps. This endpoint runs four
    indexed lookups (one header + three GROUP BYs) and returns in single-digit
    milliseconds even for 50k-task sweeps.
    """
    snapshot = await get_sweep_status_async(session, sweep_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    return SweepStatusResponse.model_validate(snapshot)


@router.post("/sweeps/{sweep_id}/launch", response_model=SweepLaunchResponse)
def start_sweep(
    sweep_id: int,
    from_chunk: int = Query(1, ge=1, description="1-indexed chunk ordinal to start from. Already-done chunks are skipped."),
) -> SweepLaunchResponse:
    # Sequential chunk dispatcher (one chunk at a time). Returns immediately with the
    # dispatcher's task id; clients poll GET /sweeps/{id} for status='done'. Pass
    # ?from_chunk=K to resume a large sweep from a specific chunk ordinal.
    result = launch_sweep_workflow(sweep_id, from_ordinal=from_chunk)
    response_status = "queued"
    # Read live conf rather than `settings` so test fixtures that flip eager mode at runtime
    # are reflected here.
    if celery_app.conf.task_always_eager:
        # In eager mode the whole recursive chain ran inline by the time apply_async() returned.
        response_status = "done" if result.successful() else "failed"
    return SweepLaunchResponse(sweep_id=sweep_id, status=response_status, root_task_id=str(result.id))


@router.get("/sweeps/{sweep_id}/launch/{root_task_id}", response_model=SweepLaunchStatusResponse)
def get_launch_status(sweep_id: int, root_task_id: str) -> SweepLaunchStatusResponse:
    async_result = celery_app.AsyncResult(root_task_id)
    ready = async_result.ready()
    payload: dict | None = None
    successful: bool | None = None
    if ready:
        successful = async_result.successful()
        if successful:
            value = async_result.result
            if isinstance(value, dict):
                payload = value
    return SweepLaunchStatusResponse(
        sweep_id=sweep_id,
        root_task_id=root_task_id,
        ready=ready,
        state=async_result.state,
        successful=successful,
        result=payload,
    )


@router.get("/sweeps/{sweep_id}/failures", response_model=SweepFailuresResponse)
def get_sweep_failures(sweep_id: int) -> SweepFailuresResponse:
    """List failed task_variants for a sweep — the DLQ view.

    Two failure modes are recorded here:

    * `validation_message` starts with `error:` — the task exhausted its
      Celery retries (recorded by execute_task_variant.on_failure). This is
      the Redis-broker DLQ surrogate.
    * `validation_message == 'validation mismatch'` — the task ran cleanly
      but its `actual_value` did not equal `expected_value`. Indicates a
      logic / config bug, not infrastructure.

    Replay path: fix the upstream cause, then
    `POST /sweeps/{sweep_id}/launch?from_chunk=K` where K is the lowest
    chunk ordinal containing failures.
    """
    with SyncSessionLocal() as session:
        rows = list_sweep_failures(session, sweep_id)
    return SweepFailuresResponse(
        sweep_id=sweep_id,
        count=len(rows),
        failures=[FailedTaskRead.model_validate(r) for r in rows],
    )


@router.get("/metrics", include_in_schema=False)
def get_metrics() -> Response:
    return Response(content=metrics_payload(), media_type=CONTENT_TYPE_LATEST)


@router.get("/diagnostics/db")
def get_db_diagnostics() -> dict:
    with SyncSessionLocal() as session:
        return {
            "pool": get_pool_snapshot(),
            "pg_stat_activity": get_pg_stat_activity_snapshot(session),
        }
