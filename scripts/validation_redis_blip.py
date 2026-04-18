#!/usr/bin/env python3
"""Validation #5: pause Redis for ~5s mid-sweep, unpause, verify the cascade survives.

Kombu retries broker connections, so workers should re-attach and pick up where they
left off. Tasks may be re-delivered; with task_acks_late=True and idempotent task
implementations this should produce the right final state.
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
        name="validation-redis-blip",
        chunks=chunks, jobs_per_chunk=jobs_per_chunk, tasks_per_job=tasks_per_job,
        sleep_ms_classes=[sleep_ms], base_seed=97_000,
    )

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        r = await client.post("/sweeps", json=body); r.raise_for_status()
        sweep_id = int(r.json()["id"])
        print(f"[1] created sweep id={sweep_id}", flush=True)

        r = await client.post(f"/sweeps/{sweep_id}/launch"); r.raise_for_status()
        print(f"[2] launched: {r.json()}", flush=True)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            r = await client.get(f"/sweeps/{sweep_id}"); r.raise_for_status()
            data = r.json()
            if any(c["status"] == "done" for c in data["chunks"]):
                break
            await asyncio.sleep(0.5)
        print(f"[3] chunk 1 done; pausing redis for 5s NOW", flush=True)

        subprocess.run(["docker", "compose", "pause", "redis"], capture_output=True, text=True)
        await asyncio.sleep(5.0)
        subprocess.run(["docker", "compose", "unpause", "redis"], capture_output=True, text=True)
        print(f"[4] redis unpaused after 5s", flush=True)

        deadline = time.monotonic() + 240
        last_status = None
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"/sweeps/{sweep_id}", timeout=10.0); r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"  poll err: {type(e).__name__}", flush=True)
                await asyncio.sleep(1.0); continue
            if data["status"] != last_status:
                done = sum(1 for c in data["chunks"] if c["status"] == "done")
                print(f"  status={data['status']}  chunks_done={done}/{len(data['chunks'])}", flush=True)
                last_status = data["status"]
            if data["status"] == "done":
                if data["total_tasks"] != expected_total:
                    print(f"FAIL: total_tasks={data['total_tasks']} expected={expected_total}", flush=True)
                    return 1
                print(f"[5] OK: sweep finished after Redis blip with {data['total_tasks']} tasks", flush=True)
                return 0
            await asyncio.sleep(1.0)

    print("FAIL: sweep did not finish within 240s after Redis blip", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
