from __future__ import annotations

import json
import time
from typing import Any

from celery import Task, chain, chord

from app.celery_app import celery_app
from app.config import settings
from app.db.sync_db import SyncSessionLocal
from app.services.job_service import (
    build_chunk_execution_plan,
    complete_task,
    finalize_chunk,
    finalize_job,
    finalize_sweep,
    get_job_sync,
    list_sweep_chunks_summary,
    record_task_failure,
    reset_sweep_for_relaunch,
    start_chunk,
    start_job,
    start_task,
)


def _compute_actual_value(shared_context_json: str, point_idx: int) -> int:
    shared_context = json.loads(shared_context_json)
    base = int(shared_context.get("base_value", 0))
    step = int(shared_context.get("step", 1))
    offset = int(shared_context.get("offset", 0))
    return base + point_idx * step + offset


def _task_sleep_seconds(shared_context_json: str) -> float:
    shared_context = json.loads(shared_context_json)
    sleep_ms = int(shared_context.get("sleep_ms", 0))
    return max(0.0, sleep_ms / 1000.0)


@celery_app.task(bind=True)
def identify_execution_worker(self) -> dict[str, Any]:
    return {"worker": getattr(self.request, "hostname", None), "task_id": getattr(self.request, "id", None)}


@celery_app.task(bind=True)
def identify_job_finalize_worker(self) -> dict[str, Any]:
    return {"worker": getattr(self.request, "hostname", None), "task_id": getattr(self.request, "id", None)}


@celery_app.task(bind=True)
def identify_chunk_finalize_worker(self) -> dict[str, Any]:
    return {"worker": getattr(self.request, "hostname", None), "task_id": getattr(self.request, "id", None)}


@celery_app.task(bind=True)
def identify_sweep_finalize_worker(self) -> dict[str, Any]:
    return {"worker": getattr(self.request, "hostname", None), "task_id": getattr(self.request, "id", None)}


class _ExecuteTaskBase(Task):
    """Base class for execute_task_variant. Hooks `on_failure` so that when
    a task exhausts its retries (or fails with a non-retried exception class),
    we record the terminal failure to the DB. This is the "DLQ surface" on
    the Redis broker: failed task_variant rows are queryable via
    GET /sweeps/{id}/failures and replayable via POST /sweeps/{id}/launch?from_chunk=K.
    """

    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        try:
            variant_id = args[0] if args else kwargs.get("task_id")
            if variant_id is None:
                return
            with SyncSessionLocal() as session:
                record_task_failure(
                    session,
                    task_id=int(variant_id),
                    error_message=f"error: {type(exc).__name__}: {exc}",
                    processed_by=getattr(self.request, "hostname", None),
                    celery_task_id=task_id,
                )
        except Exception:
            # Never let a DLQ-recording failure mask the original exception
            # or crash the worker. The exception is still raised by Celery
            # and counted in celery_task_failed_total.
            pass


@celery_app.task(
    bind=True,
    base=_ExecuteTaskBase,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def execute_task_variant(self, task_id: int) -> dict[str, Any]:
    with SyncSessionLocal() as session:
        task = start_task(session, task_id)
        job = get_job_sync(session, task.job_id)
        if job is None:
            raise ValueError(f"Job {task.job_id} not found")
        sleep_seconds = _task_sleep_seconds(job.shared_context_json)
        actual_value = _compute_actual_value(job.shared_context_json, task.point_idx)
        expected_value = task.expected_value
        worker_name = getattr(self.request, "hostname", None)
        celery_task_id = getattr(self.request, "id", None)

    if sleep_seconds:
        time.sleep(sleep_seconds)

    with SyncSessionLocal() as session:
        updated = complete_task(
            session,
            task_id=task_id,
            actual_value=actual_value,
            expected_value=expected_value,
            validation_message="validated" if actual_value == expected_value else "validation mismatch",
            processed_by=worker_name,
            celery_task_id=celery_task_id,
        )
        return {
            "task_id": updated.id,
            "status": updated.status,
            "processed_by": updated.processed_by,
            "celery_task_id": updated.celery_task_id,
            "queue": settings.celery_queue_execution,
        }


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def finalize_job_task(self, results: list[dict[str, Any]], job_id: int) -> dict[str, Any]:
    worker_name = getattr(self.request, "hostname", None)
    with SyncSessionLocal() as session:
        job = finalize_job(session, job_id, finalized_by=worker_name)
        return {
            "job_id": job.id,
            "status": job.status,
            "tasks": len(job.tasks),
            "finalized_by": job.finalized_by,
            "queue": settings.celery_queue_job_finalize,
        }


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def finalize_chunk_task(self, results: list[dict[str, Any]], chunk_id: int) -> dict[str, Any]:
    worker_name = getattr(self.request, "hostname", None)
    with SyncSessionLocal() as session:
        chunk = finalize_chunk(session, chunk_id, finalized_by=worker_name)
        return {
            "chunk_id": chunk.id,
            "status": chunk.status,
            "jobs": len(chunk.jobs),
            "finalized_by": chunk.finalized_by,
            "queue": settings.celery_queue_chunk_finalize,
        }


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def finalize_sweep_task(self, results: list[dict[str, Any]], sweep_id: int) -> dict[str, Any]:
    worker_name = getattr(self.request, "hostname", None)
    with SyncSessionLocal() as session:
        sweep = finalize_sweep(session, sweep_id, finalized_by=worker_name)
        return {
            "sweep_id": sweep.id,
            "status": sweep.status,
            "chunks": len(sweep.chunks),
            "finalized_by": sweep.finalized_by,
            "queue": settings.celery_queue_sweep_finalize,
        }


def _build_chunk_chord(sweep_id: int, chunk_id: int, ordinal: int):
    """Build the chord for one chunk:
    - Header: chord-per-job (execute_task_variant fan-out -> finalize_job_task)
    - Body chain: finalize_chunk_task -> dispatch_next_chunk_task(sweep_id, ordinal+1)
    """
    with SyncSessionLocal() as session:
        plan = build_chunk_execution_plan(session, chunk_id)
        for job_entry in plan:
            start_job(session, job_entry["job_id"])
        start_chunk(session, chunk_id)

    job_chords = [
        chord(
            [
                execute_task_variant.s(task_id).set(queue=settings.celery_queue_execution)
                for task_id in job_entry["task_ids"]
            ],
            finalize_job_task.s(job_entry["job_id"]).set(queue=settings.celery_queue_job_finalize),
            app=celery_app,
        )
        for job_entry in plan
    ]

    callback = chain(
        finalize_chunk_task.s(chunk_id).set(queue=settings.celery_queue_chunk_finalize),
        # .si() = immutable signature: ignore upstream finalize_chunk result so dispatch_next_chunk_task
        # gets exactly (sweep_id, ordinal+1) and not the chunk-finalize dict prepended.
        dispatch_next_chunk_task.si(sweep_id, ordinal + 1).set(queue=settings.celery_queue_sweep_finalize),
        app=celery_app,
    )
    return chord(job_chords, callback, app=celery_app)


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def dispatch_next_chunk_task(self, sweep_id: int, chunk_ordinal: int) -> dict[str, Any]:
    """Sequential chunk dispatcher. Runs ONE chunk at a time:
      - skips chunks already status='done' (resume support)
      - if no more chunks, fires finalize_sweep_task and returns
      - otherwise dispatches a chord for the chunk and chains to itself for the next ordinal
    """
    with SyncSessionLocal() as session:
        chunks = list_sweep_chunks_summary(session, sweep_id)

    target = next((c for c in chunks if c["ordinal"] == chunk_ordinal), None)
    if target is None:
        # All chunks processed — fan-in to finalize the sweep on its own queue.
        result = finalize_sweep_task.apply_async(
            args=[None, sweep_id],
            queue=settings.celery_queue_sweep_finalize,
        )
        return {"sweep_id": sweep_id, "phase": "sweep_finalize_dispatched", "task_id": result.id}

    if target["status"] == "done":
        # Resume optimization — skip to next chunk without launching anything.
        result = dispatch_next_chunk_task.apply_async(
            args=[sweep_id, chunk_ordinal + 1],
            queue=settings.celery_queue_sweep_finalize,
        )
        return {"sweep_id": sweep_id, "chunk_ordinal": chunk_ordinal, "phase": "skipped_done", "task_id": result.id}

    chord_async = _build_chunk_chord(sweep_id, target["chunk_id"], target["ordinal"]).apply_async()
    return {
        "sweep_id": sweep_id,
        "chunk_ordinal": chunk_ordinal,
        "chunk_id": target["chunk_id"],
        "phase": "chunk_dispatched",
        "task_id": chord_async.id,
    }


def launch_sweep_workflow(sweep_id: int, from_ordinal: int = 1):
    """Kick off chunk-by-chunk execution starting at from_ordinal (1-indexed).

    For resume: pass from_ordinal=K to skip earlier completed chunks. Any chunks at or after
    from_ordinal whose status is not 'done' are reset to pending so they execute cleanly.
    """
    with SyncSessionLocal() as session:
        reset_sweep_for_relaunch(session, sweep_id, from_ordinal)
    return dispatch_next_chunk_task.apply_async(
        args=[sweep_id, from_ordinal],
        queue=settings.celery_queue_sweep_finalize,
    )
