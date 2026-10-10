"""M4-R1 (Task 13a): config.get/config.patch/agents.list on an AgentCore row are served
from the profile in S3 + the Paperclip roster -- no microVM wake for a settings read. ECS rows are
untouched.

Fix round 1 (continuation), Ruling M4-R43: the wire shape below is driven by what the REAL
browser clients send and read -- ``apps/frontend/src/hooks/useConfigPatch.ts`` and
``PluginsTab.tsx`` (never send an ``agentId``, read ``snapshot.config``/``snapshot.hash``, and
send ``config.patch`` as ``{raw: JSON.stringify(patch), baseHash}``), and
``packages/shared/src/hooks/useAgents.ts`` (reads ``data.agents``/``data.defaultId``). Every test
below drives ``execute_sync`` with exactly those params and asserts exactly those response keys --
not a call recorder.
"""

import json

from core.services import runtime_rpc


def _fake_roster(monkeypatch, rows):
    async def fake(owner_id):
        return rows

    monkeypatch.setattr(runtime_rpc, "load_agent_roster", fake)


def _fake_profile_store(monkeypatch, initial: dict):
    """A minimal, STATEFUL stand-in for the S3-backed config_snapshot/patch_profile_config --
    proves the round-trip (a patch is visible on the next get, with the hash the patch returned
    matching the next get's hash) instead of a call recorder that always answers a canned value."""
    import hashlib

    store = {agent_id: dict(cfg) for agent_id, cfg in initial.items()}

    def _hash(agent_id):
        return hashlib.sha256(repr(sorted(store[agent_id].items())).encode()).hexdigest()

    def config_snapshot(owner_id, agent_id):
        return dict(store[agent_id]), _hash(agent_id)

    def patch_profile_config(owner_id, agent_id, patch):
        store[agent_id] = {**store[agent_id], **patch}
        return dict(store[agent_id])

    monkeypatch.setattr(runtime_rpc.hermes_profiles, "config_snapshot", config_snapshot)
    monkeypatch.setattr(runtime_rpc.hermes_profiles, "patch_profile_config", patch_profile_config)
    return store


async def test_config_get_with_an_agent_id_returns_the_snapshot_shape_useconfigpatch_reads(monkeypatch):
    """``useConfigPatch.ts``'s ``ConfigGetSnapshot`` type is ``{config, hash, valid?}`` --
    ``snapshot?.config``/``snapshot?.hash`` are the two fields every real reader touches."""
    monkeypatch.setattr(
        runtime_rpc.hermes_profiles,
        "config_snapshot",
        lambda owner_id, agent_id: ({"model": {"default": "arn:son"}}, "deadbeef"),
    )
    monkeypatch.setattr(runtime_rpc.container_rpc, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={"agentId": "a1"}
    )
    assert out["config"] == {"model": {"default": "arn:son"}}
    assert out["hash"] == "deadbeef"


async def test_config_get_with_no_agent_id_never_400s_and_serves_a_roster_map(monkeypatch):
    """This is exactly what useConfigPatch.ts/PluginsTab.tsx send -- config.get with NO agentId
    param at all. C1: must never 400; must serve a name-keyed map instead."""

    async def fake_roster(owner_id):
        return [{"id": "a1", "role": "ceo"}, {"id": "a2", "role": "engineering"}]

    monkeypatch.setattr(runtime_rpc, "load_agent_roster", fake_roster)
    snapshots = {"a1": ({"model": {"default": "arn:opu"}}, "hash1"), "a2": ({"model": {"default": "arn:son"}}, "hash2")}
    monkeypatch.setattr(runtime_rpc.hermes_profiles, "config_snapshot", lambda owner_id, agent_id: snapshots[agent_id])
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={}
    )
    assert out["config"] == {"a1": {"model": {"default": "arn:opu"}}, "a2": {"model": {"default": "arn:son"}}}
    import hashlib

    assert out["hash"] == hashlib.sha256(b"hash1hash2").hexdigest()  # roster order, per M4-R43


async def test_config_patch_with_no_agent_id_fans_out_to_every_roster_profile_and_the_hash_round_trips(monkeypatch):
    """M4-R48 (fix round 2): BOTH real callers (useConfigPatch.ts:41-43, PluginsTab.tsx:90-96)
    send config.patch with NO agentId at all -- {raw: JSON.stringify(patch), baseHash}. There is
    no single profile to target, so the patch applies to EVERY roster profile, CAS'd against the
    SAME map hash config.get (no agentId) returns. Asserts exactly the three things the brief
    names: (a) visible on every profile via the next config.get, (b) the returned hash equals
    that next config.get's hash, (c) covered by the sibling stale-baseHash test below."""
    _fake_roster(monkeypatch, [{"id": "a1", "role": "ceo"}, {"id": "a2", "role": "engineering"}])
    store = _fake_profile_store(
        monkeypatch, {"a1": {"model": {"default": "arn:opu"}}, "a2": {"model": {"default": "arn:son"}}}
    )

    before = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={}
    )
    out = await runtime_rpc.execute_sync(
        owner_id="org_a",
        user_id="u1",
        request_id="r1",
        method="config.patch",
        params={
            "raw": json.dumps({"plugins": {"entries": {"stripe": {"enabled": False}}}}),
            "baseHash": before["hash"],
        },
    )
    after = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={}
    )

    assert store["a1"]["plugins"] == {"entries": {"stripe": {"enabled": False}}}  # (a)
    assert store["a2"]["plugins"] == {"entries": {"stripe": {"enabled": False}}}  # (a)
    assert out["hash"] == after["hash"]  # (b)
    assert out["config"] == after["config"]


async def test_config_patch_with_no_agent_id_and_a_stale_baseHash_errors_and_writes_nothing(monkeypatch):
    """(c): a baseHash that doesn't match the current map hash -- the stale-config error, no
    profile touched."""
    import pytest

    from core.services.messaging import ApiProblem

    _fake_roster(monkeypatch, [{"id": "a1", "role": "ceo"}])
    store = _fake_profile_store(monkeypatch, {"a1": {"model": {"default": "arn:opu"}}})

    with pytest.raises(ApiProblem):
        await runtime_rpc.execute_sync(
            owner_id="org_a",
            user_id="u1",
            request_id="r1",
            method="config.patch",
            params={"raw": json.dumps({"memory": {"memory_enabled": False}}), "baseHash": "stale-hash"},
        )
    assert store["a1"] == {"model": {"default": "arn:opu"}}  # untouched


async def test_config_patch_with_no_agent_id_and_an_empty_patch_writes_nothing(monkeypatch):
    _fake_roster(monkeypatch, [{"id": "a1", "role": "ceo"}])
    store = _fake_profile_store(monkeypatch, {"a1": {"model": {"default": "arn:opu"}}})

    before = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={}
    )
    out = await runtime_rpc.execute_sync(
        owner_id="org_a",
        user_id="u1",
        request_id="r1",
        method="config.patch",
        params={"raw": json.dumps({}), "baseHash": before["hash"]},
    )
    assert out["hash"] == before["hash"]
    assert store["a1"] == {"model": {"default": "arn:opu"}}  # untouched


async def test_config_patch_still_accepts_a_plain_patch_dict_for_non_web_callers(monkeypatch):
    """Back-compat: an internal caller that already sends {"patch": {...}} (no baseHash) keeps
    working -- baseHash is optional-concurrency, not mandatory."""
    patched = {}
    monkeypatch.setattr(
        runtime_rpc.hermes_profiles,
        "patch_profile_config",
        lambda owner_id, agent_id, patch: (
            patched.update(owner_id=owner_id, agent_id=agent_id, patch=patch) or {"ok": True}
        ),
    )
    out = await runtime_rpc.execute_sync(
        owner_id="org_a",
        user_id="u1",
        request_id="r1",
        method="config.patch",
        params={"agentId": "a1", "patch": {"memory": {"memory_enabled": False}}},
    )
    assert out == {"ok": True}
    assert patched["patch"] == {"memory": {"memory_enabled": False}}
    assert patched["owner_id"] == "org_a" and patched["agent_id"] == "a1"


def _fake_profile_models(monkeypatch, models: dict, *, names: dict | None = None):
    """``read_profile_config`` for each roster row -- what ``agents.list`` reads each agent's
    pinned ``model.default`` out of (M6 task 10-C, row 29) -- plus the id->name map it labels
    them with (M6-R53), which comes from the SAME backend-served listing ``models.list`` uses.
    A value that is an Exception is raised, standing in for an S3 blip."""

    def read(owner_id, agent_id):
        value = models.get(agent_id)
        if isinstance(value, Exception):
            raise value
        return {"model": {"default": value}} if value else {}

    monkeypatch.setattr(runtime_rpc.hermes_profiles, "read_profile_config", read)

    async def listing(owner_id):
        return {"models": [{"id": mid, "name": name} for mid, name in (names or {}).items()], "defaultId": ""}

    monkeypatch.setattr(runtime_rpc, "_agentcore_models_list", listing)


async def test_agents_list_returns_the_defaultId_and_agents_shape_useagents_reads(monkeypatch):
    """packages/shared/src/hooks/useAgents.ts:57 reads ``data.agents``/``data.defaultId`` -- the
    CEO row (or the first roster row with no CEO) is the default."""
    called = {}

    async def fake_roster(owner_id):
        return [{"id": "a2", "role": "engineering"}, {"id": "a1", "role": "ceo"}]

    monkeypatch.setattr(runtime_rpc, "load_agent_roster", fake_roster)
    _fake_profile_models(
        monkeypatch,
        {"a2": "arn:son", "a1": "arn:opu"},
        names={"arn:son": "Claude Sonnet", "arn:opu": "Claude Opus"},
    )
    monkeypatch.setattr(runtime_rpc.container_rpc, "call", lambda *a, **k: called.setdefault("forwarded", True))
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="agents.list", params={}
    )
    # M6 task 10-C (row 29): every row also carries the REAL model id its own profile pins --
    # Paperclip roster rows carry no model field at all, so without this the model label is blank
    # on every agent surface, and a tier word would name no model the owner could look up. The
    # NAME rides along too (M6-R53), so the chat sidebar never dials models.list for a label.
    assert out["agents"] == [
        {"id": "a2", "role": "engineering", "model": "arn:son", "modelName": "Claude Sonnet"},
        {"id": "a1", "role": "ceo", "model": "arn:opu", "modelName": "Claude Opus"},
    ]
    assert out["defaultId"] == "a1"
    assert "forwarded" not in called


async def test_agents_list_survives_a_transient_profile_read_failure(monkeypatch):
    """Fix round 1, I3: `_s3_get` re-raises anything that is not a 404, and this is the hottest
    control RPC on the platform. One 503 on one of six profiles must blank one model LABEL, not
    empty the whole agent sidebar."""
    _fake_roster(monkeypatch, [{"id": "a1"}, {"id": "a2"}])
    _fake_profile_models(
        monkeypatch, {"a1": RuntimeError("SlowDown"), "a2": "arn:son"}, names={"arn:son": "Claude Sonnet"}
    )

    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="agents.list", params={}
    )

    assert [row["id"] for row in out["agents"]] == ["a1", "a2"]
    assert out["agents"][0]["model"] == "" and out["agents"][1]["model"] == "arn:son"


async def test_agents_list_model_is_the_id_models_list_offers_not_a_tier_word(monkeypatch):
    """The id a row REPORTS must be one ``models.list`` names and ``agents.update`` would ACCEPT
    -- otherwise the picker round-trip 400s on the value it was just handed."""
    _fake_roster(monkeypatch, [{"id": "a1", "role": "ceo"}, {"id": "a2", "role": "finance"}, {"id": "a3"}])
    _fake_profile_models(monkeypatch, {"a1": "arn:opu", "a2": "arn:son"})
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="agents.list", params={}
    )
    # a3's profile pins nothing -- "" is the honest answer, never a made-up tier.
    assert {row["id"]: row["model"] for row in out["agents"]} == {"a1": "arn:opu", "a2": "arn:son", "a3": ""}


async def test_agents_list_defaults_to_the_first_roster_row_when_no_one_is_ceo(monkeypatch):

    async def fake_roster(owner_id):
        return [{"id": "a3", "role": "engineering"}]

    monkeypatch.setattr(runtime_rpc, "load_agent_roster", fake_roster)
    _fake_profile_models(monkeypatch, {"a3": "arn:son"})
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="agents.list", params={}
    )
    assert out["defaultId"] == "a3"
