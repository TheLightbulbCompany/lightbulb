import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.repositories import a2a_repo
from core.services import a2a_authority as a
from core.services import a2a_rules, a2a_tasks, network_member
from core.services.network_proof import _unb64
from tests.unit.services.conftest import repos  # noqa: F401

ORG = "org_1"


def test_header_is_capped_at_512_bytes_and_200_intent_chars():
    h = a2a_tasks.session_header(
        requester_display="user_r", for_whom="user_origin", intent="é" * 500, record_url="https://x/r"
    )
    assert len(h.encode()) <= 512 and h.startswith("[A2A] user_r for user_origin: ") and h.endswith("\n")
    assert h.count("é") <= 200
    h = a2a_tasks.session_header(requester_display="u" * 600, for_whom="o", intent="i", record_url="u")
    assert h.startswith("[A2A] ") and len(h.encode()) <= 512 and h.endswith("\n")


LINK = {"key": "k1", "userId": "user_origin", "generation": "g1", "connectedAt": 5, "taskAddress": "http://t"}


@pytest.fixture
def net(monkeypatch):
    """The network layer below ``app_call``: the origin person's link rows and the wire ``call``."""
    conns = SimpleNamespace(routes_for_user=AsyncMock(return_value=[LINK]), live_routes=AsyncMock(return_value=[LINK]))
    monkeypatch.setattr("core.services.network_route._connections", lambda: conns)
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Origin Person"))
    call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("core.services.network_route.call", call)
    # No warm cloud member: the phase-1 path, the origin person's computer link.
    monkeypatch.setattr("core.services.network_member._session_is_warm", AsyncMock(return_value=False))
    return SimpleNamespace(conns=conns, call=call)


@pytest.fixture
def setup(repos, monkeypatch):  # noqa: F811
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    monkeypatch.setattr("core.services.a2a_tasks.agent_name", AsyncMock(return_value="Friday"))
    req = a.AuthorityRequest(
        operation=a.Operation(
            skill="ask_session", resource="session:abcdefghijkl/claude:conv-1", operands={"text": "go"}
        ),
        intent="do it",
    )
    chain = [{"agent": None, "session": None, "person": "user_origin", "at": 1}]
    jws, claims = a.mint(
        requester_person="user_origin", org=ORG, verified_by="origin-message", chain=chain, request=req, now=1_000
    )
    return jws, claims


def rules(effect):
    return a2a_rules.Rules(
        revision="r",
        holders={"session:*": ["@session-person"]},
        rules=[
            a2a_rules.Rule(
                id="s1", resource="session:*", skill="ask_session", requesters=["agent:friday"], effect=effect
            )
        ],
    )


async def store(jws, claims, effect="allow"):
    return await a2a_tasks._decide_and_store(
        AuthContext(user_id="user_origin", org_id=ORG),
        ORG,
        "friday",
        jws,
        claims,
        "go",
        rules=rules(effect),
        chain_agents=["friday"],
        session_person="user_laptop",
        now=1_000,
    )


async def test_allowed_session_ask_sends_app_send_through_the_origin_link_and_completes_delivered(setup, net):
    row = await store(*setup)
    net.conns.routes_for_user.assert_awaited_once_with("user_origin")
    (link, op, params, timeout), _ = net.call.call_args
    assert link == LINK and op == "app.send" and timeout == a2a_tasks.SESSION_SEND_TIMEOUT_S
    assert params["app"] == "friday" and params["title"] == "Friday via Origin"
    assert params["person"] == network_member.app_person("user_origin")
    assert params["to"] == "abcdefghijkl/claude:conv-1" and params["request_id"] == row["id"]
    assert params["text"].startswith("[A2A] user_origin for user_origin: do it") and params["text"].endswith("\ngo")
    stored = await a2a_repo.get_task(ORG, row["id"])
    assert stored["state"] == "completed" and stored["delivered"] is True
    assert stored["artifact"] == {"artifactId": row["id"], "parts": [{"text": "delivered"}]}
    task = a2a_tasks.to_a2a_task(stored)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED" and task["metadata"][a.URI]["delivered"] is True
    recs = [json.loads(_unb64(r["jws"].split(".")[1])) for r in await a2a_repo.list_records(ORG, row["id"])]
    assert [r["decision"] for r in recs] == ["allow", "completed"]


async def test_no_link_fails_undeliverable_with_record(setup, net):
    net.conns.routes_for_user.return_value = []
    row = await store(*setup)
    net.call.assert_not_awaited()
    stored = await a2a_repo.get_task(ORG, row["id"])
    assert stored["state"] == "failed" and stored["reason"] == "undeliverable"
    recs = [json.loads(_unb64(r["jws"].split(".")[1])) for r in await a2a_repo.list_records(ORG, row["id"])]
    assert [(r["decision"], r["rule"]) for r in recs] == [("allow", "s1"), ("failed", "undeliverable")]


async def test_a_refused_send_fails_undeliverable(setup, net):
    net.call.side_effect = RuntimeError(json.dumps({"code": "NOT_FOUND", "message": "no such session"}))
    row = await store(*setup)
    assert (await a2a_repo.get_task(ORG, row["id"]))["reason"] == "undeliverable"
