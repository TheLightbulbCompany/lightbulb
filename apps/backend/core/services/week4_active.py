"""``week4_active`` (T23): a formed team that still had a human message in its fourth week.

Once a day (the digest sweeper's loop, its own pass) every active workspace whose
``team_formed`` marker (T02, ``team-formed:<org>`` in the webhook-dedup table) is 28-31
days old is checked: a human room message in days 21-28 after the team formed emits
``week4_active``, once (a marker taken after the scan). The three-day window absorbs a
missed day or a failed read. Week-4 inactive teams emit nothing; ``team_formed`` is the denominator.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from core.observability.product_analytics import capture_product_event
from core.repositories import messaging_repo
from core.services import webhook_dedup

logger = logging.getLogger(__name__)

PASS_NAME = "week4_active"
PASS_SECONDS = 86400
_DAY = 86400
_WEEK4 = (21 * _DAY, 28 * _DAY)
_CHECK_WINDOW = (28 * _DAY, 31 * _DAY)
_PAGE = 100
_MAX_PAGES = 20  # ponytail: 2,000 messages read inside the window per room
_MAX_SKIP_PAGES = 200  # ponytail: up to 20,000 newer messages skipped to reach the window
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:week4_active")


def _epoch(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return None


async def _human_message_between(room_id: str, lo: float, hi: float) -> bool:
    meta = await messaging_repo.get_room(room_id) or {}
    before = int(meta.get("lastSeq") or 0) + 1
    in_window = 0
    for _ in range(_MAX_PAGES + _MAX_SKIP_PAGES):
        page, older = await messaging_repo.list_messages_before_page(room_id, before, _PAGE)
        oldest = _epoch(page[0].get("createdAt")) if page else None
        if oldest is None or oldest < hi:
            # Only pages that reach into week four count against the read budget; newer
            # traffic (an agent-heavy room) is skipped on its own, larger budget.
            in_window += 1
            if in_window > _MAX_PAGES:
                return False
        for message in reversed(page):  # newest first
            t = _epoch(message.get("createdAt"))
            if t is None:
                continue
            if t < lo:
                return False
            if t < hi and (message.get("author") or {}).get("kind") == "human":
                return True
        if older is None:
            return False
        before = older
    return False


async def check_workspace(org_id: str, now: float) -> bool:
    """``True`` when this call emitted ``week4_active``."""
    marker = await webhook_dedup._read_event(f"team-formed:{org_id}")
    formed = marker.get("recorded_at") if marker else None
    if formed is None:
        return False
    formed = int(formed)
    if not (formed + _CHECK_WINDOW[0] <= now < formed + _CHECK_WINDOW[1]):
        return False
    lo, hi = formed + _WEEK4[0], formed + _WEEK4[1]
    # Scan first, claim after: a failed read leaves nothing taken, so tomorrow's pass retries.
    active = False
    for room_id in await messaging_repo.list_owner_room_ids(org_id):
        if await _human_message_between(room_id, lo, hi):
            active = True
            break
    if not active:
        return False  # re-scanned on the window's other days; nothing to emit
    if (
        await webhook_dedup.record_event_or_skip(f"week4-checked:{org_id}", source="analytics", ttl_seconds=60 * _DAY)
        is not webhook_dedup.WebhookDedupResult.RECORDED
    ):
        return False
    capture_product_event(
        distinct_id=org_id,  # the identity rule makes the creator the person
        event="week4_active",
        properties={
            "workspace_id": org_id,
            "team_formed_at": datetime.fromtimestamp(formed, timezone.utc).isoformat(),
        },
        event_uuid=str(uuid.uuid5(_NS, org_id)),
    )
    return True


async def run_once(now: float | None = None) -> int:
    from core.repositories.workspace_repo import WorkspaceRepo

    now = now if now is not None else time.time()
    emitted = 0
    for org_id in await WorkspaceRepo().list_active_workspace_members():
        if not org_id.startswith("org_"):
            continue
        try:
            emitted += await check_workspace(org_id, now)
        except Exception:  # noqa: BLE001 -- one workspace never costs the rest of the pass
            logger.warning("week4_active: check failed org=%s", org_id, exc_info=True)
    return emitted
