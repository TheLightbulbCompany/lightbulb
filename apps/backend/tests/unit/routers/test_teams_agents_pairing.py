"""Tests for /teams agent create → openclaw pairing."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

# Some imports transitively construct a ManagementApiClient, which requires
# WS_MANAGEMENT_API_URL — set a bogus value so construction never raises.
# pair_with_openclaw is monkeypatched in every test below regardless.
os.environ.setdefault(
    "WS_MANAGEMENT_API_URL",
    "https://test.execute-api.us-east-1.amazonaws.com/test",
)

# service_token.mint() reads PAPERCLIP_SERVICE_TOKEN_KEY at call time;
# every test in this module either patches mint() outright or runs the
# create_agent path that calls it. Set a deterministic key so the few
# call paths that exercise mint() unmocked don't crash.
os.environ.setdefault("PAPERCLIP_SERVICE_TOKEN_KEY", "test-key-not-used")


@pytest.fixture(autouse=True)
def _ecs_default_container(monkeypatch):
    """``create_agent`` picks its ADAPTER TYPE via
    ``paperclip_agent_lifecycle.resolve_agent_adapter``, which reads the owner's
    container row (``adapter_type_for``) -- a row that doesn't exist under test. A
    MISSING row is the correct ECS default for that selection (see that function's
    docstring), not a real DynamoDB call, so this stands in for "no container row
    yet" everywhere. This is independent of ``runtime_transport.host_for``, which
    is pinned to AgentCore fleet-wide -- every test's actual post-create wiring
    goes through the Hermes profile write, never container pairing (deletion PR-1;
    see ``tests/unit/services/test_runtime_rpc_agent_lifecycle_agentcore.py``)."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_teams_create_agent_uses_user_id_for_personal_owner(monkeypatch):
    """Personal user (no org_id) → owner_id == user_id, threaded through to the
    Hermes profile write -- the only reachable transport (``host_for`` is
    pinned to AgentCore; there is no container to pair with any more)."""
    pc_uuid = "11111111-1111-1111-1111-111111111111"

    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)

    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value={"id": pc_uuid, "name": "Helper", "role": "engineer"})
    monkeypatch.setattr(
        "routers.teams.agents._admin",
        lambda: admin_instance,
    )
    monkeypatch.setattr(
        "routers.teams.agents._user_session",
        AsyncMock(return_value="cookie123"),
    )

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    ctx = MagicMock()
    ctx.auth.user_id = "user_personal"
    ctx.auth.org_id = None
    ctx.company_id = "co_personal"

    await create_agent(CreateAgentBody(name="Helper", role="engineer"), ctx)

    assert write_profile.await_args.args[0] == "user_personal"


@pytest.mark.asyncio
async def test_container_is_reachable_uses_resolve_running_container(monkeypatch):
    """The helper now goes through ecs_manager.resolve_running_container
    (which uses ECS DescribeTasks) rather than reading a non-existent
    private_ip field from DDB."""
    from unittest.mock import AsyncMock, MagicMock

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(
        return_value=(
            {"owner_id": "user_x", "gateway_token": "gw_tok", "service_name": "svc"},
            "10.0.0.42",
        )
    )
    monkeypatch.setattr("routers.teams.agents.get_ecs_manager", lambda: fake_ecs)

    from routers.teams.agents import _container_is_reachable

    reachable, ip, token = await _container_is_reachable("user_x")
    assert reachable is True
    assert ip == "10.0.0.42"
    assert token == "gw_tok"
    fake_ecs.resolve_running_container.assert_awaited_once_with("user_x")


@pytest.mark.asyncio
async def test_container_is_reachable_returns_false_when_no_running_task(monkeypatch):
    """If resolve_running_container returns (None, None) — no row or
    not running — _container_is_reachable returns (False, None, None)."""
    from unittest.mock import AsyncMock, MagicMock

    fake_ecs = MagicMock()
    fake_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    monkeypatch.setattr("routers.teams.agents.get_ecs_manager", lambda: fake_ecs)

    from routers.teams.agents import _container_is_reachable

    assert await _container_is_reachable("user_x") == (False, None, None)


@pytest.mark.asyncio
async def test_teams_create_agent_mints_service_token_with_owner_id(monkeypatch):
    """In org context the synthesized adapterConfig.authToken MUST be a
    JWT minted with sub=owner_id (the
    org_id), NOT sub=member_user_id. The WS authorizer treats service
    tokens as orgId="" + userId=<sub>, so a member-keyed token routes
    Paperclip wakes to the member's PERSONAL container instead of the
    org's shared container.
    """
    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    # create_agent no longer mints/synthesizes inline -- both now happen inside the shared
    # paperclip_agent_lifecycle.resolve_agent_adapter (openclaw_gateway branch), so that's
    # where the patches now go.
    mint_mock = MagicMock(return_value="svc.tok.minted-for-org")
    monkeypatch.setattr(
        "core.services.paperclip_agent_lifecycle.service_token.mint",
        mint_mock,
    )
    # Capture the synthesized adapter config so we can confirm the minted
    # token threaded through.
    synth_mock = MagicMock(return_value={"authToken": "svc.tok.minted-for-org"})
    monkeypatch.setattr(
        "core.services.paperclip_agent_lifecycle.synthesize_openclaw_adapter",
        synth_mock,
    )

    # host_for is pinned to AgentCore now -- the row-less owner (the autouse
    # fixture's ``None``) still routes through the Hermes profile write, not
    # container pairing; mock it out so create_agent completes instead of
    # rolling back on a real S3/Paperclip call.
    monkeypatch.setattr(
        "core.services.hermes_provisioning.write_profile_for_agent",
        AsyncMock(),
    )
    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value={"id": "a_new"})
    monkeypatch.setattr(
        "routers.teams.agents._admin",
        lambda: admin_instance,
    )
    monkeypatch.setattr(
        "routers.teams.agents._user_session",
        AsyncMock(return_value="cookie123"),
    )

    ctx = MagicMock()
    ctx.auth.user_id = "user_member_b"
    ctx.auth.org_id = "org_xyz"
    ctx.company_id = "co_xyz"

    await create_agent(CreateAgentBody(name="Helper", role="engineer"), ctx)

    # P1: token is minted under the OWNER's id, not the member's.
    mint_mock.assert_called_once_with("org_xyz")
    # The minted token threaded through to synthesize_openclaw_adapter.
    assert synth_mock.call_args.kwargs["service_token"] == "svc.tok.minted-for-org"


@pytest.mark.asyncio
async def test_teams_create_agent_mints_with_user_id_for_personal_owner(monkeypatch):
    """Regression guard for the personal-owner branch: when the caller
    has no org_id, mint with sub=user_id so personal users keep working
    after the org-context fix."""
    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    mint_mock = MagicMock(return_value="svc.tok.minted-for-personal")
    monkeypatch.setattr(
        "core.services.paperclip_agent_lifecycle.service_token.mint",
        mint_mock,
    )
    monkeypatch.setattr(
        "core.services.paperclip_agent_lifecycle.synthesize_openclaw_adapter",
        MagicMock(return_value={"authToken": "svc.tok.minted-for-personal"}),
    )
    # host_for is pinned to AgentCore now -- see the comment in the sibling
    # org-context test above.
    monkeypatch.setattr(
        "core.services.hermes_provisioning.write_profile_for_agent",
        AsyncMock(),
    )
    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value={"id": "a_new"})
    monkeypatch.setattr(
        "routers.teams.agents._admin",
        lambda: admin_instance,
    )
    monkeypatch.setattr(
        "routers.teams.agents._user_session",
        AsyncMock(return_value="cookie123"),
    )

    ctx = MagicMock()
    ctx.auth.user_id = "user_personal"
    ctx.auth.org_id = None
    ctx.company_id = "co_personal"

    await create_agent(CreateAgentBody(name="Helper", role="engineer"), ctx)

    # Personal owner: sub == user_id (no regression).
    mint_mock.assert_called_once_with("user_personal")


# ---------------------------------------------------------------------------
# M6-R59: the Teams create/patch doors on a MIGRATED (AgentCore) workspace.
#
# There is no container to pair with there, so the ECS pairing block was simply
# skipped and nothing wrote the agent's Hermes profile -- the roster row existed
# and the agent could never run a turn (found live on a migrated dev workspace).
# ---------------------------------------------------------------------------

_AGENTCORE_ROW = {
    "owner_id": "org_xyz",
    "runtime_host": "agentcore",
    "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
    "hook_token": "hook-tok",
    "bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"},
}


def _agentcore_ctx():
    ctx = MagicMock()
    ctx.auth.user_id = "user_member_b"
    ctx.auth.org_id = "org_xyz"
    ctx.company_id = "co_xyz"
    return ctx


def _wire_create_agent(monkeypatch, *, container, paperclip_agent):
    """Stub the I/O ``create_agent`` does either side of the transport branch."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value=container))
    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value=paperclip_agent)
    admin_instance.delete_agent = AsyncMock(return_value={})
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin_instance)
    monkeypatch.setattr("routers.teams.agents._user_session", AsyncMock(return_value="cookie123"))
    return admin_instance


@pytest.mark.asyncio
async def test_teams_create_agent_on_agentcore_writes_the_hermes_profile(monkeypatch):
    """The profile write IS this transport's pairing -- through the same
    ``hermes_provisioning.write_profile_for_agent`` seam runtime_rpc's agents.create uses,
    and with no container pairing attempted at all."""
    paperclip_agent = {"id": "11111111-1111-1111-1111-111111111111", "name": "Helper", "role": "engineer"}
    _wire_create_agent(monkeypatch, container=_AGENTCORE_ROW, paperclip_agent=paperclip_agent)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
    pair_mock = AsyncMock()
    monkeypatch.setattr("routers.teams.agents.pair_with_openclaw", pair_mock)
    paired_mock = AsyncMock()
    monkeypatch.setattr("routers.teams.agents.ensure_operator_device_paired", paired_mock)
    reachable = AsyncMock(side_effect=AssertionError("no container probe on an agentcore row"))
    monkeypatch.setattr("routers.teams.agents._container_is_reachable", reachable)

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    result = await create_agent(CreateAgentBody(name="Helper", role="engineer"), _agentcore_ctx())

    assert result["id"] == paperclip_agent["id"]
    write_profile.assert_awaited_once()
    args, kwargs = write_profile.await_args
    assert args[0] == "org_xyz"
    assert args[1]["id"] == paperclip_agent["id"]
    assert kwargs["container_row"] == _AGENTCORE_ROW
    pair_mock.assert_not_awaited()
    paired_mock.assert_not_awaited()
    reachable.assert_not_awaited()


@pytest.mark.asyncio
async def test_teams_create_agent_on_agentcore_rolls_back_when_the_profile_write_fails(monkeypatch):
    """A failed profile write must never leave a roster row that cannot run: the Paperclip
    agent is deleted and the request answers 502 instead of a silent 200."""
    from fastapi import HTTPException

    paperclip_agent = {"id": "22222222-2222-2222-2222-222222222222", "name": "Helper", "role": "engineer"}
    admin_instance = _wire_create_agent(monkeypatch, container=_AGENTCORE_ROW, paperclip_agent=paperclip_agent)
    monkeypatch.setattr(
        "core.services.hermes_provisioning.write_profile_for_agent",
        AsyncMock(side_effect=RuntimeError("s3 down")),
    )

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    with pytest.raises(HTTPException) as excinfo:
        await create_agent(CreateAgentBody(name="Helper", role="engineer"), _agentcore_ctx())

    assert excinfo.value.status_code == 502
    admin_instance.delete_agent.assert_awaited_once_with(session_cookie="cookie123", agent_id=paperclip_agent["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("container", "body_kwargs", "expected"),
    [
        (_AGENTCORE_ROW, {"name": "Renamed"}, True),
        (_AGENTCORE_ROW, {"title": "Head of Ops"}, True),
        # budgetMonthlyCents never appears in SOUL.md -- no S3 round trip for it.
        (_AGENTCORE_ROW, {"budget_monthly_cents": 500}, False),
    ],
)
async def test_teams_patch_agent_refreshes_soul_only_on_agentcore_identity_edits(
    monkeypatch, container, body_kwargs, expected
):
    """A rename reaches a Hermes agent ONLY through its SOUL.md -- and only there."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value=container))
    admin_instance = MagicMock()
    admin_instance.patch_agent = AsyncMock(return_value={"id": "agent-1", "name": "Renamed"})
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin_instance)
    refresh = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.refresh_agent_soul", refresh)

    from routers.teams.agents import patch_agent
    from routers.teams.schemas import PatchAgentBody

    await patch_agent("agent-1", PatchAgentBody(**body_kwargs), _agentcore_ctx())

    assert refresh.await_count == (1 if expected else 0)
    if expected:
        refresh.assert_awaited_once_with("org_xyz", "agent-1")


def test_soul_fields_covers_every_field_build_soul_md_renders():
    """M6 task-15 item 4. ``_SOUL_FIELDS`` must track ``build_soul_md``, not ``PatchAgentBody``:
    a field the renderer uses but this set omits produces no error anywhere — the Paperclip row
    and the UI show the new value while the agent keeps introducing itself with the old one. It
    omitted ``role``, which the renderer puts on two lines. ``id`` is the key, never patched."""
    from core.services.hermes_profiles import build_soul_md
    from routers.teams.agents import _SOUL_FIELDS

    base = {"id": "a1", "name": "Ada", "role": "ceo", "title": "Chief", "capabilities": "things"}
    rendered = build_soul_md(base)
    for field in _SOUL_FIELDS:
        assert build_soul_md({**base, field: "changed-value"}) != rendered, f"{field} is not in SOUL.md"
    # And nothing the renderer ignores has crept in.
    assert not _SOUL_FIELDS & {"reportsTo", "budgetMonthlyCents", "icon"}


@pytest.mark.asyncio
async def test_teams_patch_agent_survives_a_failed_soul_refresh(monkeypatch):
    """The Paperclip row (what the UI reads) already landed, so a SOUL hiccup is logged,
    not raised -- unlike create, where the agent would be dead on arrival."""
    from core.repositories import container_repo

    monkeypatch.setattr(container_repo, "get_by_owner_id", AsyncMock(return_value=_AGENTCORE_ROW))
    admin_instance = MagicMock()
    admin_instance.patch_agent = AsyncMock(return_value={"id": "agent-1"})
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin_instance)
    monkeypatch.setattr(
        "core.services.hermes_provisioning.refresh_agent_soul",
        AsyncMock(side_effect=RuntimeError("s3 down")),
    )

    from routers.teams.agents import patch_agent
    from routers.teams.schemas import PatchAgentBody

    assert await patch_agent("agent-1", PatchAgentBody(name="Renamed"), _agentcore_ctx()) == {"id": "agent-1"}


@pytest.mark.asyncio
async def test_teams_delete_agent_on_agentcore_removes_the_s3_profile(monkeypatch):
    """Delete already reached the profile through ``delete_agent_everywhere``'s own AgentCore
    branch -- pinned here at the Teams door so the third route cannot regress with the others."""
    from unittest.mock import patch

    from core.services import paperclip_agent_lifecycle as lifecycle

    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(_AGENTCORE_ROW, "agentcore"))
    delete_profile = MagicMock()
    workspace = MagicMock()

    from routers.teams.agents import delete_agent

    with (
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.containers.get_ecs_manager", return_value=ecs),
        patch("core.containers.get_workspace", return_value=workspace),
        patch("core.services.hermes_profiles.delete_profile", delete_profile),
        patch("core.containers.workspace.cleanup_deleted_agent", new=AsyncMock()),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        result = await delete_agent("agent-1", _agentcore_ctx())

    assert result["deleted"] is True
    delete_profile.assert_called_once_with("org_xyz", "agent-1")
    delete_paperclip.assert_awaited_once_with(owner_id="org_xyz", agent_id="agent-1")
