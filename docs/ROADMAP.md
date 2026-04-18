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

### ✅ P1-1. Indices for status filters

**Status.** Done — branch `improvements/p1-p4`. Alembic migration
`0002_status_indexes.py` adds composite `(parent_id, status)` indexes on
`chunks`, `jobs`, `task_variants` plus a partial index
`task_variants(job_id) WHERE status='running'` for the hot "anything still
running?" check. `ix_sweeps_status` powers the new `/sweeps?status=` filter.

Original plan:

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

### ⏸️ P1-2. Promote `status` to a Postgres ENUM

**Status.** Deferred. Rationale: ENUMs in Postgres are awkward to evolve
(`ALTER TYPE … ADD VALUE` cannot run inside a transaction, blocks rollback
on failure, and doesn't reorder values). Combined with the partial indexes
shipped in P1-1, the upside is small: typos at insert time would be caught
by the existing `Literal[…]` Pydantic schemas before they reach SQL. Revisit
when we add a new status value (e.g. `cancelled`) and want to enforce
exhaustiveness.

### ✅ P1-3. Structured / correlated logging

**Status.** Done — branch `improvements/p1-p4`. New `app/logging_setup.py`
with 6 ContextVars (request_id, celery_task_id, sweep_id, chunk_id, job_id,
task_variant_id) injected into every log record by a `_CorrelationFilter`.
JSON formatter (env: `APP_JSON_LOGS=1`) for production, plain-text formatter
with `[req=… sweep=… ctid=…]` prefix for local dev. `RequestIdMiddleware`
honors inbound `X-Request-ID`. Celery `task_prerun` hook derives sweep/chunk/
job/task_variant ids from each known task's positional args.

Original plan:

* Inject `sweep_id`, `chunk_id`, `job_id`, `celery_task_id` into a
  `contextvars.ContextVar` at task start.
* Use `python-json-logger` (or `structlog`) for the API + workers.
* Add a `request_id` middleware for FastAPI.

**Verify.** Run validation #3 (worker kill); grep logs across services for the
shared `sweep_id` and confirm the full timeline reconstructs without manual
correlation.

### ✅ P1-4. Health probes that mean something

**Status.** Done — branch `improvements/p1-p4`. New `GET /health/ready`
returns 200 with per-check details on success, 503 on failure. Checks: DB
(`SELECT 1`), broker (`kombu.ensure_connection` 2s timeout), and Alembic
schema at head (Postgres only — sqlite tests skip gracefully). Original
`/health` stays as the cheap liveness probe.

### ✅ P1-5. Make `POST /launch` truly O(1)

**Status.** Done — branch `improvements/p1-p4`. New
`prepare_and_dispatch_sweep_task` worker task does the
`reset_sweep_for_relaunch` work (which is O(chunks·jobs·tasks)) and the
first dispatch. `POST /launch` is now a single `apply_async` — request
returns immediately even for 50k-chunk sweeps. Eager-mode behavior
preserved (whole chain still runs inline, tests still see `status=done`).

### ✅ P1-6. Pagination + listing endpoints

**Status.** Done — branch `improvements/p1-p4`. `GET /sweeps?status=&limit=&offset=`
returns header-only `SweepSummary` rows (no chunks/jobs/tasks). Sort id
DESC, default limit=50, max=500. Powers operator dashboards without paying
the eager-load cost. Indexed by `ix_sweeps_status` from P1-1.

---

## P2 — Performance / scale

### ✅ P2-1. Bulk-insert sweep graph

**Status.** Done — branch `improvements/p1-p4`. `create_sweep_async` now
does 3 round-trips total (chunks → jobs → tasks) using
`insert(...).returning(id)` to capture auto-generated PKs in order. For a
50k-task sweep this drops creation from ~57k INSERTs in one transaction
to 3 batched parameterized INSERTs.

### ✅ P2-2. Pre-warm broker pool on API startup

**Status.** Done — branch `improvements/p1-p4`. New `_prewarm_broker_pool()`
in `app/main.py` lifespan pre-acquires `APP_BROKER_PREWARM_COUNT` Kombu
connections (default 32) and immediately releases them so the pool is hot
before the first launch arrives. Tolerates partial failure.

### ✅ P2-3. Tier-aware autoscaling hints in compose

**Status.** Done — branch `improvements/p1-p4`. New `docs/SCALING.md` with
concrete formulas per tier, DB connection budget, Redis sizing, and the
HPA-style probe story (`/health/ready` for readiness, queue depth for
worker autoscaling).

### ⏸️ P2-4. Batched `processed_by` writes

**Status.** Deferred. The 2-UPDATE pattern (start + complete) is what
makes the at-least-once + idempotent semantics observable
(`task_variants.created_at` + `updated_at` are the single source of truth
for latency). Compressing to one UPDATE removes that observability. The
modest perf win isn't worth losing the diagnostic surface that powered the
Test #7 RCA. Revisit if write amplification becomes the bottleneck.

### ✅ P2-5. Migrate stress harness + dashboards to `/sweeps/{id}/status`

**Status.** Done — branch `improvements/p1-p4`. `scripts/docker_stress_test.py`
and the validation polling loops in `validation_{cancel,redis_blip,api_restart,kill_resume}.py`
now poll `/sweeps/{id}/status` instead of `/sweeps/{id}` for the hot
"is it done yet?" check. Pool-saturation script intentionally still uses
`/sweeps/{id}` (it's stressing the *expensive* endpoint deliberately).

---

## P3 — Defensive depth

### ✅ P3-1. Rate-limit `POST /launch` per sweep

**Status.** Done — branch `improvements/p1-p4`. In-memory token bucket
keyed by `sweep_id`; defaults to `APP_LAUNCH_RATE_LIMIT_PER_WINDOW=4` per
`APP_LAUNCH_RATE_LIMIT_WINDOW_SECONDS=5.0`. Returns 429 with `Retry-After`
when exceeded. Single-process semantics — fine for the current 1× API
replica deployment; flagged below to swap for Redis-backed bucket on
multi-replica rollout.

**Multi-replica follow-up.** The current bucket lives in process memory.
For N>1 API replicas, replace `_launch_window` in `app/api/routes.py` with
a Redis `INCR + EXPIRE` token bucket (or use the
`fastapi-limiter` library which provides exactly this against an existing
Redis we already run). Tracked here as a non-blocking follow-up since
production deployments today are single-replica.

### ⏸️ P3-2. Authentication

**Status.** Deferred — needs product decision. Drop-in candidates:
* `fastapi-users` for username/password + JWT.
* `python-jose` + custom dependency for OIDC bearer validation against an
  existing IdP (Keycloak, Auth0, Azure AD).
* Trivial `APIKeyHeader` for service-to-service in private networks.

Whichever lands should also gate `/diagnostics/db` (currently exposes pool
internals to anyone reachable on port 8000).

### ⏸️ P3-3. mTLS for Postgres

**Status.** Deferred — orthogonal to the API/Celery work and currently
mitigated by SAN-bypass via `verify-ca` on the cert. Action item is on
the certificate generation script (`docker/postgres/`) not the application.

### ✅ P3-4. Quota: max chunks/jobs/tasks per sweep

**Status.** Done — branch `improvements/p1-p4`. New settings
`APP_MAX_{CHUNKS,JOBS,TASKS}_PER_SWEEP` (defaults 10k / 200k / 2M).
`POST /sweeps` rejects oversized payloads with 422 *before* the bulk insert
hits the DB.

### ✅ P3-5. Dead-letter visibility

**Status.** Done — already shipped as part of P0-1. `GET /sweeps/{id}/failures`
returns the DB-backed DLQ surface with both retries-exhausted failures
(`error: <ExcType>: <msg>`) and clean validation mismatches. The "DLQ" is
the `task_variants` table itself; no separate broker-side DLX.

---

## P4 — Developer experience

### ✅ P4-1. CI workflow

**Status.** Done — branch `improvements/p1-p4`. `.github/workflows/ci.yml`
runs `ruff check` (advisory until baseline lint is clean) and the fast
sqlite test suite (`pytest -k "not live_worker"`) on every push and PR.
Concurrency-group cancels in-flight runs of the same ref.

**Follow-up.** Add a second job that spins up Postgres + Redis as services
and runs `pytest -m live_worker`. Also flip ruff to `continue-on-error: false`
once the lint baseline is clean.

### ⏸️ P4-2. Type hints + mypy strict

**Status.** Deferred — meaningful undertaking. The codebase is mostly typed
but has a long tail of `Any` corners (Celery `apply_async` results, Kombu
producer pool, the prerun-hook tokens dict added in P1-3). A clean
`--strict` pass would be its own multi-day effort. The fast tests already
catch most regressions; mypy adds upside but isn't blocking.

### ✅ P4-3. Replace ad-hoc validation scripts with a unified CLI

**Status.** Done — branch `improvements/p1-p4`. New `app/validation/`
package with `__main__.py` dispatch:

```
python -m app.validation list
python -m app.validation run kill_resume
python -m app.validation run all
```

Each scenario delegates to the existing `scripts/validation_*.py:main()`
(no logic duplicated). Extra args after the scenario name are forwarded.

### ⏸️ P4-4. Snapshot `/diagnostics/db` to a time-series store during runs

**Status.** Deferred — cosmetic. Stress harness already prints pool /
pg_stat_activity snapshots at `monitor_interval` cadence. Wiring to a TSDB
or CSV-then-matplotlib is a per-deployment exercise (which TSDB? where
hosted?) and not application code.

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

---

## Status snapshot (this branch)

Shipped on `improvements/p1-p4` (merged to `main`):

| Tier | Done | Deferred |
|------|------|----------|
| P0   | P0-1, P0-2, P0-3 | — |
| P1   | P1-1, P1-3, P1-4, P1-5, P1-6 | P1-2 (ENUM) |
| P2   | P2-1, P2-2, P2-3, P2-5 | P2-4 (batched writes) |
| P3   | P3-1, P3-4, P3-5 | P3-2 (auth), P3-3 (mTLS) |
| P4   | P4-1, P4-3 | P4-2 (mypy strict), P4-4 (TSDB) |

12 of 18 tracked items shipped. The 6 deferred items are flagged inline
above with concrete rationale ("why not now") and trigger conditions
("revisit when…"). None block production deployment of the current
single-replica topology.
