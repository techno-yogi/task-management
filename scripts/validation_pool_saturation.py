#!/usr/bin/env python3
"""Validation #9: deliberately oversaturate the API DB pools and verify behavior.

API pool sizes: sync=40+60 overflow, async=55+50 overflow.
We blast 500 concurrent GET /sweeps/{id} (async pool) + 500 concurrent GET /diagnostics/db
(sync pool) for a few seconds. Expectations:
  - No 5xx (FastAPI/SQLAlchemy should queue waiters under the pool ceiling, not error).
  - No leaked connections after the storm (pool returns to checkedout=0).
  - Latency increases under saturation but the API stays responsive.
"""
from __future__ import annotations

import asyncio
import statistics
import time

import httpx


async def main() -> int:
    base_url = "http://127.0.0.1:8000"

    # Get baseline.
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        r = await client.get("/diagnostics/db"); r.raise_for_status()
        baseline = r.json()
        print(f"baseline: pool={baseline['pool']}", flush=True)

        # Pick a known sweep id to GET. Use 754 (recently completed).
        sweep_id = 754
        r = await client.get(f"/sweeps/{sweep_id}")
        if r.status_code != 200:
            sweep_id = 1
            r = await client.get(f"/sweeps/{sweep_id}")
            r.raise_for_status()

        # Storm.
        N_ASYNC = 500
        N_SYNC = 500
        print(f"firing {N_ASYNC} async-pool requests + {N_SYNC} sync-pool requests in parallel...", flush=True)

        latencies_async: list[float] = []
        latencies_sync: list[float] = []
        errors: dict[str, int] = {}

        sem_async = asyncio.Semaphore(N_ASYNC)
        sem_sync = asyncio.Semaphore(N_SYNC)

        async def hit_async(_: int) -> None:
            async with sem_async:
                t0 = time.perf_counter()
                try:
                    r = await client.get(f"/sweeps/{sweep_id}", timeout=30.0)
                    latencies_async.append(time.perf_counter() - t0)
                    if r.status_code >= 400:
                        errors[f"async_{r.status_code}"] = errors.get(f"async_{r.status_code}", 0) + 1
                except Exception as e:  # noqa: BLE001
                    errors[f"async_{type(e).__name__}"] = errors.get(f"async_{type(e).__name__}", 0) + 1

        async def hit_sync(_: int) -> None:
            async with sem_sync:
                t0 = time.perf_counter()
                try:
                    r = await client.get("/diagnostics/db", timeout=30.0)
                    latencies_sync.append(time.perf_counter() - t0)
                    if r.status_code >= 400:
                        errors[f"sync_{r.status_code}"] = errors.get(f"sync_{r.status_code}", 0) + 1
                except Exception as e:  # noqa: BLE001
                    errors[f"sync_{type(e).__name__}"] = errors.get(f"sync_{type(e).__name__}", 0) + 1

        t0 = time.perf_counter()
        await asyncio.gather(
            *[hit_async(i) for i in range(N_ASYNC)],
            *[hit_sync(i) for i in range(N_SYNC)],
        )
        elapsed = time.perf_counter() - t0
        print(f"storm complete in {elapsed:.1f}s", flush=True)

        def stats(name: str, lats: list[float]) -> None:
            if not lats:
                print(f"  {name}: no data", flush=True); return
            lats.sort()
            print(
                f"  {name}: count={len(lats)} p50={lats[len(lats)//2]:.3f} "
                f"p95={lats[int(len(lats)*0.95)]:.3f} p99={lats[int(len(lats)*0.99)]:.3f} "
                f"max={lats[-1]:.3f} mean={statistics.mean(lats):.3f}",
                flush=True,
            )

        stats("async (/sweeps)", latencies_async)
        stats("sync (/diagnostics/db)", latencies_sync)
        print(f"errors: {errors or '(none)'}", flush=True)

        # Settle, then verify recovery.
        await asyncio.sleep(5)
        r = await client.get("/diagnostics/db"); r.raise_for_status()
        post = r.json()
        print(f"recovered: pool={post['pool']}", flush=True)

        ok = (
            errors == {}
            and post["pool"]["checkedout"] == 0
            and post["pool"]["overflow"] == baseline["pool"]["overflow"]
        )
        print(f"PASS={ok}", flush=True)
        return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
