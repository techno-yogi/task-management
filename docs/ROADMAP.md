# Robustness & Performance Roadmap

Concrete, prioritized improvements derived from the code review and the
11-validation pass. Each item lists the **observed problem**, the **proposed
change**, and the **verification** (how we’ll know it worked).

---

## P0 — Critical correctness / safety

### P0-1. Add `acks_on_failure_or_timeout=False` to `execute_task_variant`

**Problem.** Today `task_acks_late=True` is on, but if `execute_task_variant`
fails *and* exhausts retries, Celery acks the message. With `complete_task`
crashing late in the cycle (e.g. PG outage during update) the task can be
silently lost.

**Change.** Set `acks_on_failure_or_timeout=False` on the execute task **and**
the finalize tasks. Combined with bounded `max_retries`, failed tasks land in
the broker dead-letter (per-queue `x-dead-letter-exchange`) for replay rather
than disappearing.

**Verify.** Inject `raise RuntimeError` 100 % of the time in `_compute_actual_value`
for one chunk, run a sweep; confirm those task IDs appear in the DLX queue and
re-running them clears `failed` rows.

### P0-2. Replace per-task race in `start_task → complete_task` with a single UPDATE

**Problem.** Today two separate sessions touch a `TaskVariant`:
1. `start_task`: `SELECT … FOR UPDATE`-less read, then update to `running`.
2. `complete_task`: read again, write `done`.

Between (1) and (2) the row could be reset by `reset_sweep_for_relaunch`,
re-claimed by another worker, etc. Idempotent value computation hides this for
now, but a non-idempotent task body would corrupt.

**Change.** Single `UPDATE … WHERE id=:id AND status='running' RETURNING *` for
the completion path. If 0 rows updated, raise → autoretry kicks in.

**Verify.** Existing tests + a new race test that calls `complete_task` twice
concurrently and asserts only one wins.

### P0-3. `/sweeps/{id}` must not eagerly load the full graph

**Problem.** Each `GET /sweeps/{id}` selectin-loads all chunks, jobs, and tasks.
Polling clients (the stress harness, dashboards, watchers) hit this every
0.5–1 s. At 50 k tasks per sweep that’s ~50 k rows per poll. We measured this
as the dominant cost during the heavy stress test (Postgres CPU spent in the
`task_variants` index range scan).

**Change.** Two endpoints:

* `GET /sweeps/{id}/status` — returns `{id, status, total_*, counts_by_status}`
  computed via a single `SELECT count(*) FILTER (WHERE status=…)` per level
  (one round-trip, ~3 ms).
* `GET /sweeps/{id}` — keep eager-load behavior but cap depth via query params
  (`?include=chunks` / `?include=jobs` / `?include=tasks`).

**Verify.** Replay validations 7 + 8 against `/status`; confirm DB CPU drops
significantly while wall-clock numbers don’t regress.

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
