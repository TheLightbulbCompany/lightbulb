"""Q9a rows #1 + #2 (task-9-scan.md): agents.create / agents.update on an AgentCore row.

Both RPC doors used to stage Paperclip agent metadata into openclaw.json via
``config_patcher.apply_paperclip_meta`` -- an EFS write that raises
``ConfigPatchError`` for an owner with no openclaw.json at all (an AgentCore
workspace has none; see the module docstring on ``hermes_provisioning``).
``agents.create`` propagated that error as an unhandled 500; ``agents.update``
swallowed it silently behind a bare ``except Exception``. Both now branch on
the owner's runtime instead.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import runtime_rpc as rpc
from core.services.hermes_provisioning import HermesProfileError
from core.services.messaging import ApiProblem


def _agentcore(is_agentcore: bool):
    """``_is_agentcore`` is ``async def`` -- the replacement must be a real coroutine
    function, never a bare value (an ``await`` on a non-awaitable raises ``TypeError``)."""

    async def fake(owner_id):
        return is_agentcore

    return fake


def _wire_create(monkeypatch, *, container, paperclip_agent):
    monkeypatch.setattr(rpc.container_rpc, "running_container", AsyncMock(return_value=(container, "10.0.0.1")))
    monkeypatch.setattr(rpc, "create_paperclip_for_chat", AsyncMock(return_value=paperclip_agent))
    monkeypatch.setattr(rpc, "pair_with_openclaw", AsyncMock())
    monkeypatch.setattr(rpc, "ensure_operator_device_paired", AsyncMock())


# ---------------------------------------------------------------------------
# Row #1 -- agents.create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_create_on_agentcore_row_writes_profile_not_efs(monkeypatch):
    container = {"gateway_token": "tok", "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    paperclip_agent = {"id": "agent_new", "name": "Ada", "role": "engineer", "title": "Backend engineer"}
    _wire_create(monkeypatch, container=container, paperclip_agent=paperclip_agent)
    apply_meta = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.apply_paperclip_meta", apply_meta)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
    publish = AsyncMock()
    monkeypatch.setattr(rpc, "publish_owner_users", publish)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.create",
        params={"name": "Ada", "params": {"paperclip": {"role": "engineer", "title": "Backend engineer"}}},
    )

    assert result["agentId"] == "agent_new"
    apply_meta.assert_not_awaited()
    write_profile.assert_awaited_once_with("owner_1", paperclip_agent, container_row=container)
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == (
        "owner_1",
        "runtime.resource.invalidated",
        {"resource": "agents"},
    )


@pytest.mark.asyncio
async def test_agents_create_on_ecs_row_still_applies_paperclip_meta(monkeypatch):
    """The ECS branch stays byte-identical: proves the new AgentCore branch above didn't
    disturb the existing EFS meta-staging path for a plain ECS row."""
    container = {"gateway_token": "tok"}  # no runtime_host -> "ecs" (host_for's own default)
    paperclip_agent = {"id": "agent_new", "name": "Ada"}
    _wire_create(monkeypatch, container=container, paperclip_agent=paperclip_agent)
    apply_meta = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.apply_paperclip_meta", apply_meta)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
    publish = AsyncMock()
    monkeypatch.setattr(rpc, "publish_owner_users", publish)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.create",
        params={"name": "Ada", "params": {"paperclip": {"role": "engineer", "title": "Backend engineer"}}},
    )

    assert result["agentId"] == "agent_new"
    apply_meta.assert_awaited_once()
    assert apply_meta.await_args.kwargs["meta"] == {"role": "engineer", "title": "Backend engineer"}
    write_profile.assert_not_awaited()
    publish.assert_not_awaited()  # unchanged ECS behaviour: agents.create never invalidated here


@pytest.mark.asyncio
async def test_agents_create_hermes_profile_error_is_a_clean_rpc_error_not_a_500(monkeypatch):
    container = {"gateway_token": "tok", "runtime_host": "agentcore"}
    paperclip_agent = {"id": "agent_new", "name": "Ada"}
    _wire_create(monkeypatch, container=container, paperclip_agent=paperclip_agent)
    monkeypatch.setattr(
        "core.services.hermes_provisioning.write_profile_for_agent",
        AsyncMock(side_effect=HermesProfileError("hermes profile for owner=owner_1: byo api key read failed")),
    )
    monkeypatch.setattr(rpc, "publish_owner_users", AsyncMock())

    with pytest.raises(ApiProblem) as exc_info:
        await rpc.execute_sync(
            owner_id="owner_1", user_id="user_1", request_id="r1", method="agents.create", params={"name": "Ada"}
        )

    assert exc_info.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Row #2 -- agents.update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_update_on_agentcore_row_patches_only_the_mapped_model_key(monkeypatch):
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(True))
    mirror = AsyncMock()
    monkeypatch.setattr(rpc, "_mirror_agent_update", mirror)
    patch_config = MagicMock(return_value={"model": {"default": "gpt-5", "provider": "openai-api"}})
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    publish = AsyncMock()
    monkeypatch.setattr(rpc, "publish_owner_users", publish)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "model": "gpt-5", "name": "Renamed"},
    )

    assert result == {"ok": True}
    mirror.assert_awaited_once()
    assert mirror.await_args.kwargs == {"agentcore": True}
    patch_config.assert_called_once_with("owner_1", "a1", {"model": {"default": "gpt-5"}})
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == (
        "owner_1",
        "runtime.resource.invalidated",
        {"resource": "agents"},
    )


@pytest.mark.asyncio
async def test_agents_update_on_agentcore_row_skips_profile_patch_when_no_model_field(monkeypatch):
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(True))
    monkeypatch.setattr(rpc, "_mirror_agent_update", AsyncMock())
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    monkeypatch.setattr(rpc, "publish_owner_users", AsyncMock())

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "name": "Renamed"},
    )

    assert result == {"ok": True}
    patch_config.assert_not_called()


@pytest.mark.asyncio
async def test_agents_update_on_agentcore_row_never_reaches_container_rpc(monkeypatch):
    """No AgentCore bridge RPC method exists for this door -- the AgentCore leg must never
    fall through to the generic container_rpc.call dispatch below it."""
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(True))
    monkeypatch.setattr(rpc, "_mirror_agent_update", AsyncMock())
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", MagicMock())
    monkeypatch.setattr(rpc, "publish_owner_users", AsyncMock())
    call_mock = AsyncMock()
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "model": "gpt-5"},
    )

    call_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_agents_update_on_ecs_row_still_forwards_to_container_rpc(monkeypatch):
    """The ECS branch stays byte-identical: unchanged fallthrough to the live gateway."""
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(False))
    monkeypatch.setattr(rpc, "_mirror_agent_update", AsyncMock())
    call_mock = AsyncMock(return_value={"ok": True, "forwarded": True})
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "model": "gpt-5"},
    )

    assert result == {"ok": True, "forwarded": True}
    call_mock.assert_awaited_once_with(
        "owner_1", "agents.update", {"agentId": "a1", "model": "gpt-5"}, timeout_s=rpc._RPC_TIMEOUT_S
    )
