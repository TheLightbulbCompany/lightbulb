"""What a workspace's team did in its trial (T19): the day-28 recap card and day-27 email.

Numbers (docs/marketing/15 §B, plan Appendix C), over the 30 days before ``trial_end``:

- ``approvals``: approval Questions a person allowed (any answer but ``deny``).
- ``handoffs``: Questions a person answered (a denial is a handoff too).
- ``agents``: distinct agents that posted in a room.
- ``people``: distinct humans that posted in a room.
- ``top_item``: the title of the most recently allowed approval.

ponytail: handoffs count Question answers only; session pick-ups and moved tasks join when their
events exist (T03). Room scans stop after ``_MAX_PAGES`` per room, so a very busy room undercounts.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from core.repositories import messaging_repo, questions_repo

logger = logging.getLogger(__name__)

WINDOW_S = 30 * 86400
_MAX_PAGES = 20
_cache: dict[tuple[str, str], dict] = {}


def _epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def room_activity(owner_id: str, since_s: float) -> tuple[int, int]:
    """(people, agents): distinct humans and agents with a room message since ``since_s``."""
    people: set[str] = set()
    agents: set[str] = set()
    for room_id in await messaging_repo.list_owner_room_ids(owner_id):
        meta = await messaging_repo.get_room(room_id) or {}
        before = int(meta.get("lastSeq") or 0) + 1
        for _ in range(_MAX_PAGES):
            page, before = await messaging_repo.list_messages_before_page(room_id, before, 100)
            for msg in page:
                at = _epoch(msg.get("createdAt"))
                author = msg.get("author") or {}
                if at is None or at < since_s or not author.get("id"):
                    continue
                (people if author.get("kind") == "human" else agents).add(author["id"])
            oldest = _epoch(page[0].get("createdAt")) if page else None
            if before is None or oldest is None or oldest < since_s:
                break
    return len(people), len(agents)


def question_numbers(questions: list[dict]) -> tuple[int, int, str | None]:
    """(handoffs, approvals, top_item) from answered Question rows."""
    allowed = sorted(
        (q for q in questions if q.get("kind") == "approval" and (q.get("answer") or {}).get("optionId") != "deny"),
        key=lambda q: q.get("answeredAt") or "",
    )
    return len(questions), len(allowed), (allowed[-1].get("title") or None) if allowed else None


async def recap(owner_id: str, trial_end: int) -> dict:
    """The recap numbers, cached per owner per UTC day."""
    day = datetime.now(UTC).date().isoformat()
    cached = _cache.get((owner_id, day))
    if cached is not None:
        return cached
    since = trial_end - WINDOW_S
    people, agents = await room_activity(owner_id, since)
    try:
        answered = await questions_repo.answered_since(owner_id, datetime.fromtimestamp(since, UTC).isoformat())
    except Exception:  # noqa: BLE001 -- a tasks-table blip never hides the recap; its numbers just read 0
        logger.warning("trial recap: questions unavailable owner=%s", owner_id, exc_info=True)
        answered = None
    handoffs, approved, top = question_numbers(answered or [])
    result = {
        "trial_end": trial_end,
        "handoffs": handoffs,
        "approvals": approved,
        "agents": agents,
        "people": people,
        "top_item": top,
    }
    if answered is not None:  # never cache an outage as zeros
        _cache[(owner_id, day)] = result
    return result


def in_recap_window(trial_end: int | None, now: float | None = None) -> bool:
    """Days 28-30 of the trial: the last three days before ``trial_end``."""
    if trial_end is None:
        return False
    left = trial_end - (now if now is not None else time.time())
    return 0 < left <= 3 * 86400
