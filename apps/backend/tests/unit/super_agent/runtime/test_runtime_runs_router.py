"""Router-mode run identities (wave-2 O Task 4): the lane's Router session, no raw user id."""

import re

from core import tasks_schema as ts
from core.services import runtime_runs

CEO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BATCH = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


def test_router_identity_wire_shape():
    lane = ts.router_lane_key("user_sam")
    wire = runtime_runs.RunIdentity("r1", "org_1", BATCH, 1, CEO, mode="router", lane=lane).to_wire()
    assert wire["mode"] == "router" and wire["lane"] == lane
    assert wire["sessionKey"] == f"agent:{CEO}:lightbulb:router:{lane}"
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
        "r1", "org_1", BATCH, 1, CEO, mode="router", lane="shared", model="m-small"
    ).to_wire()
    assert wire["model"] == "m-small"
