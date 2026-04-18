from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

registry = CollectorRegistry()

celery_task_started_total = Counter(
    "celery_task_started_total",
    "Total number of Celery tasks started.",
    labelnames=("task_name", "queue", "worker"),
    registry=registry,
)

celery_task_succeeded_total = Counter(
    "celery_task_succeeded_total",
    "Total number of Celery tasks succeeded.",
    labelnames=("task_name", "queue", "worker"),
    registry=registry,
)

celery_task_failed_total = Counter(
    "celery_task_failed_total",
    "Total number of Celery tasks failed.",
    labelnames=("task_name", "queue", "worker"),
    registry=registry,
)

celery_task_inflight = Gauge(
    "celery_task_inflight",
    "Current number of Celery tasks in flight.",
    labelnames=("task_name", "queue", "worker"),
    registry=registry,
)

celery_worker_heartbeat = Gauge(
    "celery_worker_heartbeat_unixtime",
    "Last observed worker heartbeat timestamp.",
    labelnames=("worker",),
    registry=registry,
)


def metrics_payload() -> bytes:
    return generate_latest(registry)


__all__ = [
    "CONTENT_TYPE_LATEST",
    "celery_task_started_total",
    "celery_task_succeeded_total",
    "celery_task_failed_total",
    "celery_task_inflight",
    "celery_worker_heartbeat",
    "metrics_payload",
]
