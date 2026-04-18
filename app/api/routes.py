from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.db.async_db import get_async_session
from app.db.sync_db import SyncSessionLocal, get_pg_stat_activity_snapshot, get_pool_snapshot
from app.metrics import CONTENT_TYPE_LATEST, metrics_payload
from app.config import settings
from app.schemas.job import (
    SweepCreate,
    SweepLaunchResponse,
    SweepLaunchStatusResponse,
    SweepRead,
    SweepStatusResponse,
)
from app.services.job_service import create_sweep_async, get_sweep_async, get_sweep_status_async
from app.tasks import launch_sweep_workflow

router = APIRouter(tags=["sweeps"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
