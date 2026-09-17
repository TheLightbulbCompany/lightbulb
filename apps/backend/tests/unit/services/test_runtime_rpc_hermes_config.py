"""M4-R1 (Task 13a): config.get/config.patch/config.set/agents.list on an AgentCore row are served
from the profile in S3 + the Paperclip roster -- no microVM wake for a settings read. ECS rows are
untouched.

Fix round 1 (continuation), Ruling M4-R43: the wire shape below is driven by what the REAL
browser clients send and read -- ``apps/frontend/src/hooks/useConfigPatch.ts`` and
``PluginsTab.tsx`` (never send an ``agentId``, read ``snapshot.config``/``snapshot.hash``, and
send ``config.patch`` as ``{raw: JSON.stringify(patch), baseHash}``), and
``packages/shared/src/hooks/useAgents.ts`` (reads ``data.agents``/``data.defaultId``). Every test
below drives ``execute_sync`` with exactly those params and asserts exactly those response keys --
not a call recorder.

Note on ``_is_agentcore``: it is ``async def`` (it awaits ``runtime_transport.is_agentcore_owner``,
which itself awaits a DynamoDB read), so every monkeypatch below replaces it with a real coroutine
function, never a bare value -- ``await`` on a non-awaitable raises ``TypeError`` regardless of
what the replacement returns.
"""

import json

from core.services import runtime_rpc


def _agentcore(is_agentcore: bool):
    async def fake(owner_id):
        return is_agentcore

    return fake


async def test_config_get_with_an_agent_id_returns_the_snapshot_shape_useconfigpatch_reads(monkeypatch):
    """``useConfigPatch.ts``'s ``ConfigGetSnapshot`` type is ``{config, hash, valid?}`` --
    ``snapshot?.config``/``snapshot?.hash`` are the two fields every real reader touches."""
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
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
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))

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


async def test_config_patch_accepts_the_raw_baseHash_shape_useconfigpatch_sends(monkeypatch):
    """The ONLY real web caller (useConfigPatch.ts:41-43, PluginsTab.tsx:90-96) sends
    ``{raw: JSON.stringify(patch), baseHash}`` -- never a ``patch`` dict."""
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
    monkeypatch.setattr(runtime_rpc.hermes_profiles, "config_snapshot", lambda owner_id, agent_id: ({}, "hash-before"))
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
        params={
            "agentId": "a1",
            "raw": json.dumps({"plugins": {"entries": {"stripe": {"enabled": False}}}}),
            "baseHash": "hash-before",
        },
    )
    assert out == {"ok": True}
    assert patched["patch"] == {"plugins": {"entries": {"stripe": {"enabled": False}}}}
    assert patched["agent_id"] == "a1"


async def test_config_patch_with_a_stale_baseHash_errors_and_uploads_nothing(monkeypatch):
    import pytest

    from core.services.messaging import ApiProblem

    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
    monkeypatch.setattr(runtime_rpc.hermes_profiles, "config_snapshot", lambda owner_id, agent_id: ({}, "hash-current"))

    def _must_not_upload(*a, **k):
        raise AssertionError("a stale baseHash must not upload")

    monkeypatch.setattr(runtime_rpc.hermes_profiles, "patch_profile_config", _must_not_upload)
    with pytest.raises(ApiProblem):
        await runtime_rpc.execute_sync(
            owner_id="org_a",
            user_id="u1",
            request_id="r1",
            method="config.patch",
            params={"agentId": "a1", "raw": json.dumps({"memory": {"memory_enabled": False}}), "baseHash": "stale"},
        )


async def test_config_patch_with_an_empty_patch_uploads_nothing_and_still_reports_ok(monkeypatch):
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
    monkeypatch.setattr(runtime_rpc.hermes_profiles, "config_snapshot", lambda owner_id, agent_id: ({}, "hash-current"))

    def _must_not_upload(*a, **k):
        raise AssertionError("an empty patch must not upload or bump the revision")

    monkeypatch.setattr(runtime_rpc.hermes_profiles, "patch_profile_config", _must_not_upload)
    out = await runtime_rpc.execute_sync(
        owner_id="org_a",
        user_id="u1",
        request_id="r1",
        method="config.patch",
        params={"agentId": "a1", "raw": json.dumps({}), "baseHash": "hash-current"},
    )
    assert out == {"ok": True}


async def test_config_patch_still_accepts_a_plain_patch_dict_for_non_web_callers(monkeypatch):
    """Back-compat: an internal caller that already sends {"patch": {...}} (no baseHash) keeps
    working -- baseHash is optional-concurrency, not mandatory."""
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
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


async def test_config_set_on_an_agentcore_row_replaces_wholesale(monkeypatch):
    """M4-R1 extends the brief's config.get/config.patch pair with config.set (full replace,
    not a merge) and agents.list."""
    seen = {}
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
    monkeypatch.setattr(
        runtime_rpc.hermes_profiles,
        "set_profile_config",
        lambda owner_id, agent_id, config: seen.update(config=config) or config,
    )
    out = await runtime_rpc.execute_sync(
        owner_id="org_a",
        user_id="u1",
        request_id="r1",
        method="config.set",
        params={"agentId": "a1", "config": {"model": {"default": "x"}}},
    )
    assert out == {"model": {"default": "x"}}
    assert seen["config"] == {"model": {"default": "x"}}


async def test_config_set_still_requires_an_agent_id(monkeypatch):
    """Unlike config.get, config.set/config.patch inherently target ONE profile -- there is no
    real caller that sends config.set/config.patch with no agentId, and no sane roster-map
    fallback for a WRITE."""
    import pytest

    from core.services.messaging import ApiProblem

    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
    with pytest.raises(ApiProblem):
        await runtime_rpc.execute_sync(owner_id="org_a", user_id="u1", request_id="r1", method="config.set", params={})


async def test_agents_list_returns_the_defaultId_and_agents_shape_useagents_reads(monkeypatch):
    """packages/shared/src/hooks/useAgents.ts:57 reads ``data.agents``/``data.defaultId`` -- the
    CEO row (or the first roster row with no CEO) is the default."""
    called = {}
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))

    async def fake_roster(owner_id):
        return [{"id": "a2", "role": "engineering"}, {"id": "a1", "role": "ceo"}]

    monkeypatch.setattr(runtime_rpc, "load_agent_roster", fake_roster)
    monkeypatch.setattr(runtime_rpc.container_rpc, "call", lambda *a, **k: called.setdefault("forwarded", True))
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="agents.list", params={}
    )
    assert out["agents"] == [{"id": "a2", "role": "engineering"}, {"id": "a1", "role": "ceo"}]
    assert out["defaultId"] == "a1"
    assert "forwarded" not in called


async def test_agents_list_defaults_to_the_first_roster_row_when_no_one_is_ceo(monkeypatch):
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))

    async def fake_roster(owner_id):
        return [{"id": "a3", "role": "engineering"}]

    monkeypatch.setattr(runtime_rpc, "load_agent_roster", fake_roster)
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="agents.list", params={}
    )
    assert out["defaultId"] == "a3"


async def test_config_methods_still_forward_for_an_ecs_row(monkeypatch):
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(False))
    seen = {}

    async def fake_call(owner_id, method, params=None, **k):
        seen.update(method=method)
        return {"from": "container"}

    monkeypatch.setattr(runtime_rpc.container_rpc, "call", fake_call)
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={}
    )
    assert out == {"from": "container"} and seen["method"] == "config.get"
