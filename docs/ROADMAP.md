# Robustness & Performance Roadmap

Concrete, prioritized improvements derived from the code review and the
11-validation pass. Each item lists the **observed problem**, the **proposed
change**, and the **verification** (how we’ll know it worked).

---

## P0 — Critical correctness / safety

### ✅ P0-1. Failure handling: `task_reject_on_worker_lost` + DB-backed DLQ

**Status.** Done — branch `improvements/p0`.

**Original idea.** Set `task_acks_on_failure_or_timeout=False` so failed tasks
land in a broker dead-letter for replay.

**Why we changed direction.** Redis (our broker) has no native
`x-dead-letter-exchange`. Setting `acks_on_failure_or_timeout=False` on Redis
+ `autoretry_for=(Exception,), max_retries=3` would create a poison-pill: once
retries are exhausted, the message is requeued, fails again, requeues, …

**What shipped.**

* `task_reject_on_worker_lost=True` globally — closes the SIGKILL hole
  (worker killed mid-task ⇒ broker requeues, not silently acks). This is the
  actual at-least-once delivery guarantee under acks_late.
* `execute_task_variant` now uses a custom `_ExecuteTaskBase` whose
  `on_failure` hook calls `record_task_failure` to write
  `status='failed', validation_message='error: <ExcType>: <msg>'` once retries
  are exhausted. This is the DB-backed DLQ surface (no broker-side DLX needed).
* `finalize_{job,chunk,sweep}_task` and `dispatch_next_chunk_task` get
  `autoretry_for=(Exception,), max_retries=5` so a transient PG/Redis blip on
  the cascade critical path doesn't strand a chord forever.
* New `GET /sweeps/{id}/failures` endpoint returns the DLQ view — both
  retries-exhausted failures (`error: …`) and clean validation mismatches.

**Verify.** Unit tests cover the empty case, the recorded-DLQ case (mixed
exhausted-retries + validation-mismatch), and confirm pending tasks don't
appear. End-to-end inject test (10×always-raise on one chunk) is queued
under "Validation #12" — see VALIDATION.md TODO.

### ✅ P0-2. Atomic `complete_task` transition + StaleTaskStateError

**Status.** Done — branch `improvements/p0`.

`complete_task` now does a single
`UPDATE … WHERE id=:id AND status='running' RETURNING *`; raises
`StaleTaskStateError` if 0 rows updated, which the existing
`autoretry_for=(Exception,)` policy on `execute_task_variant` catches and
re-delivers. Caller passes `expected_value` explicitly. Three new unit tests
cover happy path, stale, and double-call (first writer wins, second raises).

### ✅ P0-3. Lightweight `GET /sweeps/{id}/status` endpoint

**Status.** Done — branch `improvements/p0`.

Returns sweep header + per-status counts at every level via three indexed
GROUP BY queries. Cost stays in single-digit ms for 50k-task sweeps.
Polling clients (stress harness, dashboards) should switch from
`GET /sweeps/{id}` to `/status`. Three new unit tests cover initial, post-run,
and 404 paths.

**Follow-up.** Wire the stress harness over to `/status` and re-run validations
7 + 8 to quantify the DB CPU drop. Tracked in P2-5 below.

---

## P1 — Operationally important

### P1-1. Indices for status filters

`task_variants.status`, `jobs.status`, `chunks.status` — all read by
`/sweeps/{id}/status` and validation queries. Add covering partial indexes:

```sql
CREATE INDEX CONCURRENTLY ix_task_variants_running
  ON task_variants (job_id) WHERE status = 'running';
CREATE INDEX CONCURRENTLY ix_chunks_sweep_status
  ON chunks (sweep_id, status);
```

**Verify.** Compare `EXPLAIN (ANALYZE, BUFFERS)` on the new `/status` query
before/after.

### P1-2. Promote `status` to a Postgres ENUM

Single source of truth for valid states. Catches typos at insert time. Cheap
schema migration.

### P1-3. Structured / correlated logging

* Inject `sweep_id`, `chunk_id`, `job_id`, `celery_task_id` into a
  `contextvars.ContextVar` at task start.
* Use `python-json-logger` (or `structlog`) for the API + workers.
* Add a `request_id` middleware for FastAPI.

**Verify.** Run validation #3 (worker kill); grep logs across services for the
shared `sweep_id` and confirm the full timeline reconstructs without manual
correlation.

### P1-4. Health probes that mean something

`/health/live` (current `/health`) and `/health/ready` that:
* Pings DB (`SELECT 1` against async pool).
* Pings Redis broker (`PING`).
* Verifies migrations are at head (`alembic_version` matches).

**Verify.** `docker compose stop redis`; `/health/ready` must 503 within 1 s.

### P1-5. Make `POST /launch` truly O(1)

`reset_sweep_for_relaunch` runs synchronously inside the request and bulk-loads
the chunk graph. For 1 000-chunk sweeps this can take seconds.

**Change.** Make `launch` enqueue a `prepare_and_dispatch_sweep_task(sweep_id, from_chunk)`
that does both the reset and the first dispatch on a worker. API returns
`202 Accepted` immediately.

**Verify.** Time `POST /launch` against a 5 000-chunk sweep before/after; assert
< 50 ms.

### P1-6. Pagination + listing endpoints

`GET /sweeps?status=running&limit=50&offset=0` so dashboards don’t need to keep
their own ID inventory.

---

## P2 — Performance / scale

### P2-1. Bulk-insert sweep graph

`create_sweep_async` currently inserts row-by-row (chunks, then jobs, then
tasks). For a 50 k-task sweep this is ~57 k INSERTs in one transaction.
Replace with `Connection.execute(insert(...).values([...]))` after pre-computing
parent IDs via `RETURNING id` per level.

**Verify.** Time creation of a 50 k-task sweep before/after; expect 5–10×.

### P2-2. Pre-warm broker pool on API startup

Today the first 200 concurrent launches under cold-start pay the Kombu
producer-pool ramp-up. Open + park N connections in `lifespan` so the first
launches fly.

### P2-3. Tier-aware autoscaling hints in compose

Document explicit `--scale` targets per workload class:
* CPU-bound mostly-aggregate: 4× aggregate, 4× exec.
* Long-tail mostly-execute: 16× exec, 2× aggregate.
* Bursty backpressure: scale_sweep_finalize=4 to keep the dispatcher tier hot.

### P2-4. Batched `processed_by` writes

A single `complete_task` does two UPDATEs (start + complete) plus one each for
`finalize_job/chunk`. Compress to one update per state change with `RETURNING`.

### P2-5. Migrate stress harness + dashboards to `/sweeps/{id}/status`

Now that the lightweight endpoint exists (P0-3), update `docker_stress_test.py`
and any polling clients to use `/status` instead of `/sweeps/{id}`. Re-run
validations 7 + 8; quantify the DB CPU drop on the 50k-task heavy benchmark.

---

## P3 — Defensive depth

### P3-1. Rate-limit `POST /launch` per sweep

A buggy client could spam re-launches. Add a Redis-backed token bucket keyed by
`sweep_id`; default 1 launch / 5 s.

### P3-2. Authentication

Currently the API is open. Add OIDC bearer-token middleware (or at minimum a
shared secret header) before any non-localhost deployment.

### P3-3. mTLS for Postgres

`sslmode=verify-full` requires server cert CN = hostname. Today the cert CN is
`postgres` and we live with `verify-ca` for Windows native workers. Generate
SAN-aware certs on container boot so verify-full works for `postgres`,
`localhost`, and the host’s LAN IP simultaneously.

### P3-4. Quota: max chunks/jobs/tasks per sweep

Today a client can post a sweep with 1 M tasks; nothing rejects it. Add
configurable upper bounds in `app/config.py` and a 422 response.

### P3-5. Dead-letter visibility

`GET /admin/dlq` to inspect dead-lettered task IDs (after P0-1 ships).

---

## P4 — Developer experience

### P4-1. CI workflow

GitHub Actions: `pytest`, `pytest -m live_worker` (with services), `pytest -m
postgres_integration`, `ruff`, `mypy --strict app/`. Cache `uv` venv.

### P4-2. Type hints + mypy strict

The codebase is mostly typed; tighten the few `Any` corners (`shared_context_json`
parsing, Celery `apply_async` results) and turn on `mypy --strict`.

### P4-3. Replace ad-hoc validation scripts with a unified CLI

```
uv run python -m app.validation kill-resume
uv run python -m app.validation backpressure --concurrency=200
```

Shared argument parsing, shared cluster-readiness probe, shared latency
reporter.

### P4-4. Snapshot `/diagnostics/db` to a time-series store during runs

Easy CSV-on-disk dump from the stress harness’ background poller already exists;
wire it into a Grafana panel via VictoriaMetrics or just append-to-CSV +
plot via `matplotlib` in CI artifacts.

---

## What we deliberately did **not** add

* **Distributed locks for state transitions.** The data model is structured to
  be safe under at-least-once, idempotent execution. Adding pessimistic locks
  would add latency for no win.
* **A second cache tier in front of `/sweeps/{id}`.** Fixing the eager-load
  (P0-3) makes this unnecessary. Caches are how you hide bugs, not fix them.
* **Custom Celery routing strategies.** The 4-queue / 3-tier separation already
  buys us the isolation benefits without the complexity of priority queues.

---

## Execution order

If you have one engineer-week, do P0-1, P0-3, P1-1, P1-3, P1-4 — that closes
the visible gaps from the validation runs and turns the system into something
you can reasonably operate in production.

P1-5, P2-1, and P3-1 follow naturally as the load profile grows.
