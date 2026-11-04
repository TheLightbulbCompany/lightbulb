import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.repositories import a2a_repo
from core.services import a2a_authority, a2a_rules, a2a_tasks, network_route
from core.services.network_proof import _unb64
from tests.unit.services.conftest import repos  # noqa: F401

ORG, OTHER = "org_1", "org_2"
RULES = a2a_rules.Rules(revision="x", holders={"agent:*": ["u1"]}, rules=[], default="hold")


def claims_of(jws: str) -> dict:
    return json.loads(_unb64(jws.split(".")[1]))


def row(user: str) -> dict:
    return {"key": f"k-{user}", "userId": user, "generation": "g", "connectedAt": 1, "taskAddress": "http://t"}


@pytest.fixture
def net(repos, monkeypatch):  # noqa: F811
    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    members = {ORG: ["u1", "u2"], OTHER: ["u3"]}
    monkeypatch.setattr(
        "core.services.clerk_admin.list_organization_members",
        AsyncMock(side_effect=lambda org, **_: [{"user_id": u} for u in members[org]]),
    )
    conns = SimpleNamespace(routes_for_user=AsyncMock(side_effect=lambda u: [row(u)]))
    monkeypatch.setattr("core.services.network_route._connections", lambda: conns)
    call = AsyncMock(return_value={"stored": True})
    monkeypatch.setattr("core.services.network_route.call", call)
    return SimpleNamespace(call=call, members=members)


def test_sign_rules_has_typ_workspace_revision_and_doc(net):
    doc = {"revision": "r-9", "holders": {}, "rules": []}
    jws = a2a_authority.sign_rules(ORG, doc, now=1_000)
    assert json.loads(_unb64(jws.split(".")[0]))["typ"] == "lightbulb-a2a-rules"
    assert claims_of(jws) == {"workspace": ORG, "revision": "r-9", "doc": doc, "iat": 1_000}


def test_put_rules_pushes_to_every_link_of_the_workspace(net):
    admin = AuthContext(user_id="u1", org_id=ORG, org_role="org:admin")
    doc = asyncio.run(a2a_tasks.put_rules(admin, ORG, RULES, now=5))
    assert {c.args[0]["userId"] for c in net.call.await_args_list} == {"u1", "u2"}  # not u3, of OTHER
    sent = net.call.await_args_list[0]
    assert sent.args[1] == "rules.set"
    assert claims_of(sent.args[2]["jws"])["doc"] == doc


def test_push_continues_past_an_unsupported_engine(net):
    net.call.side_effect = [RuntimeError('{"code":"unsupported"}'), {"stored": True}]
    assert asyncio.run(network_route.push_rules(ORG, "jws")) == 1
    assert net.call.await_count == 2


def test_connect_pushes_the_default_document_when_none_is_stored(net):
    link = network_route.Link("k-u1", "u1", "g", AsyncMock())
    asyncio.run(network_route.push_current_rules(link, ORG))
    (sent,) = net.call.await_args_list
    claims = claims_of(sent.args[2]["jws"])
    assert claims["revision"] == "default" and claims["workspace"] == ORG
    assert claims["doc"] == a2a_rules.default_rules(ORG, []).model_dump()


def test_push_is_scoped_to_the_links_workspace(net, monkeypatch):
    asyncio.run(a2a_repo.put_rules(ORG, {**RULES.model_dump(), "revision": "mine"}))
    asyncio.run(a2a_repo.put_rules(OTHER, {**RULES.model_dump(), "revision": "theirs"}))
    link = network_route.Link("k-u1", "u1", "g", AsyncMock())
    asyncio.run(network_route.push_current_rules(link, ORG))  # u1 is in both; the link is in ORG
    (sent,) = net.call.await_args_list
    assert claims_of(sent.args[2]["jws"])["workspace"] == ORG
    assert claims_of(sent.args[2]["jws"])["revision"] == "mine"
    # no workspace on the proof and two memberships: ambiguous, so nothing is pushed
    net.call.reset_mock()
    monkeypatch.setattr(
        "core.services.clerk_admin.list_user_organizations", AsyncMock(return_value=[{"id": ORG}, {"id": OTHER}])
    )
    asyncio.run(network_route.push_current_rules(link, None))
    net.call.assert_not_awaited()
