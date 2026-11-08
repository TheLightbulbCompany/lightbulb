"""The workspace as one computer on the lightbulb network: sessions.json and its publishing."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import json
import logging
from unittest.mock import AsyncMock, MagicMock

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
            {"name": "friday", "agent": "Friday", "title": "Friday", "agentId": A1},
            {"name": "ops-lead", "agent": "Ops Lead", "title": "Ops Lead", "agentId": A2},
            app,
        ],
        "wake": {"url": "http://localhost:8000/api/v1/network/wake", "token": token},
        "members": ["user_1", "user_2"],
        "revoked_keys": [],
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
    # No warning traceback on a normal provision. `asyncio` records are excluded: they are
    # another test's leftovers ("Task was destroyed but it is pending!" when a dropped task is
    # garbage-collected), and GC can run them inside any test under xdist.
    assert not [r for r in caplog.records if r.name != "asyncio"]


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
async def test_cli_credentials_seed_every_writer_but_the_roster_fan_out(monkeypatch, published):
    """A company-deploy agent arrives with ``inputs`` yet is created AFTER connects, so it is seeded;
    only ``write_profiles`` (provision, before any connect) skips it."""
    hp = _profile_write_stubs(monkeypatch)
    seed = AsyncMock()
    monkeypatch.setattr("core.services.credentials.hermes_cli.seed_new_profile", seed)
    inputs = await hp.resolve_profile_inputs("org_a")
    await hp.write_profile_for_agent("org_a", {"id": A1, "name": "Friday"}, inputs=inputs, publish=False)
    seed.assert_awaited_once_with("org_a", A1)
    seed.reset_mock()
    await hp.write_profiles("org_a")
    seed.assert_not_awaited()


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
    from core.services import agent_lifecycle as lifecycle, agent_registry

    monkeypatch.setattr(agent_registry, "roster", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_registry, "delete", AsyncMock())
    monkeypatch.setattr(
        "core.repositories.workspace_repo.WorkspaceRepo",
        MagicMock(return_value=MagicMock(remove_starter_agent_id=AsyncMock())),
    )
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    monkeypatch.setattr("core.services.hermes_profiles.delete_profile", lambda o, a: None)

    await lifecycle.delete_agent_everywhere(owner_id="org_a", agent_id=A1)
    assert published == ["org_a"]


@pytest.mark.asyncio
async def test_join_network_makes_a_new_member_first_then_asks_the_bridge(store, monkeypatch):
    order = []
    monkeypatch.setattr(
        "core.services.hermes_provisioning._delete_prefixes", lambda prefixes: order.append(("delete", prefixes))
    )
    monkeypatch.setattr(
        "core.services.hermes_provisioning.prepare_hermes_workspace",
        AsyncMock(side_effect=lambda o: order.append("prepare") or False),
    )

    async def _rpc(owner_id, method, params=None, **kw):
        order.append((method, params, network_member.sessions_key(owner_id) in store))
        return {"state": "joining"}

    monkeypatch.setattr(network_member.container_rpc, "call", _rpc)
    assert await network_member.join_network("org_a", "TICKET") == {"state": "joining"}
    assert order == [("delete", ["org_a/network/state.db"]), "prepare", ("network.join", {"ticket": "TICKET"}, True)]

    order.clear()  # a member already: straight to the bridge
    await network_member.join_network("org_a", "TICKET")
    assert order == [("network.join", {"ticket": "TICKET"}, True)]


@pytest.mark.asyncio
async def test_join_network_maps_the_bridges_refusals(store, monkeypatch):
    await network_member.write_sessions("org_a")
    for code, problem in (("already_in_team", "NETWORK_ALREADY_JOINED"), ("UNAVAILABLE", "RUNTIME_UNAVAILABLE")):
        error = RuntimeError(json.dumps({"code": code, "message": "x"}))
        monkeypatch.setattr(network_member.container_rpc, "call", AsyncMock(side_effect=error))
        with pytest.raises(ApiProblem) as raised:
            await network_member.join_network("org_a", "TICKET")
        assert raised.value.error.code == problem


@pytest.mark.asyncio
async def test_the_joined_mark_survives_rewrites_and_leave_clears_it(store, monkeypatch):
    await network_member.write_sessions("org_a")
    monkeypatch.setattr(network_member.container_rpc, "call", AsyncMock(return_value={"state": "joining"}))
    await network_member.join_network("org_a", "TICKET")
    assert _doc(store)["joined"] is True
    await network_member.publish_sessions("org_a")
    assert _doc(store)["joined"] is True
    await network_member.leave_network("org_a")
    assert "joined" not in _doc(store)
    network_member.container_rpc.call.assert_awaited_with("org_a", "network.leave", {})


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
    await network_member.write_sessions("org_a")  # what a provision leaves behind
    assert _doc(store)["members"] == ["user_1"]

    humans.return_value = ["user_1", "user_3"]  # a join
    await network_member.publish_sessions("org_a")
    assert _doc(store)["members"] == ["user_1", "user_3"]

    humans.return_value = ["user_3"]  # a leave
    await network_member.publish_sessions("org_a")
    assert _doc(store)["members"] == ["user_3"]


@pytest.mark.asyncio
async def test_a_members_signed_out_computer_is_in_revoked_keys(store):
    from core.services import network_login

    key = "a" * 52
    await network_login.revoke("user_2", key)
    await network_login.revoke("user_9", "b" * 52)  # not a member: not this workspace's to refuse
    await network_member.write_sessions("org_a")
    assert _doc(store)["revoked_keys"] == [key]


@pytest.mark.asyncio
async def test_team_id_survives_write(store):
    await network_member.write_sessions("org_a")
    doc = _doc(store)
    store[network_member.sessions_key("org_a")] = json.dumps({**doc, "teamId": "tm1"}).encode()
    await network_member.publish_sessions("org_a")
    assert _doc(store)["teamId"] == "tm1"
    await network_member._write("org_a", [])  # a writer with no prior in hand
    assert _doc(store)["teamId"] == "tm1"


@pytest.mark.asyncio
async def test_join_and_leave_clear_the_stored_team_id(store, monkeypatch):
    monkeypatch.setattr(network_member.container_rpc, "call", AsyncMock(return_value={"state": "joining"}))
    for move in (lambda: network_member.join_network("org_a", "T"), lambda: network_member.leave_network("org_a")):
        await network_member.write_sessions("org_a")
        store[network_member.sessions_key("org_a")] = json.dumps({**_doc(store), "teamId": "tm1"}).encode()
        await move()
        assert "teamId" not in _doc(store)


def _line(monkeypatch, *, warm, doorbell, rpc=None):
    call = rpc or AsyncMock(return_value={"ticket": "T", "team_id": "tm1", "expires_at": 5})
    monkeypatch.setattr(network_member.container_rpc, "call", call)
    monkeypatch.setattr(network_member, "_session_is_warm", AsyncMock(return_value=warm))
    wake = AsyncMock(return_value=doorbell)
    monkeypatch.setattr(network_member, "wake", wake)
    return call, wake


@pytest.mark.asyncio
async def test_mint_line_warm_calls_rpc(store, monkeypatch):
    await network_member.write_sessions("org_a")
    call, wake = _line(monkeypatch, warm=True, doorbell=True)
    assert await network_member.mint_line("org_a") == {"ticket": "T", "teamId": "tm1", "expiresAt": 5}
    assert call.await_args.args[:3] == ("org_a", "network.invite", {}) and "timeout_s" not in call.await_args.kwargs
    wake.assert_not_awaited()
    assert await network_member.stored_team_id("org_a") == "tm1"


@pytest.mark.asyncio
async def test_mint_line_cold_rings_and_answers_503_without_rpc(store, monkeypatch):
    call, wake = _line(monkeypatch, warm=False, doorbell=True)
    with pytest.raises(ApiProblem) as raised:
        await network_member.mint_line("org_a")
    assert raised.value.status_code == 503 and raised.value.error.retry_after == 15
    wake.assert_awaited_once_with("org_a")
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_mint_line_no_doorbell_calls_rpc(store, monkeypatch):
    await network_member.write_sessions("org_a")
    call, wake = _line(monkeypatch, warm=False, doorbell=False)
    assert (await network_member.mint_line("org_a"))["ticket"] == "T"
    wake.assert_awaited_once_with("org_a")
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_mint_line_unavailable_is_503(store, monkeypatch):
    await network_member.write_sessions("org_a")
    for error in (RuntimeError(json.dumps({"code": "UNAVAILABLE", "message": "x"})), TimeoutError()):
        _line(monkeypatch, warm=True, doorbell=False, rpc=AsyncMock(side_effect=error))
        with pytest.raises(ApiProblem) as raised:
            await network_member.mint_line("org_a")
        assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_mint_line_leaks_the_ticket_nowhere(store, monkeypatch, caplog):
    await network_member.write_sessions("org_a")
    caplog.set_level(logging.DEBUG)
    secret = "TICKET-secret-abc"
    _line(monkeypatch, warm=True, doorbell=False, rpc=AsyncMock(return_value={"ticket": secret, "team_id": "tm1"}))
    await network_member.mint_line("org_a")
    # a refused call whose error text carries the ticket must not reach the raised error
    failing = RuntimeError(json.dumps({"code": "boom", "message": secret}))
    _line(monkeypatch, warm=True, doorbell=False, rpc=AsyncMock(side_effect=failing))
    with pytest.raises(ApiProblem) as raised:
        await network_member.mint_line("org_a")
    assert secret not in caplog.text and secret not in str(raised.value) and secret not in repr(raised.value.body())
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.asyncio
async def test_already_joined_message(store, monkeypatch):
    await network_member.write_sessions("org_a")
    error = RuntimeError(json.dumps({"code": "already_in_team", "message": "x"}))
    monkeypatch.setattr(network_member.container_rpc, "call", AsyncMock(side_effect=error))
    with pytest.raises(ApiProblem) as raised:
        await network_member.join_network("org_a", "T")
    assert raised.value.error.message == (
        "Your workspace's agents are already on a network. Sign in with lightbulb login to join it."
    )
