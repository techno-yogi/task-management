# Architecture

## 1. Domain model

A **Sweep** is a hierarchical execution graph:

```
Sweep
└── Chunk (ordinal=1..N)         ← unit of sequential dispatch + recovery
    └── Job (1..M per chunk)     ← unit of fan-out / aggregation
        └── TaskVariant (1..K per job)  ← unit of work executed by a worker
```

Each level has a `status` (`pending` | `running` | `done` | `failed`) and a
`finalized_by` worker hostname that lets you reconstruct who did what for forensics.

`TaskVariant` carries `expected_value` (validation target), `actual_value`
(filled at completion), `validation_message`, `processed_by` (worker hostname),
and `celery_task_id` (correlation back to broker).

Cascade deletes on FK ensure a sweep delete tears down its full graph.

## 2. Storage layout

| Table | Indexes | Notes |
|---|---|---|
| `sweeps` | PK `id` | `status`, `total_chunks/jobs/tasks` (denormalized counts) |
| `chunks` | PK `id`, `ix_chunks_sweep_id` | `ordinal` is logical chunk number within sweep |
| `jobs` | PK `id`, `ix_jobs_chunk_id` | `attempts`, `shared_context_json` (per-job parameters) |
| `task_variants` | PK `id`, `ix_task_variants_job_id` | `point_idx` is logical position within job |

`TimestampMixin` adds `created_at` (server default `now()`) and `updated_at`
(`onupdate=now()`); we use `updated_at - created_at` for the latency reports.

Schema is managed by Alembic (`alembic/versions/0001_create_jobs.py`); the API
container runs `alembic upgrade head` on boot.

## 3. Concurrency model

### 3.1 Sequential chunk dispatcher

```
launch_sweep_workflow(sweep, from_ordinal=K)
  └── reset_sweep_for_relaunch(sweep, from_ordinal=K)   # in-API DB write
  └── dispatch_next_chunk_task.apply_async(K)           # → sweep.sweep.finalize queue

dispatch_next_chunk_task(sweep_id, ordinal):
  if no chunk at ordinal:
      finalize_sweep_task.apply_async(sweep_id)         # done
  elif chunks[ordinal].status == 'done':
      dispatch_next_chunk_task.apply_async(ordinal+1)   # skip, recurse
  else:
      _build_chunk_chord(sweep_id, chunk_id, ordinal).apply_async()

_build_chunk_chord =
  chord(
    [chord([execute_task_variant(t) for t in job.tasks],
           finalize_job_task(job_id))
     for job in chunk.jobs],
    chain(finalize_chunk_task(chunk_id),
          dispatch_next_chunk_task.si(sweep_id, ordinal+1))
  )
```

Why chunks are sequential and not parallel:

* Bounds in-flight work per sweep — important for chunked I/O or downstream
  systems that can’t accept all chunks’ output at once.
* Resume semantics are trivially `?from_chunk=K`.
* Finalize cascade (`finalize_chunk → dispatch_next`) lives on a single critical path
  worker tier so it can be tuned aggressively for low latency.

### 3.2 Queues and worker tiers

| Queue | Tasks | Worker tier | Why a separate tier |
|---|---|---|---|
| `sweep.execution` | `execute_task_variant` (potentially long, sleep_ms in test) | `worker_exec` (12 × 8 prefork) + Windows winexec (6 solo) | Heavy work; `--disable-prefetch` gives slow-worker fairness |
| `sweep.job.finalize` | `finalize_job_task` (~50 ms) | `worker_aggregate` (2 × 8 prefork) + Windows winagg (2 solo) | Short cascade tasks; **don’t** disable-prefetch |
| `sweep.chunk.finalize` | `finalize_chunk_task` (~50 ms) | same as above | bundled — same tasks scale together |
| `sweep.sweep.finalize` | `dispatch_next_chunk_task`, `finalize_sweep_task` | `worker_sweep_finalize` (2 × 8 prefork) — **Docker only** | Critical path; Windows solo workers excluded after Test #7 found 120 s stalls |

### 3.3 Acks-late + autoretry = at-least-once

`task_acks_late=True` and `execute_task_variant` declares
`autoretry_for=(Exception,)` with exponential backoff. Combined with
`task_acks_late`, this gives at-least-once delivery: a worker that dies
mid-task does **not** ack the message, so it’s redelivered.

The task is structurally idempotent — `actual_value = base + idx*step + offset`
plus a row update — so a duplicate delivery converges to the same final state.

## 4. Database connection topology

The API uses an **async** SQLAlchemy engine (asyncpg). Workers use a **sync**
engine (psycopg). Both go to the same Postgres.

```
parent worker process (Celery main)
  └── creates engine at import (best-effort, will be disposed on fork)
  └── fork → child process
      └── worker_process_init signal → _configure_session_factory()
          └── disposes parent's engine (close=False — leave file descriptors)
          └── creates a fresh engine bound to this PID
```

`pool_pre_ping=True` and `pool_recycle=1800` insulate against stale connections
(idle PG kills, NAT timeouts).

Per-process pool sizing matters: each prefork child is single-threaded inside a
task, so `pool_size=1, max_overflow=2` is enough. Total connections drawn from
PG = `(scale × concurrency × (1 + 2)) + (api_pool + api_overflow)` =
`(12 × 8 × 3) + (40 + 60) ≈ 388` < `max_connections=600`.

## 5. Postgres TLS

Postgres in compose runs with `ssl=on` and a self-signed CA + server cert (built
into the postgres image at `docker/postgres/Dockerfile`). The CA is mounted
read-only into both API and workers at `/etc/ssl/postgres/server.crt` and
referenced via `sslmode=verify-full&sslrootcert=…` in libpq URLs.

For asyncpg (which doesn’t accept libpq URL parameters), we strip the params and
build an `ssl.SSLContext` from the same CA file via `APP_ASYNC_PG_SSL_CA_FILE`.

For Windows native workers connecting to host-published Postgres on port
**55432**, `sslmode=verify-ca` is used (the cert CN is `postgres`, not
`localhost`).

## 6. Broker / result-backend

Redis 7 acts as both Celery broker and result backend (DBs 0 and 1
respectively). Two non-default settings matter:

1. `broker_pool_limit=200` — Kombu producer pool. Our chord canvas issues many
   concurrent `apply_async()` calls; the default of 10 deadlocks under load.
2. `broker_transport_options.socket_timeout=15.0` — bounds the time an
   `apply_async()` can block waiting on Redis. Without this, we observed a
   120 s stall on a Windows solo worker under heavy backpressure
   (Test #7). With it, kombu fails fast and retries on a fresh socket.

## 7. Observability

* `/metrics` — Prometheus exposition. Counters and gauges per
  `(task_name, queue, worker)`:
  * `celery_task_started_total`
  * `celery_task_succeeded_total`
  * `celery_task_failed_total`
  * `celery_task_inflight` (gauge)
  * `celery_worker_heartbeat_unixtime` (gauge)
* `/diagnostics/db` — SQLAlchemy pool snapshot + `pg_stat_activity` rollup.
  This is what the stress harness polls every 2 s for the live throughput
  monitor.
* Per-task `processed_by` and per-aggregate `finalized_by` columns — direct SQL
  visibility into worker distribution without scraping logs.

## 8. What makes this “hardened”

* All DB writes are **single-statement commits**; no long-running transactions.
* `task_acks_late` + idempotent task body = at-least-once with eventual
  consistency.
* Resume semantics built into the data model and dispatcher, not bolted on.
* Per-process engines, pre-ping, recycle on every connection: no leaks across
  1 250–50 000-task validations.
* Bounded socket timeouts on broker — no operation can wedge an entire sweep
  for more than a few seconds without explicit retry.
* Pluggable scale knobs: every queue has its own worker tier so you can scale
  the bottleneck without scaling the rest.

## 9. Known boundaries

* `GET /sweeps/{id}` eagerly loads the entire graph (chunks + jobs + tasks).
  At 50 k tasks per sweep this is ~50 k rows per poll and dominates DB time
  under aggressive polling. See `docs/ROADMAP.md` for the planned lightweight
  status endpoint.
* No row-level locking on state transitions — relies on idempotent task bodies
  for safety. Adequate for the current task semantics; would need
  `SELECT … FOR UPDATE` for non-idempotent business logic.
* Single-region: no replicas, no horizontal Postgres sharding. Cluster-wide
  ceiling = single Postgres can handle (≈ 600 conns × write QPS).
