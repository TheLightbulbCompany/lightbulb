"""Retire the pre-platform-app Google grants (PR #4323), per owner.

Before the platform app, every Google connection used the owner's OWN OAuth client and a
``gmail.modify`` + ``drive`` refresh token projected into the gog keyring. Narrowing the
scope list in providers.yaml only changes the NEXT consent, so this runs once after the
deploy and, for every ``google`` integrations row that is still ``connected`` and was
connected before the deploy:

  1. flips it to ``reconnect_required`` through ``access_token._mark_reconnect`` (owner
     facts, MCP/profile reconcile, one Needs-you item per member), then
  2. clears the gog keyring projection (``disconnect_all`` revokes each account's old broad
     grant at Google and drops the BYO client config; the reconnect re-projects).

It also pauses the routines the removed ``gmail.poll`` / ``gmail.message`` events seeded
(matched by their rendered title; ``routine.status = paused`` is reversible from the
Automations page) and drops the owner's ``gmail_watch`` entries so the trigger proxy
rejects any Pub/Sub push still in flight; the watch itself lapses within ~7 days.
Routines the owner ATTACHED those events to are hand-written work: they are only listed
(``MANUAL``), never touched.

Re-runnable. DRY RUN BY DEFAULT. Runs inside the backend task (Paperclip is VPC-internal):

    uv run python -m scripts.retire_byo_google --connected-before <deploy epoch seconds>
    uv run python -m scripts.retire_byo_google --connected-before <epoch> --apply
    uv run python -m scripts.retire_byo_google --connected-before <epoch> --owner-id <org> --apply

``--connected-before`` is required so a row an owner already reconnected through the
platform app is never flipped back.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from core.config import settings
from core.repositories import google_accounts_repo, integrations_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.credentials import hermes_cli, integration_secret_store, registry
from core.services.credentials.access_token import _mark_reconnect
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

# The rendered `template.title.format(event_label=...)` of the two retired events.
RETIRED_TITLES = frozenset(
    {
        "Google: New email (checked on a schedule)",
        "Google: New email (real-time push)",
    }
)
# Starter-prompt openings, to LIST routines the events were attached to (never touched).
_RETIRED_PROMPT_MARKERS = ("Scheduled inbox check (account:", "A Gmail push notification just fired")


def needs_reconnect(row: dict, connected_before: int) -> bool:
    return (
        row.get("provider") == "google"
        and row.get("status") == "connected"
        and int(row.get("connected_at") or 0) < connected_before
    )


def classify_routines(routines: list[dict]) -> tuple[list[dict], list[dict]]:
    """``(to_pause, manual)``: active routines carrying a retired title, and routines whose body
    carries a retired starter prompt under some other (owner-written) title."""
    to_pause, manual = [], []
    for r in routines:
        if not r.get("id") or r.get("status") != "active":
            continue
        if r.get("title") in RETIRED_TITLES:
            to_pause.append(r)
        elif any(m in (r.get("description") or "") for m in _RETIRED_PROMPT_MARKERS):
            manual.append(r)
    return to_pause, manual


async def _retire_grant(owner_id: str, *, apply: bool) -> str:
    if not apply:
        return "would mark reconnect_required + clear gog keyring"
    await _mark_reconnect(owner_id, "google")
    p = registry.get_provider("google")
    cred = await integration_secret_store.get(owner_id, "google") or {}
    cred = {
        **cred,
        "disconnect_all": True,
        "accounts": [a["email"] for a in await google_accounts_repo.list_accounts(owner_id)],
    }
    await hermes_cli.clear(owner_id, "google", p.wiring.materializer, cred)  # best-effort by contract
    return "marked reconnect_required, keyring cleared"


async def _retire_automations(owner_id: str, *, apply: bool) -> str:
    ws = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, include_inactive=True)
    if ws is None or not ws.company_id:
        return "no Paperclip company"
    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        listed = await admin.list_routines(session_cookie=cookie, company_id=ws.company_id)
        rows = listed if isinstance(listed, list) else (listed.get("routines") or listed.get("items") or [])
        to_pause, manual = classify_routines([r for r in rows if isinstance(r, dict)])
        if apply:
            for r in to_pause:
                await admin.patch_routine(session_cookie=cookie, routine_id=r["id"], body={"status": "paused"})
    watches = await integrations_repo.get_google_watch_channels(owner_id)
    gmail = [w for w in watches if w.get("kind") == "gmail_watch"]
    if apply and gmail:
        await integrations_repo.set_google_watch_channels(owner_id, [w for w in watches if w not in gmail])
    verb = "paused" if apply else "would pause"
    return (
        f"{verb} routines={[r['id'] for r in to_pause]} gmail_watches_dropped={len(gmail)} "
        f"MANUAL(attached, untouched)={[r['id'] for r in manual]}"
    )


async def run_owner(owner_id: str, row: dict, connected_before: int, *, apply: bool) -> str:
    parts = []
    if needs_reconnect(row, connected_before):
        parts.append(await _retire_grant(owner_id, apply=apply))
    parts.append(await _retire_automations(owner_id, apply=apply))
    return f"{owner_id}: " + "; ".join(parts)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--connected-before", type=int, required=True, help="epoch seconds; the deploy time")
    parser.add_argument("--owner-id")
    parser.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    args = parser.parse_args(argv)

    rows = [r for r in await integrations_repo.scan_all() if r.get("provider") == "google"]
    if args.owner_id:
        rows = [r for r in rows if r["owner_id"] == args.owner_id]
    failed = 0
    for row in sorted(rows, key=lambda r: r["owner_id"]):
        try:
            print(await run_owner(row["owner_id"], row, args.connected_before, apply=args.apply))
        except Exception as e:  # noqa: BLE001 -- one owner's failure must not stop the sweep
            failed += 1
            print(f"{row['owner_id']}: ERROR {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
