"""Pause Paperclip automation for owners that are not live, and cancel the BLOCKED tasks
their routines already filed.

Live code now pauses an owner's routines and timer heartbeats when they stop being live
(``paperclip_agent_lifecycle.pause_owner_automation``). Workspaces that lapsed before that
shipped still fire every night into a runtime that is gone. This runs the same seam on them,
then cancels their ``blocked`` issues whose ``originKind`` is ``routine_execution``.

"Not live" is ``provision_gate.should_arm_nightly_ceo`` False -- the predicate the re-arm
path uses, so a later subscribe resumes exactly what this paused.

Re-runnable. DRY RUN BY DEFAULT: prints what it would pause and cancel. Runs inside the
backend task (Paperclip is VPC-internal), like ``refresh_owner_facts``:

    uv run python -m scripts.pause_unlive_paperclip_automation --all
    uv run python -m scripts.pause_unlive_paperclip_automation --owner-id <org> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from core.config import settings
from core.repositories import billing_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_agent_lifecycle import list_all_issues, pause_owner_automation
from core.services.provision_gate import should_arm_nightly_ceo


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await billing_repo.scan_all()
    return sorted(r["owner_id"] for r in rows if r.get("owner_id") and not should_arm_nightly_ceo(r))


async def _blocked_routine_issues(company_id: str) -> list[dict]:
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        issues = await list_all_issues(
            admin,
            cookie=cookie,
            company_id=company_id,
            params={"status": "blocked", "originKind": "routine_execution"},
        )
    # Re-filter: never trust a server-side filter with a cancel.
    return [i for i in issues if i.get("status") == "blocked" and i.get("originKind") == "routine_execution"]


async def _cancel(issue_ids: list[str]) -> None:
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        for issue_id in issue_ids:
            await admin.patch_issue(issue_id=issue_id, body={"status": "cancelled"}, session_cookie=cookie)


async def run_owner(owner_id: str, *, apply: bool) -> str:
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if should_arm_nightly_ceo(account):
        return f"{owner_id}: live ({(account or {}).get('subscription_status')}), skipped"
    ws = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, include_inactive=True)
    if ws is None or not ws.company_id:
        return f"{owner_id}: no Paperclip company, skipped"
    paused = await pause_owner_automation(owner_id, dry_run=not apply)
    blocked = await _blocked_routine_issues(ws.company_id)
    if apply:
        await _cancel([i["id"] for i in blocked])
    verb = "paused/cancelled" if apply else "would pause/cancel"
    return (
        f"{owner_id} ({(account or {}).get('subscription_status')}) company={ws.company_id}: {verb} "
        f"heartbeats={paused['agent_ids']} routines={paused['routine_ids']} "
        f"blocked_routine_issues={[i.get('identifier') or i['id'] for i in blocked]}"
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    args = parser.parse_args(argv)

    failed = 0
    for owner_id in await _targets(args):
        try:
            print(await run_owner(owner_id, apply=args.apply))
        except Exception as e:  # noqa: BLE001 -- one owner's failure must not stop the sweep
            failed += 1
            print(f"{owner_id}: ERROR {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
