"""The workspace as one computer on the lightbulb network: sessions.json and its publishing."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import json
from unittest.mock import AsyncMock

import pytest

from core.services import network_member
from core.services.messaging import ApiProblem
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

A1 = "11111111-1111-1111-1111-111111111111"
A2 = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def store(monkeypatch, table):
    """The owner-state bucket as a dict, plus an org name and a two-agent roster (and the
    messaging table, which holds the doorbell's token hash)."""
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
    monkeypatch.setattr("core.services.website_logins._humans_to_ask", AsyncMock(return_value=["user_1", "user_2"]))
    return objects


def _doc(objects):
    return json.loads(objects[network_member.sessions_key("org_a")])


@pytest.mark.asyncio
async def test_sessions_json_lists_every_agent_and_keeps_app_entries(store):
    app = {"name": "github-1a2b3c4d", "agent": "app", "title": "GitHub"}
    await network_member.write_sessions("org_a", apps=[app])
    await network_member.write_sessions("org_a")  # apps=None keeps the earlier app entry

    assert network_member.sessions_key("org_a") == "org_a/network/sessions.json"
    token = _doc(store)["wake"]["token"]
    assert token.startswith("org_a.")
    assert _doc(store) == {
        "workspace": "Acme Builders",
        "sessions": [
            {"name": "friday", "agent": "hermes", "title": "Friday", "agentId": A1},
            {"name": "ops-lead", "agent": "hermes", "title": "Ops Lead", "agentId": A2},
            app,
        ],
        "wake": {"url": "http://localhost:8000/api/v1/network/wake", "token": token},
        "members": ["user_1", "user_2"],
    }
    assert network_member.app_person("user_1") == network_member.app_person("user_1")
    assert len(network_member.app_person("user_1")) == 8


@pytest.mark.asyncio
async def test_agents_whose_names_and_id_prefixes_collide_get_distinct_names(store, monkeypatch):
    ids = [f"abcd1111-0000-0000-0000-00000000000{i}" for i in range(4)]
    roster = [{"id": i, "name": "Friday"} for i in ids]
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    await network_member.write_sessions("org_a", apps=[])
    first = [s["name"] for s in _doc(store)["sessions"]]
    assert first == ["friday", "friday-abcd", "friday-idabcd1111", f"friday-id{ids[3]}"]
    assert not any(network_member.app_target(n) for n in first)  # an agent never reads as an app
    await network_member.write_sessions("org_a", apps=[])  # deterministic for an unchanged roster
    assert [s["name"] for s in _doc(store)["sessions"]] == first


@pytest.mark.asyncio
async def test_an_agent_named_like_an_app_address_gets_a_suffix(store, monkeypatch):
    roster = [{"id": A1, "name": "Ops deadbeef"}]
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    await network_member.write_sessions("org_a", apps=[])
    assert [s["name"] for s in _doc(store)["sessions"]] == ["ops-deadbeef-1111"]


@pytest.mark.asyncio
async def test_a_101st_session_is_refused_with_a_clear_error(store):
    apps = [{"name": f"app-{i}", "agent": "app", "title": f"App {i}"} for i in range(98)]
    await network_member.write_sessions("org_a", apps=apps)  # 2 agents + 98 apps = 100
    assert len(_doc(store)["sessions"]) == 100

    with pytest.raises(ApiProblem) as exc:
        await network_member.write_sessions("org_a", apps=[*apps, {"name": "one-more", "agent": "app"}])
    assert str(exc.value) == "This workspace has reached 100 network addresses."
    assert len(_doc(store)["sessions"]) == 100  # nothing written


def _session(monkeypatch, *, arn="arn:r", warm_for=600, session_arn="arn:r"):
    """The containers row's runtime ARN and the ``__session__#<owner>`` warm estimate."""
    import time

    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"agentcore_runtime_arn": arn} if arn else {}),
    )
    monkeypatch.setattr(
        "core.repositories.container_repo.get_session_sync",
        lambda owner_id: {"warm_until": int(time.time()) + warm_for, "arn": session_arn},
    )


@pytest.fixture
def rpc(monkeypatch):
    calls = []

    async def _rpc(owner_id, method, params=None, **kwargs):
        calls.append((method, params, kwargs))
        return {"count": 2}

    monkeypatch.setattr(network_member.container_rpc, "call", _rpc)
    return calls


@pytest.mark.asyncio
async def test_a_warm_runtime_gets_the_sessions_rpc(store, monkeypatch, rpc):
    _session(monkeypatch)
    await network_member.publish_sessions("org_a")  # not a member yet: nothing at all
    assert store == {} and rpc == []

    await network_member.write_sessions("org_a")
    await network_member.publish_sessions("org_a")
    assert rpc == [("network.sessions", {}, {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session",
    [
        {"warm_for": -1},  # parked
        {"session_arn": "arn:old"},  # warm, but on a runtime that was re-created
        {"arn": None},  # fresh provision: profiles are written before CreateAgentRuntime
    ],
)
async def test_a_parked_runtime_gets_sessions_json_rewritten_and_no_rpc(store, monkeypatch, rpc, session, caplog):
    _session(monkeypatch, **session)
    await network_member.write_sessions("org_a")
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": A1, "name": "Friday"}, {"id": "3" * 8, "name": "Nova"}]),
    )
    await network_member.publish_sessions("org_a")
    assert [s["title"] for s in _doc(store)["sessions"]] == ["Friday", "Nova"]
    assert rpc == []
    assert not caplog.records  # no warning traceback on a normal provision


@pytest.mark.asyncio
async def test_publish_sessions_never_raises(store, monkeypatch):
    _session(monkeypatch)
    await network_member.write_sessions("org_a")
    monkeypatch.setattr(network_member.container_rpc, "call", AsyncMock(side_effect=RuntimeError("down")))
    await network_member.publish_sessions("org_a")


# --- every agent create / rename / delete door republishes through a shared seam -------------


@pytest.fixture
def published(monkeypatch):
    calls = []
    monkeypatch.setattr(network_member, "publish_sessions", AsyncMock(side_effect=calls.append))
    return calls


def _profile_write_stubs(monkeypatch):
    from types import SimpleNamespace

    from core.services import hermes_provisioning as hp

    inputs = SimpleNamespace(
        provider_choice="openrouter_managed",
        integrations={},
        env_vars={},
        byo_provider=None,
        byo_api_key=None,
        front_desk_number_for=lambda agent_id: None,
    )
    monkeypatch.setattr(hp, "resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr(hp, "_stage_owner_facts", AsyncMock())
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pcp"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr("core.services.hermes_profiles.write_agent_profile", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", AsyncMock())
    monkeypatch.setattr("core.services.hermes_profiles.read_soul_md", lambda o, a: None)
    return hp


@pytest.mark.asyncio
async def test_agents_create_republishes_sessions(monkeypatch, published):
    """``write_profile_for_agent`` is the seam behind agents.create, Teams create, marketplace
    and company deploys."""
    hp = _profile_write_stubs(monkeypatch)
    await hp.write_profile_for_agent("org_a", {"id": A1, "name": "Friday"})
    assert published == ["org_a"]


@pytest.mark.asyncio
async def test_write_profiles_publishes_once_after_the_fan_out(monkeypatch, published):
    hp = _profile_write_stubs(monkeypatch)
    await hp.write_profiles("org_a")  # the two-agent roster from the store fixture's default
    assert published == ["org_a"]


@pytest.fixture(autouse=True)
def _roster(monkeypatch):
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": A1, "name": "Friday"}, {"id": A2, "name": "Ops Lead"}]),
    )


@pytest.mark.asyncio
async def test_a_rename_republishes_sessions(monkeypatch, published):
    """``refresh_agent_soul`` is the seam behind agents.update, the Teams PATCH, voice and text."""
    from core.services import hermes_provisioning as hp

    monkeypatch.setattr("core.services.hermes_profiles.read_soul_md", lambda o, a: "owner text")
    monkeypatch.setattr("core.services.hermes_profiles.read_voice_notes", lambda o, a: None)
    monkeypatch.setattr("core.services.hermes_profiles.upload_voice_notes", lambda *a: None)
    monkeypatch.setattr("core.services.messaging.refresh_agent_member_names", AsyncMock(return_value={}))
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())

    await hp.refresh_agent_soul("org_a", A1)
    assert published == ["org_a"]


@pytest.mark.asyncio
async def test_an_agent_delete_republishes_sessions(monkeypatch, published):
    from core.services import paperclip_agent_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "prepare_paperclip_agent_delete", AsyncMock(return_value={}))
    monkeypatch.setattr(lifecycle, "delete_paperclip_for_chat", AsyncMock())
    monkeypatch.setattr(lifecycle, "_refresh_facts", AsyncMock())
    monkeypatch.setattr("core.services.hermes_profiles.delete_profile", lambda o, a: None)

    await lifecycle.delete_agent_everywhere(owner_id="org_a", agent_id=A1)
    assert published == ["org_a"]


@pytest.mark.asyncio
async def test_connect_clears_a_stale_state_db_before_joining(monkeypatch):
    order = []
    monkeypatch.setattr(network_member, "sessions_exist", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "core.services.hermes_provisioning._delete_prefixes", lambda prefixes: order.append(("delete", prefixes))
    )
    monkeypatch.setattr(
        "core.services.hermes_provisioning.prepare_hermes_workspace",
        AsyncMock(side_effect=lambda o: order.append("prepare") or False),
    )
    monkeypatch.setattr(network_member.container_rpc, "call", AsyncMock(return_value={"line": "l", "expires_at": "t"}))
    await network_member.connect("org_a", "user_1", "Sam")
    assert order == [("delete", ["org_a/network/state.db"]), "prepare"]


@pytest.mark.asyncio
async def test_a_write_with_no_prior_keeps_the_wake_token_and_hash(store, table):
    await network_member.write_sessions("org_a", apps=[])
    token = _doc(store)["wake"]["token"]
    key = {"pk": "OWNER#org_a", "sk": "NETWAKE"}
    hashed = table.get_item(Key=key)["Item"]["tokenHash"]

    await network_member._write("org_a", [])  # a writer with no prior in hand
    assert _doc(store)["wake"]["token"] == token
    assert table.get_item(Key=key)["Item"]["tokenHash"] == hashed


@pytest.mark.asyncio
async def test_members_follow_the_workspace_on_join_and_leave(store, monkeypatch):
    humans = AsyncMock(return_value=["user_1"])
    monkeypatch.setattr("core.services.website_logins._humans_to_ask", humans)
    await network_member.write_sessions("org_a")  # what connect() leaves behind
    assert _doc(store)["members"] == ["user_1"]

    humans.return_value = ["user_1", "user_3"]  # a join
    await network_member.publish_sessions("org_a")
    assert _doc(store)["members"] == ["user_1", "user_3"]

    humans.return_value = ["user_3"]  # a leave
    await network_member.publish_sessions("org_a")
    assert _doc(store)["members"] == ["user_3"]
