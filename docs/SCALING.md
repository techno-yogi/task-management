# Scaling guide

Tier-aware sizing for the three Celery worker pools and the underlying
Postgres / Redis tiers. Use this when deciding `docker compose up --scale …`
counts, when sizing managed-service plans, or when writing autoscaler
policies (HPA, ECS service auto-scaling, etc.).

## Three tiers, three scaling axes

| Tier | Queue(s) | What runs there | Hot path? | Scale axis |
|------|----------|-----------------|-----------|------------|
| `worker_exec` | `sweep.execution` | `execute_task_variant` (the simulated work) | No — bulk parallelism | Tasks per second |
| `worker_aggregate` | `sweep.job.finalize`, `sweep.chunk.finalize` | `finalize_job_task`, `finalize_chunk_task` | Yes — cascade | Concurrent in-flight jobs |
| `worker_sweep_finalize` | `sweep.sweep.finalize` | `finalize_sweep_task`, `dispatch_next_chunk_task`, `prepare_and_dispatch_sweep_task` | **Critical path** | Concurrent sweeps |

`worker_exec` is your throughput knob — every task in a sweep runs here. The
other two tiers are about latency: they're tiny in CPU but they sit on the
critical path, so under-provisioning them shows up as "the sweep is slow"
even when `worker_exec` is idle.

## Formulas

Symbols:

* `S` = expected concurrent sweeps in flight.
* `C` = chunks per sweep (typical).
* `J` = jobs per chunk (typical).
* `T` = tasks per job (typical).
* `t_task` = mean wall time of one `execute_task_variant`.
* `target_p95_s` = your latency target for a single sweep.

### worker_exec

```
exec_child_procs   ≈ ceil( S · J · t_task / target_chunk_wall_s )
exec_containers    = ceil( exec_child_procs / concurrency_per_container )
```

With `concurrency=8` (the compose default) and `t_task = 2.0s`, sustaining
50 concurrent sweeps of `J=50, T=5` (12,500 in-flight tasks) at a 30s
target chunk wall time ⇒ `exec_child_procs ≈ ceil(50·50·2/30) = 167`,
which rounds up to **21 containers**.

### worker_aggregate

`finalize_job_task` runs once per job; `finalize_chunk_task` runs once per
chunk. Each is ~50ms.

```
aggregate_child_procs ≥ ceil( S · J · 0.05 / target_chunk_wall_s )
                      + ceil( S · 0.05 / target_chunk_wall_s )
```

In practice `--scale worker_aggregate=2` (16 child procs) handles
S ≤ 100 cleanly. Add one container per +50 concurrent sweeps.

### worker_sweep_finalize

This is the **critical path**. `dispatch_next_chunk_task` runs once per chunk
per sweep, AND it publishes the next chord (`apply_async` is the real cost
here, not the SQL). Under-provisioning shows up as sweep tail latency
spikes — see Validation Test #7 in `docs/VALIDATION.md` for the 120s stall
RCA that triggered the broker-socket-timeout fix.

```
sweep_finalize_child_procs ≥ max( S, ceil( S · C / target_sweep_wall_s · t_dispatch ) )
```

With `C=10`, `t_dispatch≈0.3s` (broker publish dominates), sustaining
S=200 concurrent launches ⇒ ~60 child procs ⇒ **8 containers** at
concurrency=8. The compose default of `--scale=2` (16 procs) handles
S ≤ 50 well; scale to 4 for S=100, 8 for S=200.

## Database connections

Total Postgres connections used:

```
api_conns          = APP_ASYNC_DB_POOL_SIZE + APP_ASYNC_DB_MAX_OVERFLOW
                   + APP_SYNC_DB_POOL_SIZE  + APP_SYNC_DB_MAX_OVERFLOW
exec_conns         = exec_containers · concurrency · (1 + 2)        # pool + overflow per child
aggregate_conns    = aggregate_containers · concurrency · (1 + 3)
sweep_final_conns  = sweep_final_containers · concurrency · (1 + 3)

total ≤ postgres `max_connections`     # currently 600 in compose
```

The compose-default `worker_exec` env caps each child at `pool=1, overflow=2`
so a single child can hold at most 3 conns. This keeps a 50-container exec
fleet at ≤ 1200 conns in the *worst-case spike*, with steady-state ~1× per
busy child.

## Redis

`celery_broker_pool_limit=200` (default) is sized for ~50 concurrent
launches; the chord canvas is 3 levels deep so each launch synchronously
holds 3 producers. Multiply pool_limit by `(S/50)` for higher S.

For Redis sizing itself: the broker DB (`/0`) holds queue messages, the
result backend (`/1`) holds chord/group result keys. Worst-case bytes ≈
`S · C · J · T · ~250B` for messages + `S · J · ~500B` for chord results.
At S=200, C=10, J=50, T=5 ⇒ ~30 MB messages + 5 MB results — comfortably
fits in the default Redis configuration.

## Health-driven autoscaling

`/health/ready` (added in P1-4) is the right probe for orchestrators:

* `livenessProbe` → `/health` (cheap, never fails unless the process is
  hung).
* `readinessProbe` → `/health/ready` (DB + broker + alembic-head check).

For HPA-style autoscaling on the API tier, scale on `request rate` or
`p95 latency` from `/metrics` (Prometheus exposed via `prometheus-fastapi-instrumentator`).

For Celery worker tiers, the standard signal is queue depth:

```
celery -A app.celery_app:celery_app inspect reserved
celery -A app.celery_app:celery_app inspect active
```

Or, more cheaply, `redis-cli LLEN sweep.execution`. Scale `worker_exec`
when `LLEN > S * concurrency * 2` for more than 30s.
