from unittest.mock import AsyncMock, MagicMock, call

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle


def _wire_prepare(monkeypatch, agents):
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.list_routines = AsyncMock(return_value=[])
    admin.list_issues = AsyncMock(return_value=[])
    admin.patch_routine = AsyncMock(return_value={})
    admin.patch_issue = AsyncMock(return_value={})
    http = MagicMock()
    http.aclose = AsyncMock()
    monkeypatch.setattr(lifecycle, "PaperclipRepo", lambda table_name: MagicMock())
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="company"))
    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: http)
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_seeded_agent_ids",
        AsyncMock(side_effect=AssertionError("lifecycle must not depend on seeded ids")),
    )
    return admin


@pytest.mark.asyncio
async def test_specialist_delete_uses_surviving_root_ceo(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "root", "name": "Arbitrary", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Friday", "role": "researcher", "reportsTo": "root"},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="specialist")

    assert result["replacement_agent_id"] == "root"


@pytest.mark.asyncio
async def test_renamed_final_root_ceo_cannot_be_deleted(monkeypatch):
    admin = _wire_prepare(
        monkeypatch,
        [
            {"id": "root", "name": "Ada", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Friday", "role": "researcher", "reportsTo": "root"},
        ],
    )

    with pytest.raises(RuntimeError, match="final root CEO"):
        await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="root")

    admin.list_routines.assert_not_awaited()
    admin.list_issues.assert_not_awaited()


@pytest.mark.asyncio
async def test_root_ceo_can_be_deleted_after_another_root_exists(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "old-root", "name": "Ada", "role": "ceo", "reportsTo": None},
            {"id": "new-root", "name": "Bea", "role": "ceo", "reportsTo": None},
            {"id": "specialist", "name": "Cid", "role": "engineer", "reportsTo": "old-root"},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="old-root")

    assert result["replacement_agent_id"] == "new-root"


@pytest.mark.asyncio
async def test_replacement_falls_back_to_first_surviving_agent(monkeypatch):
    _wire_prepare(
        monkeypatch,
        [
            {"id": "retiring", "name": "Ada", "role": "general", "reportsTo": None},
            {"id": "first", "name": "Bea", "role": "engineer", "reportsTo": None},
            {"id": "second", "name": "Cid", "role": "researcher", "reportsTo": None},
        ],
    )

    result = await lifecycle.prepare_paperclip_agent_delete(owner_id="owner", agent_id="retiring")

    assert result["replacement_agent_id"] == "first"


@pytest.mark.asyncio
async def test_paperclip_delete_conditionally_removes_starter_uuid(monkeypatch):
    repo = MagicMock()
    repo.remove_starter_agent_id = AsyncMock(return_value=None)
    monkeypatch.setattr(lifecycle, "PaperclipRepo", lambda table_name: repo)
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="company"))
    admin = MagicMock()
    admin.delete_agent = AsyncMock(return_value={})
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    http = MagicMock()
    http.aclose = AsyncMock()
    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: http)

    await lifecycle.delete_paperclip_for_chat(owner_id="owner", agent_id="starter-uuid")
    await lifecycle.delete_paperclip_for_chat(owner_id="owner", agent_id="starter-uuid")

    assert admin.delete_agent.await_count == 2
    assert repo.remove_starter_agent_id.await_args_list == [
        call("owner", "starter-uuid"),
        call("owner", "starter-uuid"),
    ]


# ---------------------------------------------------------------------------
# M6 task-15 item 1 — repoint_agent_adapter (task-14 row 1, the founder blocker)
# ---------------------------------------------------------------------------


def _wire_repoint(monkeypatch, *, hook_token="hook-tok"):
    """Paperclip admin behind a real async context manager (``repoint_agent_adapter`` uses
    ``async with httpx.AsyncClient(...)``), plus the container row its http branch reads."""
    admin = MagicMock()
    admin.patch_agent = AsyncMock(return_value={})

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(lifecycle.service_token, "mint", lambda owner_id: "svc-jwt")
    monkeypatch.setattr(lifecycle.container_repo, "get_by_owner_id", AsyncMock(return_value={"hook_token": hook_token}))
    return admin


@pytest.mark.asyncio
async def test_repoint_moves_an_existing_agent_onto_the_http_adapter(monkeypatch):
    """The whole point: an agent CREATED on ECS keeps ``openclaw_gateway`` forever, so a migrated
    workspace dials an operator socket at a dead address for every routine/issue/Run."""
    admin = _wire_repoint(monkeypatch)
    agent = {"id": "agent-1", "name": "Ada", "adapterType": "openclaw_gateway"}

    assert await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="agentcore") is True

    body = admin.patch_agent.await_args.kwargs["body"]
    assert body["adapterType"] == lifecycle.HTTP_ADAPTER_TYPE
    assert body["replaceAdapterConfig"] is True
    assert body["adapterConfig"]["url"].endswith("/api/v1/internal/paperclip/runs/org_1")
    assert body["adapterConfig"]["headers"]["Authorization"] == "Bearer hook-tok"
    # The REAL agent id, not a name label: a create cannot know it, a re-point always can.
    assert body["adapterConfig"]["headers"]["X-Lightbulb-Agent"] == "agent-1"


@pytest.mark.asyncio
async def test_repoint_is_idempotent_on_an_agent_already_on_the_target(monkeypatch):
    """A re-entered migration tail / re-run rollback must re-point only what is still wrong."""
    admin = _wire_repoint(monkeypatch)
    agent = {"id": "agent-1", "adapterType": lifecycle.HTTP_ADAPTER_TYPE}

    assert await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="agentcore") is False

    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_repoint_refuses_to_write_an_http_adapter_with_no_hook_token(monkeypatch):
    """``build_http_adapter_config`` happily emits ``Bearer `` — an adapter whose every run 401s
    at the internal door. Failing here is the point: a half-re-pointed workspace looks healthy."""
    admin = _wire_repoint(monkeypatch, hook_token="")
    agent = {"id": "agent-1", "adapterType": "openclaw_gateway"}

    with pytest.raises(RuntimeError):
        await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="agentcore")

    admin.patch_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_repoint_back_to_gateway_binds_the_operator_device(monkeypatch):
    """Rollback: a freshly synthesized gateway adapter carries the service JWT as ``authToken``
    and an unregistered device, so the live gateway rejects it until the per-agent bind runs."""
    admin = _wire_repoint(monkeypatch)
    monkeypatch.setattr(lifecycle.settings, "ENVIRONMENT", "")  # -> ws://localhost:8000, allowlisted
    monkeypatch.setattr(lifecycle.service_token, "mint", lambda owner_id: "svc-jwt")
    paired = AsyncMock()
    monkeypatch.setattr("core.services.paperclip_openclaw_pairing.ensure_operator_device_paired", paired)
    agent = {"id": "agent-1", "adapterType": lifecycle.HTTP_ADAPTER_TYPE}

    assert await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="ecs", gateway_token="gw") is True

    body = admin.patch_agent.await_args.kwargs["body"]
    assert body["adapterType"] == lifecycle.OPENCLAW_GATEWAY_TYPE
    assert body["adapterConfig"]["authToken"] == "gw"
    assert body["adapterConfig"]["agentId"] == "agent-1"
    assert body["adapterConfig"]["claimedApiKeyPath"].endswith("workspace-agent-1/paperclip-claimed-api-key.json")
    paired.assert_awaited_once()
    assert paired.await_args.kwargs["gateway_token"] == "gw"
    assert paired.await_args.kwargs["paperclip_agent"]["adapterConfig"] == body["adapterConfig"]


@pytest.mark.asyncio
async def test_repoint_back_to_gateway_requires_the_gateway_token_before_patching(monkeypatch):
    admin = _wire_repoint(monkeypatch)
    agent = {"id": "agent-1", "adapterType": lifecycle.HTTP_ADAPTER_TYPE}

    with pytest.raises(RuntimeError, match="gateway_token"):
        await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="ecs")

    admin.patch_agent.assert_not_awaited()


def _partial_gateway_adapter():
    return {
        "url": "ws://localhost:8000?token=svc-jwt&clientId=paperclip-operator",
        "authToken": "svc-jwt",
        "sessionKeyStrategy": "issue",
        "sessionKey": "paperclip:org_1",
        "devicePrivateKeyPem": "stable-existing-pem",
        "timeoutSec": 600,
        "timeoutMs": 600_000,
        "waitTimeoutMs": 600_000,
    }


@pytest.mark.asyncio
async def test_repoint_repairs_a_partially_bound_same_type_gateway_adapter(monkeypatch):
    """A failed old bind leaves adapterType correct but authToken/agentId wrong. Retry must repair
    that row, preserve its stable device and timeout fields, and not skip on type alone."""
    admin = _wire_repoint(monkeypatch)
    paired = AsyncMock()
    monkeypatch.setattr("core.services.paperclip_openclaw_pairing.ensure_operator_device_paired", paired)
    agent = {
        "id": "agent-1",
        "adapterType": lifecycle.OPENCLAW_GATEWAY_TYPE,
        "adapterConfig": _partial_gateway_adapter(),
    }

    assert await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="ecs", gateway_token="gw") is True

    config = admin.patch_agent.await_args.kwargs["body"]["adapterConfig"]
    assert config["authToken"] == "gw"
    assert config["agentId"] == "agent-1"
    assert config["devicePrivateKeyPem"] == "stable-existing-pem"
    assert config["timeoutSec"] == 600
    assert config["timeoutMs"] == 600_000
    assert config["waitTimeoutMs"] == 600_000
    paired.assert_awaited_once()


@pytest.mark.asyncio
async def test_repoint_required_bind_failure_is_observable_and_the_same_retry_converges(monkeypatch):
    """The first authoritative patch is the required bind. If it fails, report failure; retry the
    same partial row rather than skipping it because adapterType already says gateway."""
    admin = _wire_repoint(monkeypatch)
    admin.patch_agent.side_effect = [RuntimeError("bind failed"), {}]
    paired = AsyncMock()
    monkeypatch.setattr("core.services.paperclip_openclaw_pairing.ensure_operator_device_paired", paired)
    agent = {
        "id": "agent-1",
        "adapterType": lifecycle.OPENCLAW_GATEWAY_TYPE,
        "adapterConfig": _partial_gateway_adapter(),
    }

    with pytest.raises(RuntimeError, match="bind failed"):
        await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="ecs", gateway_token="gw")
    paired.assert_not_awaited()

    assert await lifecycle.repoint_agent_adapter("org_1", agent, runtime_host="ecs", gateway_token="gw") is True
    assert admin.patch_agent.await_count == 2
    paired.assert_awaited_once()
