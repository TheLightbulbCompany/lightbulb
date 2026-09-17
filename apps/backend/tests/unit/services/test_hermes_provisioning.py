from unittest.mock import AsyncMock

import pytest

from core.repositories.container_repo import ProvisionSuperseded
from core.services import hermes_provisioning

HERMES_IMAGE = "acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-v1"


def _row(value):
    async def _get(owner_id, *, consistent=False):
        return value

    return _get


async def _noop_async(*args, **kwargs):
    return None


def _noop_async_returns(value):
    async def _f(*args, **kwargs):
        return value

    return _f


async def _explode(*args, **kwargs):
    raise AssertionError(f"should not have been called: args={args!r} kwargs={kwargs!r}")


def test_the_request_has_no_efs_leg_and_points_at_the_hermes_image():
    req = hermes_provisioning.build_hermes_runtime_request(
        owner_id="org_a",
        image_uri="877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-v1",
        role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a",
        subnets=["subnet-1"],
        security_groups=["sg-1"],
        environment={"ISOL8_OWNER_ID": "org_a"},
    )
    assert "filesystemConfigurations" not in req
    assert req["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith("hermes-agent-arm64:h-v1")
    assert req["networkConfiguration"]["networkMode"] == "VPC"
    assert req["protocolConfiguration"]["serverProtocol"] == "HTTP"
    assert req["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 4 * 60 * 60
    assert req["tags"]["ManagedBy"] == "lightbulb-backend"


def test_the_runtime_env_carries_identifiers_only_and_no_secret_value(monkeypatch):
    """M4-R4/R5/R19: no plaintext bearer/hook/gateway token, ever — only the ONE boot-secret
    NAME plus non-secret identifiers."""
    env = hermes_provisioning.build_hermes_runtime_env(
        owner_id="org_a",
        api_base="https://api-dev.flickmeon.ai",
        bucket="lightbulb-dev-openclaw-replicas",
        boot_secret_id="lightbulb/dev/hermes/org_a/boot",
        gate_endpoint="https://gate.example/invoke",
        sonnet_aip_arn="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/abc",
    )
    assert env["S3_PREFIX"] == "org_a/hermes"  # owner FIRST -- the IAM grant is <owner>/*
    assert env["S3_BUCKET"] == "lightbulb-dev-openclaw-replicas"
    assert env["HERMES_HOME"] == "/opt/data"
    assert env["HERMES_HEADLESS"] == "1"
    assert env["AWS_BEDROCK_FORCE_HTTP1"] == "1"
    assert env["AWS_ENDPOINT_URL_BEDROCK_RUNTIME"] == "https://gate.example/invoke"
    assert env["ANTHROPIC_MODEL"].endswith("/abc")
    assert env["LIGHTBULB_SECRET_ID"] == "lightbulb/dev/hermes/org_a/boot"
    for key, value in env.items():
        assert not value.startswith(("sk-", "pcp_")), key
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert "OPENCLAW_HOOK_TOKEN" not in env
    assert "GATEWAY_TOKEN" not in env


# --- prepare_hermes_workspace (M4-R33): tokens + boot secret BEFORE step 0 -----------------


def _patch_owner_aws(monkeypatch, *, hook_token=None, gateway_token=None, existing_row=None):
    import core.billing.bedrock_aip as bedrock_aip_mod
    import core.billing.bedrock_gate_tokens as bedrock_gate_tokens_mod

    monkeypatch.setattr(bedrock_aip_mod, "ensure_owner_aips", AsyncMock(return_value={"sonnet": "arn:aip:sonnet"}))
    monkeypatch.setattr(
        bedrock_aip_mod,
        "ensure_owner_bedrock_role",
        AsyncMock(return_value="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(bedrock_gate_tokens_mod, "mint_gate_token", AsyncMock(return_value="bearer-plaintext"))
    monkeypatch.setattr(hermes_provisioning, "ensure_agentcore_trust", AsyncMock(return_value=True))
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(existing_row))


@pytest.mark.asyncio
async def test_prepare_hermes_workspace_mints_tokens_and_writes_one_json_boot_secret(monkeypatch):
    _patch_owner_aws(monkeypatch, existing_row=None)

    written = {}

    async def _fake_write_boot_secret(owner_id, *, hook_token, gateway_token, bedrock_bearer):
        written.update(hookToken=hook_token, gatewayToken=gateway_token, bedrockBearer=bedrock_bearer)
        return f"lightbulb/dev/hermes/{owner_id}/boot"

    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _fake_write_boot_secret)

    row_fields, row_kwargs = {}, {}

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        row_fields.update(fields)
        row_kwargs.update(kwargs)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)

    await hermes_provisioning.prepare_hermes_workspace("org_a", claim_id="claim-1", attempt_token="tok-1")

    assert set(written) == {"hookToken", "gatewayToken", "bedrockBearer"}
    assert written["bedrockBearer"] == "bearer-plaintext"
    assert row_fields["hook_token"] == written["hookToken"]
    assert row_fields["gateway_token"] == written["gatewayToken"]
    # Non-terminal: the lease is refreshed, not released -- the provision isn't done yet.
    assert row_kwargs["release_lease"] is False
    assert row_kwargs["attempt_token"] == "tok-1"


@pytest.mark.asyncio
async def test_prepare_hermes_workspace_reuses_existing_tokens_on_a_retry(monkeypatch):
    """A retry of a partially-failed provision must not orphan an already-issued token."""
    _patch_owner_aws(monkeypatch, existing_row={"hook_token": "existing-hook", "gateway_token": "existing-gw"})
    monkeypatch.setattr(
        hermes_provisioning, "write_boot_secret", _noop_async_returns("lightbulb/dev/hermes/org_a/boot")
    )

    written = {}

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        written.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)

    await hermes_provisioning.prepare_hermes_workspace("org_a", claim_id="claim-1")
    assert written["hook_token"] == "existing-hook"
    assert written["gateway_token"] == "existing-gw"


@pytest.mark.asyncio
async def test_prepare_hermes_workspace_is_a_noop_once_already_fully_provisioned(monkeypatch):
    """No AWS call, no bearer rotation, no row write for an owner that already has a runtime --
    avoids rotating a LIVE bearer on every reconcile dispatch against an already-Hermes owner."""
    monkeypatch.setattr(
        hermes_provisioning.container_repo, "get_by_owner_id", _row({"agentcore_runtime_arn": "arn:existing"})
    )
    monkeypatch.setattr(hermes_provisioning, "ensure_agentcore_trust", _explode)
    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _explode)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _explode)

    await hermes_provisioning.prepare_hermes_workspace("org_a", claim_id="claim-1")


# --- provision_hermes_runtime ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_writes_the_row_fields_every_existing_seam_reads(monkeypatch):
    updated = {}
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:aws:bedrock-agentcore:...:runtime/r1")
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(
            ("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "arn:aip:sonnet"})
        ),
    )
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        updated.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)
    arn = await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", attempt_token="tok-1")
    assert arn == "arn:aws:bedrock-agentcore:...:runtime/r1"
    assert updated["runtime_host"] == "agentcore"
    assert updated["agentcore_runtime_arn"] == arn
    assert updated["status"] == "running"
    # hook_token/gateway_token are prepare_hermes_workspace's job now -- not repeated here.
    assert "hook_token" not in updated
    assert "gateway_token" not in updated


@pytest.mark.asyncio
async def test_provision_refuses_without_a_configured_image(monkeypatch):
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", "")
    with pytest.raises(RuntimeError, match="AGENTCORE_HERMES_IMAGE"):
        await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")


@pytest.mark.asyncio
async def test_provision_is_idempotent_on_a_row_that_already_has_a_runtime(monkeypatch):
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", HERMES_IMAGE)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:existing"}),
    )
    created = {}
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: created.setdefault("x", 1))
    assert await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1") == "arn:existing"
    assert created == {}


# --- M4-R28: fenced terminal write, mirroring ecs_manager.wait_for_running -----------------


@pytest.mark.asyncio
async def test_provision_writes_through_the_fenced_lease_helper_not_a_bare_update(monkeypatch):
    calls = []
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:r1")
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(
            ("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "arn:aip:sonnet"})
        ),
    )
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))

    async def _spy_update(owner_id, claim_id, fields, **kwargs):
        calls.append((owner_id, claim_id, fields, kwargs))

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("bare update_fields must never be the terminal write for a fenced provision")

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _spy_update)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _must_not_be_called)

    await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", attempt_token="tok-1")
    assert len(calls) == 1
    owner_id, claim_id, fields, kwargs = calls[0]
    assert owner_id == "org_a"
    assert claim_id == "claim-1"
    assert kwargs.get("attempt_token") == "tok-1"
    assert kwargs.get("release_lease") is True
    assert fields["runtime_host"] == "agentcore"


@pytest.mark.asyncio
async def test_a_lost_fence_propagates_and_leaves_the_row_untouched(monkeypatch):
    """A straggler from a superseded attempt must not clobber a takeover's row -- same contract
    as ecs_manager.wait_for_running. DynamoDB's conditional write is atomic, so a raise here IS
    "the row untouched": nothing partial can have landed."""
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:r1")
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(
            ("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "arn:aip:sonnet"})
        ),
    )
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))

    async def _refuse(owner_id, claim_id, fields, **kwargs):
        raise ProvisionSuperseded("stale attempt")

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _refuse)
    with pytest.raises(ProvisionSuperseded):
        await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", attempt_token="stale-tok")


# --- Writer failure handling: catch per agent, name the variable, don't half-provision silently


def _patch_write_profiles_collaborators(
    monkeypatch,
    *,
    provider_choice="bedrock_claude",
    byo_provider=None,
    env_vars=None,
    read_byo_key=None,
    write_agent_profile=None,
):
    async def _fake_roster(owner_id):
        return [{"id": "11111111-1111-1111-1111-111111111111", "name": "Ada", "role": "ceo"}]

    async def _fake_resolve_provider_choice(owner_id):
        return provider_choice, byo_provider

    async def _fake_build_connected_integration_servers(owner_id):
        return {}

    async def _fake_build_provisioned_env(owner_id):
        return env_vars or {}

    async def _fake_ensure_paperclip_api_key(*, owner_id, paperclip_agent_id, admin_client):
        return "pcp_key"

    monkeypatch.setattr("core.services.container_provision.resolve_provider_choice", _fake_resolve_provider_choice)
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers",
        _fake_build_connected_integration_servers,
    )
    monkeypatch.setattr("core.services.provisioning_landing.build_provisioned_env", _fake_build_provisioned_env)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", _fake_roster)
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", _fake_ensure_paperclip_api_key)
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", _noop_async)
    if read_byo_key is not None:
        monkeypatch.setattr("core.services.key_service.read_byo_key", read_byo_key)
    if write_agent_profile is not None:
        monkeypatch.setattr("core.services.hermes_profiles.write_agent_profile", write_agent_profile)


@pytest.mark.asyncio
async def test_a_bad_profile_value_surfaces_a_named_provisioning_error(monkeypatch):
    _patch_write_profiles_collaborators(
        monkeypatch,
        env_vars={"BAD_VAR": "line1\nline2"},  # illegal per hermes_profiles.render_dotenv
        read_byo_key=_explode,
    )

    with pytest.raises(RuntimeError, match="BAD_VAR"):
        await hermes_provisioning._write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})


# --- BYO key delivery (M4-R32) --------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_profiles_bakes_the_byo_key_into_the_env_for_a_byo_owner(monkeypatch):
    async def _fake_read_byo_key(owner_id, provider):
        assert owner_id == "org_a"
        assert provider == "anthropic"
        return "sk-real-byo-key"

    captured = {}

    def _fake_write_agent_profile(owner_id, agent, **kwargs):
        captured.update(kwargs)
        return object()  # ProfileFiles stand-in -- upload_profile is a no-op double

    _patch_write_profiles_collaborators(
        monkeypatch,
        provider_choice="byo_key",
        byo_provider="anthropic",
        read_byo_key=_fake_read_byo_key,
        write_agent_profile=_fake_write_agent_profile,
    )

    await hermes_provisioning._write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})
    assert captured["byo_provider"] == "anthropic"
    assert captured["byo_api_key"] == "sk-real-byo-key"


@pytest.mark.asyncio
async def test_write_profiles_never_reads_a_byo_key_for_a_non_byo_owner(monkeypatch):
    _patch_write_profiles_collaborators(
        monkeypatch,
        provider_choice="bedrock_claude",
        byo_provider=None,
        read_byo_key=_explode,
    )

    # Real write_agent_profile runs (not mocked) -- proves the non-BYO path produces a normal
    # profile with byo_api_key=None, with no read ever attempted.
    await hermes_provisioning._write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})
