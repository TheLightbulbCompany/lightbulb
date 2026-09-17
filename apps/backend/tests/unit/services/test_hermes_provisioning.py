import datetime
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
async def test_provision_skips_write_profiles_for_an_arn_row_in_error(monkeypatch):
    """N4/M4-R42: _write_profiles runs BEFORE CreateAgentRuntime, so an ARN row already has its
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _explode)  # must NOT be called
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _explode)
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
    monkeypatch.setattr(hermes_provisioning, "_write_profiles", _noop_async)
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
