"""CLI for the wave-2 acceptance harness: ``setup | run | gate | teardown`` (package docstring has the lines).

Dry-run by default: every command prints what it would do and touches nothing; ``--apply`` acts on dev (and only
dev: ``ctx.guard`` refuses any other ENVIRONMENT, API host or table prefix). Exit code 1 when a scenario failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.super_agent.acceptance import report
from scripts.super_agent.acceptance.ctx import FIXTURES, Ctx, aws_profile_default, guard, resolve_fixtures, user_email

ON_FIXTURES = ("sa-dev-on", "sa-dev-chatgpt", "sa-dev-acme")  # the switch goes on by hand; sa-dev-off stays off


def local_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


# ---------------------------------------------------------------------- setup / teardown (dev Clerk)
def _ensure_user(clerk, fixture: str, label: str) -> str:
    from scripts.load.fixtures import _bapi

    email = user_email(fixture, label)
    user = clerk.user(email) or _bapi(
        clerk,
        "/users",
        {
            "email_address": [email],
            "first_name": label.title(),
            "last_name": "Fixture",
            "skip_password_requirement": True,
        },
    )
    return user["id"]


def _wait_ready(clerk, orgs: dict[str, tuple[str, str]], timeout: int) -> None:
    """Every fixture workspace serves chat with the starter roster (scripts/load/fixtures.py ``is_ready``)."""
    from scripts.load.fixtures import TOKEN_TTL_MAX, _api, is_ready

    pending = {n: _api(clerk, creator, org, min(TOKEN_TTL_MAX, timeout + 600)) for n, (org, creator) in orgs.items()}
    deadline = time.monotonic() + timeout
    while pending:
        for name, api in list(pending.items()):
            try:
                status = api("GET", "/container/status")
                rooms = api("GET", "/messaging/rooms")["rooms"]
                general = next((r for r in rooms if (r.get("name") or "").lower() == "general"), None)
                members = api("GET", f"/messaging/rooms/{general['roomId']}")["members"] if general else None
            except RuntimeError as exc:
                print(f"  {name}: {exc}", file=sys.stderr)
                continue
            if is_ready(status, rooms, members):
                print(f"  {name}: ready")
                del pending[name]
        if pending and time.monotonic() > deadline:
            raise SystemExit(f"not ready after {timeout}s: {sorted(pending)}")
        if pending:
            time.sleep(10)


def setup(args) -> int:
    for name, labels in FIXTURES.items():
        print(f"{name}: users {', '.join(user_email(name, u) for u in labels)} (first one creates it, so admin)")
    print(f"then wait up to {args.ready_timeout}s for each workspace: runtime ready, #general with the starter roster")
    print(f"then, BY HAND (nothing here writes the switch): set the containers row flag for {', '.join(ON_FIXTURES)}")
    if not args.apply:
        print("[dry-run] nothing done; add --apply")
        return 0
    guard()
    from scripts.load.fixtures import _add_member, _bapi, _find_org
    from scripts.multiparty_e2e.runner import Clerk

    clerk = Clerk()
    orgs: dict[str, tuple[str, str]] = {}
    for name, labels in FIXTURES.items():
        ids = [_ensure_user(clerk, name, label) for label in labels]
        row = _find_org(clerk, name) or _bapi(clerk, "/organizations", {"name": name, "created_by": ids[0]})
        for uid in ids[1:]:  # one at a time: each fires the JOIN + #general seat
            _add_member(clerk, row["id"], uid)
        orgs[name] = (row["id"], ids[0])
        print(f"{name} = {row['id']} ({len(ids)} users)")
    _wait_ready(clerk, orgs, args.ready_timeout)
    print("next, by hand (profile isol8-admin): for each of", ", ".join(f"{n}={orgs[n][0]}" for n in ON_FIXTURES))
    print("  update the lightbulb-dev-containers row (owner_id = that org id): flag attribute super_agent = true")
    print("  sa-dev-chatgpt: connect the ChatGPT login in its settings; sa-dev-on: an API-key model provider")
    return 0


def teardown(args) -> int:
    print("for each sa-dev-* org whose members are all its own fixture users: remove the other members, delete")
    print("them, creator POST /users/me/delete (wipes the workspace), then the org / creator if Clerk still has them")
    if not args.apply:
        print("[dry-run] nothing done; add --apply")
        return 0
    guard()
    from scripts.load.fixtures import DELETE_RETRY_S, _api, _members, _self_delete
    from scripts.multiparty_e2e.runner import Clerk

    clerk = Clerk()
    for name, org_id in resolve_fixtures(clerk).items():
        members = _members(clerk, org_id)
        own = {user_email(name, label) for label in FIXTURES[name]}
        strangers = [m["email"] for m in members if m.get("email") not in own]
        if strangers:
            raise SystemExit(f"refusing {name}: members that are not its fixture users ({len(strangers)})")
        creator = clerk.http.get(f"/organizations/{org_id}").raise_for_status().json().get("created_by")
        emails = {m["userId"]: m["email"] for m in members}
        for m in members:
            if m["userId"] != creator:
                print(f"{name}: remove + delete {m['email']}")
                clerk.http.delete(f"/organizations/{org_id}/memberships/{m['userId']}").raise_for_status()
                clerk.http.delete(f"/users/{m['userId']}").raise_for_status()
        deadline = time.monotonic() + DELETE_RETRY_S
        while [m["userId"] for m in _members(clerk, org_id)] != [creator]:
            if time.monotonic() > deadline:
                raise SystemExit(f"{name}: members other than the creator remain")
            time.sleep(5)
        if creator in emails:
            print(f"{name}: creator self-delete {emails[creator]}")
            _self_delete(_api(clerk, creator, org_id), emails[creator])
        if clerk.http.get(f"/organizations/{org_id}").status_code == 200:
            clerk.http.delete(f"/organizations/{org_id}").raise_for_status()
        if creator and clerk.http.get(f"/users/{creator}").status_code == 200:
            clerk.http.delete(f"/users/{creator}").raise_for_status()
    return 0


# ---------------------------------------------------------------------- run / gate
def _plan(names: list[str], fixture: str) -> None:
    print(f"on {fixture}:")
    for name in names:
        print(f"  {name}: {report.status(name)}")


async def _run(args, names: list[str], *, is_gate: bool) -> int:
    ctx = Ctx.connect(ws_name=args.fixture)
    try:
        if is_gate:
            rules_expect = None
            if args.rules_expect:
                from scripts.super_agent.acceptance.rules_observe import Expect

                rules_expect = Expect(**json.loads(Path(args.rules_expect).read_text()))
            results = await report.gate(ctx, rules_expect=rules_expect)
        else:
            results = await report.run_scenarios(names, ctx)
    finally:
        await ctx.aclose()
    meta = {"sha": args.sha or local_sha(), "fixtures": ctx.fixtures, "faults": args.faults}
    if args.report:
        report.write_report(Path(args.report), results, **meta)
        print(f"report: {args.report} (+ .json)")
    else:
        print(report.render_report(results, **meta))
    return 0 if all(r.passed for r in results.values()) else 1


def run(args) -> int:
    names = report.parse_scenarios(args.scenarios)
    _plan(names, args.fixture)
    if not args.apply:
        print("[dry-run] nothing run; add --apply")
        return 0
    return asyncio.run(_run(args, names, is_gate=False))


def gate(args) -> int:
    _plan(report.ORDER, "sa-dev-on")
    print("  then A1,A2 on sa-dev-chatgpt; VM evidence (V17); rules observe (S9, needs --rules-expect)")
    if not args.apply:
        print("[dry-run] nothing run; add --apply")
        return 0
    args.fixture = "sa-dev-on"
    return asyncio.run(_run(args, report.ORDER, is_gate=True))


def main(argv=None) -> int:
    aws_profile_default()
    ap = argparse.ArgumentParser(prog="scripts.super_agent.acceptance", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    parsers = {name: sub.add_parser(name) for name in ("setup", "run", "gate", "teardown")}
    for p in parsers.values():
        p.add_argument("--apply", action="store_true", help="act on dev; without it only the plan is printed")
    parsers["setup"].add_argument("--ready-timeout", type=int, default=1800, help="seconds to wait per workspace")
    parsers["run"].add_argument("--scenarios", default="A0-A13", help="A0-A13 | A1,A2,A7 | A3")
    parsers["run"].add_argument("--fixture", default="sa-dev-on", choices=sorted(FIXTURES), help="ctx.ws")
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
    return {"setup": setup, "run": run, "gate": gate, "teardown": teardown}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
