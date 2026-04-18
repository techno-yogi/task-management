from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.models import Chunk, Job, Sweep, TaskVariant
from app.schemas.job import SweepCreate


SUCCESS_STATUSES = {"done"}


class StaleTaskStateError(RuntimeError):
    """Raised by `complete_task` when the target row was not in the expected
    `running` state at UPDATE time. Causes Celery's `autoretry_for=(Exception,)`
    to re-deliver the task, which will re-acquire the row via `start_task`.

    Most common causes:
      * `reset_sweep_for_relaunch` ran mid-flight and reset the row to pending.
      * Another worker (acks_late + redelivery) already completed the row.
    """


def _serialize_context(shared_context: dict[str, int | str | float]) -> str:
    return json.dumps(shared_context, sort_keys=True)


async def create_sweep_async(session: AsyncSession, payload: SweepCreate) -> Sweep:
    """Create a sweep graph (chunks → jobs → tasks) in three batched inserts.

    Compared to the previous per-row ORM-add approach, this issues ONE
    bulk insert per level (chunks, then jobs, then task_variants) using
    `insert().returning(id)` to capture the auto-generated PKs in order.
    For a 50k-task sweep this drops creation time from ~57k INSERTs in
    one transaction down to 3 round-trips.

    Insertion order assumptions: Postgres / sqlite both honor the order of
    `VALUES (...)` rows when emitting `RETURNING id` — i.e. the i-th
    returned id corresponds to the i-th input row.
    """
    chunk_total_jobs: list[int] = []
    chunk_total_tasks: list[int] = []
    grand_total_jobs = 0
    grand_total_tasks = 0
    for chunk_payload in payload.chunks:
        n_jobs = len(chunk_payload.jobs)
        n_tasks = sum(len(j.tasks) for j in chunk_payload.jobs)
        chunk_total_jobs.append(n_jobs)
        chunk_total_tasks.append(n_tasks)
        grand_total_jobs += n_jobs
        grand_total_tasks += n_tasks

    sweep = Sweep(
        name=payload.name,
        status="pending",
        total_chunks=len(payload.chunks),
        total_jobs=grand_total_jobs,
        total_tasks=grand_total_tasks,
    )
    session.add(sweep)
    await session.flush()  # populate sweep.id without committing

    if not payload.chunks:
        await session.commit()
        return await get_sweep_async(session, sweep.id)

    chunk_rows = [
        {
            "sweep_id": sweep.id,
            "ordinal": chunk.ordinal,
            "status": "pending",
            "total_jobs": chunk_total_jobs[i],
            "total_tasks": chunk_total_tasks[i],
        }
        for i, chunk in enumerate(payload.chunks)
    ]
    chunk_id_result = await session.execute(insert(Chunk).returning(Chunk.id), chunk_rows)
    chunk_ids = [row[0] for row in chunk_id_result.all()]

    job_rows: list[dict[str, Any]] = []
    job_chunk_index: list[int] = []  # which chunk each job belongs to (for FK)
    for ci, chunk_payload in enumerate(payload.chunks):
        for job_payload in chunk_payload.jobs:
            job_rows.append(
                {
                    "chunk_id": chunk_ids[ci],
                    "name": job_payload.name,
                    "status": "pending",
                    "attempts": 0,
                    "shared_context_json": _serialize_context(job_payload.shared_context),
                }
            )
            job_chunk_index.append(ci)

    if not job_rows:
        await session.commit()
        return await get_sweep_async(session, sweep.id)

    job_id_result = await session.execute(insert(Job).returning(Job.id), job_rows)
    job_ids = [row[0] for row in job_id_result.all()]

    task_rows: list[dict[str, Any]] = []
    job_idx = 0
    for chunk_payload in payload.chunks:
        for job_payload in chunk_payload.jobs:
            for task_payload in job_payload.tasks:
                task_rows.append(
                    {
                        "job_id": job_ids[job_idx],
                        "point_idx": task_payload.point_idx,
                        "name": task_payload.name,
                        "expected_value": task_payload.expected_value,
                        "status": "pending",
                    }
                )
            job_idx += 1

    if task_rows:
        await session.execute(insert(TaskVariant), task_rows)

    await session.commit()
    return await get_sweep_async(session, sweep.id)


async def get_sweep_async(session: AsyncSession, sweep_id: int) -> Sweep | None:
    stmt = (
        select(Sweep)
        .options(selectinload(Sweep.chunks).selectinload(Chunk.jobs).selectinload(Job.tasks))
        .where(Sweep.id == sweep_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_sweeps_async(
    session: AsyncSession,
    *,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Sweep], int]:
    """Paginated sweep listing for dashboards / operators.

    Returns (items, total_count). Sorted by id DESC so the most-recent sweep
    is first. `status_filter` matches `Sweep.status` exactly when provided.
    """
    base = select(Sweep)
    if status_filter is not None:
        base = base.where(Sweep.status == status_filter)

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(base.order_by(Sweep.id.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return list(rows), int(total)


async def get_sweep_status_async(session: AsyncSession, sweep_id: int) -> dict[str, Any] | None:
    """Cheap status snapshot for a sweep: header + per-status counts at every level.

    Avoids the full graph eager-load that `get_sweep_async` does. Designed to be
    safe to poll at sub-second cadence even for sweeps with 50k+ tasks.

    Returns a dict shaped like `SweepStatusResponse` (see app.schemas.job), or
    None if the sweep doesn't exist.
    """
    sweep_row = (
        await session.execute(
            select(
                Sweep.id,
                Sweep.name,
                Sweep.status,
                Sweep.finalized_by,
                Sweep.total_chunks,
                Sweep.total_jobs,
                Sweep.total_tasks,
            ).where(Sweep.id == sweep_id)
        )
    ).first()
    if sweep_row is None:
        return None

    chunks_counts_q = (
        select(Chunk.status, func.count())
        .where(Chunk.sweep_id == sweep_id)
        .group_by(Chunk.status)
    )
    jobs_counts_q = (
        select(Job.status, func.count())
        .where(Job.chunk_id.in_(select(Chunk.id).where(Chunk.sweep_id == sweep_id)))
        .group_by(Job.status)
    )
    tasks_counts_q = (
        select(TaskVariant.status, func.count())
        .where(
            TaskVariant.job_id.in_(
                select(Job.id).where(
                    Job.chunk_id.in_(select(Chunk.id).where(Chunk.sweep_id == sweep_id))
                )
            )
        )
        .group_by(TaskVariant.status)
    )

    def _bucket(rows: list[tuple[str, int]]) -> dict[str, int]:
        out = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for status_name, n in rows:
            out[status_name] = int(n)
        return out

    chunk_rows = (await session.execute(chunks_counts_q)).all()
    job_rows = (await session.execute(jobs_counts_q)).all()
    task_rows = (await session.execute(tasks_counts_q)).all()

    return {
        "id": sweep_row.id,
        "name": sweep_row.name,
        "status": sweep_row.status,
        "finalized_by": sweep_row.finalized_by,
        "total_chunks": sweep_row.total_chunks,
        "total_jobs": sweep_row.total_jobs,
        "total_tasks": sweep_row.total_tasks,
        "chunks": _bucket(chunk_rows),
        "jobs": _bucket(job_rows),
        "tasks": _bucket(task_rows),
    }


def get_sweep_sync(session: Session, sweep_id: int) -> Sweep | None:
    stmt = (
        select(Sweep)
        .options(selectinload(Sweep.chunks).selectinload(Chunk.jobs).selectinload(Job.tasks))
        .where(Sweep.id == sweep_id)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_chunk_sync(session: Session, chunk_id: int) -> Chunk | None:
    stmt = select(Chunk).options(selectinload(Chunk.jobs).selectinload(Job.tasks)).where(Chunk.id == chunk_id)
    return session.execute(stmt).scalar_one_or_none()


def get_job_sync(session: Session, job_id: int) -> Job | None:
    stmt = select(Job).options(selectinload(Job.tasks)).where(Job.id == job_id)
    return session.execute(stmt).scalar_one_or_none()


def get_task_sync(session: Session, task_id: int) -> TaskVariant | None:
    return session.get(TaskVariant, task_id)


def start_task(session: Session, task_id: int) -> TaskVariant:
    task = get_task_sync(session, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    task.status = "running"
    session.commit()
    session.refresh(task)
    return task


def complete_task(
    session: Session,
    task_id: int,
    actual_value: int,
    expected_value: int,
    validation_message: str,
    processed_by: str | None,
    celery_task_id: str | None,
) -> TaskVariant:
    """Atomically transition a TaskVariant from `running` to a terminal status.

    Implementation: a single
        UPDATE task_variants
           SET status = :terminal, actual_value = ..., ...
         WHERE id = :id AND status = 'running'
        RETURNING *

    If the WHERE clause matches 0 rows, raises `StaleTaskStateError` so the
    Celery `autoretry_for=(Exception,)` policy on `execute_task_variant`
    re-delivers the task. The retry path will re-call `start_task`, which is
    idempotent, then re-attempt completion.

    Why this matters: prior to this change, the read-modify-write happened in
    Python across two SELECT/UPDATE statements, so two concurrent deliveries
    (or a `reset_sweep_for_relaunch` interleaved with a worker) could clobber
    each other. With the atomic UPDATE there is exactly one winner per
    `running` -> terminal transition; everyone else gets a clean retry.
    """
    terminal_status = "done" if actual_value == expected_value else "failed"
    stmt = (
        update(TaskVariant)
        .where(TaskVariant.id == task_id, TaskVariant.status == "running")
        .values(
            status=terminal_status,
            actual_value=actual_value,
            validation_message=validation_message,
            processed_by=processed_by,
            celery_task_id=celery_task_id,
        )
        .returning(TaskVariant)
        .execution_options(synchronize_session=False)
    )
    row = session.execute(stmt).scalar_one_or_none()
    session.commit()
    if row is None:
        raise StaleTaskStateError(
            f"Task {task_id} was not in 'running' state when complete_task ran "
            "(possibly reset by relaunch or completed by another worker)."
        )
    session.refresh(row)
    return row


def record_task_failure(
    session: Session,
    task_id: int,
    error_message: str,
    processed_by: str | None,
    celery_task_id: str | None,
) -> None:
    """Record a terminal task failure (post-retries) into the DB.

    This is the "DLQ surface" given the Redis broker has no native DLX:
    failed task_variant rows are queryable via GET /sweeps/{id}/failures
    and replayable via POST /sweeps/{id}/launch?from_chunk=K.

    Idempotent — uses a single UPDATE with no status filter so it works
    whether the row was 'running' (normal failure) or 'pending' (failed
    before start_task could flip it).
    """
    stmt = (
        update(TaskVariant)
        .where(TaskVariant.id == task_id)
        .values(
            status="failed",
            validation_message=error_message[:200],
            processed_by=processed_by,
            celery_task_id=celery_task_id,
        )
        .execution_options(synchronize_session=False)
    )
    session.execute(stmt)
    session.commit()


def list_sweep_failures(session: Session, sweep_id: int) -> list[dict[str, Any]]:
    """Return all failed task_variants for a sweep (the DLQ view).

    A row with status='failed' and validation_message starting with 'error: '
    indicates an exhausted-retries Celery failure (recorded by the
    on_failure hook). A row with validation_message='validation mismatch'
    indicates a successful run whose actual_value didn't match expected_value.
    """
    stmt = (
        select(
            TaskVariant.id,
            TaskVariant.job_id,
            TaskVariant.point_idx,
            TaskVariant.name,
            TaskVariant.expected_value,
            TaskVariant.actual_value,
            TaskVariant.validation_message,
            TaskVariant.processed_by,
            TaskVariant.celery_task_id,
            TaskVariant.updated_at,
        )
        .where(
            TaskVariant.status == "failed",
            TaskVariant.job_id.in_(
                select(Job.id).where(
                    Job.chunk_id.in_(select(Chunk.id).where(Chunk.sweep_id == sweep_id))
                )
            ),
        )
        .order_by(TaskVariant.updated_at.desc())
    )
    return [
        {
            "task_id": row.id,
            "job_id": row.job_id,
            "point_idx": row.point_idx,
            "name": row.name,
            "expected_value": row.expected_value,
            "actual_value": row.actual_value,
            "validation_message": row.validation_message,
            "processed_by": row.processed_by,
            "celery_task_id": row.celery_task_id,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in session.execute(stmt).all()
    ]


def start_job(session: Session, job_id: int) -> Job:
    job = get_job_sync(session, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    job.status = "running"
    job.attempts += 1
    session.commit()
    session.refresh(job)
    return job


def start_chunk(session: Session, chunk_id: int) -> Chunk:
    chunk = get_chunk_sync(session, chunk_id)
    if chunk is None:
        raise ValueError(f"Chunk {chunk_id} not found")
    chunk.status = "running"
    chunk.finalized_by = None
    session.commit()
    session.refresh(chunk)
    return chunk


def reset_chunk_for_relaunch(session: Session, chunk_id: int) -> Chunk:
    """Reset all tasks/jobs in a chunk back to pending. Idempotent for status='done' chunks
    (those are skipped by the dispatcher anyway and not reset here)."""
    chunk = get_chunk_sync(session, chunk_id)
    if chunk is None:
        raise ValueError(f"Chunk {chunk_id} not found")
    for job in chunk.jobs:
        job.status = "pending"
        job.finalized_by = None
        for task in job.tasks:
            task.status = "pending"
            task.actual_value = None
            task.validation_message = None
            task.processed_by = None
            task.celery_task_id = None
    chunk.status = "pending"
    chunk.finalized_by = None
    session.commit()
    session.refresh(chunk)
    return chunk


def reset_sweep_for_relaunch(session: Session, sweep_id: int, from_ordinal: int) -> Sweep:
    """Reset all chunks at/after `from_ordinal` (regardless of status) so they will execute again.

    Chunks before `from_ordinal` are left untouched (their existing status / results stay).
    The sweep is marked `pending` so the final `finalize_sweep_task` can flip it back to `done`.
    """
    sweep = get_sweep_sync(session, sweep_id)
    if sweep is None:
        raise ValueError(f"Sweep {sweep_id} not found")
    for chunk in sweep.chunks:
        if chunk.ordinal >= from_ordinal:
            for job in chunk.jobs:
                job.status = "pending"
                job.attempts = 0
                job.finalized_by = None
                for task in job.tasks:
                    task.status = "pending"
                    task.actual_value = None
                    task.validation_message = None
                    task.processed_by = None
                    task.celery_task_id = None
            chunk.status = "pending"
            chunk.finalized_by = None
    sweep.status = "pending"
    sweep.finalized_by = None
    session.commit()
    session.refresh(sweep)
    return sweep


def finalize_job(session: Session, job_id: int, finalized_by: str | None = None) -> Job:
    job = get_job_sync(session, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found")
    statuses = [task.status for task in job.tasks]
    job.status = "done" if statuses and all(s in SUCCESS_STATUSES for s in statuses) else "failed"
    job.finalized_by = finalized_by
    session.commit()
    session.refresh(job)
    return job


def finalize_chunk(session: Session, chunk_id: int, finalized_by: str | None = None) -> Chunk:
    chunk = get_chunk_sync(session, chunk_id)
    if chunk is None:
        raise ValueError(f"Chunk {chunk_id} not found")
    statuses = [job.status for job in chunk.jobs]
    chunk.status = "done" if statuses and all(s == "done" for s in statuses) else "failed"
    chunk.finalized_by = finalized_by
    session.commit()
    session.refresh(chunk)
    return chunk


def finalize_sweep(session: Session, sweep_id: int, finalized_by: str | None = None) -> Sweep:
    sweep = get_sweep_sync(session, sweep_id)
    if sweep is None:
        raise ValueError(f"Sweep {sweep_id} not found")
    statuses = [chunk.status for chunk in sweep.chunks]
    sweep.status = "done" if statuses and all(s == "done" for s in statuses) else "failed"
    sweep.finalized_by = finalized_by
    session.commit()
    session.refresh(sweep)
    return sweep


def list_chunk_ids_for_sweep(session: Session, sweep_id: int) -> list[int]:
    stmt = select(Chunk.id).where(Chunk.sweep_id == sweep_id).order_by(Chunk.ordinal)
    return list(session.execute(stmt).scalars())


def list_job_ids_for_chunk(session: Session, chunk_id: int) -> list[int]:
    stmt = select(Job.id).where(Job.chunk_id == chunk_id).order_by(Job.id)
    return list(session.execute(stmt).scalars())


def list_task_ids_for_job(session: Session, job_id: int) -> list[int]:
    stmt = select(TaskVariant.id).where(TaskVariant.job_id == job_id).order_by(TaskVariant.point_idx)
    return list(session.execute(stmt).scalars())


def build_sweep_execution_plan(session: Session, sweep_id: int) -> list[dict[str, Any]]:
    sweep = get_sweep_sync(session, sweep_id)
    if sweep is None:
        raise ValueError(f"Sweep {sweep_id} not found")

    plan: list[dict[str, Any]] = []
    for chunk in sorted(sweep.chunks, key=lambda c: c.ordinal):
        chunk_entry: dict[str, Any] = {"chunk_id": chunk.id, "ordinal": chunk.ordinal, "status": chunk.status, "jobs": []}
        for job in sorted(chunk.jobs, key=lambda j: j.id):
            chunk_entry["jobs"].append(
                {"job_id": job.id, "task_ids": [task.id for task in sorted(job.tasks, key=lambda t: t.point_idx)]}
            )
        plan.append(chunk_entry)
    return plan


def list_sweep_chunks_summary(session: Session, sweep_id: int) -> list[dict[str, Any]]:
    """Lightweight summary for the chunk dispatcher: ordinal, chunk_id, status (no eager job/task load)."""
    stmt = select(Chunk.id, Chunk.ordinal, Chunk.status).where(Chunk.sweep_id == sweep_id).order_by(Chunk.ordinal)
    return [{"chunk_id": cid, "ordinal": ordinal, "status": status} for cid, ordinal, status in session.execute(stmt).all()]


def build_chunk_execution_plan(session: Session, chunk_id: int) -> list[dict[str, Any]]:
    """Return [{job_id, task_ids}] for a single chunk."""
    chunk = get_chunk_sync(session, chunk_id)
    if chunk is None:
        raise ValueError(f"Chunk {chunk_id} not found")
    return [
        {"job_id": job.id, "task_ids": [task.id for task in sorted(job.tasks, key=lambda t: t.point_idx)]}
        for job in sorted(chunk.jobs, key=lambda j: j.id)
    ]


def count_statuses(session: Session) -> dict[str, int]:
    def count(model: type[TaskVariant] | type[Job] | type[Chunk] | type[Sweep], status: str) -> int:
        return int(session.execute(select(func.count()).select_from(model).where(model.status == status)).scalar_one())

    return {
        "sweeps_done": count(Sweep, "done"),
        "chunks_done": count(Chunk, "done"),
        "jobs_done": count(Job, "done"),
        "tasks_done": count(TaskVariant, "done"),
    }
