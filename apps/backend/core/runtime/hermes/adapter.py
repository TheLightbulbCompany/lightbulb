"""Hermes runtime adapter: the second agent-runtime behind the brand-blind port.

H2.2 lands the CHAT-TRANSPORT facet (``send_chat`` + the SSE→ChatEvent
translation). The provisioner / seeder / usage facets remain
``NotImplementedError`` stubs until H2.3–H2.5.

Hermes' chat surface is a two-call HTTP shape, NOT inline streaming:
  1. ``POST /v1/runs`` -> 202 ``{"run_id", "status":"started"}`` — starts the run.
  2. ``GET /v1/runs/{run_id}/events`` -> Server-Sent Events (``data: {json}\\n\\n``,
     no ``id:`` framing, no resume — the server destroys the queue on disconnect).
  3. ``GET /v1/runs/{run_id}`` -> terminal status poll (the recovery path when the
     SSE stream drops mid-run: ``resumable_stream=False``, so re-attaching 404s).
  4. ``POST /v1/runs/{run_id}/approval`` -> answer an in-band approval prompt.
Every call carries ``Authorization: Bearer <API_SERVER_KEY>``; the host is
``http://{container_ip}:8642`` (plain HTTP, intra-VPC).

``_translate_event`` maps each Hermes SSE event onto the internal ChatEvent
shape the frontend already renders (the SAME shape OpenClaw's
``GatewayConnectionPool._transform_agent_event`` + chat-terminal forwarding
produce). That match is over the VISIBLE-CONTENT shape only: Hermes events do
NOT carry the ``runId``/``agent_id``/``session_id`` routing keys the frontend
uses for multi-bubble routing + per-session demux — the routing envelope keys
are added by the chat-routing layer at the ``on_event`` seam.

Import-lightness is load-bearing: registering Hermes means importing
``core.runtime`` imports this module, and the subprocess tripwire in the runtime
tests asserts ``core.containers`` (AWS clients at import) never leaks into that
path. Module-level imports stay stdlib/typing + ``core.runtime.base``; httpx and
``core.containers`` are imported INSIDE methods, lazily, exactly as the OpenClaw
adapter does for ``core.containers``.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from core.runtime.base import (
    Capabilities,
    ChatEventCallback,
    ChatResult,
    ConfigProfile,
    ContainerNotRunningError,
    ContainerSpec,
    OwnerProfile,
    ProviderCreds,
    ProvisionCtx,
    SessionSurface,
)

logger = logging.getLogger(__name__)

# The H1.1-confirmed Hermes capability set. Hermes has no desktop-pairing /
# device exec-approval, no resumable stream, and no durable/structured/per-run
# run model; it does give each agent its own workspace and supports multiple
# agents. Divergence from OpenClaw's all-True set is intentional and verified.
HERMES_CAPABILITIES = Capabilities(
    desktop_pairing=False,
    device_exec_approval=False,
    resumable_stream=False,
    per_agent_workspace=True,
    multi_agent=True,
    durable_runs=False,
    structured_run_context=False,
    per_run_model=False,
)

# Hermes' API server listens on this fixed port inside the container (plain
# HTTP, intra-VPC). Mirrors OpenClaw's GATEWAY_PORT constant.
HERMES_API_PORT = 8642

# Timeouts. The SSE read timeout is deliberately generous but finite: a stream
# that idles past it raises ``httpx.ReadTimeout`` (an ``httpx.HTTPError``),
# which the send_chat loop treats as a dropped connection and recovers from via
# the terminal-status poll. The poll retries a bounded number of times.
_POST_TIMEOUT_S = 30.0
_SSE_READ_TIMEOUT_S = 120.0
_POLL_TIMEOUT_S = 30.0
_POLL_MAX_ATTEMPTS = 30
_POLL_INTERVAL_S = 1.0

# Terminal SSE events and their brand-neutral status label.
_TERMINAL_EVENT_STATUS = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}

# Terminal poll statuses (``GET /v1/runs/{id}`` ``status`` field) mapped back
# onto the SSE event name so the poll-recovery path reuses ``_translate_event``
# for its terminal ChatEvent — one translation table, not two.
_POLL_STATUS_TO_EVENT = {
    "completed": "run.completed",
    "failed": "run.failed",
    "cancelled": "run.cancelled",
}

# The single place the spike's auto-approve policy lives.
_AUTO_APPROVE_CHOICE = "always"


def _client_factory() -> "Any":  # returns httpx.AsyncClient
    """Build the HTTP client for Hermes calls. Module-level so tests can swap in
    a ``MockTransport``-backed client; httpx is imported lazily to keep the
    runtime package import-light (the tripwire constraint)."""
    import httpx

    return httpx.AsyncClient(timeout=httpx.Timeout(_POST_TIMEOUT_S))


def _translate_event(event: str, data: Mapping[str, Any]) -> dict | None:
    """Translate ONE Hermes SSE event into the internal ChatEvent shape.

    The output dicts match ``GatewayConnectionPool._transform_agent_event`` and
    the pool's chat-terminal forwarding EXACTLY (connection_pool.py:633-720 for
    chunk/thinking/tool, :1322/:1355/:1366 for done/error/aborted) — but only
    for the VISIBLE-CONTENT shape. These ChatEvents do NOT carry the
    ``runId``/``agent_id``/``session_id`` routing keys the frontend needs for
    multi-bubble routing + per-session demux; the routing envelope keys are
    added by the chat-routing layer at the ``on_event`` seam:

      - ``message.delta``       -> ``{type:"chunk", content}``
      - ``reasoning.available`` -> ``{type:"thinking", content}``
      - ``tool.started``        -> ``{type:"tool_start", tool}``
      - ``tool.completed``      -> ``{type:"tool_end", tool}`` (or ``tool_error``
                                   when the completion carries an ``error``, to
                                   mirror OpenClaw's isError→tool_error split)
      - ``run.completed``       -> ``{type:"done"}``
      - ``run.failed``          -> ``{type:"error", message}``
      - ``run.cancelled``       -> ``{type:"error", message:"Agent run was cancelled"}``

    Returns ``None`` for events with no UI counterpart (unknown events, and any
    known event whose required payload field is empty) — the caller skips them.
    ``approval.request`` is NOT handled here; it is answered in ``send_chat``
    (auto-approve) and emits nothing to the UI. This is deliberately NOT a
    filter over the stream: nothing renderable is dropped.
    """
    if event == "message.delta":
        delta = data.get("delta", "")
        return {"type": "chunk", "content": delta} if delta else None

    if event == "reasoning.available":
        text = data.get("text", "")
        return {"type": "thinking", "content": text} if text else None

    if event == "tool.started":
        tool = data.get("tool", "")
        return {"type": "tool_start", "tool": tool} if tool else None

    if event == "tool.completed":
        tool = data.get("tool", "")
        if not tool:
            return None
        # OpenClaw distinguishes tool_error from tool_end on the result's
        # isError flag; Hermes carries the same signal as a truthy ``error``.
        if data.get("error"):
            return {"type": "tool_error", "tool": tool}
        return {"type": "tool_end", "tool": tool}

    if event == "run.completed":
        # Usage lives on the same terminal frame; H2.5 reads it. This facet only
        # signals turn-end (matching OpenClaw's chat.final -> {type:"done"}).
        return {"type": "done"}

    if event == "run.failed":
        msg = data.get("error") or "Agent run failed"
        return {"type": "error", "message": str(msg)}

    if event == "run.cancelled":
        # Match OpenClaw's aborted shape verbatim (connection_pool.py:1366) so
        # the frontend's cancel handling is identical across runtimes.
        return {"type": "error", "message": "Agent run was cancelled"}

    # Unknown / non-renderable event: pass through silently (do NOT invent
    # filtering — an unrecognized event simply has no UI counterpart yet).
    return None


def _auto_approve_body(data: Mapping[str, Any]) -> dict:
    """Build the body that answers an ``approval.request`` prompt.

    An ``approval.request`` event BLOCKS the Hermes run in the executor until
    answered (choices: once/session/always/deny). The per-user container is the
    user's own sandbox — equivalent trust to OpenClaw's allowCommands — so the
    spike answers every prompt with the broadest grant (``always``) and surfaces
    nothing to the UI. If the request carries an identifier, echo it back so the
    executor can correlate the answer to the prompt. This is the ONE place the
    auto-approve policy lives; H4 revisits whether prompts should reach the UI.
    """
    body: dict[str, Any] = {"choice": _AUTO_APPROVE_CHOICE}
    for key in ("id", "approval_id", "request_id", "call_id"):
        if data.get(key):
            body[key] = data[key]
            break
    return body


async def _iter_sse(response: Any) -> AsyncIterator[tuple[str, dict]]:
    """Parse an httpx streaming ``response`` as Server-Sent Events.

    Yields ``(event_name, data)`` per frame. Hermes frames use ``event:`` +
    ``data:`` lines terminated by a blank line, with no ``id:`` framing. The
    event name comes from the ``event:`` field; if absent it falls back to an
    ``event``/``type`` key inside the JSON payload. Frames whose data is not a
    JSON object are skipped (nothing renderable to translate).
    """
    event_name = ""
    data_lines: list[str] = []

    def _flush() -> tuple[str, dict] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        name = event_name or payload.get("event") or payload.get("type") or ""
        return str(name), payload

    async for line in response.aiter_lines():
        if line == "":
            frame = _flush()
            if frame is not None:
                yield frame
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):  # SSE comment / heartbeat
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value.strip()
        elif field == "data":
            data_lines.append(value)

    # Flush a trailing frame not terminated by a blank line.
    frame = _flush()
    if frame is not None:
        yield frame


class HermesRuntime:
    kind = "hermes"
    capabilities = HERMES_CAPABILITIES

    @property
    def sessions(self) -> SessionSurface:
        raise NotImplementedError("HermesRuntime.sessions lands in H2.x")

    async def _resolve(self, owner_id: str) -> tuple[str, str]:
        """Resolve the owner's running container to ``(ip, api_server_key)``.

        Mirrors ``OpenClawRuntime._resolve``: the ECS manager discovers the task
        IP, and the auth material rides the container row. Hermes authenticates
        every call with a per-container ``API_SERVER_KEY``.

        NOTE (H3): the container-row field name for that key is provisioned by
        H3; this reads ``container["api_server_key"]`` as the expected field. If
        H3 lands it under a different key, update this single accessor.
        """
        # Lazy import: core.containers builds AWS clients at import. Same
        # import-lightness reason as the OpenClaw adapter's _get_ecs.
        from core.containers import get_ecs_manager

        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        key = container.get("api_server_key") if container else None
        if not container or not ip or not key:
            # A container row without an API server key is as unreachable as a
            # cold container — no HTTP call is attempted either way.
            raise ContainerNotRunningError(f"no running container or api_server_key for {owner_id}")
        return ip, key

    async def send_chat(
        self,
        owner_id: str,
        agent_id: str,
        message: str,
        session_key: str,
        on_event: ChatEventCallback,
    ) -> ChatResult | None:
        """Drive ONE user turn against the owner's Hermes container.

        Starts the run (``POST /v1/runs``), opens the SSE stream, translates each
        event into the internal ChatEvent shape and awaits ``on_event`` per
        event. In-band ``approval.request`` prompts are auto-answered inline so
        the run never blocks. Returns the terminal ``ChatResult`` once the run
        completes/fails/cancels.

        Recovery: Hermes streams are NOT resumable, so if the SSE connection
        drops before a terminal event (network error or read-timeout), this does
        NOT re-attach (a second events GET 404s). It falls back to polling
        ``GET /v1/runs/{run_id}`` until the run is terminal and emits the
        terminal ChatEvent from that. Intermediate deltas are lost on a drop;
        the final answer is not.

        ``session_key`` (``agent:{agent_id}:{user_id}[:{session_id}]``) already
        encodes the agent, so it is passed straight through as Hermes'
        ``session_id`` to scope the run's conversation. Hermes DROPS ``metadata``
        on the run body — any Paperclip/context must ride ``input``/``instructions``
        text, which H3 threads; H2.2 sends the bare user turn.
        """
        import httpx

        ip, key = await self._resolve(owner_id)
        base = f"http://{ip}:{HERMES_API_PORT}"
        headers = {"Authorization": f"Bearer {key}"}
        # Hermes reads ``input`` (the user turn) + ``session_id`` (conversation
        # scope). ``metadata`` is dropped server-side, so nothing rides it.
        run_body = {"input": message, "session_id": session_key}

        async with _client_factory() as client:
            resp = await client.post(f"{base}/v1/runs", json=run_body, headers=headers)
            resp.raise_for_status()
            run_id = resp.json()["run_id"]

            saw_terminal = False
            try:
                async with client.stream(
                    "GET",
                    f"{base}/v1/runs/{run_id}/events",
                    headers=headers,
                    timeout=httpx.Timeout(_POST_TIMEOUT_S, read=_SSE_READ_TIMEOUT_S),
                ) as stream:
                    async for event, data in _iter_sse(stream):
                        if event == "approval.request":
                            # Answer inline so the executor unblocks; emit nothing.
                            await client.post(
                                f"{base}/v1/runs/{run_id}/approval",
                                json=_auto_approve_body(data),
                                headers=headers,
                            )
                            continue
                        chat_event = _translate_event(event, data)
                        if chat_event is not None:
                            await on_event(chat_event)
                        if event in _TERMINAL_EVENT_STATUS:
                            saw_terminal = True
                            return ChatResult(
                                run_id=run_id,
                                status=_TERMINAL_EVENT_STATUS[event],
                                output=data.get("output"),
                            )
            except httpx.HTTPError as exc:
                # Dropped stream mid-run — fall through to the poll recovery.
                logger.warning(
                    "hermes SSE stream dropped for owner=%s run=%s: %s — polling for terminal",
                    owner_id,
                    run_id,
                    exc,
                )

            if not saw_terminal:
                # Stream ended (cleanly or via a caught drop) without a terminal
                # event. Poll the run to resolve its final state.
                return await self._poll_terminal(client, base, run_id, headers, on_event)

        return None

    async def _poll_terminal(
        self,
        client: Any,
        base: str,
        run_id: str,
        headers: dict[str, str],
        on_event: ChatEventCallback,
    ) -> ChatResult:
        """Poll ``GET /v1/runs/{run_id}`` until terminal, then emit + return it.

        The recovery path for a dropped SSE stream (Hermes runs are not
        resumable). Emits the terminal ChatEvent (reusing ``_translate_event``
        via ``_POLL_STATUS_TO_EVENT``) exactly once. If the run never reaches a
        terminal state within the retry budget, emits a generic error ChatEvent
        so the UI never hangs.
        """
        import httpx

        for _ in range(_POLL_MAX_ATTEMPTS):
            try:
                resp = await client.get(
                    f"{base}/v1/runs/{run_id}",
                    headers=headers,
                    timeout=httpx.Timeout(_POLL_TIMEOUT_S),
                )
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as exc:
                # A single poll attempt failing must not escape send_chat with no
                # terminal event: GET /v1/runs/{id} 404s once its ~3600s status
                # TTL sweeps the run (or on an unknown run_id), and transport
                # errors are transient. Retry within the existing bound; if every
                # attempt errors the loop exhausts and falls through to the same
                # generic terminal error the budget-exhaustion path emits.
                logger.warning(
                    "hermes poll attempt failed for run=%s: %s — retrying",
                    run_id,
                    exc,
                )
                await asyncio.sleep(_POLL_INTERVAL_S)
                continue
            status = body.get("status")
            event_name = _POLL_STATUS_TO_EVENT.get(status)
            if event_name is not None:
                chat_event = _translate_event(event_name, body)
                if chat_event is not None:
                    await on_event(chat_event)
                return ChatResult(run_id=run_id, status=status, output=body.get("output"))
            await asyncio.sleep(_POLL_INTERVAL_S)

        # Never went terminal within budget — surface an error so the UI settles.
        await on_event({"type": "error", "message": "run did not reach a terminal state"})
        return ChatResult(run_id=run_id, status="unknown", output=None)

    async def call_rpc(
        self,
        owner_id: str,
        method: str,
        params: dict | None = None,
        *,
        fresh: bool = False,
        timeout: float | None = None,
        open_timeout: float | None = None,
        recv_timeout: float | None = None,
        rpc_timeout: float | None = None,
    ) -> Any:
        raise NotImplementedError("HermesRuntime.call_rpc lands in H2.x")

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool:
        raise NotImplementedError("HermesRuntime.revoke_provider lands in H2.x")

    async def forward_hook(
        self, owner_id: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        raise NotImplementedError("HermesRuntime.forward_hook lands in H2.x")

    # --- Provisioner facet: launching / configuring a container -----------

    def container_spec(self, owner_id: str, ctx: ProvisionCtx) -> ContainerSpec:
        raise NotImplementedError("HermesRuntime.container_spec lands in H2.x")

    async def write_config(self, owner_id: str, profile: ConfigProfile) -> None:
        raise NotImplementedError("HermesRuntime.write_config lands in H2.x")

    async def write_auth(self, owner_id: str, creds: ProviderCreds) -> None:
        raise NotImplementedError("HermesRuntime.write_auth lands in H2.x")

    # --- IdentitySeeder facet: seeding an owner's agents ------------------

    async def seed_owner_agents(self, owner_id: str, profile: OwnerProfile) -> None:
        raise NotImplementedError("HermesRuntime.seed_owner_agents lands in H2.x")
