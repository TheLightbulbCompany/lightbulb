"""Hermes HTTP+SSE chat transport (H2.2).

Two layers:
  1. Unit tests over ``_translate_event`` / ``_auto_approve_body`` / ``_iter_sse``
     — feed each Hermes SSE event and assert the EXACT internal ChatEvent, so a
     drift from OpenClaw's rendered shape fails loudly.
  2. ``send_chat`` integration over an ``httpx.MockTransport`` fake Hermes server
     (same direct-httpx-mock approach as ``tests/test_paperclip_admin_client.py``):
     happy-path stream, drop-mid-run poll recovery, and in-band approval.
"""

import json

import httpx
import pytest

from core.runtime.base import ChatResult
from core.runtime.hermes.adapter import (
    HERMES_API_PORT,
    HermesRuntime,
    _auto_approve_body,
    _iter_sse,
    _merge_instructions,
    _translate_event,
)

# ---------------------------------------------------------------------------
# _translate_event — the unit-test core (exact ChatEvent per Hermes event)
# ---------------------------------------------------------------------------


def test_translate_message_delta_to_chunk():
    assert _translate_event("message.delta", {"delta": "hello"}) == {
        "type": "chunk",
        "content": "hello",
    }


def test_translate_message_delta_empty_is_dropped():
    # An empty delta has no renderable content — mirror OpenClaw dropping empty
    # assistant text (connection_pool.py:634-636).
    assert _translate_event("message.delta", {"delta": ""}) is None
    assert _translate_event("message.delta", {}) is None


def test_translate_reasoning_to_thinking():
    assert _translate_event("reasoning.available", {"text": "let me think"}) == {
        "type": "thinking",
        "content": "let me think",
    }


def test_translate_reasoning_empty_is_dropped():
    assert _translate_event("reasoning.available", {"text": ""}) is None


def test_translate_tool_started_to_tool_start():
    assert _translate_event("tool.started", {"tool": "search", "preview": "..."}) == {
        "type": "tool_start",
        "tool": "search",
    }


def test_translate_tool_completed_to_tool_end():
    assert _translate_event("tool.completed", {"tool": "search", "duration": 1.2}) == {
        "type": "tool_end",
        "tool": "search",
    }


def test_translate_tool_completed_with_error_to_tool_error():
    # OpenClaw splits tool_error from tool_end on isError; Hermes carries the
    # same signal as a truthy ``error`` (connection_pool.py:674-677).
    assert _translate_event("tool.completed", {"tool": "search", "error": "boom"}) == {
        "type": "tool_error",
        "tool": "search",
    }


def test_translate_tool_missing_name_is_dropped():
    assert _translate_event("tool.started", {}) is None
    assert _translate_event("tool.completed", {}) is None


def test_translate_run_completed_to_done():
    assert _translate_event("run.completed", {"output": "answer", "usage": {}}) == {"type": "done"}


def test_translate_run_failed_to_error():
    assert _translate_event("run.failed", {"error": "kaboom"}) == {
        "type": "error",
        "message": "kaboom",
    }


def test_translate_run_failed_without_message_gets_default():
    assert _translate_event("run.failed", {}) == {
        "type": "error",
        "message": "Agent run failed",
    }


def test_translate_run_cancelled_matches_openclaw_aborted_shape():
    # Verbatim match to connection_pool.py:1366 so cancel renders identically.
    assert _translate_event("run.cancelled", {}) == {
        "type": "error",
        "message": "Agent run was cancelled",
    }


def test_translate_unknown_event_is_passthrough_none():
    assert _translate_event("something.new", {"x": 1}) is None
    assert _translate_event("approval.request", {"choices": ["always"]}) is None


# ---------------------------------------------------------------------------
# _auto_approve_body — the single auto-approve policy site
# ---------------------------------------------------------------------------


def test_auto_approve_body_defaults_to_always():
    assert _auto_approve_body({}) == {"choice": "always"}


def test_auto_approve_body_echoes_request_id():
    assert _auto_approve_body({"approval_id": "ap_9"}) == {
        "choice": "always",
        "approval_id": "ap_9",
    }


# ---------------------------------------------------------------------------
# _iter_sse — SSE frame parsing
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal stand-in for an httpx streaming response's ``aiter_lines``."""

    def __init__(self, text: str):
        # httpx.aiter_lines yields lines without their trailing newline.
        self._lines = text.split("\n")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


async def test_iter_sse_parses_event_and_data_frames():
    body = 'event: message.delta\ndata: {"delta": "hi"}\n\nevent: run.completed\ndata: {"output": "done"}\n\n'
    frames = [f async for f in _iter_sse(_FakeStream(body))]
    assert frames == [
        ("message.delta", {"delta": "hi"}),
        ("run.completed", {"output": "done"}),
    ]


async def test_iter_sse_falls_back_to_event_field_in_json():
    # No ``event:`` line — the event name rides the JSON payload.
    body = 'data: {"event": "message.delta", "delta": "yo"}\n\n'
    frames = [f async for f in _iter_sse(_FakeStream(body))]
    assert frames == [("message.delta", {"event": "message.delta", "delta": "yo"})]


async def test_iter_sse_skips_comments_and_non_object_data():
    body = ": heartbeat\ndata: 123\n\nevent: run.completed\ndata: {}\n\n"
    frames = [f async for f in _iter_sse(_FakeStream(body))]
    assert frames == [("run.completed", {})]


# ---------------------------------------------------------------------------
# send_chat — integration over a MockTransport fake Hermes server
# ---------------------------------------------------------------------------

_IP = "10.0.0.9"
_KEY = "api-key-abc"
_RUN_ID = "run_1"


def _sse(frames: list[tuple[str, dict]]) -> bytes:
    # Real Hermes emits data-ONLY frames: ``data: {json}\n\n`` with the event
    # name carried inside the JSON payload under an ``"event"`` key (live frames
    # also carry ``run_id``/``timestamp``, which the transport ignores). Emitting
    # the event name inside the JSON — not as an ``event:`` line — drives the
    # data-only dispatch path of ``_iter_sse`` the live server actually hits.
    return "".join(f"data: {json.dumps({'event': name, **payload})}\n\n" for name, payload in frames).encode()


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _install(monkeypatch, handler) -> list[tuple[str, str]]:
    """Wire a fake Hermes server: patch _resolve to a fixed (ip, key) and the
    client factory to a MockTransport client. Returns the recorded call log."""

    calls: list[tuple[str, str]] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return handler(request)

    async def fake_resolve(self, owner_id):
        return _IP, _KEY

    monkeypatch.setattr(HermesRuntime, "_resolve", fake_resolve)
    monkeypatch.setattr(
        "core.runtime.hermes.adapter._client_factory",
        lambda: _mock_client(recording_handler),
    )
    return calls


async def _collect(rt: HermesRuntime) -> tuple[list[dict], ChatResult | None]:
    events: list[dict] = []

    async def on_event(ev):
        events.append(ev)

    result = await rt.send_chat("org_1", "agent_x", "hi there", "agent:agent_x:u1", on_event)
    return events, result


async def test_send_chat_happy_path_streams_and_completes(monkeypatch):
    sse_body = _sse(
        [
            ("message.delta", {"delta": "Hel"}),
            ("message.delta", {"delta": "lo"}),
            ("tool.started", {"tool": "search"}),
            ("tool.completed", {"tool": "search"}),
            ("run.completed", {"output": "Hello", "usage": {"tokens": 5}}),
        ]
    )

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            # Assert the run body carries the user turn + session scope.
            sent = json.loads(request.content)
            assert sent["input"] == "hi there"
            assert sent["session_id"] == "agent:agent_x:u1"
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.method == "GET" and request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    calls = _install(monkeypatch, handler)
    events, result = await _collect(HermesRuntime())

    assert events == [
        {"type": "chunk", "content": "Hel"},
        {"type": "chunk", "content": "lo"},
        {"type": "tool_start", "tool": "search"},
        {"type": "tool_end", "tool": "search"},
        {"type": "done"},
    ]
    assert result == ChatResult(run_id=_RUN_ID, status="completed", output="Hello")
    # POST run then GET events; no poll (terminal seen on the stream).
    assert ("POST", "/v1/runs") in calls
    assert ("GET", f"/v1/runs/{_RUN_ID}/events") in calls
    assert ("GET", f"/v1/runs/{_RUN_ID}") not in calls


async def test_send_chat_auth_header_carries_api_server_key(monkeypatch):
    seen_auth: list[str | None] = []

    def handler(request):
        seen_auth.append(request.headers.get("authorization"))
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=_sse([("run.completed", {})]))
        return httpx.Response(404)

    _install(monkeypatch, handler)
    await _collect(HermesRuntime())
    assert seen_auth and all(h == f"Bearer {_KEY}" for h in seen_auth)


async def test_send_chat_targets_the_hermes_port(monkeypatch):
    hosts: list[str] = []

    def handler(request):
        hosts.append(request.url.netloc.decode())
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        return httpx.Response(200, content=_sse([("run.completed", {})]))

    _install(monkeypatch, handler)
    await _collect(HermesRuntime())
    assert hosts and all(h == f"{_IP}:{HERMES_API_PORT}" for h in hosts)


async def test_send_chat_drop_mid_run_polls_for_terminal(monkeypatch):
    # The events stream ends after a delta WITHOUT a terminal event (a drop);
    # send_chat must poll GET /v1/runs/{id} and emit the terminal from there.
    sse_body = _sse([("message.delta", {"delta": "partial"})])

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=sse_body)
        if request.method == "GET" and request.url.path == f"/v1/runs/{_RUN_ID}":
            # Poll resolves the run as terminal — the answer survived the drop.
            return httpx.Response(200, json={"status": "completed", "output": "final answer"})
        return httpx.Response(404)

    calls = _install(monkeypatch, handler)
    events, result = await _collect(HermesRuntime())

    assert events == [
        {"type": "chunk", "content": "partial"},
        {"type": "done"},
    ]
    assert result == ChatResult(run_id=_RUN_ID, status="completed", output="final answer")
    # The poll GET fired as the recovery path.
    assert ("GET", f"/v1/runs/{_RUN_ID}") in calls


async def test_send_chat_drop_then_poll_reports_failure(monkeypatch):
    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=_sse([]))  # empty stream = drop
        if request.url.path == f"/v1/runs/{_RUN_ID}":
            return httpx.Response(200, json={"status": "failed", "error": "oom"})
        return httpx.Response(404)

    _install(monkeypatch, handler)
    events, result = await _collect(HermesRuntime())
    assert events == [{"type": "error", "message": "oom"}]
    assert result == ChatResult(run_id=_RUN_ID, status="failed", output=None)


async def test_send_chat_poll_all_attempts_404_emits_terminal_error(monkeypatch):
    # The stream drops with no terminal event, then EVERY poll attempt 404s (the
    # run's status was swept after its TTL, or the run_id is unknown). send_chat
    # must NOT let the httpx 404 escape — it retries within the bound and, on
    # exhaustion, emits the same generic terminal error the budget-exhaustion
    # path emits so the UI always settles.
    monkeypatch.setattr("core.runtime.hermes.adapter._POLL_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("core.runtime.hermes.adapter._POLL_INTERVAL_S", 0)

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=_sse([]))  # empty stream = drop
        if request.url.path == f"/v1/runs/{_RUN_ID}":
            return httpx.Response(404)  # every poll attempt 404s
        return httpx.Response(404)

    _install(monkeypatch, handler)
    events, result = await _collect(HermesRuntime())
    assert events == [{"type": "error", "message": "run did not reach a terminal state"}]
    assert result == ChatResult(run_id=_RUN_ID, status="unknown", output=None)


async def test_send_chat_auto_approves_and_continues(monkeypatch):
    # An approval.request blocks the run until answered; send_chat must POST the
    # approval (emitting nothing to the UI) and the run continues to completion.
    sse_body = _sse(
        [
            ("approval.request", {"approval_id": "ap_1", "choices": ["always", "deny"]}),
            ("message.delta", {"delta": "ok"}),
            ("run.completed", {"output": "ok"}),
        ]
    )
    approval_bodies: list[dict] = []

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=sse_body)
        if request.method == "POST" and request.url.path == f"/v1/runs/{_RUN_ID}/approval":
            approval_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    calls = _install(monkeypatch, handler)
    events, result = await _collect(HermesRuntime())

    # Approval fired with the broad grant + echoed id; UI saw nothing for it.
    assert approval_bodies == [{"choice": "always", "approval_id": "ap_1"}]
    assert ("POST", f"/v1/runs/{_RUN_ID}/approval") in calls
    assert events == [
        {"type": "chunk", "content": "ok"},
        {"type": "done"},
    ]
    assert result == ChatResult(run_id=_RUN_ID, status="completed", output="ok")


async def test_send_chat_cold_container_raises(monkeypatch):
    # _resolve raises ContainerNotRunningError for a cold container — same
    # caller contract as call_rpc.
    from core.runtime.base import ContainerNotRunningError

    async def cold_resolve(self, owner_id):
        raise ContainerNotRunningError("cold")

    monkeypatch.setattr(HermesRuntime, "_resolve", cold_resolve)

    async def on_event(ev):  # pragma: no cover - must never be called
        raise AssertionError("no events on a cold container")

    with pytest.raises(ContainerNotRunningError):
        await HermesRuntime().send_chat("org_1", "a", "hi", "agent:a:u1", on_event)


# ---------------------------------------------------------------------------
# _merge_instructions — persona-first-then-context invariant
# ---------------------------------------------------------------------------


def test_merge_instructions_persona_first_then_context():
    merged = _merge_instructions("You are Friday.", "Issue #42 context.")
    assert merged == "You are Friday.\n\nIssue #42 context."
    # The load-bearing invariant: persona precedes context, never the reverse.
    assert merged.index("You are Friday.") < merged.index("Issue #42 context.")


def test_merge_instructions_persona_only():
    assert _merge_instructions("You are Friday.", None) == "You are Friday."


def test_merge_instructions_context_only():
    # Unseeded agent + a per-run context → context stands alone.
    assert _merge_instructions(None, "Issue context.") == "Issue context."


def test_merge_instructions_neither_is_none():
    assert _merge_instructions(None, None) is None
    assert _merge_instructions("  ", "") is None


async def test_send_chat_merges_persona_and_context_and_sets_session_header(monkeypatch):
    # A seeded persona MERGED persona-first with the caller's per-run context,
    # plus the X-Hermes-Session-Key header on the run POST.
    monkeypatch.setattr("core.runtime.hermes.adapter._read_persona", lambda o, a: "You are Friday.")
    captured: dict = {}

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            captured["body"] = json.loads(request.content)
            captured["session_header"] = request.headers.get("x-hermes-session-key")
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        return httpx.Response(200, content=_sse([("run.completed", {})]))

    _install(monkeypatch, handler)

    async def on_event(ev):
        pass

    await HermesRuntime().send_chat(
        "org_1", "agent_x", "hi there", "agent:agent_x:u1", on_event, context="Issue #42 context."
    )
    instructions = captured["body"]["instructions"]
    assert instructions == "You are Friday.\n\nIssue #42 context."
    assert instructions.index("You are Friday.") < instructions.index("Issue #42 context.")
    # The session key rides the header (bag is header-fed, not body-fed) AND the
    # body session_id.
    assert captured["session_header"] == "agent:agent_x:u1"
    assert captured["body"]["session_id"] == "agent:agent_x:u1"


async def test_send_chat_persona_only_when_no_context(monkeypatch):
    monkeypatch.setattr("core.runtime.hermes.adapter._read_persona", lambda o, a: "You are Friday.")
    captured: dict = {}

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            captured["body"] = json.loads(request.content)
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        return httpx.Response(200, content=_sse([("run.completed", {})]))

    _install(monkeypatch, handler)

    async def on_event(ev):
        pass

    await HermesRuntime().send_chat("org_1", "agent_x", "hi", "agent:agent_x:u1", on_event)
    assert captured["body"]["instructions"] == "You are Friday."


async def test_openclaw_send_chat_is_phase_c_stub():
    # OpenClaw chat still routes through websocket_chat/the pool; the port method
    # is a deliberate deferral so the Protocol member is present but unused.
    from core.runtime.openclaw.adapter import OpenClawRuntime

    async def on_event(ev):  # pragma: no cover
        pass

    with pytest.raises(NotImplementedError, match="Phase C"):
        await OpenClawRuntime().send_chat("org_1", "a", "hi", "agent:a:u1", on_event)
