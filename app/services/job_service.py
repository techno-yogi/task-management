from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.models import Chunk, Job, Sweep, TaskVariant
from app.schemas.job import SweepCreate


SUCCESS_STATUSES = {"done"}


def _serialize_context(shared_context: dict[str, int | str | float]) -> str:
    return json.dumps(shared_context, sort_keys=True)


async def create_sweep_async(session: AsyncSession, payload: SweepCreate) -> Sweep:
    sweep = Sweep(name=payload.name, status="pending")
    total_jobs = 0
    total_tasks = 0

    for chunk_payload in payload.chunks:
        chunk = Chunk(ordinal=chunk_payload.ordinal, status="pending")
        chunk_task_count = 0
        for job_payload in chunk_payload.jobs:
            job = Job(
                name=job_payload.name,
                status="pending",
                attempts=0,
                shared_context_json=_serialize_context(job_payload.shared_context),
            )
            for task_payload in job_payload.tasks:
                job.tasks.append(
                    TaskVariant(
                        point_idx=task_payload.point_idx,
                        name=task_payload.name,
                        expected_value=task_payload.expected_value,
                        status="pending",
                    )
                )
            chunk_task_count += len(job.tasks)
            total_jobs += 1
            chunk.jobs.append(job)
        chunk.total_jobs = len(chunk.jobs)
        chunk.total_tasks = chunk_task_count
        total_tasks += chunk_task_count
        sweep.chunks.append(chunk)

    sweep.total_chunks = len(sweep.chunks)
    sweep.total_jobs = total_jobs
    sweep.total_tasks = total_tasks

    session.add(sweep)
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
    validation_message: str,
    processed_by: str | None,
    celery_task_id: str | None,
) -> TaskVariant:
    task = get_task_sync(session, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")
    task.actual_value = actual_value
    task.validation_message = validation_message
    task.processed_by = processed_by
    task.celery_task_id = celery_task_id
    task.status = "done" if actual_value == task.expected_value else "failed"
    session.commit()
    session.refresh(task)
    return task


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
