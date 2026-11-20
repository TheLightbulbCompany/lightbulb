"""Where a shared Claude Code or Codex session's next browser connection goes (`use_browser`).

The engine's `lightbulb` MCP calls this through `/local-sessions/device/sessions/browser-place`;
the choice is kept on the session row as `browserPlace`.
"""

from __future__ import annotations

import uuid

from botocore.exceptions import ClientError

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions
from core.services.messaging import ApiProblem

NOT_SHARED = "Share this session to use the Lightbulb browser"
PLACES = ("mac", "cloud")
PLACE_ATTEMPTS = 3
PLACE_NOTE = "Applies to the session's next browser connection. To move an open one now, restart the lightbulb-browser MCP server (/mcp in Claude Code)."


def live_shared(row: dict | None) -> bool:
    """The session is still shared and running: its browser may be shown and driven."""
    return bool(row) and not row.get("unshared") and row.get("endedAt") is None and row.get("removedAt") is None


async def set_place(owner: str, session_id: str, where: str) -> dict:
    """`use_browser`: where this session's NEXT browser connection goes; an open one stays where it is.
    Kept on the row (it survives re-registration) for the session's life."""
    if where not in PLACES:
        raise ApiProblem("INVALID_REQUEST", "where must be mac or cloud.")
    # Fenced by the row's `revision` (as local_sessions_repo.admit_command is), so a registration that read
    # the old place and then puts the whole META row cannot silently revert this write.
    for _ in range(PLACE_ATTEMPTS):
        row = await repo.get_session(owner, session_id)
        if not live_shared(row):
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
        try:
            await store.run_in_thread(
                store._table().update_item,
                Key={"pk": repo.session_pk(owner, session_id), "sk": "META"},
                UpdateExpression="SET browserPlace = :w, revision = :new",
                ConditionExpression="revision = :rev",
                ExpressionAttributeValues={":w": where, ":new": str(uuid.uuid4()), ":rev": row["revision"]},
            )
            return {"sessionId": session_id, "browser": where, "note": PLACE_NOTE}
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "The session changed while switching; try again.", retryable=True)


async def place_by_native(owner: str, user: str, native_id: str, where: str) -> dict:
    """`use_browser` from the engine's `lightbulb` MCP: the person's own shared, running session whose
    native conversation (a Claude session id, a Codex thread id) is the calling one."""
    ids = [row["sessionId"] for row in await repo.list_user_sessions(owner, user)]
    # ponytail: reads every session of the person; a nativeConversationId index if anyone has thousands
    rows = await repo.get_sessions(owner, ids) if ids else []
    match = next((r for r in rows if r and r.get("nativeConversationId") == native_id and live_shared(r)), None)
    if match is None:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", NOT_SHARED)
    try:
        await local_sessions._enrolled(owner, user, match["deviceId"])  # sharing still on for that Mac
    except ApiProblem:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", NOT_SHARED) from None
    return await set_place(owner, match["sessionId"], where)
