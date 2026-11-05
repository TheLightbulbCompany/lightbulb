"""CLI for the wave-2 acceptance harness: ``run | gate`` (package docstring has the lines).

Dry-run by default: every command prints what it would do and touches nothing; ``--apply`` acts on dev (and only
dev: ``ctx.guard`` refuses any other ENVIRONMENT, API host or table prefix). Exit code 1 when a scenario failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from scripts.super_agent.acceptance import report
from scripts.super_agent.acceptance.ctx import DEFAULT_WORKSPACE, Ctx, aws_profile_default


def local_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


# ---------------------------------------------------------------------- run / gate
def _plan(names: list[str], workspace: str) -> None:
    print(f"on {workspace}:")
    for name in names:
        print(f"  {name}: {report.status(name)}")


async def _run(args, names: list[str], *, is_gate: bool) -> int:
    ctx = Ctx.connect(ws=args.workspace)
    try:
        if is_gate:
            rules_expect = None
            if args.rules_expect:
                from scripts.super_agent.acceptance.rules_observe import Expect

                rules_expect = Expect(**json.loads(Path(args.rules_expect).read_text()))
            results = await report.gate(
                ctx, rules_expect=rules_expect, skip=tuple(report.parse_scenarios(args.skip)) if args.skip else ()
            )
        else:
            results = await report.run_scenarios(names, ctx)
    finally:
        await ctx.aclose()
    meta = {"sha": args.sha or local_sha(), "workspace": ctx.ws, "faults": args.faults}
    if args.report:
        report.write_report(Path(args.report), results, **meta)
        print(f"report: {args.report} (+ .json)")
    else:
        print(report.render_report(results, **meta))
    return 0 if all(r.passed for r in results.values()) else 1


def run(args) -> int:
    names = report.parse_scenarios(args.scenarios)
    _plan(names, args.workspace)
    if not args.apply:
        print("[dry-run] nothing run; add --apply")
        return 0
    return asyncio.run(_run(args, names, is_gate=False))


def gate(args) -> int:
    _plan(report.ORDER[1:], args.workspace)
    print(
        "  (A0 needs the switch off: run it before the flip)  then VM evidence (V17); rules observe (S9, needs --rules-expect)"
    )
    if not args.apply:
        print("[dry-run] nothing run; add --apply")
        return 0
    return asyncio.run(_run(args, report.ORDER, is_gate=True))


def main(argv=None) -> int:
    aws_profile_default()
    ap = argparse.ArgumentParser(prog="scripts.super_agent.acceptance", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    parsers = {name: sub.add_parser(name) for name in ("run", "gate")}
    for p in parsers.values():
        p.add_argument("--apply", action="store_true", help="act on dev; without it only the plan is printed")
        p.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="the dev workspace (Clerk org id) to bind to")
    parsers["run"].add_argument("--scenarios", default="A0-A13", help="A0-A13 | A1,A2,A7 | A3")
    parsers["gate"].add_argument("--skip", default="", help="scenarios to leave out, e.g. A3,A5,A6,A10")
    parsers["gate"].add_argument("--rules-expect", help="JSON of rules_observe.Expect from the lead's manual steps")
    for name in ("run", "gate"):
        parsers[name].add_argument("--report", help="markdown path (a .json sidecar holds the full evidence)")
        parsers[name].add_argument("--sha", help="the deployed dev SHA to record (default: local HEAD)")
        parsers[name].add_argument(
            "--faults", default="", help="SUPER_AGENT_FAULTS set on the dev service, recorded only"
        )
    args = ap.parse_args(argv)
    if args.cmd == "gate" and args.apply and not args.report:
        raise SystemExit("gate needs --report (the ledger the founder reads)")
    return {"run": run, "gate": gate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
