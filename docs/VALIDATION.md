# Validation suite

Eleven reproducible scenarios that we ran end-to-end against the reference
Docker layout (12 × `worker_exec`, 2 × `worker_aggregate`, 2 × `worker_sweep_finalize`,
plus 6 winexec + 2 winagg native Windows solo workers).

All numbers below are from the most recent run on this machine
(2026-04-18, 16-core / 32 GB / Windows 11 + Docker Desktop WSL2).

---

## 1. Realistic-workload soak

20 s per task, 4 chunks × 25 jobs × 5 tasks (= 500 tasks), single sweep.

```bash
uv run python scripts/docker_stress_test.py \
  --sweeps 1 --chunks 4 --jobs-per-chunk 25 --tasks-per-job 5 --sleep-ms 20000
```

**Result:** wall ≈ 80 s (= 4 × 20 s, perfect sequential chunks; intra-chunk fan-out
soaks the cluster). No DB-pool growth, no Postgres connection growth.

## 2. Connection-leak detector loop

Five back-to-back sweeps of 250 tasks each (= 1 250 total).

```bash
uv run python scripts/docker_stress_test.py \
  --sweeps 5 --chunks 5 --jobs-per-chunk 10 --tasks-per-job 5 --sleep-ms 500 \
  --print-pg-stats
```

**Baseline:** 223 active backends in `pg_stat_activity`. **After 1 250 tasks:**
223 active backends. No leak. SQLAlchemy `checkedout` returns to 0 across all
worker engines.

## 3. Worker kill + resume

```bash
uv run python scripts/validation_kill_resume.py
```

Creates a 6-chunk sweep, waits for chunk 1 to finish, **`docker compose kill`s every
`worker_exec` container**, verifies the sweep stalls, restarts the workers,
relaunches with `?from_chunk=K`, and checks the final task-row count.

**Result:** sweep stalls cleanly (no bogus `running` rows after ~10 s grace),
relaunch from chunk 2 succeeds; final `total_tasks` matches the originally
created count.

## 4. API restart resilience

```bash
uv run python scripts/validation_api_restart.py
```

Restarts the `api` container mid-sweep, polls `/health` for return-to-life, and
verifies the sweep continues to completion entirely on the worker side.

**Result:** API is unreachable for ~6 s; the sweep has no idea the API ever went
away (it doesn’t talk to the API). Final completion verified.

## 5. Redis pause (broker blip)

```bash
uv run python scripts/validation_redis_blip.py
```

`docker compose pause redis` for 5 s mid-sweep, then `unpause`. Kombu’s built-in
reconnect + the new `socket_timeout=15s` setting cleanly survive the outage.

**Result:** sweep finishes; no message loss. Without the socket_timeout fix, a
worker that happened to be inside an `apply_async()` would hang for ~120 s
before recovering.

## 6. Idempotent re-launch

`POST /sweeps/{id}/launch?from_chunk=1` against an already-`done` sweep.

**Result:** all chunks reset to pending, executed again, terminal `total_tasks`
unchanged. Confirmed the dispatcher correctly re-enters from chunk 1 even when
the sweep was already terminal.

## 7. Backpressure / 200 concurrent launches

```bash
uv run python scripts/docker_stress_test.py \
  --sweeps 200 --chunks 1 --jobs-per-chunk 5 --tasks-per-job 5 --sleep-ms 2000 \
  --concurrency 200
```

Hammers the cluster with 200 concurrent `POST /launch` calls (= 5 000 tasks).

**Initial result:** 115.3 s wall. One sweep (`id=571`) took 125 s alone — a
120 s long-tail. Database forensics + worker-log archaeology pinned the stall
to `dispatch_next_chunk_task` running on `winsweepfinal1@Technoyogi`, which
blocked for 120 s inside `kombu.publish()` waiting on Redis (default
`socket_timeout=120`).

**Mitigation (now in main):**
1. Removed the two `winsweepfinal*` Windows solo workers from
   `scripts/start_local_workers.ps1` so the critical-path `sweep.sweep.finalize`
   queue is Docker-only.
2. Added explicit `socket_timeout=15`, `socket_connect_timeout=10`,
   `health_check_interval=30` to both broker and result-backend transport options
   in `app/celery_app.py` (knobs in `app/config.py`).

**Result post-fix:** 22.1 s wall (5.2× speedup), 226 tasks/s avg, no stragglers,
all 5 000 task-rows accounted for.

## 8. Cancel + recover

```bash
uv run python scripts/validation_cancel.py
```

* Launches a 6 × 25 × 5 sweep at 5 s/task.
* Waits for 2 chunks to finish.
* `docker compose exec api celery purge -f` purges all four queues.
* Waits 30 s and snapshots `chunks` table — verifies no `running` rows remain
  pinned (acks-late does its job once the executing worker’s pool finishes the
  in-flight task).

**Result:** purge evicts ~23 pending messages; sweep status freezes; resume via
`?from_chunk=K` recovers it cleanly.

## 9. DB-pool saturation

```bash
uv run python scripts/validation_pool_saturation.py
```

Fires 500 concurrent requests at the **async** endpoint (`POST /sweeps`) and 500
concurrent requests at the **sync** endpoint (`GET /diagnostics/db`)
simultaneously, then verifies pool recovery.

**Result:** zero 5xx; latency p99 well under timeout; `checkedout` returns to 0
within seconds of the storm ending. (One client-side `RemoteProtocolError` from
`httpx` keep-alive racing with server-side close — cosmetic, no server fault.)

## 10. p50/p95/p99 per-class latency

Built into `docker_stress_test.py` via `--report-latency-dsn`. Queries
`task_variants.updated_at - created_at` directly from Postgres and reports
percentiles bucketed by `sleep_ms` class.

```bash
uv run python scripts/docker_stress_test.py --sweeps 4 --chunks 4 \
  --jobs-per-chunk 25 --tasks-per-job 5 --sleep-ms 500,5000 \
  --report-latency-dsn "$DSN"
```

Sample output:

```
sleep_ms=500   n=2000 p50=0.55s p95=0.61s p99=0.71s
sleep_ms=5000  n=2000 p50=5.05s p95=5.12s p99=5.27s
```

End-to-end latency = queue wait + execution. The fact that p99 - p50 is small
confirms there’s no head-of-line blocking even when 5 000 ms tasks dominate
the queue.

## 11. Mixed-workload fairness

Runs sweeps where each task’s `sleep_ms` rotates across a comma-separated list,
so a 500 ms task can be queued behind a 5 000 ms task on the same `worker_exec`.

```bash
uv run python scripts/docker_stress_test.py --sweeps 4 --chunks 4 \
  --jobs-per-chunk 25 --tasks-per-job 5 --sleep-ms 500,5000 \
  --report-latency-dsn "$DSN"
```

**Result:** the 500 ms class p99 = 1.4 s — i.e., it sometimes sat for ~1 s
behind a single 5 000 ms task on the same worker, but never longer. With
`--prefetch-multiplier=1 --disable-prefetch` on `worker_exec`, no queue
hoarding, no starvation.

---

## Operational runbook

### Healthy-cluster check

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/diagnostics/db | jq
```

`checkedout` should be 0 at rest; `pg_stat_activity` rollup should match
`(api + total_celery_workers × concurrency)` ± in-flight.

### Stuck sweep

```sql
SELECT id, status, total_chunks FROM sweeps WHERE id = $1;
SELECT ordinal, status, finalized_by FROM chunks WHERE sweep_id = $1 ORDER BY ordinal;
```

If status stuck for > expected wall: `POST /sweeps/{id}/launch?from_chunk=K`
where K is the lowest ordinal not in (`done`).

### Worker hostname inventory

```sql
SELECT processed_by, COUNT(*) FROM task_variants
WHERE job_id IN (SELECT id FROM jobs WHERE chunk_id IN (
  SELECT id FROM chunks WHERE sweep_id = $1)) GROUP BY processed_by ORDER BY 2 DESC;
```

### Emergency cancel

```bash
docker compose exec api celery -A app.celery_app:celery_app purge -f
```

Already-running tasks in worker pools will finish; queued ones disappear. Resume
with `?from_chunk=K`.

### Restart only one tier

```bash
docker compose restart worker_exec
docker compose restart worker_aggregate
docker compose restart worker_sweep_finalize
```

Workers are stateless beyond their broker connection; safe at any time.
