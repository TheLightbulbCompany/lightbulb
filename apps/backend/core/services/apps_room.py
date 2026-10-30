"""The app's room: where its builder agent hears what happened to the app."""

from __future__ import annotations

import logging
import uuid

from core.auth import AuthContext
from core.repositories import apps_repo, messaging_repo
from core.services import messaging

logger = logging.getLogger(__name__)


async def _member(owner_id: str, user_id: str, room_id: str) -> bool:
    try:
        await messaging.require_room_member(owner_id, user_id, room_id)
    except messaging.ApiProblem:
        return False
    return True


async def _author(owner_id: str, user_id: str, room_id: str) -> str | None:
    """The approver when they sit in the room, else the room's creator (the
    human who holds `owner`). Paperclip's "paperclip" is never a member."""
    if await _member(owner_id, user_id, room_id):
        return user_id
    members = await messaging_repo.list_members(room_id)
    creator = next((m["id"] for m in members if m.get("kind") == "human" and m.get("role") == "owner"), None)
    if creator and await _member(owner_id, creator, room_id):
        return creator
    return None


async def post_approval_line(*, owner_id: str, user_id: str, workflow: dict) -> None:
    """One wake line per approved app workflow. Never raises: the approval has
    already happened and must not fail on a chat line."""
    if not workflow.get("app_id"):
        return
    workflow_id = workflow["workflow_id"]
    try:
        app = await apps_repo.get(workflow["app_id"])
        room_id = (app or {}).get("room_id")
        if not room_id:
            logger.warning("apps: no room for app %s, approval line skipped", workflow["app_id"])
            return
        author = await _author(owner_id, user_id, room_id)
        if author is None:
            logger.warning("apps: no human member to post the approval line for %s", workflow_id)
            return
        await messaging.create_human_message(
            AuthContext(user_id=author, org_id=owner_id),
            room_id,
            messaging.CreateMessageRequest(
                # A replay of the same approval is a no-op on the messaging side.
                client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, f"apps-approval:{workflow_id}"),
                text=f"approved: {workflow.get('provider_name')}/{workflow.get('service_ref')} "
                f"for {app.get('name')}, setting up",
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
            via="apps",
            wake=True,
        )
    except Exception as e:  # noqa: BLE001 — a chat line never undoes an approval
        logger.warning("apps: approval line failed for %s: %s", workflow_id, type(e).__name__)
