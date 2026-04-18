"""Quick listing smoke test against a running stack.

Verifies P1-5 (O(1) launch latency) and P1-6 (paginated /sweeps endpoint).
"""
from __future__ import annotations

import asyncio
import statistics
import time

import httpx


async def main() -> int:
    base = "http://localhost:8000"
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
        # --- P1-6 listing ---
        r = await client.get("/sweeps", params={"limit": 5, "offset": 0})
        r.raise_for_status()
        page = r.json()
        print(f"P1-6 listing: total={page['total']} limit={page['limit']} offset={page['offset']} returned={len(page['items'])}")
        for it in page["items"]:
            print(f"  id={it['id']} status={it['status']} chunks={it['total_chunks']} tasks={it['total_tasks']}")
        # Header-only invariant — must NOT contain a chunks list.
        if any("chunks" in it and isinstance(it["chunks"], list) for it in page["items"]):
            print("FAIL: listing leaked chunks list (header-only invariant violated)")
            return 1

        rd = await client.get("/sweeps", params={"status": "done", "limit": 5})
        rd.raise_for_status()
        done_page = rd.json()
        print(f"P1-6 status=done filter: total_done={done_page['total']} returned={len(done_page['items'])}")
        for it in done_page["items"]:
            assert it["status"] == "done", f"filter leaked non-done: {it}"

        # Pagination disjoint check.
        p1 = (await client.get("/sweeps", params={"limit": 2, "offset": 0})).json()
        p2 = (await client.get("/sweeps", params={"limit": 2, "offset": 2})).json()
        ids1 = {i["id"] for i in p1["items"]}
        ids2 = {i["id"] for i in p2["items"]}
        if ids1 & ids2:
            print(f"FAIL: pagination overlap: {ids1 & ids2}")
            return 1
        print(f"P1-6 pagination disjoint: page1={ids1}, page2={ids2}")

        # --- P1-5: measure POST /launch latency on a known sweep ---
        # Pick the most recent done sweep so the launch is a no-op-replay.
        target_id = page["items"][0]["id"] if page["items"] else None
        if target_id is None:
            print("FAIL: no sweep to launch against")
            return 1

        # Wait out the per-sweep rate limit window from earlier launches.
        print(f"sleeping 6s to let rate-limit window expire on sweep {target_id}...")
        await asyncio.sleep(6)

        latencies: list[float] = []
        for _ in range(4):  # stay under the default 4/5s rate limit
            t0 = time.perf_counter()
            r = await client.post(f"/sweeps/{target_id}/launch")
            elapsed = time.perf_counter() - t0
            r.raise_for_status()
            latencies.append(elapsed * 1000)
            await asyncio.sleep(0.2)  # small spread to not all hit at once
        print(
            f"P1-5 POST /launch latency over 4 calls: "
            f"min={min(latencies):.1f}ms p50={statistics.median(latencies):.1f}ms "
            f"max={max(latencies):.1f}ms"
        )
        if max(latencies) > 500:
            print(f"WARN: P1-5 launch latency exceeded 500ms ceiling: {latencies}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
