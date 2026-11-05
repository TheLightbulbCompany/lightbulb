"""Built-in proactive rules that need I/O (spec 12 §5; wave2-S Task 7). `rules.py` stays pure.

`ensure_invite_prep(ws)`: one invite-prep rule (observe) per connected Google account whose email belongs to a
workspace member; that member owns it. Idempotent (deterministic schedule id). Reproduce-and-fix is never created
here: it starts Off and is set up by a person (What I watch lists it from a client constant).
"""

from __future__ import annotations

from core.repositories import automations_repo, google_accounts_repo
from core.services import clerk_admin, rules


async def _member_emails(ws: str) -> dict[str, str]:
    if ws.startswith("user_"):
        return {(await clerk_admin.get_primary_email(ws)).lower(): ws}
    members = await clerk_admin.list_organization_members(ws, strict=True)
    return {m["email"].lower(): m["user_id"] for m in members if m.get("email")}


async def ensure_invite_prep(ws: str) -> list[str]:
    """The invite-prep rule ids for `ws` (created now or already there). Unmatched accounts get none."""
    members = await _member_emails(ws)
    ids = []
    for account in await google_accounts_repo.list_accounts(ws):
        owner = members.get(account["email"].lower())
        if not owner:
            continue
        row, new = await automations_repo.create_schedule(ws, rules.invite_prep_row(account["email"], owner))
        if new:
            await automations_repo.set_status(
                ws, row["scheduleId"], "active", reason=None, allowed_from=frozenset({"draft"})
            )
        ids.append(row["scheduleId"])
    return ids
