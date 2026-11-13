"""Stateless paired-desktop transport; DynamoDB fences every worker and reconnect.

No provider RPC or process registry. Dispatched means transport attempted; only a
desktop journal receipt can establish native admission. Unknown blocks its process generation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache


from core.repositories import messaging_repo as store
from core.services import local_sessions as sessions
from core.services.connection_service import ConnectionService
from core.services.management_api_client import ManagementApiClient
from core.services.messaging import ApiProblem, make_live_event

CLIENT_ID = "lightbulb-local-sessions"
CLAIM_SECONDS = 5
logger = logging.getLogger(__name__)
_ANALYTICS_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:local-session-lifecycle")

#: Seconds per named step of the frame being handled, for the router's slow-frame line.
frame_steps: ContextVar[dict[str, float] | None] = ContextVar("local_session_frame_steps", default=None)


@contextmanager
def step(name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        steps = frame_steps.get()
        if steps is not None:
            steps[name] = steps.get(name, 0.0) + time.monotonic() - started


Event = sessions.LocalSessionEventBody


def check_envelope(body: dict) -> None:
    # Match ManagementApiClient's *actual* encoding, including non-ASCII expansion.
    if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) > sessions.MAX_ENVELOPE_BYTES:
        raise ApiProblem("INVALID_REQUEST", "Local session envelope exceeds 16 KiB.")


#: In-flight hint fan-outs, referenced so asyncio cannot garbage-collect them before they finish.
_hint_tasks: set[asyncio.Task[None]] = set()
#: One fan-out per (owner, human, transcript, session) at a time: a hint that arrives while one is running
#: marks it dirty and the running task publishes once more at the end, so a streaming burst
#: (one call per session per second) collapses to at most one in-flight + one follow-up per key.
_hint_running: dict[tuple[str, str, bool, str | None], bool] = {}


async def publish_resources_changed(
    owner: str, human: str, *, transcript: bool = False, session_id: str | None = None
) -> None:
    """One ``workspace.sessions.changed`` per change, on the workspace sessions channel.

    Clients refetch their open room's resources (and so its transcript) from it. It used to
    fan out a ``room.resources.changed`` to every room the human is in, one membership read and
    publish at a time: ~28 round trips on every relay frame for the founder, which crossed the
    desktop's 10 s ack deadline and looped reconnects (prod 2026-09-24). Owner ruling: one hint.
    ``transcript`` marks a streaming batch: open panes refetch, the sidebar list ignores it (it
    lists liveness, not content, and a streaming teammate would refetch it once a second).
    ``session_id`` names the one session that changed: clients re-read that row
    (GET /messaging/sessions/{id}, ~0.3 s) instead of the whole list (1.7-2.5 s, measured
    2026-10-27). Without it, the change may add or drop rows, and clients refetch the list.
    """
    from core.services.live_events import get_live_event_publisher

    key = (owner, human, transcript, session_id)
    if key in _hint_running:
        _hint_running[key] = True  # dirty: the running fan-out sends one more at the end
        return
    _hint_running[key] = False
    event = make_live_event(
        "workspace.sessions.changed",
        {
            "humanUserId": human,
            **({"transcript": True} if transcript else {}),
            **({"sessionId": session_id} if session_id else {}),
        },
    )

    async def fan_out() -> None:
        try:
            while True:
                try:
                    await get_live_event_publisher().publish_sessions(owner, event)
                except Exception:
                    # Hints are expendable; authorized HTTP polling repairs lost hints.
                    logger.warning("Workspace session hint failed")
                if not _hint_running.get(key):
                    break
                _hint_running[key] = False
        finally:
            _hint_running.pop(key, None)

    # Off the caller's frame: it was one publish per workspace member (51 members = 2.4-3.2 s of a
    # register frame on dev, 2026-10-08) and is one publish on the sessions channel now, but a slow
    # AppSync still must not hold the desktop's 10 s ack. The task is kept until done so the loop
    # never drops it mid-flight.
    task = asyncio.create_task(fan_out())
    _hint_tasks.add(task)
    task.add_done_callback(_hint_tasks.discard)


ACTIVITY_TTL_S = 90 * 24 * 3600  # the projector's message rows keep the same
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    # Off the heartbeat path: the desktop acks within 10 s and a push is an Expo round trip.
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def attention_copy(row: dict, state: str, subagent: str | None = None, prompt_text: str = "") -> tuple[str, str]:
    """ "Claude · <session title>" over "Waiting for you: …" / "Finished: <reply's first line>".
    ``subagent`` is a finished background subagent's own line ('Agent "…" finished'), which is the body."""
    from core.services import notification_copy as copy

    tool = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "grok": "Grok"}.get(
        row.get("provider", ""), "Session"
    )
    # The clients' rule (sessionTitle): never an id.
    where = copy.line(row.get("displayTitle") or row.get("title") or row.get("cwdName"), 40) or "New chat"
    if subagent:
        return copy.title(tool, where), copy.line(subagent)
    if state == "waiting":
        return copy.title(tool, where), waiting_line(row.get("pendingApproval"), prompt_text)
    if state == "attention":
        return copy.title(tool, where), copy.line(row.get("note")) or "Left you a note"
    reply = copy.line(row.get("lastAssistantMessage"))
    return copy.title(tool, where), copy.line(f"Finished: {reply}") if reply else "Finished its turn"


def waiting_line(prompt: dict | None, text: str = "") -> str:
    """What the session waits on, named: the question, or the call it wants to make. Only an
    answerable prompt reaches the row; any other is named by its transcript row's ``text``
    ("Tea or coffee? (Tea / Coffee)", "Wants to run Bash(git push)") and answered in the terminal."""
    from core.services.notification_copy import line

    if not prompt:
        text = line(text, 60)
        if text.startswith("Wants to run"):
            return f"{text}: answer it in your terminal"
        if text:
            return line(f"Has a question: {text}")
        return "Waiting for you: approve or answer it in your terminal"
    tool, summary = prompt.get("tool") or "A tool", line(prompt.get("summary"), 60)
    if tool == "AskUserQuestion":
        return line(f"Has a question: {summary}") if summary else "Has a question for you"
    ask = f'{tool} wants to run "{summary}"' if summary else f"{tool} wants your OK"
    return ask if prompt.get("exact") or prompt.get("full") else f"{ask}: answer it in your terminal"


class LocalSessionRelay:
    def __init__(self, connections: ConnectionService, management: ManagementApiClient):
        self.connections, self.management = connections, management

    async def connect(self, connection_id: str) -> tuple[dict, dict]:
        row = await store.run_in_thread(self.connections.get_connection, connection_id)
        if (
            not row
            or row.get("connection_type") != "local-session"
            or row.get("auth_kind") != "device"
            or row.get("client_id") != CLIENT_ID
            or not row.get("device_id")
        ):
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Verified local session connection required.")
        owner = row.get("org_id") or row["user_id"]
        enrollment = await sessions._enrolled(owner, row["user_id"], row["device_id"])
        try:
            int(row["connected_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Connection identity is incomplete.") from exc
        return row, enrollment

        # Another worker owns progression. Heartbeat/POST/receipt will drive it again.


@lru_cache(maxsize=1)
def get_local_session_relay() -> LocalSessionRelay:
    return LocalSessionRelay(ConnectionService(), ManagementApiClient())
