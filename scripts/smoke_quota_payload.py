"""Build an oversize sweep payload as JSON on stdout (for the smoke test)."""
import json
import sys

n_chunks = int(sys.argv[1]) if len(sys.argv) > 1 else 50

payload = {
    "name": "smoke-quota-oversize",
    "chunks": [
        {
            "ordinal": i + 1,
            "jobs": [
                {
                    "name": f"j{i}",
                    "tasks": [{"point_idx": 0, "name": "t", "expected_value": 0}],
                }
            ],
        }
        for i in range(n_chunks)
    ],
}
sys.stdout.write(json.dumps(payload))
