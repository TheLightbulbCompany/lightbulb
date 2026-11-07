# ponytail: delete after the prod data move (task 3)
"""Copy each workspace's Paperclip company ``name`` and ``description`` onto its META row
(Phase 1 task 8b.4), so ``workspace_name`` and ``/teams/settings`` keep showing them once
Paperclip is gone. Only active workspaces with a ``company_id``; a META row that already has a
``name`` (set through the new ``/teams/settings``) is never overwritten.

Refuses prod (unless --allow-prod). DRY RUN BY DEFAULT. Runs inside the backend task, while the
Paperclip server is still up:

    uv run python -m scripts.copy_workspace_names --all
    uv run python -m scripts.copy_workspace_names --all --apply
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


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--owner-id")
    who.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    parser.add_argument("--allow-prod", action="store_true", help="lift the prod refusal (phase-1 task 8b.4)")
    args = parser.parse_args(argv)
    if settings.ENVIRONMENT == "prod" and not args.allow_prod:
        print("refusing: copy_workspace_names is dev-only unless --allow-prod")
        return 2

    repo = PaperclipRepo(table_name="paperclip-companies")
    owners = [args.owner_id] if args.owner_id else sorted(await repo.list_active_workspace_members())
    total = failed = 0
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        for owner_id in owners:
            try:
                ws = await repo.resolve_owner_row(owner_id)
                if ws is None or not ws.company_id or ws.name:
                    continue
                company = await admin.get_company(session_cookie=cookie, company_id=ws.company_id) or {}
                name, description = _text(company.get("name")), _text(company.get("description"))
                if not name and not description:
                    continue
                if args.apply:
                    await repo.set_profile(owner_id, name=name, description=description)
            except Exception as e:  # noqa: BLE001 -- one owner's failure must not stop the sweep
                failed += 1
                print(f"{owner_id}: ERROR {type(e).__name__}: {e}")
                continue
            total += 1
            print(f"{owner_id}: {'copied' if args.apply else 'would copy'} {name!r}")
    print(f"{total} workspaces {'written' if args.apply else 'to write (dry run)'} ({failed} errors)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
