from __future__ import annotations

from celery import Celery

from celery.signals import task_failure, task_postrun, task_prerun, worker_ready

from app.metrics import (
    celery_task_failed_total,
    celery_task_inflight,
    celery_task_started_total,
    celery_task_succeeded_total,
    celery_worker_heartbeat,
)
from kombu import Exchange, Queue

from app.config import settings
from app.logging_setup import (
    celery_task_id_ctx,
    chunk_id_ctx,
    configure_logging,
    job_id_ctx,
    sweep_id_ctx,
    task_variant_id_ctx,
)


configure_logging()

celery_app = Celery(
    "fastapi-celery-sqlalchemy-app",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks"],
)

transport_options: dict[str, object] = {}
if settings.celery_broker_url.startswith("filesystem://"):
    transport_options = {
        "data_folder_in": settings.celery_fs_in_dir,
        "data_folder_out": settings.celery_fs_out_dir,
        "data_folder_processed": settings.celery_fs_processed_dir,
        "store_processed": True,
    }
elif settings.celery_broker_url.startswith(("redis://", "rediss://")):
    # Bound the time a worker can spend blocked inside a single Redis socket op
    # (e.g. apply_async publishing a chord under heavy contention). Without this,
    # kombu/redis-py default to no timeout on the broker connection and a stalled
    # publish wedges the entire dispatcher chain. See VALIDATION.md #7.
    transport_options = {
        "socket_timeout": settings.celery_broker_socket_timeout,
        "socket_connect_timeout": settings.celery_broker_socket_connect_timeout,
        "socket_keepalive": True,
        "health_check_interval": settings.celery_redis_health_check_interval,
        # retry_on_timeout lets redis-py treat the timeout as a transient error
        # and retry once on a fresh connection before bubbling up to celery.
        "retry_on_timeout": True,
    }

execution_queue = settings.celery_queue_execution
job_finalize_queue = settings.celery_queue_job_finalize
chunk_finalize_queue = settings.celery_queue_chunk_finalize
sweep_finalize_queue = settings.celery_queue_sweep_finalize

celery_app.conf.update(
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=settings.celery_task_eager_propagates,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    worker_prefetch_multiplier=settings.celery_worker_prefetch_multiplier,
    task_acks_late=settings.celery_task_acks_late,
    # If a worker is SIGKILL'd (OOM, host reboot, container kill) mid-task, the
    # broker requeues the message instead of silently acking it. This is the
    # actual at-least-once delivery guarantee under acks_late. Without it, a
    # killed worker's in-flight tasks vanish from the broker.
    task_reject_on_worker_lost=True,
    # NOTE: we deliberately leave `task_acks_on_failure_or_timeout=True` (the
    # default). Setting it False on Redis would cause poison-pill retry storms
    # once `max_retries` is exhausted (Redis has no native dead-letter exchange).
    # Final failures are surfaced via the on_failure handler in app/tasks.py
    # which writes to task_variants (status='failed', validation_message='error: ...');
    # query that view via GET /sweeps/{id}/failures. See docs/ARCHITECTURE.md "DLQ".
    task_track_started=True,
    broker_transport_options=transport_options,
    result_backend_transport_options=transport_options if transport_options else {},
    broker_pool_limit=settings.celery_broker_pool_limit,
    # Match redis-py's connection-level timeouts on the *publish* side too. Without
    # broker_connection_timeout, a SYN to a wedged broker can hang for the OS default
    # (~75s on Linux). We want to fail fast and retry.
    broker_connection_timeout=settings.celery_broker_socket_connect_timeout,
    # If the broker disappears, retry forever with capped backoff rather than dropping
    # in-flight messages from a worker. (acks_late + this = at-least-once delivery.)
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=None,
    task_default_exchange="sweep",
    task_default_exchange_type="direct",
    task_queues=(
        Queue(execution_queue, Exchange("sweep", type="direct"), routing_key=execution_queue),
        Queue(job_finalize_queue, Exchange("sweep", type="direct"), routing_key=job_finalize_queue),
        Queue(chunk_finalize_queue, Exchange("sweep", type="direct"), routing_key=chunk_finalize_queue),
        Queue(sweep_finalize_queue, Exchange("sweep", type="direct"), routing_key=sweep_finalize_queue),
    ),
    task_routes={
        "app.tasks.execute_task_variant": {"queue": execution_queue, "routing_key": execution_queue},
        "app.tasks.identify_execution_worker": {"queue": execution_queue, "routing_key": execution_queue},
        "app.tasks.finalize_job_task": {"queue": job_finalize_queue, "routing_key": job_finalize_queue},
        "app.tasks.identify_job_finalize_worker": {"queue": job_finalize_queue, "routing_key": job_finalize_queue},
        "app.tasks.finalize_chunk_task": {"queue": chunk_finalize_queue, "routing_key": chunk_finalize_queue},
        "app.tasks.identify_chunk_finalize_worker": {"queue": chunk_finalize_queue, "routing_key": chunk_finalize_queue},
        "app.tasks.finalize_sweep_task": {"queue": sweep_finalize_queue, "routing_key": sweep_finalize_queue},
        "app.tasks.identify_sweep_finalize_worker": {"queue": sweep_finalize_queue, "routing_key": sweep_finalize_queue},
        "app.tasks.dispatch_next_chunk_task": {"queue": sweep_finalize_queue, "routing_key": sweep_finalize_queue},
    },
)


def _task_labels(task, task_id, args, kwargs, einfo=None):
    request = getattr(task, "request", None)
    delivery_info = getattr(request, "delivery_info", {}) or {}
    queue = delivery_info.get("routing_key") or delivery_info.get("exchange") or "unknown"
    worker = getattr(request, "hostname", None) or "unknown"
    task_name = getattr(task, "name", None) or "unknown"
    return task_name, queue, worker


_CORRELATION_BY_TASK = {
    # task_name -> (positional-arg index for the id, contextvar to set)
    "app.tasks.execute_task_variant": (0, task_variant_id_ctx),
    "app.tasks.finalize_job_task": (1, job_id_ctx),
    "app.tasks.finalize_chunk_task": (1, chunk_id_ctx),
    "app.tasks.finalize_sweep_task": (1, sweep_id_ctx),
    "app.tasks.dispatch_next_chunk_task": (0, sweep_id_ctx),
    "app.tasks.prepare_and_dispatch_sweep_task": (0, sweep_id_ctx),
}

_PRERUN_TOKENS: dict[str, list] = {}


@task_prerun.connect
def _on_task_prerun(task_id=None, task=None, args=None, kwargs=None, **_):
    task_name, queue, worker = _task_labels(task, task_id, args, kwargs)
    celery_task_started_total.labels(task_name=task_name, queue=queue, worker=worker).inc()
    celery_task_inflight.labels(task_name=task_name, queue=queue, worker=worker).inc()

    # Push correlation ids into contextvars so log lines for this task body
    # are auto-tagged. Tokens are stashed by celery task_id and restored in
    # task_postrun.
    entries: list[tuple] = [(celery_task_id_ctx, celery_task_id_ctx.set(task_id))]
    arg_map = _CORRELATION_BY_TASK.get(task_name)
    if arg_map and args is not None:
        idx, var = arg_map
        if idx < len(args):
            try:
                entries.append((var, var.set(int(args[idx]))))
            except (TypeError, ValueError):
                pass
    _PRERUN_TOKENS[task_id] = entries


@task_postrun.connect
def _on_task_postrun(task_id=None, task=None, args=None, kwargs=None, state=None, **_):
    task_name, queue, worker = _task_labels(task, task_id, args, kwargs)
    celery_task_inflight.labels(task_name=task_name, queue=queue, worker=worker).dec()
    if state == "SUCCESS":
        celery_task_succeeded_total.labels(task_name=task_name, queue=queue, worker=worker).inc()
    entries = _PRERUN_TOKENS.pop(task_id, None)
    if entries:
        # Reset in reverse order to mirror the LIFO stack semantics of
        # ContextVar.set/reset. Each entry is (var, token).
        for var, token in reversed(entries):
            try:
                var.reset(token)
            except (LookupError, ValueError):
                pass


@task_failure.connect
def _on_task_failure(task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, sender=None, **_):
    task_name, queue, worker = _task_labels(sender, task_id, args, kwargs, einfo=einfo)
    celery_task_failed_total.labels(task_name=task_name, queue=queue, worker=worker).inc()


@worker_ready.connect
def _on_worker_ready(sender=None, **_):
    hostname = getattr(sender, "hostname", None) or "unknown"
    import time
    celery_worker_heartbeat.labels(worker=hostname).set(time.time())
