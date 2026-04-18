# FastAPI + Celery + SQLAlchemy Sweep App

Production-oriented sweep orchestration: a FastAPI control-plane drives a Celery
worker fleet that executes hierarchical workloads (`sweep > chunk > job > task`)
backed by Postgres-with-TLS for state and Redis for the broker / result backend.
Designed for high-throughput, crash-safe, observable sweeps.

> Current verified throughput on the reference Docker layout (12 × 8 prefork exec workers
> + 6 Windows solo workers): **226 tasks/s** sustained on the backpressure benchmark, no message
> loss across 1 250 + 5 000 + 5 000 + 50 000 task validations, full row-count consistency.

---

## Architecture in 30 seconds

```
                          ┌────────────────┐
                          │    FastAPI     │  /sweeps   /launch?from_chunk=K
                          │   (uvicorn)    │  /sweeps/{id}   /diagnostics/db   /metrics
                          └───────┬────────┘
                                  │ async SQLAlchemy
                                  ▼
                          ┌────────────────┐         ┌────────────────┐
                          │   Postgres     │◄────────│     Redis      │  broker + result backend
                          │     +TLS       │         │  4 task queues │
                          └───────▲────────┘         └────────▲───────┘
                                  │                           │
                  ┌───────────────┴───────────┐               │
                  │                           │               │
        ┌─────────┴─────────┐       ┌─────────┴─────────┐    │
        │  worker_exec      │       │ worker_aggregate  │    │
        │  prefork × N×8    │       │  prefork × 2×8    │    │
        │  sweep.execution  │       │  sweep.job.fin.   │    │
        │  --disable-pref   │       │  sweep.chunk.fin. │    │
        └───────────────────┘       └───────────────────┘    │
                                                              │
                                  ┌───────────────────┐       │
                                  │ worker_sweep_     │       │
                                  │   finalize        │◄──────┘
                                  │  prefork × 2×8    │
                                  │  sweep.sweep.fin. │  (dispatcher + sweep finalize)
                                  └───────────────────┘

                  ┌─────────────────────────────────────────┐
                  │   OPTIONAL: Windows solo subprocesses   │
                  │  6× winexec   on  sweep.execution       │
                  │  2× winagg    on  sweep.{job,chunk}.fin │
                  │  (NOT on dispatcher — see VALIDATION.md)│
                  └─────────────────────────────────────────┘
```

The orchestration is a **sequential per-chunk dispatcher** (`dispatch_next_chunk_task`):
one chunk's chord runs, when it finalizes it chains a self-call for `chunk_ordinal+1`
(or `finalize_sweep_task` if no more chunks exist). This bounds concurrent in-flight
work per sweep and supports clean resume via `?from_chunk=K`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design.

---

## Quick start

Prerequisite: [`uv`](https://docs.astral.sh/uv/), Docker, Docker Compose.

```bash
uv sync --group dev
docker compose up -d --build --scale worker_exec=12 --scale worker_aggregate=2 --scale worker_sweep_finalize=2
curl http://127.0.0.1:8000/health
```

Create + launch a tiny sweep:

```bash
uv run python scripts/docker_stress_test.py \
  --sweeps 4 --chunks 2 --jobs-per-chunk 5 --tasks-per-job 5 --sleep-ms 500
```

Inspect a single sweep’s graph:

```bash
uv run python scripts/inspect_sweep.py 1
```

### Optional: add Windows native workers (Postgres-on-host port = 55432)

```powershell
docker compose cp postgres:/var/lib/postgresql/ssl/server.crt local_ssl/server.crt
powershell -ExecutionPolicy Bypass -File scripts/start_local_workers.ps1
# stop:
powershell -ExecutionPolicy Bypass -File scripts/start_local_workers.ps1 -Stop
```

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sweeps` | Create a sweep graph (chunks → jobs → tasks). Bulk-insert (3 round-trips). 422 if size exceeds quota. |
| `GET`  | `/sweeps` | Paginated header-only listing. Query params: `status=`, `limit=` (≤500), `offset=`. |
| `GET`  | `/sweeps/{id}` | Read full sweep graph (eager-loads chunks/jobs/tasks). Async handler. |
| `GET`  | `/sweeps/{id}/status` | **Lightweight** sweep header + per-status counts at every level (3 GROUP BYs). Use this for high-frequency polling instead of `GET /sweeps/{id}`. |
| `GET`  | `/sweeps/{id}/failures` | DLQ view — failed `task_variants` for the sweep (retries-exhausted + validation mismatches). |
| `POST` | `/sweeps/{id}/launch?from_chunk=K` | Enqueue `prepare_and_dispatch_sweep_task` (O(1) — reset runs on a worker). Rate-limited per sweep (default 4 / 5s ⇒ 429). |
| `GET`  | `/sweeps/{id}/launch/{root_task_id}` | Poll the dispatcher’s `AsyncResult` (ready/state/successful). Sync handler. |
| `GET`  | `/diagnostics/db` | SQLAlchemy pool snapshot + `pg_stat_activity` rollup. |
| `GET`  | `/metrics` | Prometheus exposition. |
| `GET`  | `/health` | Liveness probe (always 200 if process is up). |
| `GET`  | `/health/ready` | Readiness probe — DB ping + broker ping + alembic head check. 503 on any failure. |

`POST /launch` is **non-blocking** — it returns immediately after enqueueing the
first dispatcher task. Clients poll `GET /sweeps/{id}` for `status='done'`.

### Resume / partial relaunch

`POST /sweeps/{id}/launch?from_chunk=K` resets every chunk with `ordinal ≥ K`
(regardless of prior status — including `done`) back to `pending`, zeroes
`job.attempts`, and dispatches starting at chunk K. This is the recovery path for
worker kills, broker blips, and operator-cancelled sweeps. See validations #3 and #8.

---

## Operating the cluster

Recommended scale (verified in the 50 k-task heavy benchmark):

```bash
docker compose up -d \
  --scale worker_exec=12 \
  --scale worker_aggregate=2 \
  --scale worker_sweep_finalize=2
```

Worker-tier defaults and rationale live in [`docker-compose.yml`](docker-compose.yml):

* `worker_exec`: `--prefetch-multiplier=1 --disable-prefetch` so fast Docker workers
  do not hoard the queue; lets slower Windows solo workers participate.
* `worker_aggregate` and `worker_sweep_finalize`: prefetch=1 but **no** `--disable-prefetch`,
  because their tasks (50 ms) live on the cascade critical path; wake-up latency
  from disable-prefetch added a 60 s tail to multi-chunk sweeps.

Pool sizing per child process is intentionally tiny (`pool=1, overflow=2-3`) so
a 12 × 8 prefork tier draws ~288 max Postgres connections — well under
`max_connections=600` configured on the Postgres container.

### Tuning knobs (env, prefix `APP_`)

| Var | Default | Notes |
|---|---|---|
| `APP_CELERY_BROKER_POOL_LIMIT` | 200 | Kombu producer pool. Must exceed concurrent `apply_async` callers. |
| `APP_CELERY_BROKER_SOCKET_TIMEOUT` | 15.0 | Bounds individual Redis socket op. Stops solo workers from blocking 120 s on a wedged publish. |
| `APP_CELERY_BROKER_SOCKET_CONNECT_TIMEOUT` | 10.0 | Bounds initial TCP/TLS connect. |
| `APP_CELERY_REDIS_HEALTH_CHECK_INTERVAL` | 30 | redis-py heartbeat. |
| `APP_SYNC_DB_POOL_SIZE` / `APP_SYNC_DB_MAX_OVERFLOW` | 5 / 15 (worker default 1 / 2-3) | Per-process. Total DB conns = scale × concurrency × (pool + overflow). |
| `APP_ASYNC_DB_POOL_SIZE` / `APP_ASYNC_DB_MAX_OVERFLOW` | 15 / 25 | API-only. |
| `APP_SYNC_THREAD_POOL_CAPACITY` | 128 | AnyIO threadpool used by sync FastAPI handlers. |

### Postgres host-port mapping

We publish Postgres on **`55432`** and Redis on **`56379`** to avoid colliding with
native Windows Postgres / Redis services that commonly bind 5432/6379 first and
silently break the SSL handshake. See [`docker-compose.yml`](docker-compose.yml).

---

## Validation suite

11 reproducible validations live under `scripts/validation_*.py` /
`scripts/docker_stress_test.py`. Full transcript and pass/fail evidence in
[`docs/VALIDATION.md`](docs/VALIDATION.md).

| # | Test | Headline |
|---|---|---|
| 1 | 20 s realistic-workload soak | wall ≈ 2 × per-chunk; latency p50/p95 match sequential model |
| 2 | Connection-leak detector loop | pg_stat_activity 223 → 223 across 1 250 tasks |
| 3 | SIGKILL all exec workers + resume from chunk K | clean recovery; row counts match exactly |
| 4 | API restart mid-sweep | sweep continues; API down ~6 s, no data loss |
| 5 | Redis pause 5 s | Kombu auto-reconnect; sweep finishes |
| 6 | Re-launch a `done` sweep | idempotent; row counts re-converge |
| 7 | 200 concurrent launches (backpressure) | **22 s** wall (was 115 s before fix); 226 tasks/s avg |
| 8 | Cancel mid-sweep + recover | `celery purge` evicts pending; resume from chunk K |
| 9 | DB-pool saturation (500 + 500 in-flight HTTP) | zero 5xx, full pool recovery |
| 10 | p50/p95/p99 per-class latency | bucketed by `sleep_ms` class via direct SQL |
| 11 | Mixed workload fairness (`--sleep-ms 500,5000`) | 500 ms p99 = 1.4 s under 5 s peers — no starvation |

Run any one:

```bash
uv run python scripts/validation_kill_resume.py
uv run python scripts/validation_redis_blip.py
uv run python scripts/validation_pool_saturation.py
# … etc
```

Run the latency-bucketed stress test:

```bash
uv run python scripts/docker_stress_test.py \
  --sweeps 10 --chunks 4 --jobs-per-chunk 25 --tasks-per-job 5 \
  --sleep-ms 500,5000 \
  --report-latency-dsn "postgresql://app:app@localhost:55432/appdb?sslmode=verify-ca&sslrootcert=$PWD/local_ssl/server.crt"
```

---

## Tests

### Default pytest run
Runs fast local tests with Celery eager mode auto-enabled:

```bash
uv run pytest -q
```

### Real live multi-worker Celery tests

```bash
RUN_LIVE_WORKERS=1 uv run pytest -q -m live_worker
```

### Postgres integration

```bash
TEST_POSTGRES_URL='postgresql+psycopg://app:app@localhost:55432/appdb?sslmode=require' \
  uv run pytest -q -m postgres_integration
```

---

## See also

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design, data model, why each piece exists
* [`docs/VALIDATION.md`](docs/VALIDATION.md) — full validation transcripts + ops runbook
* [`docs/SCALING.md`](docs/SCALING.md) — tier-aware sizing formulas + DB / Redis budgets
* [`docs/ROADMAP.md`](docs/ROADMAP.md) — prioritized robustness/perf improvements (12/18 shipped)
