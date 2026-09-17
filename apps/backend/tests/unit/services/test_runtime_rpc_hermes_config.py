"""M4-R1 (Task 13a): config.get/config.patch/config.set/agents.list on an AgentCore row are served
from the profile in S3 + the Paperclip roster — no microVM wake for a settings read. ECS rows are
untouched.

Note on ``_is_agentcore``: it is ``async def`` (it awaits ``runtime_transport.is_agentcore_owner``,
which itself awaits a DynamoDB read), so every monkeypatch below replaces it with a real coroutine
function, never a bare value — ``await`` on a non-awaitable raises ``TypeError`` regardless of what
the replacement returns.
"""

from core.services import runtime_rpc


def _agentcore(is_agentcore: bool):
    async def fake(owner_id):
        return is_agentcore

    return fake


async def test_config_get_on_an_agentcore_row_is_served_from_s3_not_the_runtime(monkeypatch):
    called = {}
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
    monkeypatch.setattr(
        runtime_rpc.hermes_profiles,
        "read_profile_config",
        lambda owner_id, agent_id: {"model": {"default": "arn:son"}},
    )
    monkeypatch.setattr(runtime_rpc.container_rpc, "call", lambda *a, **k: called.setdefault("forwarded", True))
    out = await runtime_rpc.execute_sync(
        owner_id="org_a",
        user_id="u1",
        request_id="r1",
        method="config.get",
        params={"agentId": "a1"},
    )
    assert out == {"model": {"default": "arn:son"}}
    assert "forwarded" not in called


async def test_config_patch_on_an_agentcore_row_rewrites_s3_and_bumps_the_revision(monkeypatch):
    patched = {}
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
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


async def test_agents_list_on_an_agentcore_row_reads_the_paperclip_roster_not_the_runtime(monkeypatch):
    called = {}
    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))

    async def fake_roster(owner_id):
        return [{"id": "a1", "role": "ceo"}]

    monkeypatch.setattr(runtime_rpc, "load_agent_roster", fake_roster)
    monkeypatch.setattr(runtime_rpc.container_rpc, "call", lambda *a, **k: called.setdefault("forwarded", True))
    out = await runtime_rpc.execute_sync(
        owner_id="org_a", user_id="u1", request_id="r1", method="agents.list", params={}
    )
    assert out == [{"id": "a1", "role": "ceo"}]
    assert "forwarded" not in called


async def test_config_get_on_an_agentcore_row_requires_an_agent_id(monkeypatch):
    import pytest

    from core.services.messaging import ApiProblem

    monkeypatch.setattr(runtime_rpc, "_is_agentcore", _agentcore(True))
    with pytest.raises(ApiProblem):
        await runtime_rpc.execute_sync(owner_id="org_a", user_id="u1", request_id="r1", method="config.get", params={})


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
