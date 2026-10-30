import argparse
import asyncio
import json
import sys
from pathlib import Path

from .agent import execute
from .core import FIELDS, atomic_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded anonymous Threads acquisition agent")
    parser.add_argument("--message", required=True)
    parser.add_argument("--model", choices=["openai-compatible", "mock", "none"], default="openai-compatible")
    parser.add_argument(
        "--fields", nargs="+", choices=FIELDS, help="Required only for explicit zero-model diagnostic mode"
    )
    parser.add_argument("--fixture", type=Path, help="Synthetic local evidence; never a live result")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace", type=Path, default=Path("trace.json"))
    parser.add_argument("--seconds", type=float, default=120)
    args = parser.parse_args()
    if args.output and args.output.resolve() == args.trace.resolve():
        parser.error("--output and --trace must be different files")
    result, trace = asyncio.run(
        execute(args.message, args.model, args.fixture, args.fields, seconds=args.seconds, trace_file=str(args.trace))
    )
    code = {"complete": 0, "invalid_request": 2, "partial": 3, "unavailable": 3, "error": 1}[result["status"]]
    try:
        atomic_json(args.trace, trace)
        if args.output:
            atomic_json(args.output, result)
    except (OSError, ValueError):
        print("output_write_error: One or more requested files could not be saved.", file=sys.stderr)
        result["issues"].append(
            {"reason_code": "output_write_error", "reason": "One or more requested files could not be saved."}
        )
        result["trace_file"] = None
        if result["status"] == "complete":
            result["status"] = "partial"
        trace["status"] = result["status"]
        trace["events"][-1].update(status=result["status"], issues=result["issues"])
        # Best effort: update any writable file with the same failure state; never claim both saved.
        for path, value in ((args.trace, trace), (args.output, result)):
            if path:
                try:
                    atomic_json(path, value)
                except (OSError, ValueError):
                    pass
        code = 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
