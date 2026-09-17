import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.repositories.container_repo import ProvisionSuperseded
from core.services import hermes_provisioning

HERMES_IMAGE = "acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-v1"


def _configure_hermes_settings(monkeypatch):
    """Both loud-failure guards provision_hermes_runtime checks on entry (m3: OWNER_STATE_BUCKET
    got the same guard AGENTCORE_HERMES_IMAGE already had)."""
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "lightbulb-dev-openclaw-replicas")


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


def _no_runtime_found_by_name(monkeypatch):
    """Every fresh-create test needs the C1 name-lookup mocked -- it runs unconditionally
    whenever the row has no ARN yet, and the real function makes a live AWS call."""
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: None)


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
    # I1: both must point at the gate -- Hermes's Claude route may resolve either.
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "https://gate.example/invoke"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["ANTHROPIC_MODEL"].endswith("/abc")
    assert env["LIGHTBULB_SECRET_ID"] == "lightbulb/dev/hermes/org_a/boot"
    for key, value in env.items():
        assert not value.startswith(("sk-", "pcp_")), key
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert "OPENCLAW_HOOK_TOKEN" not in env
    assert "GATEWAY_TOKEN" not in env


def test_the_runtime_env_never_carries_the_nous_guest_launch_gate(monkeypatch):
    """M4-R38: Hermes's vision auxiliary task falls through to its auto chain (OpenRouter ->
    Nous -> DeepInfra) even with fallback_chain: [], and is unreachable in our runtime ONLY
    because no OPENROUTER_API_KEY/DEEPINFRA_API_KEY exists AND Nous's free tier is gated on
    HERMES_GUEST_ONBOARDING=1 (hermes_cli/anon_auth.py::guest_enabled). None of these three keys
    must ever appear here -- this is the egress fence for that aux task."""
    env = hermes_provisioning.build_hermes_runtime_env(
        owner_id="org_a",
        api_base="https://api-dev.flickmeon.ai",
        bucket="lightbulb-dev-openclaw-replicas",
        boot_secret_id="lightbulb/dev/hermes/org_a/boot",
        gate_endpoint="https://gate.example/invoke",
        sonnet_aip_arn="arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/abc",
    )
    assert "HERMES_GUEST_ONBOARDING" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert "DEEPINFRA_API_KEY" not in env


# --- I5: _wait_for_runtime_ready ------------------------------------------------------------


class _FakeAgentCoreClient:
    def __init__(self, statuses):
        self._statuses = iter(statuses)

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803 -- matches boto3's own param casing
        return {"status": next(self._statuses)}


def test_wait_for_runtime_ready_returns_once_status_is_ready(monkeypatch):
    import boto3

    monkeypatch.setattr(hermes_provisioning, "_READY_POLL_INTERVAL_S", 0)
    client = _FakeAgentCoreClient(["CREATING", "CREATING", "READY"])
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)

    hermes_provisioning._wait_for_runtime_ready("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1")
    # Exhausted exactly the three statuses above -- a fourth call would raise StopIteration.
    with pytest.raises(StopIteration):
        client.get_agent_runtime(agentRuntimeId="r1")


def test_wait_for_runtime_ready_raises_on_create_failed_with_the_status_in_the_message(monkeypatch):
    import boto3

    monkeypatch.setattr(hermes_provisioning, "_READY_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: _FakeAgentCoreClient(["CREATE_FAILED"]))

    with pytest.raises(RuntimeError, match="CREATE_FAILED"):
        hermes_provisioning._wait_for_runtime_ready("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1")


def test_wait_for_runtime_ready_raises_on_update_failed_with_the_status_in_the_message(monkeypatch):
    import boto3

    monkeypatch.setattr(hermes_provisioning, "_READY_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: _FakeAgentCoreClient(["UPDATE_FAILED"]))

    with pytest.raises(RuntimeError, match="UPDATE_FAILED"):
        hermes_provisioning._wait_for_runtime_ready("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1")


def test_wait_for_runtime_ready_raises_on_window_timeout(monkeypatch):
    import boto3

    monkeypatch.setattr(hermes_provisioning, "_READY_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(hermes_provisioning, "_READY_POLL_ATTEMPTS", 3)  # bound the loop for test speed
    # Never READY, never terminal -- the window itself must be what raises.
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: _FakeAgentCoreClient(["CREATING"] * 3))

    with pytest.raises(RuntimeError, match="did not reach READY"):
        hermes_provisioning._wait_for_runtime_ready("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1")


# --- N2/M4-R40: _update_runtime_image sends the FULL spec, validated by real botocore --------


def test_update_runtime_image_sends_the_full_create_spec_not_just_the_image(monkeypatch):
    """A MagicMock client can't catch this class of bug -- botocore's OWN parameter validator
    (which runs on a real client before any network call) is what proves roleArn/network/env/
    lifecycle are all present, not just agentRuntimeId + agentRuntimeArtifact. Verified against
    the installed botocore service model: UpdateAgentRuntime REQUIRES agentRuntimeId,
    agentRuntimeArtifact AND roleArn, and does NOT accept agentRuntimeName or tags."""
    import boto3
    from botocore.stub import Stubber

    request = hermes_provisioning.build_hermes_runtime_request(
        owner_id="org_a",
        image_uri="877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-v2",
        role_arn="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a",
        subnets=["subnet-1"],
        security_groups=["sg-1"],
        environment={"ISOL8_OWNER_ID": "org_a"},
    )

    client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
    stubber = Stubber(client)
    expected_params = {k: v for k, v in request.items() if k not in ("agentRuntimeName", "tags")}
    expected_params["agentRuntimeId"] = "r1"
    stubber.add_response(
        "update_agent_runtime",
        {
            "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/r1",
            "agentRuntimeId": "r1",
            "agentRuntimeVersion": "2",
            "createdAt": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            "lastUpdatedAt": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            "status": "UPDATING",
        },
        expected_params,
    )
    stubber.activate()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)

    hermes_provisioning._update_runtime_image("arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/r1", request)

    stubber.assert_no_pending_responses()
    # agentRuntimeName/tags are CREATE-only members -- UpdateAgentRuntime's model rejects both.
    assert "agentRuntimeName" not in expected_params
    assert "tags" not in expected_params
    assert expected_params["roleArn"] == request["roleArn"]
    assert expected_params["networkConfiguration"] == request["networkConfiguration"]
    assert expected_params["environmentVariables"] == request["environmentVariables"]
    assert expected_params["protocolConfiguration"] == request["protocolConfiguration"]
    assert expected_params["lifecycleConfiguration"] == request["lifecycleConfiguration"]


# --- N3/M4-R41: _find_existing_runtime_arn paginates ----------------------------------------


def _list_runtimes_item(name, arn):
    return {
        "agentRuntimeArn": arn,
        "agentRuntimeId": arn.rsplit("/", 1)[-1],
        "agentRuntimeVersion": "1",
        "agentRuntimeName": name,
        "description": "hermes runtime",
        "lastUpdatedAt": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        "status": "READY",
    }


def test_find_existing_runtime_arn_matches_on_page_two(monkeypatch):
    """One runtime per owner means the fleet outgrows page 1 of list_agent_runtimes eventually --
    a match sitting on page 2 must still be found, not silently missed."""
    import boto3
    from botocore.stub import Stubber

    client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "list_agent_runtimes",
        {
            "agentRuntimes": [
                _list_runtimes_item("hermes-org-other", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/other")
            ],
            "nextToken": "page-2-token",
        },
    )
    stubber.add_response(
        "list_agent_runtimes",
        {"agentRuntimes": [_list_runtimes_item("hermes-org_a", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/org_a")]},
    )
    stubber.activate()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)

    arn = hermes_provisioning._find_existing_runtime_arn("hermes-org_a")

    assert arn == "arn:aws:bedrock-agentcore:us-east-1:1:runtime/org_a"
    stubber.assert_no_pending_responses()


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


# --- I3/M4-R37: freshness guard against rotating a live ECS owner's bearer -----------------


@pytest.mark.asyncio
async def test_prepare_hermes_workspace_refuses_a_live_ecs_owner_without_migrate(monkeypatch):
    """HERMES_RUNTIME is one env-wide flag; the first dispatch after a flip must not mint
    Hermes credentials (and rotate the owner's live bearer) for an owner already running on
    ECS -- mirrors ensure_runtime's own guard against two gateways on one owner."""
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"service_name": "openclaw-org_a-abc123", "hook_token": "ecs-hook", "gateway_token": "ecs-gw"}),
    )
    monkeypatch.setattr(hermes_provisioning, "ensure_agentcore_trust", _explode)
    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _explode)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _explode)

    await hermes_provisioning.prepare_hermes_workspace("org_a", claim_id="claim-1")


@pytest.mark.asyncio
async def test_prepare_hermes_workspace_migrate_true_proceeds_for_a_live_ecs_owner(monkeypatch):
    """The one sanctioned bypass: the M6 cutover job passes migrate=True and deliberately
    crosses an existing ECS owner over, reusing its hook/gateway tokens exactly as the
    fresh-provision path does and minting only a fresh bearer."""
    _patch_owner_aws(
        monkeypatch,
        existing_row={"service_name": "openclaw-org_a-abc123", "hook_token": "ecs-hook", "gateway_token": "ecs-gw"},
    )

    written = {}

    async def _fake_write_boot_secret(owner_id, *, hook_token, gateway_token, bedrock_bearer):
        written.update(hookToken=hook_token, gatewayToken=gateway_token, bedrockBearer=bedrock_bearer)
        return f"lightbulb/dev/hermes/{owner_id}/boot"

    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _fake_write_boot_secret)

    row_fields = {}

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        row_fields.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)

    await hermes_provisioning.prepare_hermes_workspace("org_a", claim_id="claim-1", migrate=True)

    # Reused off the row -- the ECS-minted tokens, not fresh ones.
    assert written["hookToken"] == "ecs-hook"
    assert written["gatewayToken"] == "ecs-gw"
    assert row_fields["hook_token"] == "ecs-hook"
    assert row_fields["gateway_token"] == "ecs-gw"
    # The bearer is always freshly minted (see _patch_owner_aws's mint_gate_token double).
    assert written["bedrockBearer"] == "bearer-plaintext"


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
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    _no_runtime_found_by_name(monkeypatch)

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
    # m7: explicit rather than relying on the image guard firing before this is ever read --
    # brittle if the guard order ever changes.
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _explode)
    with pytest.raises(RuntimeError, match="AGENTCORE_HERMES_IMAGE"):
        await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")


@pytest.mark.asyncio
async def test_provision_refuses_without_a_configured_bucket(monkeypatch):
    """m3: the same loud-failure guard AGENTCORE_HERMES_IMAGE has -- an empty bucket is
    otherwise silent all the way down (upload_profile no-ops, restore_profile no-ops, every
    agent boots on Hermes's own unpinned auto-chain)."""
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "")
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _explode)
    with pytest.raises(RuntimeError, match="OWNER_STATE_BUCKET"):
        await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")


@pytest.mark.asyncio
async def test_provision_is_idempotent_on_a_row_that_already_has_a_runtime(monkeypatch):
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:existing", "status": "running"}),
    )
    created = {}
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: created.setdefault("x", 1))
    assert await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1") == "arn:existing"
    assert created == {}


@pytest.mark.asyncio
async def test_provision_polls_instead_of_creating_when_the_row_has_an_unready_arn(monkeypatch):
    """M4-R35/C1: a retry after a create-but-not-yet-ready runtime must reuse the recorded ARN,
    not hit CreateAgentRuntime again (which would 409 on the unique runtime name)."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:not-ready-yet"}),  # no status="running" yet
    )
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)  # must not be called
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", _explode)  # arn already on the row
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: HERMES_IMAGE)  # unchanged -- no update
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)

    polled = {}

    def _fake_wait(arn):
        polled["arn"] = arn

    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", _fake_wait)

    terminal = {}

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        terminal.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")
    assert arn == "arn:not-ready-yet"
    assert polled["arn"] == "arn:not-ready-yet"
    assert terminal["status"] == "running"


@pytest.mark.asyncio
async def test_a_create_failed_runtime_is_deleted_arn_cleared_and_the_failure_raised(monkeypatch):
    """F1 (predeploy review, M6-R34): a CREATE_FAILED/UPDATE_FAILED runtime is dead -- AWS
    refuses UpdateAgentRuntime against it, so every retry that found this ARN on the row would
    otherwise call Update on a terminal runtime forever. The terminal branch must delete it and
    clear the row's ARN (same fenced write the ARN record used) before re-raising."""
    _configure_hermes_settings(monkeypatch)
    _no_runtime_found_by_name(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:dead")

    def _fake_wait(arn):
        raise hermes_provisioning.AgentCoreRuntimeCreateFailed(
            f"AgentCore runtime {arn} failed to start: CREATE_FAILED"
        )

    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", _fake_wait)
    deleted = {}
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda arn: deleted.setdefault("arn", arn))

    writes: list[dict] = []

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        writes.append(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)

    with pytest.raises(hermes_provisioning.AgentCoreRuntimeCreateFailed, match="CREATE_FAILED"):
        await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", attempt_token="tok-1")

    assert deleted["arn"] == "arn:dead"
    # writes[0] is the pre-poll ARN record (M4-R35/C1); writes[1] is this fix's clear.
    assert writes[0]["agentcore_runtime_arn"] == "arn:dead"
    assert writes[-1]["agentcore_runtime_arn"] is None


@pytest.mark.asyncio
async def test_a_create_failed_runtime_delete_is_best_effort(monkeypatch):
    """The runtime may already be gone (a concurrent operator cleanup, a prior attempt's own
    delete) -- a failed DeleteAgentRuntime must not stop the row's ARN from being cleared or
    the original failure from being raised."""
    _configure_hermes_settings(monkeypatch)
    _no_runtime_found_by_name(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:dead")
    monkeypatch.setattr(
        hermes_provisioning,
        "_wait_for_runtime_ready",
        lambda arn: (_ for _ in ()).throw(hermes_provisioning.AgentCoreRuntimeCreateFailed("CREATE_FAILED")),
    )

    def _explode_delete(arn):
        raise RuntimeError("already gone")

    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", _explode_delete)
    writes: list[dict] = []

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        writes.append(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)

    with pytest.raises(hermes_provisioning.AgentCoreRuntimeCreateFailed):
        await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", attempt_token="tok-1")

    assert writes[-1]["agentcore_runtime_arn"] is None


@pytest.mark.asyncio
async def test_provision_skips_write_profiles_for_an_arn_row_in_error(monkeypatch):
    """N4/M4-R42: write_profiles runs BEFORE CreateAgentRuntime, so an ARN row already has its
    profiles in S3. Idempotence keys on status == "running" (M4-R35/C1), so an ARN row sitting in
    error (which C2's release-to-error path now produces routinely) must NOT re-run the profile
    fan-out and overwrite owner-edited profiles / bump .revision -- it only recovers the runtime."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:existing-error-row", "status": "error"}),
    )
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _explode)  # must NOT be called
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", _explode)  # arn already on the row
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: HERMES_IMAGE)  # unchanged
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)

    terminal = {}

    async def _fake_update_container_leased(owner_id, claim_id, fields, **kwargs):
        terminal.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _fake_update_container_leased)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")
    assert arn == "arn:existing-error-row"
    assert terminal["status"] == "running"


@pytest.mark.asyncio
async def test_provision_resolves_a_name_conflict_via_the_list_lookup(monkeypatch):
    """M4-R35/C1: no ARN on the row at all, but AgentCore already has a runtime under this
    owner's derived name -- a prior attempt's row write was lost. Reuse it instead of hitting
    CreateAgentRuntime (which would 409 on the name)."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)  # must not be called
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:found-by-name")
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)

    calls = []

    async def _spy_update(owner_id, claim_id, fields, **kwargs):
        calls.append((fields, kwargs))

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _spy_update)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")
    assert arn == "arn:found-by-name"
    # The row never had the ARN, so it must be recorded (non-terminal) before the terminal write.
    assert len(calls) == 2
    assert calls[0][0] == {"agentcore_runtime_arn": "arn:found-by-name"}
    assert calls[0][1]["release_lease"] is False
    assert calls[1][1]["release_lease"] is True


@pytest.mark.asyncio
async def test_provision_updates_the_reused_runtime_when_the_image_drifted(monkeypatch):
    """A reused runtime (found by name, not on the row) whose configured image no longer matches
    settings.AGENTCORE_HERMES_IMAGE gets UpdateAgentRuntime before the READY poll."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:found-by-name")
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: "old-broken-image:tag")

    updated = {}

    def _fake_update_image(arn, request):
        updated["arn"] = arn
        updated["request"] = request

    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _fake_update_image)
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _noop_async)

    await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")
    assert updated["arn"] == "arn:found-by-name"
    # N2/M4-R40: the FULL create spec, not just the image -- this is what proves the drift path
    # can never omit roleArn/network/env/lifecycle again.
    request = updated["request"]
    assert request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == HERMES_IMAGE
    assert request["roleArn"] == "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"
    assert request["networkConfiguration"]["networkMode"] == "VPC"
    assert request["environmentVariables"]
    assert request["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert request["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 4 * 60 * 60


# --- N1/M4-R39: provision_hermes_runtime gets the same freshness guard as prepare -----------


@pytest.mark.asyncio
async def test_provision_hermes_runtime_refuses_a_live_ecs_owner_without_migrate(monkeypatch):
    """A service_name row is a live ECS workspace -- without migrate=True this must not create a
    SECOND runtime and flip the row to runtime_host=agentcore/status=running out from under it."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"service_name": "openclaw-org_a-abc123"}),
    )
    monkeypatch.setattr(hermes_provisioning, "_load_role_and_aips", _explode)
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _explode)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _explode)

    with pytest.raises(ValueError, match="openclaw-org_a-abc123"):
        await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")


@pytest.mark.asyncio
async def test_provision_hermes_runtime_migrate_true_proceeds_for_a_live_ecs_owner(monkeypatch):
    """The M6 cutover job's sanctioned bypass: migrate=True proceeds to create the runtime even
    though the row still carries a live ECS service_name -- the job stops that ECS service itself
    once this reaches READY, which is not this module's job."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"service_name": "openclaw-org_a-abc123"}),
    )
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:aws:bedrock-agentcore:...:runtime/r1")
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(
            ("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "arn:aip:sonnet"})
        ),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _no_runtime_found_by_name(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _noop_async)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", migrate=True)
    assert arn == "arn:aws:bedrock-agentcore:...:runtime/r1"


# --- M4-R28: fenced terminal write, mirroring ecs_manager.wait_for_running -----------------


@pytest.mark.asyncio
async def test_create_record_poll_happen_in_that_order(monkeypatch):
    """M4-R35/C1: the ARN must be recorded (non-terminal, fenced) BEFORE the READY poll runs --
    a CREATE_FAILED or a poll-window timeout must not lose it."""
    order = []
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: order.append("create") or "arn:r1")
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: order.append("poll"))
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    _no_runtime_found_by_name(monkeypatch)

    async def _spy_update(owner_id, claim_id, fields, **kwargs):
        order.append(("record", fields, kwargs.get("release_lease")))

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _spy_update)

    await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", attempt_token="tok-1")
    assert order == [
        "create",
        ("record", {"agentcore_runtime_arn": "arn:r1"}, False),
        "poll",
        ("record", {"runtime_host": "agentcore", "agentcore_runtime_arn": "arn:r1", "status": "running"}, True),
    ]


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
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    _no_runtime_found_by_name(monkeypatch)

    async def _spy_update(owner_id, claim_id, fields, **kwargs):
        calls.append((owner_id, claim_id, fields, kwargs))

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("bare update_fields must never be the terminal write for a fenced provision")

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _spy_update)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _must_not_be_called)

    await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1", attempt_token="tok-1")
    # Two fenced writes now (C1): the non-terminal ARN record, then the terminal transition.
    assert len(calls) == 2
    owner_id, claim_id, fields, kwargs = calls[-1]
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
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    _no_runtime_found_by_name(monkeypatch)

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
    paperclip_api_key="pcp_key",
    mock_facts_staging=True,
):
    """``mock_facts_staging=True`` (the default) no-ops ``_stage_owner_facts`` -- since
    ``write_profile_for_agent`` now stages facts per agent (minor, predeploy review), every
    caller here that doesn't care about that would otherwise make a real, unmocked
    billing_repo/owner_facts read. The three facts-focused tests below pass ``False`` to let
    the real function run against their own mocked collaborators."""

    async def _fake_roster(owner_id):
        return [{"id": "11111111-1111-1111-1111-111111111111", "name": "Ada", "role": "ceo"}]

    async def _fake_resolve_provider_choice(owner_id):
        return provider_choice, byo_provider

    async def _fake_build_connected_integration_servers(owner_id):
        return {}

    async def _fake_build_provisioned_env(owner_id):
        return env_vars or {}

    async def _fake_ensure_paperclip_api_key(*, owner_id, paperclip_agent_id, admin_client):
        return paperclip_api_key

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
    if mock_facts_staging:
        monkeypatch.setattr(hermes_provisioning, "_stage_owner_facts", _noop_async)
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
        await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})


@pytest.mark.asyncio
async def test_a_none_paperclip_api_key_raises_named_instead_of_baking_an_empty_credential(monkeypatch):
    """I2: ensure_paperclip_api_key returns None on ANY failure (its documented contract is
    "skip, try again next time") -- there is no next time once provision_hermes_runtime
    idempotent-returns on the ARN, so this must fail the provision, not ship an empty
    PAPERCLIP_API_KEY."""

    def _write_agent_profile_must_not_be_called(*args, **kwargs):
        raise AssertionError("write_agent_profile must not be reached -- the raise happens first")

    _patch_write_profiles_collaborators(
        monkeypatch,
        paperclip_api_key=None,
        read_byo_key=_explode,
        write_agent_profile=_write_agent_profile_must_not_be_called,
    )

    with pytest.raises(RuntimeError, match="org_a.*11111111-1111-1111-1111-111111111111"):
        await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})


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

    await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})
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
    await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})


@pytest.mark.asyncio
async def test_write_profiles_resolves_owner_level_inputs_exactly_once_for_a_six_agent_roster(monkeypatch):
    """Q9a fix round 1, I3: write_profile_for_agent's own inputs= param lets write_profiles
    resolve owner-level inputs (billing/Secrets/integrations/env) ONCE and reuse them across the
    whole starter roster, instead of paying that cost -- and risking it diverging mid-fan-out --
    once per agent."""
    roster = [
        {"id": f"1111111{i}-1111-1111-1111-111111111111", "name": f"Agent{i}", "role": "general"} for i in range(6)
    ]
    _patch_write_profiles_collaborators(monkeypatch)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    real_resolve = hermes_provisioning.resolve_profile_inputs
    calls: list[str] = []

    async def _counting_resolve(owner_id, *, container_row):
        calls.append(owner_id)
        return await real_resolve(owner_id, container_row=container_row)

    monkeypatch.setattr(hermes_provisioning, "resolve_profile_inputs", _counting_resolve)

    await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})

    assert calls == ["org_a"]


# --- owner facts staged at provision (fix round 1, Important 2) --------------------------------
# A freshly provisioned AgentCore workspace never runs ecs_manager.write_user_configs (the only
# caller of owner_facts.write_owner_facts), so it had no lightbulb-facts.json at all while the
# bundled skill tells every agent to read it. write_profiles now stages it directly.


@pytest.mark.asyncio
async def test_write_profiles_uploads_a_facts_file_to_every_agent_in_a_six_agent_roster(monkeypatch):
    """Minor (predeploy review, M6-R39 housekeeping): facts staging moved INTO
    write_profile_for_agent (per agent) -- the six-agent roster fan-out now produces SIX
    separate one-agent write_facts_to_profiles calls, not one batched six-agent call."""
    roster = [
        {"id": f"1111111{i}-1111-1111-1111-111111111111", "name": f"Agent{i}", "role": "general"} for i in range(6)
    ]
    _patch_write_profiles_collaborators(monkeypatch, provider_choice="bedrock_claude", mock_facts_staging=False)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active"}),
    )
    monkeypatch.setattr("core.services.owner_facts._lookup_email_address", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.owner_facts._lookup_provisioning", AsyncMock(return_value=[]))

    uploaded: list[tuple[str, list[str], str]] = []

    def _fake_write_facts(owner_id, agent_ids, facts_json):
        uploaded.append((owner_id, agent_ids, facts_json))

    monkeypatch.setattr("core.services.hermes_profiles.write_facts_to_profiles", _fake_write_facts)

    await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})

    assert len(uploaded) == 6
    seen_ids = [agent_ids[0] for _owner, agent_ids, _facts in uploaded]
    assert seen_ids == [row["id"] for row in roster]
    for owner_id, agent_ids, facts_json in uploaded:
        assert owner_id == "org_a"
        assert len(agent_ids) == 1
        assert '"provider_choice": "bedrock_claude"' in facts_json


@pytest.mark.asyncio
async def test_write_profiles_facts_staging_uses_this_provisions_own_provider_choice(monkeypatch):
    """Sourced as account_overrides, the same way ecs_manager.write_user_configs's own
    write_owner_facts call does -- this provision's just-resolved provider_choice/byo_provider,
    not a stale/racing billing-row re-read."""
    _patch_write_profiles_collaborators(
        monkeypatch,
        provider_choice="byo_key",
        byo_provider="anthropic",
        read_byo_key=AsyncMock(return_value="sk-real-byo-key"),
        mock_facts_staging=False,
    )
    monkeypatch.setattr(
        # The billing row itself still says bedrock_claude (an eventually-consistent read racing
        # this provision's own provider switch) -- the override must still win.
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "bedrock_claude"}),
    )
    monkeypatch.setattr("core.services.owner_facts._lookup_email_address", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.owner_facts._lookup_provisioning", AsyncMock(return_value=[]))

    captured = {}
    monkeypatch.setattr(
        "core.services.hermes_profiles.write_facts_to_profiles",
        lambda owner_id, agent_ids, facts_json: captured.update(facts_json=facts_json),
    )

    await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})

    assert '"provider_choice": "byo_key"' in captured["facts_json"]
    assert "BYO Anthropic key" in captured["facts_json"]


@pytest.mark.asyncio
async def test_write_profiles_facts_staging_is_best_effort(monkeypatch):
    """A facts-file hiccup must never fail provisioning -- write_profile_for_agent's own
    per-agent writes already succeeded by the time this runs."""
    _patch_write_profiles_collaborators(monkeypatch, mock_facts_staging=False)
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb down"))
    )

    await hermes_provisioning.write_profiles("org_a", {"sonnet": "arn:aip:sonnet"})  # must not raise


# --- write_profile_for_agent (M6-R23) -----------------------------------------------------------
# Moved, unchanged in behaviour, from catalog_service._build_and_upload_agent_profile -- the
# shared per-agent "write this agent's profile" seam write_profiles (above) now delegates to,
# and runtime_rpc's agents.create door for an AgentCore owner calls directly (Q9a row #1).


@pytest.mark.asyncio
async def test_write_profile_for_agent_writes_and_uploads(monkeypatch):
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="bedrock_claude",
        byo_provider=None,
        byo_api_key=None,
        aip_arns={"sonnet": "arn:aip:sonnet"},
        integrations={},
        env_vars={},
    )
    monkeypatch.setattr(hermes_provisioning, "resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pk-123"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    written = {}

    def fake_write_agent_profile(owner_id, agent, **kwargs):
        written["owner_id"] = owner_id
        written["agent"] = agent
        written["kwargs"] = kwargs
        return SimpleNamespace(soul_md="ORIGINAL")

    monkeypatch.setattr("core.services.hermes_profiles.write_agent_profile", fake_write_agent_profile)
    upload_mock = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", upload_mock)
    # Facts staging (minor, predeploy review) isn't this test's concern -- see
    # test_write_profile_for_agent_stages_facts_for_the_new_agent below.
    stage_facts = AsyncMock()
    monkeypatch.setattr(hermes_provisioning, "_stage_owner_facts", stage_facts)
    agent_row = {"id": "agent_new", "name": "Demo", "role": "general"}

    await hermes_provisioning.write_profile_for_agent(
        "owner_1", agent_row, container_row={"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"}}
    )

    assert written["owner_id"] == "owner_1"
    assert written["agent"] is agent_row
    assert written["kwargs"]["provider_choice"] == "bedrock_claude"
    assert written["kwargs"]["aip_arns"] == {"sonnet": "arn:aip:sonnet"}
    assert written["kwargs"]["paperclip_api_key"] == "pk-123"
    upload_mock.assert_awaited_once()
    assert upload_mock.await_args.args[0] == "owner_1"
    assert upload_mock.await_args.args[1] == "agent_new"
    assert upload_mock.await_args.args[2].soul_md == "ORIGINAL"
    stage_facts.assert_awaited_once_with("owner_1", ["agent_new"], inputs)


@pytest.mark.asyncio
async def test_write_profile_for_agent_stages_facts_for_the_new_agent(monkeypatch):
    """Minor (predeploy review, M6-R39 housekeeping): before this fix, only the six-agent
    provision fan-out staged lightbulb-facts.json -- a hired/created agent installed through
    catalog_service's single-agent path or runtime_rpc's agents.create door had no facts file
    at all until the next billing event re-wrote it."""
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="bedrock_claude",
        byo_provider=None,
        byo_api_key=None,
        aip_arns={"sonnet": "arn:aip:sonnet"},
        integrations={},
        env_vars={},
    )
    monkeypatch.setattr(hermes_provisioning, "resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pk-123"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.hermes_profiles.write_agent_profile",
        lambda owner_id, agent, **kwargs: SimpleNamespace(soul_md="S"),
    )
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", AsyncMock())
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active"}),
    )
    monkeypatch.setattr("core.services.owner_facts._lookup_email_address", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.owner_facts._lookup_provisioning", AsyncMock(return_value=[]))
    staged = {}
    monkeypatch.setattr(
        "core.services.hermes_profiles.write_facts_to_profiles",
        lambda owner_id, agent_ids, facts_json: staged.update(owner_id=owner_id, agent_ids=agent_ids),
    )

    await hermes_provisioning.write_profile_for_agent(
        "owner_1", {"id": "agent_new"}, container_row={"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"}}
    )

    assert staged == {"owner_id": "owner_1", "agent_ids": ["agent_new"]}


@pytest.mark.asyncio
async def test_write_profile_for_agent_propagates_a_hermes_profile_error_from_resolve_profile_inputs(monkeypatch):
    """A missing BYO key (or any other resolve_profile_inputs hard-fail, raised as
    HermesProfileError -- M6-R10/M6-R11/M6-R13) must never ship a profile with no LLM
    credential at all."""

    async def _raise(*_args, **_kwargs):
        raise hermes_provisioning.HermesProfileError("hermes profile for owner=owner_1: byo api key read failed")

    monkeypatch.setattr(hermes_provisioning, "resolve_profile_inputs", _raise)
    write_mock = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.write_agent_profile", write_mock)
    upload_mock = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", upload_mock)

    with pytest.raises(hermes_provisioning.HermesProfileError, match="byo"):
        await hermes_provisioning.write_profile_for_agent("owner_1", {"id": "agent_new"}, container_row={})

    write_mock.assert_not_called()
    upload_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_profile_for_agent_raises_when_paperclip_key_mint_fails(monkeypatch):
    """A failed/empty Paperclip key mint must never ship a profile whose paperclip skill
    silently 401s on every call."""
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="bedrock_claude",
        byo_provider=None,
        byo_api_key=None,
        aip_arns={},
        integrations={},
        env_vars={},
    )
    monkeypatch.setattr(hermes_provisioning, "resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    write_mock = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.write_agent_profile", write_mock)
    upload_mock = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", upload_mock)

    with pytest.raises(RuntimeError, match="paperclip"):
        await hermes_provisioning.write_profile_for_agent("owner_1", {"id": "agent_new"}, container_row={})

    write_mock.assert_not_called()
    upload_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_profile_for_agent_soul_md_override_replaces_the_derived_soul(monkeypatch):
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="bedrock_claude",
        byo_provider=None,
        byo_api_key=None,
        aip_arns={"sonnet": "arn:aip:sonnet"},
        integrations={},
        env_vars={},
    )
    monkeypatch.setattr(hermes_provisioning, "resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pk-123"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    monkeypatch.setattr(
        "core.services.hermes_profiles.write_agent_profile",
        lambda owner_id, agent, **kwargs: SimpleNamespace(soul_md="DERIVED"),
    )
    upload_mock = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", upload_mock)
    monkeypatch.setattr(hermes_provisioning, "_stage_owner_facts", AsyncMock())

    await hermes_provisioning.write_profile_for_agent(
        "owner_1", {"id": "agent_new"}, container_row={}, soul_md_override="CUSTOM SOUL"
    )

    assert upload_mock.await_args.args[2].soul_md == "CUSTOM SOUL"


# --- resolve_profile_inputs (fix round 3, M6-R10/M6-R11/M6-R13) ----------------------------------


def _patch_resolve_profile_inputs_collaborators(
    monkeypatch,
    *,
    billing_row=None,
    read_byo_key=None,
    integrations=None,
    env_vars=None,
):
    """Patches the underlying collaborators resolve_profile_inputs composes
    (NOT resolve_provider_choice itself) so provider_choice_from_row's real
    trial_local/byo_provider logic actually runs -- that logic is exactly
    what this function's own tests need to exercise."""

    async def _fake_get_billing_row(owner_id):
        return billing_row

    async def _fake_build_connected_integration_servers(owner_id):
        return integrations or {}

    async def _fake_build_provisioned_env(owner_id):
        return env_vars or {}

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", _fake_get_billing_row)
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers",
        _fake_build_connected_integration_servers,
    )
    monkeypatch.setattr("core.services.provisioning_landing.build_provisioned_env", _fake_build_provisioned_env)
    if read_byo_key is not None:
        monkeypatch.setattr("core.services.key_service.read_byo_key", read_byo_key)


@pytest.mark.asyncio
async def test_resolve_profile_inputs_reads_aip_arns_from_the_container_row(monkeypatch):
    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row={"provider_choice": "bedrock_claude"})

    inputs = await hermes_provisioning.resolve_profile_inputs(
        "org_a", container_row={"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet", "opus": "arn:aip:opus"}}
    )
    assert inputs.aip_arns == {"sonnet": "arn:aip:sonnet", "opus": "arn:aip:opus"}


@pytest.mark.asyncio
async def test_resolve_profile_inputs_defaults_aip_arns_to_empty_when_row_has_none(monkeypatch):
    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row={"provider_choice": "bedrock_claude"})

    inputs = await hermes_provisioning.resolve_profile_inputs("org_a", container_row={})
    assert inputs.aip_arns == {}


@pytest.mark.asyncio
async def test_resolve_profile_inputs_returns_bedrock_trial_choice_for_a_trial_local_owner(monkeypatch):
    """M6-R10, divergence (1): catalog_service used to skip
    provider_choice_from_row's trial_local branch entirely -- a card-less
    trial owner's marketplace deploy would get the full paid model ladder.
    resolve_profile_inputs is the one place both callers now go through, so
    this proves the real provider_choice_from_row logic fires for real."""
    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row={"subscription_status": "trial_local"})

    inputs = await hermes_provisioning.resolve_profile_inputs(
        "org_trial", container_row={"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"}}
    )

    from core.containers.config import BEDROCK_TRIAL_CHOICE

    assert inputs.provider_choice == BEDROCK_TRIAL_CHOICE
    assert inputs.byo_provider is None
    assert inputs.aip_arns == {"sonnet": "arn:aip:sonnet"}


@pytest.mark.asyncio
async def test_resolve_profile_inputs_reads_the_anthropic_key_for_an_unset_byo_provider(monkeypatch):
    """M6-R10, divergence (2): catalog_service used to short-circuit to None
    for an unset byo_provider instead of reading the "anthropic" default
    provisioning has always read."""

    async def _fake_read_byo_key(owner_id, provider):
        assert provider == "anthropic"
        return "sk-real-byo-key"

    _patch_resolve_profile_inputs_collaborators(
        monkeypatch,
        billing_row={"provider_choice": "byo_key", "byo_provider": None},
        read_byo_key=_fake_read_byo_key,
    )

    inputs = await hermes_provisioning.resolve_profile_inputs("org_a", container_row={})
    assert inputs.byo_api_key == "sk-real-byo-key"


@pytest.mark.asyncio
async def test_resolve_profile_inputs_raises_when_byo_key_missing(monkeypatch):
    """M6-R11: a byo_key owner with no readable key must never ship a
    profile with no LLM credential at all."""
    _patch_resolve_profile_inputs_collaborators(
        monkeypatch,
        billing_row={"provider_choice": "byo_key", "byo_provider": "anthropic"},
        read_byo_key=_noop_async,  # returns None
    )

    with pytest.raises(hermes_provisioning.HermesProfileError, match="org_a"):
        await hermes_provisioning.resolve_profile_inputs("org_a", container_row={})


@pytest.mark.asyncio
async def test_resolve_profile_inputs_defaults_to_bedrock_claude_when_billing_row_missing(monkeypatch):
    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row=None)

    inputs = await hermes_provisioning.resolve_profile_inputs("org_a", container_row={})
    assert inputs.provider_choice == "bedrock_claude"
    assert inputs.byo_api_key is None


def test_classify_provision_error_treats_hermes_profile_error_like_runtime_error():
    """M6-R13: subclassing RuntimeError (not a bare Exception) keeps the
    provisioning runner's classification unchanged -- none of
    classify_provision_error's branches single out RuntimeError by exact
    type, so a HermesProfileError falls into the same "everything else"
    default as a plain RuntimeError."""
    from core.repositories.container_repo import classify_provision_error

    _, hermes_retryable = classify_provision_error(hermes_provisioning.HermesProfileError("boom"))
    _, plain_retryable = classify_provision_error(RuntimeError("boom"))
    assert hermes_retryable == plain_retryable


# --- M6-R43: a rolled-back (deleted / DELETING) runtime is never reused ------------------------


@pytest.mark.asyncio
async def test_provision_creates_fresh_when_the_row_arn_names_a_deleted_runtime(monkeypatch):
    """M6-R43 (dev re-migration after --rollback): the row still carries the deleted runtime's
    ARN (stamped by the attempt that found it DELETING by name). Describe says it is gone, so
    the provisioner waits out the delete and CREATES -- the row write replaces the stale ARN."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:rolled-back", "status": "provisioning"}),
    )
    monkeypatch.setattr(
        hermes_provisioning,
        "_load_role_and_aips",
        _noop_async_returns(("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "a"})),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _explode)  # ARN row: profiles already in S3
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", _explode)
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)

    def _gone(arn):
        raise hermes_provisioning._RuntimeGone(arn)

    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", _gone)
    waited = {}
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_gone", lambda arn: waited.setdefault("arn", arn))
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:fresh")
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: None)

    writes = []

    async def _spy_update(owner_id, claim_id, fields, **kwargs):
        writes.append(dict(fields))

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_container_leased", _spy_update)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a", claim_id="claim-1")
    assert arn == "arn:fresh"
    assert waited["arn"] == "arn:rolled-back"
    assert writes[0] == {"agentcore_runtime_arn": "arn:fresh"}  # stale ARN replaced before the READY poll
    assert writes[-1]["status"] == "running" and writes[-1]["agentcore_runtime_arn"] == "arn:fresh"


def test_runtime_image_uri_raises_gone_on_not_found_and_on_deleting(monkeypatch):
    import boto3

    class _NotFound(Exception):
        pass

    class _Client:
        exceptions = SimpleNamespace(ResourceNotFoundException=_NotFound)

        def __init__(self, status):
            self._status = status

        def get_agent_runtime(self, agentRuntimeId):
            if self._status is None:
                raise _NotFound()
            return {"status": self._status, "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "img"}}}

    for status in (None, "DELETING"):
        monkeypatch.setattr(boto3, "client", lambda *a, _s=status, **k: _Client(_s))
        with pytest.raises(hermes_provisioning._RuntimeGone):
            hermes_provisioning._runtime_image_uri("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Client("READY"))
    assert hermes_provisioning._runtime_image_uri("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1") == "img"


# --- M6-R44: CreateAgentRuntime retries only the IAM trust-propagation rejection ----------------


class _ValidationException(Exception):
    pass


class _CreateClient:
    exceptions = SimpleNamespace(ValidationException=_ValidationException)

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def create_agent_runtime(self, **request):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"agentRuntimeArn": "arn:created"}


def test_create_runtime_retries_the_role_propagation_rejection_then_succeeds(monkeypatch):
    import boto3

    rejection = _ValidationException(
        "An error occurred (ValidationException) when calling the CreateAgentRuntime operation: Role validation "
        "failed for 'arn:aws:iam::1:role/r'. Please verify that the role exists and its trust policy allows "
        "assumption by this service"
    )
    client = _CreateClient([rejection, rejection])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    assert hermes_provisioning._create_runtime({"agentRuntimeName": "x"}) == "arn:created"
    assert client.calls == 3 and slept == [hermes_provisioning._ROLE_PROPAGATION_INTERVAL_S] * 2


def test_create_runtime_raises_other_validation_errors_at_once(monkeypatch):
    import boto3

    client = _CreateClient([_ValidationException("agentRuntimeName does not match the pattern")])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr("time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("must not sleep")))

    with pytest.raises(_ValidationException):
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})
    assert client.calls == 1


def test_create_runtime_gives_up_after_the_bound(monkeypatch):
    import boto3

    rejection = _ValidationException("Role validation failed for 'arn:aws:iam::1:role/r'.")
    client = _CreateClient([rejection] * hermes_provisioning._ROLE_PROPAGATION_ATTEMPTS)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr("time.sleep", lambda s: None)

    with pytest.raises(_ValidationException):
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})
    assert client.calls == hermes_provisioning._ROLE_PROPAGATION_ATTEMPTS
