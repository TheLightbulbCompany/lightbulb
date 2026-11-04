"""One line in an app's conversation (else its room) when a live app has not been opened for 30 days.

Runs once a day from the digest sweeper's loop (its own pass). The line is posted by the
app's own agent when it has one, else the workspace CEO. ``idle_posted_at`` is only written
after a successful post, so a failed post retries tomorrow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from core.repositories import apps_repo
from core.services import apps_room, messaging, owner_facts

logger = logging.getLogger(__name__)

PASS_NAME = "apps_idle"
PASS_SECONDS = 86400
IDLE = timedelta(days=30)
_ORIGIN = {"suggested": "suggested by an agent", "asked": "asked for in this room"}


def _dt(value) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _baseline(app: dict) -> datetime | None:
    if app.get("last_opened_at"):
        return _dt(app["last_opened_at"])
    live = [d for v in app.get("versions") or [] if v.get("live") and (d := _dt(v.get("at")))]
    return min(live) if live else _dt(app.get("created_at"))


def _due(app: dict, now: datetime) -> bool:
    if app.get("state") != "live":
        return False
    base = _baseline(app)
    if base is None or now - base < IDLE:
        return False
    posted = _dt(app.get("idle_posted_at"))
    return posted is None or now - posted >= IDLE


async def _author(app: dict, owner_id: str, roster: list[dict]) -> str | None:
    ids = {t["id"] for t in roster}
    if app.get("actor_kind") == "agent" and app.get("actor_id") in ids:
        return app["actor_id"]
    return next((t["id"] for t in roster if t.get("is_ceo")), None)


async def post_idle_lines(owner_id: str, *, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    due = [a for a in await apps_repo.list_for_owner(owner_id) if _due(a, now)]
    if not due:
        return 0
    roster = await owner_facts._lookup_teammates(owner_id)
    posted = 0
    for app in due:
        try:
            agent_id = await _author(app, owner_id, roster)
            if not agent_id:
                logger.warning("apps_idle: no author owner=%s app=%s", owner_id, app["app_id"])
                continue
            # Its conversation when it has one, else its room; a CLI-only app has neither (the CLI says it).
            room_id = apps_room.room_of(app)
            if not room_id:
                continue
            name = app["name"]
            said = (
                ""
                if app.get("conversation_id")
                else f" ({_ORIGIN.get((app.get('origin') or {}).get('kind'), 'built in this room')})"
            )
            window = int(now.timestamp() // IDLE.total_seconds())
            await messaging.post_agent_message(
                owner_id,
                messaging.AgentPostRequest(
                    event_id=f"app-idle:{app['app_id']}:{window}",
                    agent_id=uuid.UUID(agent_id),
                    room_id=uuid.UUID(str(room_id)),
                    text=f"{name}{said} has not been opened in 30 days. Say 'remove {name}' if it's no longer needed.",
                ),
            )
            await apps_repo.patch(app["app_id"], {"idle_posted_at": now.isoformat()})
            posted += 1
        except Exception as e:  # noqa: BLE001 -- one app never costs the rest
            logger.warning("apps_idle: failed owner=%s app=%s %s", owner_id, app.get("app_id"), type(e).__name__)
    return posted


async def run_once() -> int:
    from core.repositories.paperclip_repo import PaperclipRepo

    total = 0
    for owner_id in await PaperclipRepo(table_name="paperclip-companies").list_active_workspace_members():
        try:
            total += await post_idle_lines(owner_id)
        except Exception as e:  # noqa: BLE001 -- one owner never costs the rest
            logger.warning("apps_idle: owner=%s failed %s", owner_id, type(e).__name__)
    return total
