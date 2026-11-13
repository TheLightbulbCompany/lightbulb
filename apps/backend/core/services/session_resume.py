"""Resume: start an ended session again on its owner's Mac, from web, desktop or iPhone (spec §5.7).

Who: the session's owner, and the people who could send to it when it ended (``resumeAllowed``,
copied by the relay's end handler; the owner can take it back). Never an agent.

Rows, all in the messaging table:
  LOCALSESSION#<owner>#<sid> / META                       ``resume``: the one waiting request
  LOCALUSER#<owner>#<human>  / RESUMEWANT#<device>#<sid>  what the owner's Mac is asked to start

The waiting request lives on META, not in a row of its own: its one-per-session condition is the
META revision, so it is decided together with "the session has ended" and "this person may
resume", and every reader gets its state from the row it already reads. The want row is the Mac's
copy, delivered in the device-heartbeat acknowledgement like pick-up's, so a Mac that is asleep
starts it when it wakes. Both expire after RESUME_SECONDS (the want row by DynamoDB TTL).
"""

from __future__ import annotations

import logging
import time


from core.repositories import local_sessions_repo as repo, messaging_repo as store

logger = logging.getLogger(__name__)


def live(resume: dict | None, now: int | None = None) -> bool:
    """A request still waiting for the Mac. A refused or expired one is history."""
    now = int(time.time()) if now is None else now
    return bool(resume) and resume.get("state") == "pending" and int(resume["until"]) > now


def _want_key(row: dict) -> dict:
    return {
        "pk": repo.user_pk(row["ownerId"], row["humanUserId"]),
        "sk": f"RESUMEWANT#{row['deviceId']}#{row['sessionId']}",
    }


def _want_delete(row: dict) -> dict:
    return {"Delete": {"TableName": store._table().name, "Key": _want_key(row)}}
