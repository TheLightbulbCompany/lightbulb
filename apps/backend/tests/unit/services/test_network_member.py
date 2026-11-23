"""The workspace's cloud member: sessions.json and its publishing."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import json
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
    messaging table, which holds the wake row)."""
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(network_member.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr("core.services.hermes_profiles.put_owner_object", lambda k, b: objects.__setitem__(k, b))
    monkeypatch.setattr("core.services.hermes_profiles.get_owner_object", lambda k: objects.get(k))
    monkeypatch.setattr(
        "core.services.hermes_profiles.owner_object_size", lambda k: len(objects[k]) if k in objects else None
    )
    monkeypatch.setattr("core.services.workspace_name.saved_name", AsyncMock(return_value="Acme Builders"))
    roster = [{"id": A1, "name": "Friday"}, {"id": A2, "name": "Ops Lead"}]
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr("core.services.website_logins._humans_to_ask", AsyncMock(return_value=["user_1", "user_2"]))
    return objects


def _doc(objects):
    return json.loads(objects[network_member.sessions_key("org_a")])


@pytest.mark.asyncio
async def test_a_signed_out_computer_stays_revoked_until_its_proof_expires(store, table):
    import time

    now = int(time.time())
    table.put_item(Item={"pk": "NETREVOKED#user_1", "sk": "k" * 52, "ttl": now + 3600})
    table.put_item(Item={"pk": "NETREVOKED#user_2", "sk": "x" * 52, "ttl": now - 1})  # its proof has expired
    await network_member.write_sessions("org_a")
    assert _doc(store)["revoked_keys"] == ["k" * 52]


@pytest.mark.asyncio
async def test_agents_whose_names_and_id_prefixes_collide_get_distinct_names(store, monkeypatch):
    ids = [f"abcd1111-0000-0000-0000-00000000000{i}" for i in range(4)]
    roster = [{"id": i, "name": "Friday"} for i in ids]
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    await network_member.write_sessions("org_a")
    first = [s["name"] for s in _doc(store)["sessions"]]
    assert first == ["friday", "friday-abcd", "friday-idabcd1111", f"friday-id{ids[3]}"]
    await network_member.write_sessions("org_a")  # deterministic for an unchanged roster
    assert [s["name"] for s in _doc(store)["sessions"]] == first


@pytest.mark.asyncio
async def test_a_101st_session_is_refused_with_a_clear_error(store, monkeypatch):
    roster = [{"id": f"{i:08d}-0000-0000-0000-000000000000", "name": f"Agent {i}"} for i in range(101)]
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    with pytest.raises(ApiProblem) as exc:
        await network_member.write_sessions("org_a")
    assert str(exc.value) == "This workspace has reached 100 network addresses."
    assert network_member.sessions_key("org_a") not in store  # nothing written


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
async def test_a_live_old_runtime_still_receives_the_sessions_rpc_during_drain(store, monkeypatch, rpc):
    _session(monkeypatch, session_arn="arn:old", warm_for=-100)
    await network_member.write_sessions("org_a")
    await network_member.publish_sessions("org_a")
    assert rpc == [("network.sessions", {}, {})]


@pytest.fixture(autouse=True)
def _canonical_pool_configured(monkeypatch):
    monkeypatch.setattr(network_member.settings, "HERMES_SHARED_RUNTIME_ARN", "arn:r")
