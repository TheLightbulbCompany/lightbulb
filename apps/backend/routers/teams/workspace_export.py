"""Owner-only export of workspace records."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from core.auth import AuthContext, get_current_user
from core.repositories import messaging_repo
from core.services import messaging

from . import agents as _agents
from .agents import _ctx
from .deps import TeamsContext

router = APIRouter()
logger = logging.getLogger(__name__)

_INTERNAL_KEYS = {"pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk"}


def _public_record(value: Any) -> Any:
    """Remove DynamoDB keys while retaining the complete user-facing record."""
    if isinstance(value, dict):
        return {key: _public_record(item) for key, item in value.items() if key not in _INTERNAL_KEYS}
    if isinstance(value, list):
        return [_public_record(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        _public_record(value),
        indent=2,
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


async def _all_messages(room_id: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    after_seq = 0
    while True:
        page, next_seq = await messaging_repo.list_messages(room_id, after_seq, 100)
        # The same projection normal reads serve: a deleted message keeps no
        # text or attachments, an edited one shows its edited text.
        messages.extend(messaging.canonical_message(row).model_dump(mode="json") for row in page)
        if next_seq is None:
            return messages
        after_seq = next_seq


async def _all_tasks(ctx: TeamsContext) -> list[dict[str, Any]]:
    # Keyset pagination on the issue id: offsets skip or repeat rows when the
    # board changes mid-export. The Paperclip fork accepts afterId only with
    # sortField=id, sortDir=asc and no offset.
    tasks: list[dict[str, Any]] = []
    limit = 500
    admin = _agents._admin()
    params = {"limit": str(limit), "sortField": "id", "sortDir": "asc"}
    while True:
        page = await admin.list_issues(
            company_id=ctx.company_id,
            session_cookie=ctx.session_cookie,
            params=params,
        )
        tasks.extend(page)
        if len(page) < limit:
            return tasks
        params = {**params, "afterId": str(page[-1]["id"])}


@router.get("/workspace/export")
async def export_workspace(
    ctx: TeamsContext = Depends(_ctx),
    auth: AuthContext = Depends(get_current_user),
) -> Response:
    """Download all workspace rooms and board tasks as a ZIP archive."""
    if auth.org_role != "org:admin":
        raise HTTPException(status_code=403, detail="Workspace owner access required")

    # Owner role does not open a room: only rooms the owner can read in the
    # app (the messaging membership seam) go into the archive.
    rooms: list[tuple[str, dict[str, Any]]] = []
    for room_id in await messaging_repo.list_owner_room_ids(ctx.owner_id):
        try:
            room, _member = await messaging.require_room_member(ctx.owner_id, auth.user_id, room_id)
        except messaging.ApiProblem:
            continue
        rooms.append((room_id, room))
    tasks = await _all_tasks(ctx)

    generated_at = datetime.now(UTC).isoformat()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            _json_bytes(
                {
                    "formatVersion": 1,
                    "generatedAt": generated_at,
                    "workspaceId": ctx.company_id,
                    "roomCount": len(rooms),
                    "taskCount": len(tasks),
                }
            ),
        )
        archive.writestr(
            "board/tasks.json",
            _json_bytes({"exportedAt": generated_at, "tasks": tasks}),
        )
        for room_id, room in rooms:
            messages = await _all_messages(room_id)
            archive.writestr(
                f"rooms/{room_id}.json",
                _json_bytes({"room": room, "messages": messages}),
            )

    logger.info(
        "workspace_export_requested owner_id=%s company_id=%s rooms=%d tasks=%d",
        ctx.owner_id,
        ctx.company_id,
        len(rooms),
        len(tasks),
    )
    filename = f"lightbulb-workspace-{datetime.now(UTC).date().isoformat()}.zip"
    return Response(
        content=output.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
