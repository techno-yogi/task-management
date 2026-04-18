#!/usr/bin/env python3
"""Validation #4: restart the API container while a sweep is running.

The dispatcher and tasks live in workers, not the API. Polling clients should temporarily
fail while the API is restarting, but the sweep itself should continue and finish.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import httpx

from docker_stress_test import build_sweep_payload  # type: ignore


async def main() -> int:
    base_url = "http://127.0.0.1:8000"
    chunks, jobs_per_chunk, tasks_per_job, sleep_ms = 4, 10, 5, 2000
    expected_total = chunks * jobs_per_chunk * tasks_per_job

    body = build_sweep_payload(
        name="validation-api-restart",
        chunks=chunks, jobs_per_chunk=jobs_per_chunk, tasks_per_job=tasks_per_job,
        sleep_ms_classes=[sleep_ms], base_seed=98_000,
    )

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        r = await client.post("/sweeps", json=body)
        r.raise_for_status()
        sweep_id = int(r.json()["id"])
        print(f"[1] created sweep id={sweep_id}", flush=True)

        r = await client.post(f"/sweeps/{sweep_id}/launch")
        r.raise_for_status()
        print(f"[2] launched: {r.json()}", flush=True)

        # Wait for chunk 1 to be done before restarting API.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            r = await client.get(f"/sweeps/{sweep_id}")
            r.raise_for_status()
            data = r.json()
            if any(c["status"] == "done" for c in data["chunks"]):
                break
            await asyncio.sleep(0.5)
        print(f"[3] chunk 1 done; restarting API container NOW", flush=True)

    restart_t0 = time.perf_counter()
    rc = subprocess.run(["docker", "compose", "restart", "api"], capture_output=True, text=True)
    print(f"[3.1] api restart returned in {time.perf_counter() - restart_t0:.1f}s, "
          f"stderr_tail={rc.stderr.strip().splitlines()[-3:]}", flush=True)

    # Poll until API responds again, then poll for done.
    api_came_back = False
    deadline = time.monotonic() + 240
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        last_status = None
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"/sweeps/{sweep_id}")
                r.raise_for_status()
            except Exception:
                if not api_came_back:
                    print("  ...API still down, waiting", flush=True)
                await asyncio.sleep(2.0)
                continue
            if not api_came_back:
                api_came_back = True
                print(f"[4] API reachable again after {time.perf_counter() - restart_t0:.1f}s", flush=True)
            data = r.json()
            if data["status"] != last_status:
                done_chunks = sum(1 for c in data["chunks"] if c["status"] == "done")
                print(f"  status={data['status']}  chunks_done={done_chunks}/{len(data['chunks'])}", flush=True)
                last_status = data["status"]
            if data["status"] == "done":
                if data["total_tasks"] != expected_total:
                    print(f"FAIL: total_tasks={data['total_tasks']} expected={expected_total}", flush=True)
                    return 1
                print(f"[5] OK: sweep finished cleanly with {data['total_tasks']} tasks", flush=True)
                return 0
            await asyncio.sleep(1.0)

    print("FAIL: sweep did not finish within 240s after API restart", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
