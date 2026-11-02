import json
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.repositories import a2a_repo
from core.services import a2a_authority as a
from core.services import a2a_tasks
from tests.unit.services.conftest import repos  # noqa: F401

ORG = "org_1"


def test_header_is_capped_at_512_bytes_and_200_intent_chars():
    h = a2a_tasks.session_header(
        requester_display="user_r", for_whom="user_origin", intent="é" * 500, record_url="https://x/r"
    )
    assert len(h.encode()) <= 512 and h.startswith("[A2A] user_r for user_origin: ") and h.endswith("\n")
    assert h.count("é") <= 200
    h = a2a_tasks.session_header(requester_display="u" * 600, for_whom="o", intent="i", record_url="u")
    assert h.startswith("[A2A] ")  # intent is cut first, never raises


@pytest.fixture
def setup(repos, monkeypatch):  # noqa: F811
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    monkeypatch.setattr("core.services.a2a_tasks.agent_name", AsyncMock(return_value="Friday"))
    req = a.AuthorityRequest(
        operation=a.Operation(skill="message", resource="session:abc", operands={"text": "go"}), intent="do it"
    )
    chain = [{"agent": None, "session": None, "person": "user_origin", "at": 1}]
    jws, _ = a.mint(
        requester_person="user_r", org=ORG, verified_by="clerk-session", chain=chain, request=req, now=1_000
    )
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "agentId": "friday",
        "requester": "user_r",
        "authority": jws,
        "policyRevision": "r",
        "rule": "allow",
        "holders": [],
        "state": "working",
        "createdAt": 1_000,
    }


async def test_session_dispatch_uses_origin_person_link(setup, monkeypatch):
    through = AsyncMock()
    monkeypatch.setattr("core.services.hosted_mcp._through_member", through)
    ok = await a2a_tasks._dispatch(AuthContext(user_id="user_r", org_id=ORG), ORG, "friday", setup, "go")
    assert ok and setup["dispatchRequestId"] == setup["id"]
    (auth,), kw = through.call_args
    assert auth.user_id == "user_origin" and auth.org_id == ORG
    assert kw["to"] == "abc" and kw["request_id"] == setup["id"] and kw["sender"] == ("friday", "Friday")
    assert kw["text"].startswith("[A2A]") and kw["text"].endswith("\ngo")


async def test_no_link_fails_undeliverable_with_record(setup, monkeypatch):
    monkeypatch.setattr("core.services.hosted_mcp._through_member", AsyncMock(side_effect=RuntimeError("no_link")))
    assert not await a2a_tasks._dispatch(AuthContext(user_id="user_r", org_id=ORG), ORG, "friday", setup, "go")
    await a2a_tasks._fail_dispatch(ORG, setup, 1_001)
    assert setup["state"] == "failed" and setup["reason"] == "undeliverable"
    await a2a_repo.put_task(ORG, setup)
    assert json.loads(json.dumps((await a2a_repo.get_task(ORG, setup["id"]))["reason"])) == "undeliverable"
