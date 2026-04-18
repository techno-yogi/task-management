from __future__ import annotations

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = ConfigDict(env_prefix="APP_", extra="ignore")

    app_name: str = "FastAPI Celery SQLAlchemy Sweep App"
    # Defaults target local Postgres with TLS. Docker Compose mounts the same CA as Postgres and uses verify-full.
    # For managed Postgres, set sslrootcert / sslmode (e.g. verify-full) via the URL or libpq env vars.
    async_database_url: str = (
        "postgresql+asyncpg://app:app@localhost:5432/appdb?ssl=require"
    )
    sync_database_url: str = (
        "postgresql+psycopg://app:app@localhost:5432/appdb?sslmode=require"
    )
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_task_always_eager: bool = False
    celery_task_eager_propagates: bool = True
    celery_worker_prefetch_multiplier: int = 1
    celery_task_acks_late: bool = True
    # Kombu connection/producer pool. Our chord canvas is 3 levels deep so each /launch
    # synchronously holds 3 producers; the default of 10 deadlocks with >=4 concurrent launches.
    celery_broker_pool_limit: int = 200
    # Redis socket timeouts. Default kombu/redis-py is None (block forever) on the broker
    # connection and 120s elsewhere. Validation Test #7 caught a 120s stall inside
    # apply_async() on a solo worker under high backpressure. A small explicit timeout makes
    # such stalls fail fast so kombu retries on a fresh connection.
    celery_broker_socket_timeout: float = 15.0
    celery_broker_socket_connect_timeout: float = 10.0
    celery_redis_health_check_interval: int = 30
    sql_echo: bool = False
    # Sync pool: each Celery prefork child process gets its own pool — keep moderate defaults;
    # raise APP_SYNC_DB_POOL_* on dedicated API hosts or low worker concurrency.
    sync_db_pool_size: int = 5
    sync_db_max_overflow: int = 15
    sync_db_pool_timeout: int = 60
    async_db_pool_size: int = 15
    async_db_max_overflow: int = 25
    async_db_pool_timeout: int = 60
    # asyncpg does not accept libpq sslmode/sslrootcert URL params; use this CA file for verify-full-style TLS.
    async_pg_ssl_ca_file: str | None = None
    sqlite_timeout_seconds: int = 30
    celery_fs_in_dir: str = "/tmp/celery-broker/in"
    celery_fs_out_dir: str = "/tmp/celery-broker/out"
    celery_fs_processed_dir: str = "/tmp/celery-broker/processed"
    celery_queue_execution: str = "sweep.execution"
    celery_queue_job_finalize: str = "sweep.job.finalize"
    celery_queue_chunk_finalize: str = "sweep.chunk.finalize"
    celery_queue_sweep_finalize: str = "sweep.sweep.finalize"
    # HTTP handler blocks until the sweep chord finishes; allow headroom for large graphs under load.
    sweep_launch_result_timeout_seconds: int = 600
    # Sync endpoints (/launch, /diagnostics) run in AnyIO's thread pool; raise under many concurrent launches.
    sync_thread_pool_capacity: int = 128


settings = Settings()
