"""`home-services gate ...` arguments (no service config required)."""
from __future__ import annotations

import json
from pathlib import Path


def add_gate_arguments(parser) -> None:
    parser.add_argument("--out", default=".dev/gate", help="report/media output directory")
    parser.add_argument("--engine-url", help="engine API base URL on loopback, e.g. http://127.0.0.1:5000")
    parser.add_argument("--engine-camera", action="append", help="engine camera names to check")
    parser.add_argument("--benchmark-seconds", type=float, default=0)
    parser.add_argument("--evidence", action="append", default=[], help="STEP=path/to/evidence.json for manual engine steps")


def run_gate_command(args) -> int:
    from .runner import run_gate
    evidence = {}
    for item in args.evidence:
        step, _, path = item.partition("=")
        evidence[step] = Path(path)
    report = run_gate(Path(args.out), engine_url=args.engine_url, engine_cameras=args.engine_camera,
                      benchmark_seconds=args.benchmark_seconds, evidence=evidence)
    for s in report["steps"]:
        print(f"[{s['status']:7}] {s['step']}")
    print(json.dumps({"result": report["result"], "smallest_failing_step": report["smallest_failing_step"],
                      "report": str(Path(args.out) / "gate-report.json")}))
    return 0 if report["result"] == "passed" else 3
