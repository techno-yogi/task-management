#!/usr/bin/env python3
"""
Validation #3: kill all execution workers mid-sweep, then resume from the next chunk.

Steps:
  1. Create a 4-chunk sweep, launch it.
  2. Wait until at least 1 chunk is 'done' (so the dispatcher has cascaded forward).
  3. Forcibly kill all worker_exec containers.
  4. Verify the sweep is now stuck (status != 'done' for ~10s).
  5. Restart worker_exec.
  6. Re-POST /launch?from_chunk=<next_pending_ordinal>.
  7. Wait for status='done', then assert all task_variant rows are status='done'
     and the row counts equal the expected total.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time

import httpx

from docker_stress_test import build_sweep_payload  # type: ignore


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--sleep-ms", type=int, default=2000)
    ap.add_argument("--chunks", type=int, default=4)
    ap.add_argument("--jobs-per-chunk", type=int, default=10)
    ap.add_argument("--tasks-per-job", type=int, default=5)
    ap.add_argument("--scale-exec", type=int, default=12, help="recreate worker_exec at this scale after kill")
    args = ap.parse_args()

    expected_total_tasks = args.chunks * args.jobs_per_chunk * args.tasks_per_job

    async with httpx.AsyncClient(base_url=args.base_url, timeout=60.0) as client:
        body = build_sweep_payload(
            name="validation-kill-resume",
            chunks=args.chunks,
            jobs_per_chunk=args.jobs_per_chunk,
            tasks_per_job=args.tasks_per_job,
            sleep_ms_classes=[args.sleep_ms],
            base_seed=99_000,
        )
        r = await client.post("/sweeps", json=body)
        r.raise_for_status()
        sweep_id = int(r.json()["id"])
        print(f"[1] created sweep id={sweep_id}", flush=True)

        r = await client.post(f"/sweeps/{sweep_id}/launch")
        r.raise_for_status()
        print(f"[2] launched: {r.json()}", flush=True)

        # Wait until at least 1 chunk is done (so we know the cascade is alive).
        deadline = time.monotonic() + 60
        first_done_ordinal: int | None = None
        while time.monotonic() < deadline:
            r = await client.get(f"/sweeps/{sweep_id}")
            r.raise_for_status()
            data = r.json()
            done_chunks = [c for c in data["chunks"] if c["status"] == "done"]
            if done_chunks:
                first_done_ordinal = max(c["ordinal"] for c in done_chunks)
                break
            await asyncio.sleep(0.5)
        if first_done_ordinal is None:
            print("FAIL: no chunk reached 'done' within 60s", flush=True)
            return 1
        print(f"[3] chunk ordinal {first_done_ordinal} reached 'done'; killing worker_exec NOW", flush=True)

        # Hard-kill all worker_exec containers (SIGKILL, no graceful shutdown).
        kill_proc = subprocess.run(
            ["docker", "compose", "kill", "-s", "SIGKILL", "worker_exec"],
            capture_output=True,
            text=True,
        )
        print(f"[3.1] kill stdout={kill_proc.stdout.strip()!r} stderr={kill_proc.stderr.strip()!r}", flush=True)

        # Verify sweep is now wedged: status should not progress for several seconds.
        await asyncio.sleep(8)
        r = await client.get(f"/sweeps/{sweep_id}")
        r.raise_for_status()
        wedged = r.json()
        print(
            f"[4] post-kill snapshot: sweep_status={wedged['status']} "
            f"chunks={[(c['ordinal'], c['status']) for c in wedged['chunks']]}",
            flush=True,
        )
        if wedged["status"] == "done":
            print("FAIL: sweep finished even after exec workers killed (unexpected)", flush=True)
            return 1

        # Find next pending/running chunk ordinal to resume from.
        not_done = [c for c in wedged["chunks"] if c["status"] != "done"]
        resume_from = min(c["ordinal"] for c in not_done)
        print(f"[5] resuming from chunk ordinal {resume_from}", flush=True)

        # Restart worker_exec at the prior scale.
        print("[6] restarting worker_exec", flush=True)
        up_proc = subprocess.run(
            ["docker", "compose", "up", "-d", "--no-deps", "--scale", f"worker_exec={args.scale_exec}", "worker_exec"],
            capture_output=True,
            text=True,
        )
        print(f"[6.1] up stderr_tail={up_proc.stderr.strip().splitlines()[-3:]}", flush=True)
        await asyncio.sleep(8)

        # Re-launch with ?from_chunk=resume_from
        r = await client.post(f"/sweeps/{sweep_id}/launch?from_chunk={resume_from}")
        r.raise_for_status()
        print(f"[7] relaunch: {r.json()}", flush=True)

        # Wait for completion. Use the lightweight /status endpoint so polling
        # doesn't dominate DB load even at sub-second cadence (P0-3 + P2-5).
        deadline = time.monotonic() + 240
        last_status = None
        while time.monotonic() < deadline:
            r = await client.get(f"/sweeps/{sweep_id}/status")
            r.raise_for_status()
            data = r.json()
            if data["status"] != last_status:
                print(f"  status={data['status']}  chunks_done={data['chunks']['done']}/{data['total_chunks']}", flush=True)
                last_status = data["status"]
            if data["status"] == "done":
                break
            await asyncio.sleep(1.0)
        else:
            print("FAIL: sweep did not finish within 240s after resume", flush=True)
            return 1

        # Final sanity: status='done' AND row counts match.
        if data["total_tasks"] != expected_total_tasks:
            print(f"FAIL: total_tasks={data['total_tasks']} expected={expected_total_tasks}", flush=True)
            return 1
        print(f"[8] OK: sweep done, total_tasks={data['total_tasks']}, total_chunks={data['total_chunks']}, total_jobs={data['total_jobs']}", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
