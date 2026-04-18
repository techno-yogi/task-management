#!/usr/bin/env python3
"""
Drive the real Docker stack (Postgres TLS + Redis + Celery + API) with heavy concurrent load.

Example:
  docker compose up -d --build --scale worker_exec=25
  uv run python scripts/docker_stress_test.py --base-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any


def build_sweep_payload(
    *,
    name: str,
    chunks: int,
    jobs_per_chunk: int,
    tasks_per_job: int,
    sleep_ms_classes: list[int],
    base_seed: int,
) -> dict[str, Any]:
    """Build a sweep payload. ``sleep_ms_classes`` rotates per-job so the same sweep can
    contain a mix of fast and slow jobs (for the mixed-workload validation test)."""
    payload: dict[str, Any] = {"name": name, "chunks": []}
    base = base_seed
    job_counter = 0
    for c in range(chunks):
        chunk: dict[str, Any] = {"ordinal": c + 1, "jobs": []}
        for j in range(jobs_per_chunk):
            sleep_ms = sleep_ms_classes[job_counter % len(sleep_ms_classes)]
            job_counter += 1
            shared = {"base_value": base, "step": 3, "offset": 0, "sleep_ms": sleep_ms}
            job: dict[str, Any] = {"name": f"job-{c + 1}-{j + 1}-s{sleep_ms}", "shared_context": shared, "tasks": []}
            for t in range(tasks_per_job):
                expected = base + t * 3
                job["tasks"].append({"point_idx": t, "name": f"t{t}", "expected_value": expected})
            chunk["jobs"].append(job)
            base += 50
        payload["chunks"].append(chunk)
    return payload


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def report_task_latencies(sweep_ids: list[int], dsn: str) -> None:
    """Pull per-task end-to-end latency from Postgres and print percentile distributions.
    Buckets by sleep_ms class (extracted from job.shared_context_json) so mixed-workload
    runs surface starvation: short tasks should retain low latency even when interleaved
    with long tasks on the same queues."""
    try:
        import psycopg
    except ImportError:
        print("[latency] psycopg not available; skipping latency report", flush=True)
        return

    sql = """
      SELECT
        (j.shared_context_json::json ->> 'sleep_ms')::int AS sleep_ms,
        EXTRACT(EPOCH FROM (tv.updated_at - tv.created_at)) AS latency_s
      FROM task_variants tv
      JOIN jobs j ON j.id = tv.job_id
      JOIN chunks ch ON ch.id = j.chunk_id
      WHERE ch.sweep_id = ANY(%s)
        AND tv.status = 'done'
    """
    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, (sweep_ids,))
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"[latency] db query failed: {type(exc).__name__}: {exc}", flush=True)
        return

    if not rows:
        print("[latency] no rows returned", flush=True)
        return

    buckets: dict[int, list[float]] = {}
    for sleep_ms, latency_s in rows:
        buckets.setdefault(int(sleep_ms), []).append(float(latency_s))

    print("[latency] per-task end-to-end (queue+exec, seconds), by sleep_ms class:", flush=True)
    print(f"  {'sleep_ms':>9} {'count':>7} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'mean':>8}", flush=True)
    for sleep_ms in sorted(buckets):
        vals = sorted(buckets[sleep_ms])
        mean = sum(vals) / len(vals)
        print(
            f"  {sleep_ms:>9} {len(vals):>7} "
            f"{_percentile(vals, 50):>8.3f} {_percentile(vals, 95):>8.3f} "
            f"{_percentile(vals, 99):>8.3f} {vals[-1]:>8.3f} {mean:>8.3f}",
            flush=True,
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Stress-test sweep API against Docker Postgres/Redis/Celery.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API root URL")
    parser.add_argument("--sweeps", type=int, default=45, help="Number of sweeps to create and run")
    parser.add_argument("--chunks", type=int, default=1)
    parser.add_argument("--jobs-per-chunk", type=int, default=3)
    parser.add_argument("--tasks-per-job", type=int, default=4)
    parser.add_argument(
        "--sleep-ms",
        type=str,
        default="500",
        help="Per-task worker sleep (simulated work). Single int OR comma-separated mix "
        "(e.g. '500,5000') that rotates across jobs to validate fairness with mixed task lengths.",
    )
    parser.add_argument(
        "--report-latency-dsn",
        type=str,
        default=None,
        help="If set, after the run query Postgres at this DSN and print per-task latency "
        "percentiles bucketed by sleep_ms class. Example: "
        "postgresql://app:app@localhost:55432/appdb?sslmode=disable",
    )
    parser.add_argument("--launch-timeout", type=float, default=720.0, help="Max time to wait for all sweeps to reach status=done")
    parser.add_argument("--client-timeout", type=float, default=120.0)
    parser.add_argument(
        "--create-concurrency",
        type=int,
        default=40,
        help="Max concurrent POST /sweeps (async handlers; avoids overwhelming DB pool)",
    )
    parser.add_argument(
        "--launch-concurrency",
        type=int,
        default=64,
        help="Max concurrent POST /launch — non-blocking 202 enqueue, so this can be high",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds between status polls per sweep",
    )
    parser.add_argument(
        "--monitor-interval",
        type=float,
        default=2.0,
        help="Seconds between throughput-progress prints",
    )
    args = parser.parse_args()

    import httpx

    sleep_ms_classes = [int(x) for x in args.sleep_ms.split(",") if x.strip()]
    if not sleep_ms_classes:
        print("FAIL: --sleep-ms must contain at least one integer", flush=True)
        return 1

    total_tasks = args.sweeps * args.chunks * args.jobs_per_chunk * args.tasks_per_job
    print(
        f"Target {args.base_url}: {args.sweeps} sweeps, "
        f"{args.chunks}x{args.jobs_per_chunk}x{args.tasks_per_job} tasks/sweep => {total_tasks} task rows "
        f"(sleep_ms classes={sleep_ms_classes})",
        flush=True,
    )

    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
    timeout = httpx.Timeout(args.client_timeout, connect=30.0)
    async with httpx.AsyncClient(base_url=args.base_url, limits=limits, timeout=timeout) as client:
        r = await client.get("/health")
        r.raise_for_status()
        print("health:", r.json(), flush=True)

        t0 = time.perf_counter()
        create_sem = asyncio.Semaphore(args.create_concurrency)
        launch_sem = asyncio.Semaphore(args.launch_concurrency)

        async def create_one(i: int) -> int:
            body = build_sweep_payload(
                name=f"stress-{i}",
                chunks=args.chunks,
                jobs_per_chunk=args.jobs_per_chunk,
                tasks_per_job=args.tasks_per_job,
                sleep_ms_classes=sleep_ms_classes,
                base_seed=10_000 + i * 1_000,
            )
            async with create_sem:
                resp = await client.post("/sweeps", json=body)
            resp.raise_for_status()
            return int(resp.json()["id"])

        sweep_ids = await asyncio.gather(*[create_one(i) for i in range(args.sweeps)])
        print(f"Created {len(sweep_ids)} sweeps in {time.perf_counter() - t0:.1f}s", flush=True)

        async def launch_one(sid: int) -> dict[str, Any]:
            async with launch_sem:
                resp = await client.post(f"/sweeps/{sid}/launch")
            resp.raise_for_status()
            return resp.json()

        t1 = time.perf_counter()
        launch_results = await asyncio.gather(*[launch_one(sid) for sid in sweep_ids])
        print(f"Enqueued all chord launches in {time.perf_counter() - t1:.1f}s", flush=True)

        bad = [r for r in launch_results if r.get("status") not in ("queued", "done")]
        if bad:
            print("FAIL: non-queued launches:", bad[:5], "...", flush=True)
            return 1

        # Poll GET /sweeps/{id}/status for status=='done'. The /status endpoint is
        # ~50x cheaper than /sweeps/{id} on large sweeps because it skips the
        # full chunk/job/task graph eager-load (P0-3 + P2-5 in ROADMAP).
        expected_total = args.chunks * args.jobs_per_chunk * args.tasks_per_job
        done_set: set[int] = set()
        last_print = {"t": time.perf_counter(), "count": 0}
        stop_monitor = asyncio.Event()

        async def wait_done(sid: int) -> None:
            deadline = time.monotonic() + args.launch_timeout
            while True:
                if sid in done_set:
                    return
                resp = await client.get(f"/sweeps/{sid}/status")
                resp.raise_for_status()
                data = resp.json()
                if data["status"] == "done":
                    assert data["total_tasks"] == expected_total
                    done_set.add(sid)
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"sweep {sid} did not finish: status={data['status']}")
                await asyncio.sleep(args.poll_interval)

        # Track task-level progress aggregated across all sweeps so we can see
        # work rate even when no individual sweep has hit `done` yet (a 500-task
        # sweep can sit at status='pending' for 20s while still doing 350 tasks).
        last_task_count = {"v": 0}

        async def monitor() -> None:
            while not stop_monitor.is_set():
                try:
                    await asyncio.wait_for(stop_monitor.wait(), timeout=args.monitor_interval)
                except asyncio.TimeoutError:
                    pass
                now = time.perf_counter()
                count = len(done_set)
                delta_t = now - last_print["t"]
                delta_c = count - last_print["count"]
                sweeps_per_s = delta_c / delta_t if delta_t > 0 else 0.0

                # Cheap aggregate: sum tasks.done across all our sweeps via /status.
                # 500-sweep N=100 sample is fine — /status is single-digit ms each.
                # We sample a fixed quorum (50 sweeps spread across the range) rather
                # than all of them, to keep monitor overhead bounded.
                sample_ids = sweep_ids[:: max(1, len(sweep_ids) // 50)]
                tasks_done_total = 0
                tasks_running_total = 0
                try:
                    snaps = await asyncio.gather(
                        *[client.get(f"/sweeps/{sid}/status", timeout=5.0) for sid in sample_ids],
                        return_exceptions=True,
                    )
                    valid = [s for s in snaps if not isinstance(s, BaseException) and s.status_code == 200]
                    for s in valid:
                        d = s.json()
                        tasks_done_total += d["tasks"]["done"]
                        tasks_running_total += d["tasks"]["running"]
                    # Extrapolate to full population.
                    if valid:
                        scale = len(sweep_ids) / len(valid)
                        tasks_done_total = int(tasks_done_total * scale)
                        tasks_running_total = int(tasks_running_total * scale)
                except Exception:
                    pass

                tasks_per_s_real = (tasks_done_total - last_task_count["v"]) / delta_t if delta_t > 0 else 0.0

                pool_snapshot = ""
                try:
                    diag_resp = await client.get("/diagnostics/db", timeout=5.0)
                    if diag_resp.status_code == 200:
                        diag = diag_resp.json()
                        pool = diag["pool"]
                        states = {row["state"]: row["connections"] for row in diag["pg_stat_activity"]}
                        pool_snapshot = (
                            f"  pool[in={pool['checkedin']} out={pool['checkedout']} "
                            f"overflow={pool['overflow']} size={pool['size']}]"
                            f"  pg[active={states.get('active', 0)} idle={states.get('idle', 0)}"
                            f" idle_in_tx={states.get('idle in transaction', 0)}]"
                        )
                except Exception as exc:  # noqa: BLE001
                    pool_snapshot = f"  diag_error={type(exc).__name__}"
                print(
                    f"[monitor +{now - t2:6.1f}s] sweeps_done={count}/{len(sweep_ids)}"
                    f"  tasks~done={tasks_done_total}/{total_tasks} run={tasks_running_total}"
                    f"  rate={tasks_per_s_real:6.0f} tasks/s ({sweeps_per_s:5.2f} sweeps/s)"
                    f"{pool_snapshot}",
                    flush=True,
                )
                last_print["t"] = now
                last_print["count"] = count
                last_task_count["v"] = tasks_done_total

        t2 = time.perf_counter()
        last_print["t"] = t2
        monitor_task = asyncio.create_task(monitor())
        try:
            await asyncio.gather(*[wait_done(sid) for sid in sweep_ids])
        finally:
            stop_monitor.set()
            await monitor_task
        elapsed = time.perf_counter() - t2
        total_tasks_done = expected_total * len(sweep_ids)
        print(
            f"All {len(sweep_ids)} sweeps done in {elapsed:.1f}s "
            f"=> avg {total_tasks_done / elapsed:.1f} tasks/s",
            flush=True,
        )

        diag = await client.get("/diagnostics/db")
        diag.raise_for_status()
        print("diagnostics/db:", diag.json(), flush=True)

        metrics = await client.get("/metrics")
        metrics.raise_for_status()
        if "celery_task_succeeded_total" not in metrics.text:
            print("WARN: missing celery_task_succeeded_total in /metrics", flush=True)

    if args.report_latency_dsn:
        report_task_latencies(sweep_ids, args.report_latency_dsn)

    print(f"OK — stress finished in {time.perf_counter() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
