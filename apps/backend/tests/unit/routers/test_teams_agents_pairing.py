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


@pytest.fixture(autouse=True)
def _provisioned_container(monkeypatch):
    """``create_agent`` builds its adapter via ``paperclip_agent_lifecycle.resolve_agent_adapter``,
    which reads the owner's container row for the hook token -- and the shared builder REFUSES to
    write an adapter without one (every run would 401 at the facade, reported per RUN rather than
    as a broken agent). These tests are not about that guard, so hand them a provisioned row;
    ``test_paperclip_agent_lifecycle.py`` covers the refusal. Tests needing a different row stub
    the same read themselves."""
    from core.repositories import container_repo

    monkeypatch.setattr(
        container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"hook_token": "hook-tok", "agentcore_runtime_arn": "arn:r1"}),
    )


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
@pytest.mark.parametrize(
    "row",
    [
        None,
        {"owner_id": "user_x", "gateway_token": "gw_tok"},  # not provisioned yet
        {"owner_id": "user_x", "agentcore_runtime_arn": "arn:r1"},  # no gateway token
    ],
)
async def test_container_is_reachable_is_false_without_a_runtime_and_a_token(monkeypatch, row):
    """Both halves are required: the ARN is the address and the token is the only way to
    authenticate to it, so a row carrying one without the other is not reachable."""
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=row))

    from routers.teams.agents import _container_is_reachable

    assert await _container_is_reachable("user_x") == (False, None, None)


@pytest.mark.asyncio
async def test_container_is_reachable_reads_the_row_and_pings_nothing(monkeypatch):
    """It used to resolve a live task IP through ECS. Any ``InvokeAgentRuntime`` wakes the
    microVM, so there is no task IP and no "is the task serving yet" question to answer -- and
    asking one would WAKE the workspace just to answer whether an agent can be created in it."""
    row = {"owner_id": "user_x", "gateway_token": "gw_tok", "agentcore_runtime_arn": "arn:r1"}
    read = AsyncMock(return_value=row)
    monkeypatch.setattr("core.repositories.container_repo.get_by_owner_id", read)

    from routers.teams.agents import _container_is_reachable

    assert await _container_is_reachable("user_x") == (True, "agentcore", "gw_tok")
    read.assert_awaited_once_with("user_x")


@pytest.mark.asyncio
async def test_container_is_reachable_reads_a_ddb_blip_as_not_reachable(monkeypatch):
    """Defensive: the caller refuses to create an agent it cannot wire, which is the safe answer
    when we simply could not tell."""
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id", AsyncMock(side_effect=RuntimeError("throttled"))
    )

    from routers.teams.agents import _container_is_reachable

    assert await _container_is_reachable("user_x") == (False, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "org_id", "expected_owner"),
    [("user_member_b", "org_xyz", "org_xyz"), ("user_personal", None, "user_personal")],
)
async def test_teams_create_agent_scopes_the_adapter_to_the_owner(monkeypatch, user_id, org_id, expected_owner):
    """P1: the adapter is built for the OWNER, never the member who happens to be creating the
    agent. It used to be a service JWT minted with ``sub=owner_id``, where a member's id would
    have routed Paperclip's wakes to that member's own workspace; it is a per-workspace callback
    URL plus that workspace's hook token now, and the same mistake points the agent at the wrong
    workspace just as silently.
    """
    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", AsyncMock())
    admin_instance = MagicMock()
    admin_instance.create_agent = AsyncMock(return_value={"id": "a_new"})
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin_instance)
    monkeypatch.setattr("routers.teams.agents._user_session", AsyncMock(return_value="cookie123"))

    ctx = MagicMock()
    ctx.auth.user_id = user_id
    ctx.auth.org_id = org_id
    ctx.company_id = "co_xyz"

    await create_agent(CreateAgentBody(name="Helper", role="engineer"), ctx)

    config = admin_instance.create_agent.await_args.kwargs["adapter_config"]
    assert config["apiBaseUrl"].endswith(f"/api/v1/internal/paperclip/hermes/{expected_owner}")
    assert config["apiKey"] == "hook-tok"


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
    ctx.user_id = "user_member_b"
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
async def test_teams_create_agent_writes_the_hermes_profile(monkeypatch):
    """The profile write IS the pairing -- through the same
    ``hermes_provisioning.write_profile_for_agent`` seam runtime_rpc's agents.create uses."""
    paperclip_agent = {"id": "11111111-1111-1111-1111-111111111111", "name": "Helper", "role": "engineer"}
    _wire_create_agent(monkeypatch, container=_AGENTCORE_ROW, paperclip_agent=paperclip_agent)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
    reachable = AsyncMock(side_effect=AssertionError("create_agent must not probe for a container"))
    monkeypatch.setattr("routers.teams.agents._container_is_reachable", reachable)

    from routers.teams.agents import create_agent
    from routers.teams.schemas import CreateAgentBody

    result = await create_agent(CreateAgentBody(name="Helper", role="engineer"), _agentcore_ctx())

    assert result["id"] == paperclip_agent["id"]
    write_profile.assert_awaited_once()
    args, kwargs = write_profile.await_args
    assert args[0] == "org_xyz"
    assert args[1]["id"] == paperclip_agent["id"]
    assert "container_row" not in kwargs
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
        refresh.assert_awaited_once_with("org_xyz", "agent-1", by="user_member_b")


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

    delete_profile = MagicMock()

    from routers.teams.agents import delete_agent

    with (
        patch.object(lifecycle, "prepare_paperclip_agent_delete", new=AsyncMock(return_value={})),
        patch("core.services.hermes_profiles.delete_profile", delete_profile),
        patch.object(lifecycle, "delete_paperclip_for_chat", new=AsyncMock()) as delete_paperclip,
    ):
        result = await delete_agent("agent-1", _agentcore_ctx())

    assert result["deleted"] is True
    delete_profile.assert_called_once_with("org_xyz", "agent-1")
    delete_paperclip.assert_awaited_once_with(owner_id="org_xyz", agent_id="agent-1")
