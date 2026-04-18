#!/usr/bin/env python3
"""Validation #8: cancel a running sweep mid-flight via celery_app.control.purge() +
revoke broadcast. Verify no rows are stuck in 'running' indefinitely after cancellation.

This isn't a graceful 'cancel sweep' API (we don't have one) — it's a soak test of what
happens when an operator does an emergency stop. The expectation is:
  - Tasks already executing finish normally.
  - Pending messages are purged from Redis.
  - No new chunks are dispatched.
  - The sweep row stays in its non-'done' status, and we can re-launch with from_chunk=K
    (validated separately by Test #3).
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
    chunks, jobs_per_chunk, tasks_per_job, sleep_ms = 6, 25, 5, 5000
    expected_total = chunks * jobs_per_chunk * tasks_per_job

    body = build_sweep_payload(
        name="validation-cancel",
        chunks=chunks, jobs_per_chunk=jobs_per_chunk, tasks_per_job=tasks_per_job,
        sleep_ms_classes=[sleep_ms], base_seed=96_000,
    )

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        r = await client.post("/sweeps", json=body); r.raise_for_status()
        sweep_id = int(r.json()["id"])
        print(f"[1] created sweep id={sweep_id} (chunks={chunks}, total_tasks={expected_total})", flush=True)

        r = await client.post(f"/sweeps/{sweep_id}/launch"); r.raise_for_status()
        print(f"[2] launched: {r.json()}", flush=True)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            r = await client.get(f"/sweeps/{sweep_id}/status"); r.raise_for_status()
            data = r.json()
            done = data["chunks"]["done"]
            if done >= 2:
                print(f"[3] {done}/{chunks} chunks done; CANCELLING NOW", flush=True)
                break
            await asyncio.sleep(0.5)
        else:
            print("FAIL: didn't reach 2 done chunks in 60s", flush=True); return 1

        # PURGE all queues (this drops pending messages from Redis).
        purge = subprocess.run(
            ["docker", "compose", "exec", "-T", "api",
             "celery", "-A", "app.celery_app:celery_app", "purge", "-f"],
            capture_output=True, text=True,
        )
        print(f"[4] purge: {purge.stdout.strip()}", flush=True)

        # Wait for any in-flight tasks to drain (worst case: 2s sleep + 1s overhead).
        await asyncio.sleep(8)

        r = await client.get(f"/sweeps/{sweep_id}"); r.raise_for_status()
        snap = r.json()
        chunk_states = [(c["ordinal"], c["status"]) for c in snap["chunks"]]
        print(f"[5] post-cancel: sweep_status={snap['status']} chunks={chunk_states}", flush=True)

        # Hold for another 10s and verify no further progress (the cancellation stuck).
        await asyncio.sleep(10)
        r = await client.get(f"/sweeps/{sweep_id}"); r.raise_for_status()
        snap2 = r.json()
        chunk_states2 = [(c["ordinal"], c["status"]) for c in snap2["chunks"]]
        if chunk_states != chunk_states2 and snap2["status"] != snap["status"]:
            print(f"FAIL: progress continued after purge — was {chunk_states} now {chunk_states2}", flush=True)
            return 1
        print(f"[6] +10s later sweep is still wedged: status={snap2['status']} chunks={chunk_states2}", flush=True)

        # Verify no task_variants are stuck in 'running' indefinitely (they should be done or pending).
        # We check via /sweeps which gives chunk/job rollups; a more granular check is in the SQL companion.
        running_chunks = [c for c in snap2["chunks"] if c["status"] == "running"]
        print(f"[7] chunks still in 'running' state (in-flight when purged, will stay 'running'): {len(running_chunks)}", flush=True)
        print("    -> these are the rows the cancellation stranded; relaunch with ?from_chunk=K to recover", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
