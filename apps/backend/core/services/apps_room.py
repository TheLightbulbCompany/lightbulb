"""The app's room: where its builder agent hears what happened to the app. That is the app's own
conversation (``app_conversation``) once it has one, else (an older app) the room it was built in."""

from __future__ import annotations

import logging
import uuid

from core.auth import AuthContext
from core.constants import SYSTEM_ACTOR_ID
from core.repositories import apps_repo, messaging_repo
from core.services import messaging

logger = logging.getLogger(__name__)


def room_of(app: dict) -> str | None:
    """Where the app's builder hears about it: its own conversation, else the room it was built in."""
    return app.get("conversation_id") or app.get("room_id")


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


async def _builder(app: dict, room_id: str, *, guess: bool = True) -> dict | None:
    """The member who builds this app: the one recorded at the source upload, else ``actor_id``, when
    they can run (a hook-token CLI with no agent header stores the placeholder "agent", which is nobody).
    ``guess``: otherwise someone who can hear it, for a line that must reach somebody: the only agent, the
    only one with an open run, the first. A wake never guesses."""
    agents = sorted(
        (
            m
            for m in await messaging_repo.list_members(room_id)
            if m.get("kind") == "agent" and m.get("listenMode") != "observe"
        ),
        key=lambda m: m["id"],
    )
    for wanted in ((app.get("build") or {}).get("builder"), app.get("actor_id")):
        named = next((m for m in agents if m["id"] == wanted), None)
        if named:
            return named
    if not guess or not agents:
        return None
    if len(agents) == 1:
        return agents[0]
    running = {r.get("agentId") for r in await messaging_repo.list_open_runs_for_room(app["owner_id"], room_id)}
    busy = [m for m in agents if m["id"] in running]
    return busy[0] if len(busy) == 1 else agents[0]


async def post_approval_line(*, owner_id: str, user_id: str, workflow: dict) -> None:
    """One wake line per approved app workflow. Never raises: the approval has
    already happened and must not fail on a chat line."""
    if not workflow.get("app_id"):
        return
    workflow_id = workflow["workflow_id"]
    try:
        app = await apps_repo.get(workflow["app_id"])
        room_id = room_of(app or {})
        if not room_id:
            logger.warning("apps: no room for app %s, approval line skipped", workflow["app_id"])
            return
        author = await _author(owner_id, user_id, room_id)
        if author is None and not app.get("conversation_id"):
            logger.warning("apps: no human member to post the approval line for %s", workflow_id)
            return
        provider = str(workflow.get("provider_name") or "")
        what = {"vercel": "hosting", "neon": "a database"}.get(
            provider.lower(), f"{provider}/{workflow.get('service_ref')}"
        )
        env = "live" if workflow.get("environment") == "prod" else "preview"
        display = app.get("display_name") or app.get("name")
        # Address the builder agent so the line wakes it, not just the room.
        builder = await _builder(app, room_id) if app.get("actor_kind") == "agent" else None
        mentions = [messaging.MemberRef(kind="agent", id=builder["id"])] if builder else []
        text = (
            f"Approved hosting and data for {display}. Setting it up."
            if workflow.get("bundle")
            else f"Approved {what} for {display} ({env}). Setting it up."
        )
        if author is None:  # the app's conversation with nobody seated who can sign it: Lightbulb says it
            targets = [{"agentId": builder["id"], "eventKind": "user_request"}] if builder else []
            await post_system_line(app, f"apps-approval:{workflow_id}", text, targets)
            return
        await messaging.create_human_message(
            AuthContext(user_id=author, org_id=owner_id),
            room_id,
            messaging.CreateMessageRequest(
                # A replay of the same approval is a no-op on the messaging side.
                client_message_id=uuid.uuid5(uuid.NAMESPACE_URL, f"apps-approval:{workflow_id}"),
                text=text,
                mentions=mentions,
                reply_to_message_id=None,
                thread_root_message_id=None,
            ),
            via="apps",
            wake=True,
        )
    except Exception as e:  # noqa: BLE001 — a chat line never undoes an approval
        logger.warning("apps: approval line failed for %s: %s", workflow_id, type(e).__name__)


async def post_system_line(
    app: dict, key: str, text: str, targets: list[dict] | None = None, *, room_id: str | None = None
) -> dict | None:
    """A system line in the app's room (``room_of``), or in ``room_id``. The key is the receipt: a resend
    replays it. ``None``: the room is gone."""
    room_id = room_id or room_of(app)
    if not room_id:
        return None
    try:
        message, _ = await messaging_repo.create_message(
            room_id=room_id,
            owner_id=app["owner_id"],
            idemp_pk=messaging_repo.room_pk(room_id),
            idemp_sk=messaging_repo.human_idemp_sk(SYSTEM_ACTOR_ID, key),
            req_hash=messaging_repo.request_hash(key),
            author={"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"},
            text=text,
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
            run_id=None,
            dispatch_targets=targets or [],
            source_agent_id=None,
            client_message_id=None,
        )
        return message
    except (messaging_repo.RoomArchived, messaging_repo.RoomNotFound):
        return None


async def post_stuck_line(app: dict, display: str, step: str) -> None:
    """Once per quiet spell (keyed by ``progress_at``); wakes nobody. Never raises: the stuck mark is stored."""
    try:
        await post_system_line(
            app,
            f"apps-stuck:{app['app_id']}:{app.get('progress_at')}",
            f"{display} stopped at {step}: nothing has moved for 5 minutes. Ask the agent here to pick it up again.",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("apps: stuck line failed for %s: %s", app.get("app_id"), type(e).__name__)


async def post_origin_ready_line(app: dict, display: str, canvas_url: str) -> None:
    """Once ever, in the room the request came from, at the app's first real preview: its first version
    is in Canvas. Not "ready": only the CLI knows when the builder is done. Never raises."""
    if not app.get("origin_room_id") or not app.get("conversation_id"):
        return
    try:
        await post_system_line(
            app,
            f"apps-origin-ready:{app['app_id']}",
            f"{display}'s first version is in Canvas: {canvas_url}",
            room_id=app["origin_room_id"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("apps: origin ready line failed for %s: %s", app.get("app_id"), type(e).__name__)
