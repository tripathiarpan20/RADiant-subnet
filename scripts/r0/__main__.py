"""R0 feasibility runner.

    python -m scripts.r0 datahive   # DataHive reachability, fetch time, rate limits
    python -m scripts.r0 storage    # Hippius / Backblaze presigned PUT
    python -m scripts.r0 modal      # offline Modal sandbox on L4 and L40S
    python -m scripts.r0 hf         # private HF download, pin upload, quota
    python -m scripts.r0 models     # base models scored on the demo images

Each probe writes docs/implementation/r0_results/<probe>.json with secrets
redacted, prints a one-line status per gate, and exits 0 only when every gate
passed. Credentials come from the environment (see .env.example); nothing is
read from or written to a .env file by the probes themselves.
"""

from __future__ import annotations

import argparse
import importlib
import sys

from .report import GateResult, exit_gate_met, merge_previous, write_results

PROBES = {
    "datahive": "scripts.r0.datahive",
    "storage": "scripts.r0.storage",
    "modal": "scripts.r0.modal_sandbox",
    "hf": "scripts.r0.hf",
    "models": "scripts.r0.base_models",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.r0", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-write", action="store_true", help="print results without writing the JSON report")
    parser.add_argument("--merge", action="store_true",
                        help="keep gates from the existing report that this run did not produce")
    sub = parser.add_subparsers(dest="probe", required=True)
    modules = {}
    for name, path in PROBES.items():
        module = importlib.import_module(path)
        modules[name] = module
        p = sub.add_parser(name, help=(module.__doc__ or "").strip().splitlines()[0])
        module.add_arguments(p)
    args = parser.parse_args(argv)

    results: list[GateResult] = modules[args.probe].run(args)
    for r in results:
        print(f"[{r.status.value:>8}] {r.gate}: {r.summary}")
    if not args.no_write:
        print(f"wrote {write_results(args.probe, merge_previous(args.probe, results) if args.merge else results)}")
    return 0 if exit_gate_met(results) else 1


if __name__ == "__main__":
    sys.exit(main())
