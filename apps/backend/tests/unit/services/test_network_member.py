"""The workspace as one computer on the lightbulb network: sessions.json and its publishing."""

import json
from unittest.mock import AsyncMock

import pytest

from core.services import network_member
from core.services.messaging import ApiProblem

A1 = "11111111-1111-1111-1111-111111111111"
A2 = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def store(monkeypatch):
    """The owner-state bucket as a dict, plus an org name and a two-agent roster."""
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(network_member.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr("core.services.hermes_profiles.put_owner_object", lambda k, b: objects.__setitem__(k, b))
    monkeypatch.setattr("core.services.hermes_profiles.get_owner_object", lambda k: objects.get(k))
    monkeypatch.setattr(
        "core.services.hermes_profiles.owner_object_size", lambda k: len(objects[k]) if k in objects else None
    )
    monkeypatch.setattr("core.services.clerk_admin.get_organization", AsyncMock(return_value={"name": "Acme Builders"}))
    roster = [{"id": A1, "name": "Friday"}, {"id": A2, "name": "Ops Lead"}]
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    return objects


def _doc(objects):
    return json.loads(objects[network_member.sessions_key("org_a")])


@pytest.mark.asyncio
async def test_sessions_json_lists_every_agent_and_keeps_app_entries(store):
    app = {"name": "github-1a2b3c4d", "agent": "app", "title": "GitHub"}
    await network_member.write_sessions("org_a", apps=[app])
    await network_member.write_sessions("org_a")  # apps=None keeps the earlier app entry

    assert network_member.sessions_key("org_a") == "org_a/network/sessions.json"
    assert _doc(store) == {
        "workspace": "Acme Builders",
        "sessions": [
            {"name": "friday", "agent": "hermes", "title": "Friday", "agentId": A1},
            {"name": "ops-lead", "agent": "hermes", "title": "Ops Lead", "agentId": A2},
            app,
        ],
    }
    assert network_member.app_person("user_1") == network_member.app_person("user_1")
    assert len(network_member.app_person("user_1")) == 8


@pytest.mark.asyncio
async def test_a_101st_session_is_refused_with_a_clear_error(store):
    apps = [{"name": f"app-{i}", "agent": "app", "title": f"App {i}"} for i in range(98)]
    await network_member.write_sessions("org_a", apps=apps)  # 2 agents + 98 apps = 100
    assert len(_doc(store)["sessions"]) == 100

    with pytest.raises(ApiProblem) as exc:
        await network_member.write_sessions("org_a", apps=[*apps, {"name": "one-more", "agent": "app"}])
    assert str(exc.value) == "This workspace has reached 100 network addresses."
    assert len(_doc(store)["sessions"]) == 100  # nothing written


@pytest.mark.asyncio
async def test_agents_create_republishes_sessions(store, monkeypatch):
    from core.services import runtime_rpc

    calls = []

    async def _rpc(owner_id, method, params=None, **kwargs):
        calls.append((method, params, kwargs.get("timeout_s")))
        return {"count": 2}

    monkeypatch.setattr(network_member.container_rpc, "call", _rpc)
    monkeypatch.setattr(runtime_rpc, "publish_owner_users", AsyncMock())

    # Not a member yet: nothing is written and the sidecar is not called.
    await runtime_rpc._invalidate("org_a", "agents.create")
    assert store == {} and calls == []

    await network_member.write_sessions("org_a")
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(
            return_value=[{"id": A1, "name": "Friday"}, {"id": A2, "name": "Ops Lead"}, {"id": "3" * 8, "name": "Nova"}]
        ),
    )
    await runtime_rpc._invalidate("org_a", "agents.create")
    assert [s["title"] for s in _doc(store)["sessions"]] == ["Friday", "Ops Lead", "Nova"]
    assert calls == [("network.sessions", {}, 10)]

    # config.patch is not an agents.* method.
    await runtime_rpc._invalidate("org_a", "config.patch")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_publish_sessions_never_raises(store, monkeypatch):
    await network_member.write_sessions("org_a")
    monkeypatch.setattr(network_member.container_rpc, "call", AsyncMock(side_effect=RuntimeError("parked")))
    await network_member.publish_sessions("org_a")
