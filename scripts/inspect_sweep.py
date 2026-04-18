"""Quick CLI to print a sweep's chunk/job/task summary."""
from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    sweep_id = int(sys.argv[1])
    url = f"http://localhost:8000/sweeps/{sweep_id}"
    with urllib.request.urlopen(url) as resp:
        s = json.load(resp)
    print(f"sweep#{s['id']} status={s['status']} total_tasks={s['total_tasks']}")
    for c in s["chunks"]:
        jobs_done = sum(1 for j in c["jobs"] if j["status"] == "done")
        tasks_total = sum(len(j["tasks"]) for j in c["jobs"])
        tasks_done = sum(1 for j in c["jobs"] for t in j["tasks"] if t["status"] == "done")
        print(
            f"  chunk ord={c['ordinal']} status={c['status']:<8} "
            f"jobs_done={jobs_done}/{len(c['jobs'])} tasks_done={tasks_done}/{tasks_total} "
            f"finalized_by={c.get('finalized_by')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
