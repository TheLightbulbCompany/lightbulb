"""Built-in proactive rules that need I/O (spec 12 §5; wave2-S Task 7). `rules.py` stays pure.

`ensure_invite_prep(ws)`: one invite-prep rule (observe) per connected Google account whose email belongs to a
workspace member; that member owns it. Idempotent (deterministic schedule id). The rule listens on a calendar
watch (`HOOK#` + keeper, lane A's `automation_hooks.seed`); Google refusing the watch parks the rule as
`paused_system` / `google_scope` until the owner reconnects. Reproduce-and-fix is never created here: it starts
Off and is set up by a person (What I watch lists it from a client constant).
"""

from __future__ import annotations

import logging

from core.repositories import automations_repo, google_accounts_repo
from core.services import automation_hooks, clerk_admin, rules

logger = logging.getLogger(__name__)
_ANY = frozenset({"draft", "active", "paused_system"})


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
        row, _ = await automations_repo.create_schedule(ws, rules.invite_prep_row(account["email"], owner))
        if row.get("pausedReason") == "google_watch_broken":  # the keeper gave up: drop the dead watch, open a new one
            for pid in row.get("hookPublicIds") or []:
                await automation_hooks.delete(ws, pid)
            row = await _watch(ws, row["scheduleId"], account["email"], owner)
        elif not row.get("hookPublicIds"):
            row = await _watch(ws, row["scheduleId"], account["email"], owner)
        ids.append(row["scheduleId"])
    return ids


async def _watch(ws: str, sid: str, email: str, owner: str) -> dict:
    """Open the calendar watch the rule listens on, then turn the rule on; a refusal pauses it instead."""
    try:
        await automation_hooks.seed(
            ws,
            "google",
            ["calendar.changed"],
            resource=email,
            topic=None,
            specialist=None,
            automation_id=sid,
            title=None,
            description=None,
            actor_user_id=owner,
        )
    except ValueError as exc:
        logger.warning("invite prep: Google refused the calendar watch for %s: %s", sid, exc)
        return await automations_repo.set_status(ws, sid, "paused_system", reason="google_scope", allowed_from=_ANY)
    return await automations_repo.set_status(ws, sid, "active", reason=None, allowed_from=_ANY)


async def on_google_connect(ws: str) -> None:
    """After a Google account connects: the built-in for that account, never failing the
    connect."""
    try:
        await ensure_invite_prep(ws)
    except Exception:  # noqa: BLE001 -- best effort
        logger.warning("invite prep after Google connect failed for %s", ws, exc_info=True)
