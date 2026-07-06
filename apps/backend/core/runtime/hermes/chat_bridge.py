"""Hermes chat bridge — the narrow ``/chat`` hot-path glue between
``routers/websocket_chat`` and ``HermesRuntime`` for a Hermes-backed owner.

Every function here is reached ONLY behind
``get_runtime_for_owner(owner_id).kind == "hermes"`` in the two websocket_chat
seams (``_process_agent_chat_background`` for a turn, ``_process_rpc_background``
for the control plane). An OpenClaw owner never touches this module, so the
OpenClaw path stays byte-identical.

Two responsibilities:
  1. The ``on_event`` sink (``make_frontend_event_sink``) that pushes each
     translated ChatEvent from ``HermesRuntime.send_chat`` to the frontend over
     the Management API, stamping the routing envelope (``runId``/``agent_id``/
     ``session_id``) the frontend demuxes on — the same envelope the OpenClaw
     pool stamps in ``connection_pool._handle_message``.
  2. The minimal control-plane answers (``answer_hermes_rpc`` +
     ``fetch_hermes_roster``) that let the ``/chat`` page MOUNT for a Hermes
     owner: a real ``agents.list`` roster + an empty ``sessions.list``, with
     every other RPC answered by a graceful empty-OK so the page never 500s.

Import-light where it counts: the Paperclip plumbing (httpx / admin client /
DDB repo) is imported lazily inside ``fetch_hermes_roster`` so importing this
module stays cheap and free of AWS clients at import time.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# The frontend event-sink type mirrors ``core.runtime.base.ChatEventCallback``:
# an async handler awaited once per translated ChatEvent.
FrontendEventSink = Callable[[dict], Awaitable[None]]


def make_frontend_event_sink(
    management_api: Any,
    connection_id: str,
    *,
    agent_id: str,
    run_id: str,
    session_id: str | None,
) -> FrontendEventSink:
    """Build the ``on_event`` sink handed to ``HermesRuntime.send_chat``.

    ``send_chat`` translates each Hermes SSE frame into the internal ChatEvent
    shape the frontend already renders, but those events do NOT carry the
    routing keys the frontend demuxes on (see the adapter's ``_translate_event``
    docstring). This sink adds the transport AND the routing envelope:

      - ``agent_id`` — routes the frame to the right agent conversation.
      - ``runId``    — the per-turn correlation key. We stamp the SAME value the
        frontend minted for this turn (``clientRunId``) rather than Hermes'
        internal ``run_id``, EXACTLY as the OpenClaw path does: OpenClaw echoes
        the ``clientRunId`` back as ``idempotencyKey`` → ``runId`` on every
        event, and the frontend releases its busy-gate only when a terminal's
        ``runId`` matches its own ``clientRunIdRef``. Stamping Hermes' internal
        run id here would leave that gate armed forever.
      - ``session_id`` — the per-session demux key, added only for a named
        session (mirrors the OpenClaw path adding it only when non-None; the
        "General" session carries no ``session_id``).

    The frame is a fresh dict (no mutation of the adapter's event) pushed
    synchronously to the originating ``connection_id`` — the same
    ``management_api.send_message`` sink the OpenClaw RPC path uses.
    """

    async def on_event(event: dict) -> None:
        frame = {**event, "agent_id": agent_id, "runId": run_id}
        if session_id is not None:
            frame["session_id"] = session_id
        management_api.send_message(connection_id, frame)

    return on_event


def build_agents_list_response(roster: Sequence[Mapping[str, Any]]) -> dict:
    """Shape a Paperclip roster into the ``agents.list`` RPC payload the
    frontend's ``useAgents`` consumes: ``{defaultId, agents:[{id, name,
    identity:{name}}]}``.

    Mirrors ``core.containers.config.build_openclaw_config``'s roster mapping:
    the ``ceo`` (Friday) is the default agent, else the first entry; each
    agent's friendly name rides both ``name`` and ``identity.name`` (the
    frontend's ``agentDisplayName`` prefers ``identity.name``). Agents with no
    ``id`` are skipped defensively.
    """
    agents: list[dict] = []
    default_id: str | None = None
    for agent in roster:
        agent_id = agent.get("id")
        if not agent_id:
            continue
        name = agent.get("name")
        entry: dict = {"id": agent_id}
        if name:
            entry["name"] = name
            entry["identity"] = {"name": name}
        agents.append(entry)
        if default_id is None:
            default_id = agent_id
        if agent.get("role") == "ceo":
            default_id = agent_id

    response: dict = {"agents": agents}
    if default_id is not None:
        response["defaultId"] = default_id
    return response


def answer_hermes_rpc(
    method: str,
    params: dict | None,
    *,
    roster: Sequence[Mapping[str, Any]],
) -> dict:
    """Return the ``payload`` for a Hermes owner's control-plane RPC.

    MINIMAL by design — just enough for the ``/chat`` page to mount and the turn
    to route:

      - ``agents.list``   → the seeded roster (Friday + Atlas), shaped for the
        frontend. ``roster`` is fetched by the caller (``fetch_hermes_roster``)
        only for this method.
      - ``sessions.list`` → an empty session list. Hermes has no OpenClaw session
        store to enumerate for the spike; an empty list is a valid mount answer.
      - anything else      → an empty-OK ``{}``. This is the graceful
        "unanswered" path: SWR reads an empty object without error, so no
        unhandled Hermes RPC (health, channels.status, …) 500s the page.

    Phase C migrates the full RPC surface onto ``HermesRuntime.call_rpc``; this
    is deliberately not that.
    """
    if method == "agents.list":
        return build_agents_list_response(roster)
    if method == "sessions.list":
        return {"sessions": []}
    return {}


async def fetch_hermes_roster(owner_id: str) -> list[dict]:
    """Fetch the owner's live Paperclip roster (Friday + Atlas), or ``[]``.

    Reuses ``fetch_paperclip_agents_for_owner`` — the SAME canonical roster
    source container provisioning and the OpenClaw seeder use — so the Hermes
    ``agents.list`` reflects exactly the agents that were seeded. Best-effort:
    any failure (Paperclip down, no company row yet) returns ``[]`` so
    ``agents.list`` degrades to an empty roster rather than erroring the page.

    Plumbing (httpx / admin client / DDB repo) is imported + constructed lazily
    here — never at module import — mirroring ``ecs_manager``'s roster fetch.
    """
    try:
        import httpx

        from core.config import settings
        from core.repositories.paperclip_repo import PaperclipRepo
        from core.services.paperclip_admin_client import PaperclipAdminClient
        from core.services.paperclip_openclaw_pairing import (
            fetch_paperclip_agents_for_owner,
        )

        http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
        try:
            repo = PaperclipRepo(table_name="paperclip-companies")
            admin_client = PaperclipAdminClient(http_client=http)
            return await fetch_paperclip_agents_for_owner(
                owner_id=owner_id,
                repo=repo,
                admin_client=admin_client,
            )
        finally:
            await http.aclose()
    except Exception:  # noqa: BLE001 — roster is best-effort; never break mount.
        logger.warning("hermes chat bridge: roster fetch failed for owner=%s", owner_id, exc_info=True)
        return []
