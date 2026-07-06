"""Hermes chat bridge (H3.4) — the ``/chat`` hot-path glue for a Hermes owner.

Covers the three pure/near-pure surfaces the websocket_chat seams call:
  1. ``make_frontend_event_sink`` — the routing-envelope injection
     (``runId``/``agent_id``/``session_id``) onto each streamed ChatEvent.
  2. ``build_agents_list_response`` — Paperclip roster → the ``agents.list``
     shape the frontend's ``useAgents`` consumes (ceo = default agent).
  3. ``answer_hermes_rpc`` — the minimal control-plane dispatch (agents.list /
     sessions.list / graceful empty-OK for everything else).
"""

from unittest.mock import MagicMock

from core.runtime.hermes.chat_bridge import (
    answer_hermes_rpc,
    build_agents_list_response,
    make_frontend_event_sink,
)


# ---------------------------------------------------------------------------
# make_frontend_event_sink — routing envelope injection
# ---------------------------------------------------------------------------


async def test_event_sink_injects_full_routing_envelope():
    mgmt = MagicMock()
    sink = make_frontend_event_sink(mgmt, "conn-1", agent_id="friday", run_id="run-9", session_id="sess-abc")
    await sink({"type": "chunk", "content": "hi"})
    conn, frame = mgmt.send_message.call_args[0]
    assert conn == "conn-1"
    assert frame == {
        "type": "chunk",
        "content": "hi",
        "agent_id": "friday",
        "runId": "run-9",
        "session_id": "sess-abc",
    }


async def test_event_sink_omits_session_id_for_general_session():
    # The legacy "General" session carries no session_id — mirror the OpenClaw
    # path adding session_id only when non-None.
    mgmt = MagicMock()
    sink = make_frontend_event_sink(mgmt, "conn-1", agent_id="friday", run_id="run-9", session_id=None)
    await sink({"type": "done"})
    _, frame = mgmt.send_message.call_args[0]
    assert frame == {"type": "done", "agent_id": "friday", "runId": "run-9"}
    assert "session_id" not in frame


async def test_event_sink_does_not_mutate_the_source_event():
    # The adapter emits fresh event dicts; the sink must not mutate them.
    mgmt = MagicMock()
    sink = make_frontend_event_sink(mgmt, "conn-1", agent_id="a", run_id="r", session_id=None)
    src = {"type": "chunk", "content": "x"}
    await sink(src)
    assert src == {"type": "chunk", "content": "x"}


# ---------------------------------------------------------------------------
# build_agents_list_response — Paperclip roster → agents.list shape
# ---------------------------------------------------------------------------


def test_build_agents_list_marks_ceo_as_default():
    roster = [
        {"id": "atlas", "role": "general", "name": "Atlas"},
        {"id": "friday", "role": "ceo", "name": "Friday"},
    ]
    resp = build_agents_list_response(roster)
    assert resp["defaultId"] == "friday"
    assert resp["agents"] == [
        {"id": "atlas", "name": "Atlas", "identity": {"name": "Atlas"}},
        {"id": "friday", "name": "Friday", "identity": {"name": "Friday"}},
    ]


def test_build_agents_list_defaults_to_first_when_no_ceo():
    roster = [{"id": "a1", "role": "general", "name": "One"}, {"id": "a2", "role": "general", "name": "Two"}]
    assert build_agents_list_response(roster)["defaultId"] == "a1"


def test_build_agents_list_empty_roster_has_no_default():
    resp = build_agents_list_response([])
    assert resp == {"agents": []}
    assert "defaultId" not in resp


def test_build_agents_list_skips_entries_without_id():
    roster = [{"role": "general", "name": "Nameless"}, {"id": "friday", "role": "ceo", "name": "Friday"}]
    resp = build_agents_list_response(roster)
    assert [a["id"] for a in resp["agents"]] == ["friday"]
    assert resp["defaultId"] == "friday"


# ---------------------------------------------------------------------------
# answer_hermes_rpc — minimal control-plane dispatch
# ---------------------------------------------------------------------------


def test_answer_agents_list_returns_roster():
    roster = [{"id": "friday", "role": "ceo", "name": "Friday"}]
    payload = answer_hermes_rpc("agents.list", {}, roster=roster)
    assert payload["defaultId"] == "friday"
    assert payload["agents"][0]["id"] == "friday"


def test_answer_sessions_list_is_empty_but_valid():
    assert answer_hermes_rpc("sessions.list", {}, roster=[]) == {"sessions": []}


def test_answer_unknown_rpc_is_graceful_empty_ok():
    # health / channels.status / any unhandled RPC → empty-OK so the page never
    # 500s. None params must be tolerated too.
    assert answer_hermes_rpc("health", {}, roster=[]) == {}
    assert answer_hermes_rpc("channels.status", None, roster=[]) == {}
