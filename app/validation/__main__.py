"""Unified validation CLI.

The grab-bag of `scripts/validation_*.py` files (kill_resume, api_restart,
redis_blip, cancel, pool_saturation) each duplicate argparse boilerplate
and printing conventions. This module is a thin dispatch layer so the same
scenarios are reachable as:

    python -m app.validation list
    python -m app.validation run kill_resume [--scale-exec N]
    python -m app.validation run api_restart
    python -m app.validation run redis_blip
    python -m app.validation run cancel
    python -m app.validation run pool_saturation
    python -m app.validation run all                # run every scenario, fail-fast

Each scenario delegates to the existing script's `main()` (no logic
duplicated). Scripts already accept their own argv-style flags via
argparse — extra args after the scenario name are forwarded.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from typing import Any

# scenario_name -> (module_path, attribute_name_of_main_coroutine)
SCENARIOS: dict[str, tuple[str, str]] = {
    "kill_resume": ("scripts.validation_kill_resume", "main"),
    "api_restart": ("scripts.validation_api_restart", "main"),
    "redis_blip": ("scripts.validation_redis_blip", "main"),
    "cancel": ("scripts.validation_cancel", "main"),
    "pool_saturation": ("scripts.validation_pool_saturation", "main"),
}


def _load_main(scenario: str) -> Any:
    if scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario: {scenario}. known: {sorted(SCENARIOS)}")
    module_path, attr = SCENARIOS[scenario]
    mod = importlib.import_module(module_path)
    return getattr(mod, attr)


def _run_one(scenario: str, extra_argv: list[str]) -> int:
    """Invoke a scenario's main(). Each scenario was written with sys.argv-style
    parsing, so we temporarily splice extra_argv into sys.argv."""
    main_fn = _load_main(scenario)
    saved_argv = sys.argv[:]
    try:
        sys.argv = [f"app.validation.{scenario}", *extra_argv]
        result = main_fn()
        if asyncio.iscoroutine(result):
            return int(asyncio.run(result) or 0)
        return int(result or 0)
    finally:
        sys.argv = saved_argv


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.validation")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list available scenarios")

    run = sub.add_parser("run", help="run one scenario (or 'all')")
    run.add_argument("scenario", help=f"one of: {', '.join(SCENARIOS)}, all")
    run.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="extra args forwarded to the scenario script",
    )

    args = parser.parse_args()
    if args.cmd == "list":
        for name in sorted(SCENARIOS):
            module_path, _ = SCENARIOS[name]
            print(f"  {name:18s}  -> {module_path}")
        return 0

    if args.cmd == "run":
        if args.scenario == "all":
            failed: list[str] = []
            for name in sorted(SCENARIOS):
                print(f"\n=== running scenario: {name} ===", flush=True)
                rc = _run_one(name, [])
                if rc != 0:
                    failed.append(name)
                    print(f"=== {name} FAILED (rc={rc}) ===", flush=True)
                else:
                    print(f"=== {name} OK ===", flush=True)
            if failed:
                print(f"\nFAILED scenarios: {failed}", flush=True)
                return 1
            return 0
        return _run_one(args.scenario, args.extra)

    return 1


if __name__ == "__main__":
    sys.exit(main())
