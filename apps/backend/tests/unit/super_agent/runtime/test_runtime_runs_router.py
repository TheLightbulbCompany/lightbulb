"""Router-mode run identities (wave-2 O Task 4): the lane's Router session, no raw user id."""

import re

from core import tasks_schema as ts
from core.services import runtime_runs

CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BATCH = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


def test_router_identity_wire_shape():
    lane = ts.router_lane_key("user_sam")
    room = ts.router_room_key("dm-room")
    wire = runtime_runs.RunIdentity("r1", "org_1", BATCH, 1, CEO, mode="router", lane=lane, room=room).to_wire()
    assert wire["mode"] == "router" and wire["lane"] == lane
    assert wire["sessionKey"] == f"agent:{CEO}:lightbulb:router:{lane}:{room}"
    assert re.fullmatch(ts.ROUTER_SESSION_KEY_PATTERN, wire["sessionKey"])
    assert "user_sam" not in str(wire)


def test_worker_identity_unchanged():
    wire = runtime_runs.RunIdentity("r1", "org_1", BATCH, 2, CEO).to_wire()
    assert wire["mode"] == "worker" and "lane" not in wire
    assert wire["sessionKey"] == ts.worker_session_key(CEO, BATCH)


def test_status_carries_decisions():
    status = runtime_runs._status({"run_id": "r1", "status": "completed", "decisions": [{"kind": "ignore"}]})
    assert status.decisions == [{"kind": "ignore"}]
    assert runtime_runs._status({"run_id": "r1", "status": "running"}).decisions == []


def test_router_model_on_the_wire():
    wire = runtime_runs.RunIdentity(
        "r1", "org_1", BATCH, 1, CEO, mode="router", lane="shared", room="r:0123456789abcdef", model="m-small"
    ).to_wire()
    assert wire["model"] == "m-small"


def test_router_identity_names_its_room():
    room = ts.router_room_key("11111111-1111-4111-8111-111111111111")
    wire = runtime_runs.RunIdentity("r1", "org_1", BATCH, 1, CEO, mode="router", lane="shared", room=room).to_wire()
    assert wire["room"] == room and wire["sessionKey"] == f"agent:{CEO}:lightbulb:router:shared:{room}"
    assert re.fullmatch(ts.ROUTER_SESSION_KEY_PATTERN, wire["sessionKey"])


def test_router_identity_without_a_room_is_refused():
    """No Router run may use the old per-lane session, whose history mixed every room."""
    import pytest

    with pytest.raises(ValueError):
        runtime_runs.RunIdentity("r1", "org_1", BATCH, 1, CEO, mode="router", lane="shared").to_wire()
    assert not re.fullmatch(ts.ROUTER_SESSION_KEY_PATTERN, f"agent:{CEO}:lightbulb:router:shared")


def test_a_callers_router_run_says_so_on_the_wire():
    """transfer_call is offered on a phone caller's batch only: the bridge adds its toolset on `call`."""
    room = "r:0123456789abcdef"
    wire = runtime_runs.RunIdentity("r1", "org_1", BATCH, 1, CEO, mode="router", lane="shared", room=room, call=True)
    assert wire.to_wire()["call"] is True
    plain = runtime_runs.RunIdentity("r1", "org_1", BATCH, 1, CEO, mode="router", lane="shared", room=room)
    assert "call" not in plain.to_wire()
