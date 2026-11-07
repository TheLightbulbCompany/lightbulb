"""List the open Paperclip approvals (``pending`` / ``revision_requested``) and, with ``--apply``, close them.

Phase-1 task 7 deleted every surface that could decide one (the web inbox, ``/teams/approvals``, the
Activity approval items), so an open one would wait forever. Paperclip's API closes an approval only by
deciding it, so ``--apply`` rejects each with a note saying why (a rejected hire terminates the agent it
would have hired, which never started).

Refuses prod (unless --allow-prod). DRY RUN BY DEFAULT. Runs inside the backend task:

    uv run python -m scripts.cancel_paperclip_approvals --all
    uv run python -m scripts.cancel_paperclip_approvals --all --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from core.config import settings
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

OPEN = ("pending", "revision_requested")
NOTE = "Closed by Lightbulb: Paperclip approvals were retired and this one can no longer be answered."


def _rows(value) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("approvals") or value.get("items") or []
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


async def run_owner(
    admin: PaperclipAdminClient, cookie: str, owner_id: str, company_id: str, *, apply: bool
) -> list[str]:
    rows = _rows(await admin.list_approvals(session_cookie=cookie, company_id=company_id))
    # Re-filter: never trust a server-side filter with a write.
    open_ = [r for r in rows if r.get("companyId") == company_id and r.get("status") in OPEN and r.get("id")]
    lines = []
    for r in open_:
        if apply:
            await admin.reject_approval(session_cookie=cookie, approval_id=r["id"], reason=NOTE)
        verb = "cancelled" if apply else "would cancel"
        lines.append(f"{owner_id} {r['id']} {r.get('type')} ({r['status']}, {r.get('createdAt')}): {verb}")
    return lines


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--owner-id")
    who.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    parser.add_argument("--allow-prod", action="store_true", help="lift the prod refusal (phase-1 task 7)")
    args = parser.parse_args(argv)
    if settings.ENVIRONMENT == "prod" and not args.allow_prod:
        print("refusing: cancel_paperclip_approvals is dev-only unless --allow-prod")
        return 2

    repo = PaperclipRepo(table_name="paperclip-companies")
    owners = [args.owner_id] if args.owner_id else sorted(await repo.list_active_workspace_members())
    total = failed = 0
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        for owner_id in owners:
            try:
                ws = await repo.resolve_owner_row(owner_id, include_inactive=True)
                if ws is None or not ws.company_id:
                    continue
                lines = await run_owner(admin, cookie, owner_id, ws.company_id, apply=args.apply)
            except Exception as e:  # noqa: BLE001 -- one owner's failure must not stop the sweep
                failed += 1
                print(f"{owner_id}: ERROR {type(e).__name__}: {e}")
                continue
            total += len(lines)
            for line in lines:
                print(line)
    print(f"{total} open approvals {'cancelled' if args.apply else 'to cancel (dry run)'} ({failed} errors)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
