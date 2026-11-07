"""Print every Paperclip webhook trigger with no ``HOOK#`` row (inventory §1d C, the pre-switch triggers).

Phase-1 task 7 deletes ``routers/paperclip_trigger_proxy.py``, the public front door these fired through;
after it ships, ``/api/v1/hooks/t/{owner}/{public_id}`` answers only triggers with a ``HOOK#`` row. Run this
before and after to see what stopped firing. The founder confirms none is a customer's; nothing is cancelled.

Read-only. One line per trigger, then a count. Runs inside the backend task (Paperclip is VPC-internal):

    uv run python -m scripts.list_paperclip_webhook_triggers --all
    uv run python -m scripts.list_paperclip_webhook_triggers --owner-id <org>
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from core.config import settings
from core.repositories import automations_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie


def _rows(value, key: str) -> list[dict]:
    if isinstance(value, dict):
        value = value.get(key) or value.get("items") or []
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


async def owner_triggers(admin: PaperclipAdminClient, cookie: str, owner_id: str, company_id: str) -> list[str]:
    """``"{owner} {routine title} {publicId} ({status})"`` per webhook trigger without a HOOK# row."""
    lines = []
    for routine in _rows(await admin.list_routines(session_cookie=cookie, company_id=company_id), "routines"):
        if not any(t.get("kind") == "webhook" for t in routine.get("triggers") or []):
            continue  # the list omits publicId; only routines with a webhook trigger need the detail read
        detail = await admin.get_routine(session_cookie=cookie, routine_id=routine["id"])
        for trig in detail.get("triggers") or []:
            public_id = trig.get("publicId")
            if trig.get("kind") != "webhook" or not public_id:
                continue
            if await automations_repo.get_hook(owner_id, public_id) is not None:
                continue  # already fires through routers/hooks.py
            state = "enabled" if trig.get("enabled", True) else "disabled"
            lines.append(
                f"{owner_id} {routine.get('title') or routine['id']!r} {public_id} ({routine.get('status')}, {state})"
            )
    return lines


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--owner-id")
    who.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

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
                lines = await owner_triggers(admin, cookie, owner_id, ws.company_id)
            except Exception as e:  # noqa: BLE001 -- one owner's failure must not stop the listing
                failed += 1
                print(f"{owner_id}: ERROR {type(e).__name__}: {e}")
                continue
            total += len(lines)
            for line in lines:
                print(line)
    print(f"{total} Paperclip webhook triggers without a HOOK# row across {len(owners)} workspaces ({failed} errors)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
