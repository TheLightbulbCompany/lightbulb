"""Watching a running terminal (spec §5.5, rulings 2026-10-03 §4-5).

LOCALSESSION#<owner>#<sid> / VIEWER#<user>          who gets this session's screen frames
LOCALUSER#<owner>#<human>  / WATCH#<device>#<sid>   what that Mac's heartbeat ack lists

Both expire 45 s after the viewer's last POST (they post every 15 s). `since` is when the
newest viewer arrived: the Mac sends a fresh snapshot when it moves. Screen frames themselves
are published to each viewer's own user channel and stored nowhere (see `forward`).
"""

from __future__ import annotations

import json
import logging
import time

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions

logger = logging.getLogger(__name__)

VIEWER_TTL_S = 45
ACK_CAP = 24
_clock = time.time


def _viewer_key(row: dict, user: str) -> dict:
    return {"pk": repo.session_pk(row["ownerId"], row["sessionId"]), "sk": f"VIEWER#{user}"}


def _watch_key(row: dict) -> dict:
    return {
        "pk": repo.user_pk(row["ownerId"], row["humanUserId"]),
        "sk": f"WATCH#{row['deviceId']}#{row['sessionId']}",
    }


async def watch(auth: sessions.Caller, session_id: str) -> None:
    """A person has this running session's terminal on screen. Humans who can read it only:
    `authorize_session` refuses agents on the roomless path, and no room id is accepted here."""
    row, _ = await sessions.authorize_session(auth, None, session_id)
    now = int(_clock())
    if row.get("endedAt") is not None or row.get("unshared") or int(row.get("onlineUntil", 0)) <= now:
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "This session is not running.")
    key = _viewer_key(row, auth.user_id)
    viewer = await store._get(key["pk"], key["sk"])
    fresh = viewer is None or int(viewer.get("expiresAt", 0)) <= now
    expires = now + VIEWER_TTL_S
    table = store._table()
    await store.run_in_thread(
        table.put_item,
        Item={**key, "userId": auth.user_id, "since": now if fresh else int(viewer["since"]), "expiresAt": expires},
    )
    await store.run_in_thread(
        table.update_item,
        Key=_watch_key(row),
        UpdateExpression="SET sessionId = :sid, expiresAt = :exp, since = "
        + (":now" if fresh else "if_not_exists(since, :now)"),
        ExpressionAttributeValues={":sid": row["sessionId"], ":exp": expires, ":now": now},
    )


async def ack_with_watching(ack: dict, owner: str, human: str, device: str) -> dict:
    """Every session on this Mac someone watches now; the Mac streams exactly these. Always
    present when read (an empty list stops every relay); absent only when the read failed."""
    try:
        now = int(_clock())
        rows = await store._query(repo.user_pk(owner, human), begins_with=f"WATCH#{device}#")
        watching = []
        for r in sorted(rows, key=lambda r: int(r.get("since", 0)))[-ACK_CAP:]:
            if int(r.get("expiresAt", 0)) <= now:
                continue
            meta = await repo.get_session(owner, r["sessionId"])
            if meta is None or meta.get("endedAt") is not None or meta.get("unshared"):
                continue
            watching.append({"sessionId": r["sessionId"], "since": int(r["since"])})
    except Exception:
        logger.warning("Watched sessions unavailable")  # never costs the heartbeat
        return ack
    while True:
        candidate = {**ack, "watching": watching}
        if len(json.dumps(candidate, separators=(",", ":")).encode()) <= sessions.MAX_ENVELOPE_BYTES or not watching:
            return candidate
        watching = watching[1:]  # oldest viewer set first
