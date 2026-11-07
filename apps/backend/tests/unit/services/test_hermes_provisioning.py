import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import hermes_provisioning


@pytest.fixture(autouse=True)
def _no_text_line(monkeypatch):
    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def _no_billing_row(monkeypatch):
    """Provisioning resolves the provider from billing on every path; default to a ChatGPT row.
    Tests that care patch billing_repo themselves, which wins."""
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "chatgpt_oauth"}),
    )


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


@pytest.fixture(autouse=True)
def _no_browser_profile(monkeypatch):
    """Every provision test in this module is about the RUNTIME, not the browser. Stub the
    profile resolver so none of them reaches a live AgentCore control-plane client; the
    threading itself is asserted in
    ``test_provision_threads_the_browser_profile_into_the_runtime_request``."""

    async def _none(owner_id):
        return ""

    monkeypatch.setattr(hermes_provisioning.browser_sessions, "ensure_browser_profile", _none)


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
    assert req["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 5 * 60
    assert req["tags"]["ManagedBy"] == "lightbulb-backend"


def test_the_runtime_env_carries_identifiers_only_and_no_secret_value(monkeypatch):
    """M4-R4/R5/R19: no plaintext bearer/hook/gateway token, ever — only the ONE boot-secret
    NAME plus non-secret identifiers."""
    env = hermes_provisioning.build_hermes_runtime_env(
        owner_id="org_a",
        api_base="https://api-dev.thelightbulb.company",
        bucket="lightbulb-dev-openclaw-replicas",
        boot_secret_id="lightbulb/dev/hermes/org_a/boot",
    )
    assert env["S3_PREFIX"] == "org_a/hermes"  # owner FIRST -- the IAM grant is <owner>/*
    assert env["S3_BUCKET"] == "lightbulb-dev-openclaw-replicas"
    assert env["HERMES_HOME"] == "/opt/data"
    assert env["HERMES_HEADLESS"] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"
    # Bedrock is gone: nothing points the runtime at a Bedrock endpoint or model any more.
    for key in (
        "AWS_BEDROCK_FORCE_HTTP1",
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_MODEL",
    ):
        assert key not in env
    assert env["LIGHTBULB_SECRET_ID"] == "lightbulb/dev/hermes/org_a/boot"
    for key, value in env.items():
        assert not value.startswith(("sk-", "pcp_")), key
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert "OPENCLAW_HOOK_TOKEN" not in env
    assert "GATEWAY_TOKEN" not in env


def test_managed_gate_url_is_a_runtime_identifier_without_a_bearer():
    env = hermes_provisioning.build_hermes_runtime_env(
        owner_id="org_a",
        api_base="https://api-dev.thelightbulb.company",
        bucket="bucket",
        boot_secret_id="lightbulb/dev/hermes/org_a/boot",
        managed_gate_url="https://openrouter-gate.example/v1",
        managed_model_id="candidate/model",
    )
    assert env["LIGHTBULB_MANAGED_GATE_URL"] == "https://openrouter-gate.example/v1"
    assert env["LIGHTBULB_MANAGED_MODEL_ID"] == "candidate/model"
    assert "LIGHTBULB_GATE_BEARER" not in env


def test_every_provider_carries_the_managed_gate_once_it_is_configured(monkeypatch):
    """Task 8a: the env block is a boot-time input, so it carries the managed route for EVERY
    provider -- a switch into openrouter_managed is then a profile rewrite, not a re-spec."""
    monkeypatch.setattr(hermes_provisioning.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    monkeypatch.setattr("core.billing.openrouter_gate_url.gate_url", lambda: "https://openrouter-gate.example/v1")
    envs = [
        hermes_provisioning.build_runtime_request_for_owner(
            "org_a", "arn:aws:iam::123456789012:role/owner", provider_choice=choice
        )["environmentVariables"]
        for choice in ("chatgpt_oauth", "byo_key", "openrouter_managed")
    ]
    assert all(env == envs[0] for env in envs)
    assert envs[0]["LIGHTBULB_MANAGED_GATE_URL"] == "https://openrouter-gate.example/v1"
    assert envs[0]["LIGHTBULB_MANAGED_MODEL_ID"] == "candidate/model"


def test_no_managed_gate_is_discovered_while_managed_inference_is_unconfigured(monkeypatch):
    monkeypatch.setattr(hermes_provisioning.settings, "OPENROUTER_MANAGED_MODEL_ID", "")
    monkeypatch.setattr(
        "core.billing.openrouter_gate_url.gate_url",
        lambda: (_ for _ in ()).throw(AssertionError("must not read OpenRouter SSM")),
    )
    request = hermes_provisioning.build_runtime_request_for_owner(
        "org_a", "arn:aws:iam::123456789012:role/owner", provider_choice="chatgpt_oauth"
    )
    assert "LIGHTBULB_MANAGED_GATE_URL" not in request["environmentVariables"]


def test_the_runtime_env_never_carries_the_nous_guest_launch_gate(monkeypatch):
    """M4-R38: Hermes's vision auxiliary task falls through to its auto chain (OpenRouter ->
    Nous -> DeepInfra) even with fallback_chain: [], and is unreachable in our runtime ONLY
    because no OPENROUTER_API_KEY/DEEPINFRA_API_KEY exists AND Nous's free tier is gated on
    HERMES_GUEST_ONBOARDING=1 (hermes_cli/anon_auth.py::guest_enabled). None of these three keys
    must ever appear here -- this is the egress fence for that aux task."""
    env = hermes_provisioning.build_hermes_runtime_env(
        owner_id="org_a",
        api_base="https://api-dev.thelightbulb.company",
        bucket="lightbulb-dev-openclaw-replicas",
        boot_secret_id="lightbulb/dev/hermes/org_a/boot",
    )
    assert "HERMES_GUEST_ONBOARDING" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert "DEEPINFRA_API_KEY" not in env


def _env(**extra):
    return hermes_provisioning.build_hermes_runtime_env(
        owner_id="org_a",
        api_base="https://api-dev.thelightbulb.company",
        bucket="lightbulb-dev-openclaw-replicas",
        boot_secret_id="lightbulb/dev/hermes/org_a/boot",
        **extra,
    )


def test_runtime_env_enables_the_browser_only_with_a_profile(monkeypatch):
    monkeypatch.setattr(hermes_provisioning.settings, "AWS_REGION", "us-east-1", raising=False)
    env = _env(browser_profile_id="org_x-ab12cd34ef")
    assert env["AGENT_BROWSER_PROVIDER"] == "agentcore"
    assert env["AGENTCORE_REGION"] == "us-east-1"
    assert env["AGENTCORE_BROWSER_ID"] == "aws.browser.v1"
    assert env["AGENTCORE_PROFILE_ID"] == "org_x-ab12cd34ef"
    assert env["AGENTCORE_SESSION_TIMEOUT"] == "900"
    # The gate: Hermes advertises browser_* only when this names an existing file
    # (a bare os.path.isfile, never a Chromium check). The image bakes a zero-byte
    # sentinel there.
    assert env["AGENT_BROWSER_EXECUTABLE_PATH"] == "/opt/lightbulb/no-local-browser"


def test_runtime_env_omits_every_browser_var_without_a_profile():
    """The design's "disabled" contract: the gate variable travels WITH the profile id, one
    `if`, not two. No profile => no gate variable => Hermes does not advertise browser_* at
    all -- silence, rather than a tool that fails on every call."""
    assert not [k for k in _env() if k.startswith(("AGENTCORE_", "AGENT_BROWSER_"))]


def test_runtime_env_carries_the_workspace_memory_id_only_when_configured():
    """The image enables its AgentCore memory provider only when this is set, so an unset id
    must mean an absent key -- never an empty string the provider would try to call."""
    assert _env(memory_id="lightbulb_dev_workspace_memory-abc")["AGENTCORE_MEMORY_ID"] == (
        "lightbulb_dev_workspace_memory-abc"
    )
    assert "AGENTCORE_MEMORY_ID" not in _env()
    assert "AGENTCORE_MEMORY_ID" not in _env(memory_id="")


def test_network_workers_is_in_the_env_only_when_on_and_turning_it_off_is_drift():
    assert _env(network_workers=True)["LIGHTBULB_NETWORK_WORKERS"] == "1"
    assert "LIGHTBULB_NETWORK_WORKERS" not in _env()
    assert hermes_provisioning._env_drifted(_env(network_workers=True), _env())
    assert hermes_provisioning._env_drifted(_env(), _env(network_workers=True))


def test_the_turn_cap_is_a_per_owner_env_key_and_unset_means_the_image_default():
    assert _env(max_concurrent_turns=6)["LIGHTBULB_MAX_CONCURRENT_TURNS"] == "6"
    assert "LIGHTBULB_MAX_CONCURRENT_TURNS" not in _env()
    assert "LIGHTBULB_MAX_CONCURRENT_TURNS" not in _env(max_concurrent_turns=None)
    # Setting it and clearing it are both drift, so a roll applies either.
    assert hermes_provisioning._env_drifted(_env(), _env(max_concurrent_turns=6))
    assert hermes_provisioning._env_drifted(_env(max_concurrent_turns=6), _env())
    assert not hermes_provisioning._env_drifted(_env(max_concurrent_turns=6), _env(max_concurrent_turns=6))


# --- _runtime_status: one status read, never a poll ------------------------------------------
# Optimistic create: readiness is discovered at first invoke (retry-at-use). The ONLY read
# here exists so a TERMINAL runtime is swept (deleted + ARN cleared) instead of reused
# forever. A non-terminal status — CREATING, UPDATING, READY — is returned as-is.


class _FakeAgentCoreClient:
    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.calls = 0

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803 -- matches boto3's own param casing
        self.calls += 1
        return {"status": self._statuses.pop(0)}


def test_runtime_status_reads_once_and_returns_creating_verbatim(monkeypatch):
    import boto3

    client = _FakeAgentCoreClient(["CREATING"])
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)

    assert hermes_provisioning._runtime_status("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1") == "CREATING"
    assert client.calls == 1


def test_runtime_status_passes_terminal_statuses_through_unraised(monkeypatch):
    """The single read classifies nothing: CREATE_FAILED surfaces as a string for the
    caller (provision/roll) to sweep. Nothing is consumed and nothing retries here."""
    import boto3

    client = _FakeAgentCoreClient(["CREATE_FAILED"])
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)

    assert hermes_provisioning._runtime_status("arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1") == "CREATE_FAILED"
    assert client.calls == 1


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
    expected_params["metadataConfiguration"] = {"requireMMDSV2": True}
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

    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "chatgpt_oauth"}),
    )
    monkeypatch.setattr(
        bedrock_aip_mod,
        "ensure_owner_bedrock_role",
        AsyncMock(return_value="arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(bedrock_gate_tokens_mod, "mint_gate_token", AsyncMock(return_value="bearer-plaintext"))
    monkeypatch.setattr(hermes_provisioning, "ensure_agentcore_trust", AsyncMock(return_value=True))
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(existing_row))
    # A minted hook_token triggers the adapter resync; no company by default makes it a no-op.
    import core.services.paperclip_agent_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value=None))

    # The row as DynamoDB holds it: ensure_owner_tokens is `SET x = if_not_exists(x, :x)`.
    state = dict(existing_row or {})

    async def _ensure_owner_tokens(owner_id, *, hook_token, gateway_token):
        import asyncio

        await asyncio.sleep(0)  # let a concurrent prepare interleave here
        state["hook_token"] = state.get("hook_token") or hook_token
        state["gateway_token"] = state.get("gateway_token") or gateway_token
        state["id"] = state.get("id") or "row-new"
        return dict(state)

    monkeypatch.setattr(hermes_provisioning.container_repo, "ensure_owner_tokens", _ensure_owner_tokens)
    return state


@pytest.mark.asyncio
async def test_two_concurrent_prepares_write_the_identical_row_secret_and_bearer(monkeypatch):
    """No lease: two provision runs for one owner must converge on one credential set."""
    import asyncio

    import core.billing.bedrock_gate_tokens as bedrock_gate_tokens_mod

    state = _patch_owner_aws(monkeypatch, existing_row={"owner_id": "org_a", "id": "row-1"})
    monkeypatch.setattr(
        bedrock_gate_tokens_mod,
        "mint_gate_token",
        AsyncMock(
            side_effect=lambda owner_id, *, row_id, generation: bedrock_gate_tokens_mod.derive_gate_bearer(
                owner_id, row_id, generation
            )
        ),
    )
    secrets_written = []

    async def _write_boot_secret(owner_id, **kwargs):
        secrets_written.append(kwargs)
        return f"lightbulb/dev/hermes/{owner_id}/boot"

    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _write_boot_secret)

    await asyncio.gather(
        hermes_provisioning.prepare_hermes_workspace("org_a"),
        hermes_provisioning.prepare_hermes_workspace("org_a"),
    )

    assert len(secrets_written) == 2 and secrets_written[0] == secrets_written[1]
    assert secrets_written[0]["bedrock_bearer"] == bedrock_gate_tokens_mod.derive_gate_bearer("org_a", "row-1", 0)
    assert secrets_written[0]["hook_token"] == state["hook_token"]


@pytest.mark.asyncio
async def test_prepare_derives_the_bearer_at_the_rows_generation(monkeypatch):
    import core.billing.bedrock_gate_tokens as bedrock_gate_tokens_mod

    _patch_owner_aws(monkeypatch, existing_row={"owner_id": "org_a", "gate_bearer_generation": 2})
    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _noop_async_returns("boot"))
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", AsyncMock())

    await hermes_provisioning.prepare_hermes_workspace("org_a")

    assert bedrock_gate_tokens_mod.mint_gate_token.await_args.kwargs["generation"] == 2
    assert bedrock_gate_tokens_mod.mint_gate_token.await_args.kwargs["row_id"] == "row-new"


@pytest.mark.asyncio
async def test_teardown_then_reprovision_yields_a_different_bearer(monkeypatch):
    """Teardown deletes the containers row; the next provision's row has a new id, so a bearer
    copied out of the old runtime is not re-issued. A generation bump rotates too."""
    import core.billing.bedrock_gate_tokens as bedrock_gate_tokens_mod

    bearers = []

    async def _write_boot_secret(owner_id, **kwargs):
        bearers.append(kwargs["bedrock_bearer"])
        return "boot"

    for existing in ({"id": "row-old"}, {"id": "row-old", "gate_bearer_generation": 1}, {"id": "row-after-teardown"}):
        _patch_owner_aws(monkeypatch, existing_row={"owner_id": "org_a", **existing})
        monkeypatch.setattr(
            bedrock_gate_tokens_mod,
            "mint_gate_token",
            AsyncMock(
                side_effect=lambda owner_id, *, row_id, generation: bedrock_gate_tokens_mod.derive_gate_bearer(
                    owner_id, row_id, generation
                )
            ),
        )
        monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _write_boot_secret)
        monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", AsyncMock())
        await hermes_provisioning.prepare_hermes_workspace("org_a")

    assert len(set(bearers)) == 3


@pytest.mark.asyncio
async def test_prepare_hermes_workspace_mints_tokens_and_writes_one_json_boot_secret(monkeypatch):
    row = _patch_owner_aws(monkeypatch, existing_row=None)

    written = {}

    async def _fake_write_boot_secret(owner_id, *, hook_token, gateway_token, bedrock_bearer):
        written.update(hookToken=hook_token, gatewayToken=gateway_token, bedrockBearer=bedrock_bearer)
        return True

    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _fake_write_boot_secret)

    assert await hermes_provisioning.prepare_hermes_workspace("org_a") is True

    # No vaultToken: the execution role can read this secret (and the agent's shell can read the
    # role over the metadata service); the vault token rides the backend's invokes instead.
    assert set(written) == {"hookToken", "gatewayToken", "bedrockBearer"}
    assert written["bedrockBearer"] == "bearer-plaintext"
    # The tokens on the row are the ones in the secret (ensure_owner_tokens' read-back).
    assert row["hook_token"] == written["hookToken"]
    assert row["gateway_token"] == written["gatewayToken"]


@pytest.mark.asyncio
async def test_fresh_managed_workspace_ensures_owner_key_before_boot_secret(monkeypatch):
    _patch_owner_aws(monkeypatch, existing_row=None)
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "openrouter_managed"}),
    )
    order = []

    async def ensure_key(owner_id):
        order.append(("key", owner_id))
        return "hash-only"

    async def write_secret(owner_id, **kwargs):
        order.append(("boot", owner_id))
        return "lightbulb/dev/hermes/org_a/boot"

    monkeypatch.setattr("core.services.openrouter_keys.ensure_owner_key_for_settings", ensure_key)
    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", write_secret)
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", AsyncMock())

    await hermes_provisioning.prepare_hermes_workspace("org_a")
    assert order == [("key", "org_a"), ("boot", "org_a")]


@pytest.mark.asyncio
async def test_prepare_hermes_workspace_reuses_existing_tokens_on_a_retry(monkeypatch):
    """A retry of a partially-failed provision must not orphan an already-issued token."""
    _patch_owner_aws(monkeypatch, existing_row={"hook_token": "existing-hook", "gateway_token": "existing-gw"})
    written = {}

    async def _fake_write_boot_secret(owner_id, **kwargs):
        written.update(kwargs)
        return "lightbulb/dev/hermes/org_a/boot"

    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _fake_write_boot_secret)

    await hermes_provisioning.prepare_hermes_workspace("org_a")
    assert written["hook_token"] == "existing-hook"
    assert written["gateway_token"] == "existing-gw"


@pytest.mark.asyncio
async def test_prepare_converges_a_live_workspace_without_rotating_its_bearer(monkeypatch):
    """reconcile_runtime runs prepare against a LIVE runtime: the bearer is derived, so the
    re-run reproduces the one the runtime holds instead of rotating it."""
    import core.billing.bedrock_gate_tokens as bedrock_gate_tokens_mod

    live = {"owner_id": "org_a", "id": "row-1", "agentcore_runtime_arn": "arn:existing", "status": "running"}
    _patch_owner_aws(monkeypatch, existing_row={**live, "hook_token": "hook", "gateway_token": "gw"})
    monkeypatch.setattr(
        bedrock_gate_tokens_mod,
        "mint_gate_token",
        AsyncMock(
            side_effect=lambda owner_id, *, row_id, generation: bedrock_gate_tokens_mod.derive_gate_bearer(
                owner_id, row_id, generation
            )
        ),
    )
    bearers = []

    async def _write_boot_secret(owner_id, **kwargs):
        bearers.append(kwargs["bedrock_bearer"])
        return False

    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _write_boot_secret)

    await hermes_provisioning.prepare_hermes_workspace("org_a")
    await hermes_provisioning.prepare_hermes_workspace("org_a")

    assert bearers == [bedrock_gate_tokens_mod.derive_gate_bearer("org_a", "row-1", 0)] * 2


@pytest.mark.asyncio
async def test_a_recreated_row_resyncs_the_surviving_companys_adapter_tokens(monkeypatch):
    """Row deleted, Paperclip company survives: prepare mints a NEW hook_token, and every
    hermes_gateway agent's snapshotted apiKey must follow it or every run 401s silently."""
    import core.services.paperclip_agent_lifecycle as lifecycle

    from unittest.mock import MagicMock

    row = _patch_owner_aws(monkeypatch, existing_row=None)
    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _noop_async_returns("boot"))

    async def _get(owner_id, *, consistent=False):
        return dict(row) or None

    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _get)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    admin = MagicMock()
    admin.list_agents = AsyncMock(
        return_value=[
            {"id": "ceo", "adapterType": lifecycle.HERMES_GATEWAY_TYPE},
            {"id": "eng", "adapterType": lifecycle.HERMES_GATEWAY_TYPE},
        ]
    )
    admin.patch_agent = AsyncMock(return_value={})
    monkeypatch.setattr(lifecycle, "resolve_company_id_for_owner", AsyncMock(return_value="surviving-co"))
    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", lambda **_kw: _Client())
    monkeypatch.setattr(lifecycle, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(lifecycle, "get_admin_session_cookie", AsyncMock(return_value="cookie"))

    await hermes_provisioning.prepare_hermes_workspace("org_a")

    assert row["hook_token"]  # freshly minted
    keys = {
        c.kwargs["agent_id"]: c.kwargs["body"]["adapterConfig"]["apiKey"] for c in admin.patch_agent.await_args_list
    }
    assert keys == {"ceo": row["hook_token"], "eng": row["hook_token"]}


@pytest.mark.asyncio
async def test_a_reused_hook_token_does_not_resync(monkeypatch):
    import core.services.paperclip_agent_lifecycle as lifecycle

    _patch_owner_aws(monkeypatch, existing_row={"hook_token": "h", "gateway_token": "g"})
    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _noop_async_returns("boot"))
    monkeypatch.setattr(lifecycle, "sync_agent_adapter_config", _explode)

    await hermes_provisioning.prepare_hermes_workspace("org_a")


@pytest.mark.asyncio
async def test_a_failed_resync_does_not_abort_the_provision_and_emits_a_metric(monkeypatch):
    import core.services.paperclip_agent_lifecycle as lifecycle

    _patch_owner_aws(monkeypatch, existing_row=None)
    monkeypatch.setattr(hermes_provisioning, "write_boot_secret", _noop_async_returns("boot"))

    async def _boom(owner_id):
        raise RuntimeError("paperclip 503")

    monkeypatch.setattr(lifecycle, "sync_agent_adapter_config", _boom)
    metrics = []
    monkeypatch.setattr(hermes_provisioning, "put_metric", lambda name, **kw: metrics.append(name))

    await hermes_provisioning.prepare_hermes_workspace("org_a")

    assert metrics == ["paperclip.adapter_config_sync.error"]


# --- I3/M4-R37: freshness guard against rotating a live ECS owner's bearer -----------------


# --- provision_hermes_runtime ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_writes_the_row_fields_every_existing_seam_reads(monkeypatch):
    updated = {}
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:aws:bedrock-agentcore:...:runtime/r1")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    _no_runtime_found_by_name(monkeypatch)

    async def _fake_update_fields(owner_id, fields, **kwargs):
        updated.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _fake_update_fields)
    arn = await hermes_provisioning.provision_hermes_runtime("org_a")
    assert arn == "arn:aws:bedrock-agentcore:...:runtime/r1"
    assert updated["runtime_host"] == "agentcore"
    assert updated["agentcore_runtime_arn"] == arn
    assert updated["status"] == "running"
    # hook_token/gateway_token are prepare_hermes_workspace's job now -- not repeated here.
    assert "hook_token" not in updated
    assert "gateway_token" not in updated


@pytest.mark.asyncio
async def test_provision_threads_the_browser_profile_into_the_runtime_request(monkeypatch):
    """The profile is resolved BEFORE the request is built -- its id is what turns the six
    AgentCore Browser variables on, and CreateAgentRuntime is the only place they land."""
    created = {}
    monkeypatch.setattr(
        hermes_provisioning.browser_sessions,
        "ensure_browser_profile",
        _noop_async_returns("org_a-ab12cd34ef"),
    )
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: created.update(req) or "arn:rt")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _noop_async)
    _no_runtime_found_by_name(monkeypatch)

    await hermes_provisioning.provision_hermes_runtime("org_a")

    assert created["environmentVariables"]["AGENTCORE_PROFILE_ID"] == "org_a-ab12cd34ef"
    assert created["environmentVariables"]["AGENT_BROWSER_PROVIDER"] == "agentcore"


@pytest.mark.asyncio
async def test_provision_still_builds_a_usable_request_when_the_profile_resolve_fails(monkeypatch):
    """The whole point of ensure_browser_profile's never-raise contract: AWS refusing the
    profile must cost the workspace its browser tools, not its provision. Asserted at the
    COMPOSITION level -- a refactor that made "" throw would still pass the two unit tests
    on the env builder."""
    created = {}
    # The autouse _no_browser_profile fixture already answers "" here; this test is what
    # pins the consequence.
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: created.update(req) or "arn:rt")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _noop_async)
    _no_runtime_found_by_name(monkeypatch)

    await hermes_provisioning.provision_hermes_runtime("org_a")

    env = created["environmentVariables"]
    assert env["ISOL8_OWNER_ID"] == "org_a"  # the request is complete, not half-built
    assert not [k for k in env if k.startswith(("AGENTCORE_", "AGENT_BROWSER_"))]


@pytest.mark.asyncio
async def test_provision_refuses_without_a_configured_image(monkeypatch):
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", "")
    # m7: explicit rather than relying on the image guard firing before this is ever read --
    # brittle if the guard order ever changes.
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _explode)
    with pytest.raises(RuntimeError, match="AGENTCORE_HERMES_IMAGE"):
        await hermes_provisioning.provision_hermes_runtime("org_a")


@pytest.mark.asyncio
async def test_provision_refuses_without_a_configured_bucket(monkeypatch):
    """m3: the same loud-failure guard AGENTCORE_HERMES_IMAGE has -- an empty bucket is
    otherwise silent all the way down (upload_profile no-ops, restore_profile no-ops, every
    agent boots on Hermes's own unpinned auto-chain)."""
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "")
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _explode)
    with pytest.raises(RuntimeError, match="OWNER_STATE_BUCKET"):
        await hermes_provisioning.provision_hermes_runtime("org_a")


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
    assert await hermes_provisioning.provision_hermes_runtime("org_a") == "arn:existing"
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
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)  # must not be called
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", _explode)  # arn already on the row
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: HERMES_IMAGE)  # unchanged -- no update
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)

    polled = {}

    def _fake_status(arn):
        polled["arn"] = arn
        return "READY"

    monkeypatch.setattr(hermes_provisioning, "_runtime_status", _fake_status)

    terminal = {}

    async def _fake_update_fields(owner_id, fields, **kwargs):
        terminal.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _fake_update_fields)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a")
    assert arn == "arn:not-ready-yet"
    assert polled["arn"] == "arn:not-ready-yet"
    assert terminal["status"] == "running"


@pytest.mark.asyncio
async def test_a_create_failed_runtime_is_deleted_arn_cleared_and_the_failure_raised(monkeypatch):
    """F1 (predeploy review, M6-R34): a CREATE_FAILED/UPDATE_FAILED runtime is dead -- AWS
    refuses UpdateAgentRuntime against it, so every retry that found this ARN on the row would
    otherwise call Update on a terminal runtime forever. The terminal branch must delete it and
    clear the row's ARN before re-raising."""
    _configure_hermes_settings(monkeypatch)
    _no_runtime_found_by_name(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:dead")

    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "CREATE_FAILED")
    deleted = {}
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda arn: deleted.setdefault("arn", arn))

    writes: list[dict] = []

    async def _fake_update_fields(owner_id, fields, **kwargs):
        writes.append(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _fake_update_fields)

    with pytest.raises(hermes_provisioning.AgentCoreRuntimeCreateFailed, match="CREATE_FAILED"):
        await hermes_provisioning.provision_hermes_runtime("org_a")

    assert deleted["arn"] == "arn:dead"
    # writes[0] is the pre-poll ARN record (M4-R35/C1); writes[1] is this fix's clear.
    assert writes[0]["agentcore_runtime_arn"] == "arn:dead"
    assert writes[-1]["agentcore_runtime_arn"] is None


@pytest.mark.asyncio
async def test_provision_raises_transient_without_creating_when_old_runtime_never_finishes_deleting(monkeypatch):
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:old", "status": "provisioning"}),
    )
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _explode)

    def _gone(arn):
        raise hermes_provisioning._RuntimeGone(arn)

    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", _gone)
    monkeypatch.setattr(hermes_provisioning, "_wait_runtime_gone", lambda arn: False)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)

    with pytest.raises(RuntimeError, match="still deleting") as ei:
        await hermes_provisioning.provision_hermes_runtime("org_a")
    from core.repositories.container_repo import classify_provision_error

    assert classify_provision_error(ei.value)[1] is True


@pytest.mark.asyncio
async def test_a_create_failed_arn_clear_is_conditioned_on_the_arn_still_matching(monkeypatch):
    """Two concurrent runs can both observe the same dead ARN and both reach this clear. If a
    THIRD run's fresh create already landed a new ARN on the row in between, the loser's clear
    must not blindly wipe out that newer, healthy ARN — it must be conditioned on the row still
    carrying the arn this run determined was CREATE_FAILED."""
    _configure_hermes_settings(monkeypatch)
    _no_runtime_found_by_name(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:dead")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "CREATE_FAILED")
    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", lambda arn: None)

    calls: list[dict] = []

    async def _fake_update_fields(owner_id, fields, **kwargs):
        calls.append({"fields": fields, **kwargs})

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _fake_update_fields)

    with pytest.raises(hermes_provisioning.AgentCoreRuntimeCreateFailed):
        await hermes_provisioning.provision_hermes_runtime("org_a")

    clear_call = calls[-1]
    assert clear_call["fields"]["agentcore_runtime_arn"] is None
    assert (clear_call.get("expected_field"), clear_call.get("expected_value")) == ("agentcore_runtime_arn", "arn:dead")


@pytest.mark.asyncio
async def test_a_create_failed_runtime_delete_is_best_effort(monkeypatch):
    """The runtime may already be gone (a concurrent operator cleanup, a prior attempt's own
    delete) -- a failed DeleteAgentRuntime must not stop the row's ARN from being cleared or
    the original failure from being raised."""
    _configure_hermes_settings(monkeypatch)
    _no_runtime_found_by_name(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:dead")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "CREATE_FAILED")

    def _explode_delete(arn):
        raise RuntimeError("already gone")

    monkeypatch.setattr(hermes_provisioning, "_delete_runtime", _explode_delete)
    writes: list[dict] = []

    async def _fake_update_fields(owner_id, fields, **kwargs):
        writes.append(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _fake_update_fields)

    with pytest.raises(hermes_provisioning.AgentCoreRuntimeCreateFailed):
        await hermes_provisioning.provision_hermes_runtime("org_a")

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
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _explode)  # must NOT be called
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", _explode)  # arn already on the row
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: HERMES_IMAGE)  # unchanged
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")

    terminal = {}

    async def _fake_update_fields(owner_id, fields, **kwargs):
        terminal.update(fields)

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _fake_update_fields)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a")
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
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", _explode)  # must not be called
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:found-by-name")
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: HERMES_IMAGE)
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")

    calls = []

    async def _spy_update(owner_id, fields, **kwargs):
        calls.append((fields, kwargs))

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _spy_update)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a")
    assert arn == "arn:found-by-name"
    # The row never had the ARN, so it must be recorded (non-terminal) before the terminal write.
    assert len(calls) == 2
    assert calls[0][0] == {"agentcore_runtime_arn": "arn:found-by-name"}
    assert calls[1][0]["status"] == "running"


@pytest.mark.asyncio
async def test_provision_updates_the_reused_runtime_when_the_image_drifted(monkeypatch):
    """A reused runtime (found by name, not on the row) whose configured image no longer matches
    settings.AGENTCORE_HERMES_IMAGE gets UpdateAgentRuntime before the READY poll."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
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
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")
    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _noop_async)

    await hermes_provisioning.provision_hermes_runtime("org_a")
    assert updated["arn"] == "arn:found-by-name"
    # N2/M4-R40: the FULL create spec, not just the image -- this is what proves the drift path
    # can never omit roleArn/network/env/lifecycle again.
    request = updated["request"]
    assert request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] == HERMES_IMAGE
    assert request["roleArn"] == "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"
    assert request["networkConfiguration"]["networkMode"] == "VPC"
    assert request["environmentVariables"]
    assert request["protocolConfiguration"] == {"serverProtocol": "HTTP"}
    assert request["lifecycleConfiguration"]["idleRuntimeSessionTimeout"] == 5 * 60


# --- N1/M4-R39: provision_hermes_runtime gets the same freshness guard as prepare -----------


# --- the ARN record and the terminal write ------------------------------------------------


@pytest.mark.asyncio
async def test_create_record_statuscheck_happen_in_that_order(monkeypatch):
    """M4-R35/C1: the ARN must be recorded (non-terminal) BEFORE the single terminal
    status check — a CREATE_FAILED seen there must not lose it. No poll runs anywhere."""
    order = []
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: order.append("create") or "arn:r1")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: order.append("check") or "READY")
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    _no_runtime_found_by_name(monkeypatch)

    async def _spy_update(owner_id, fields, **kwargs):
        order.append(("record", fields))

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _spy_update)

    await hermes_provisioning.provision_hermes_runtime("org_a")
    assert order == [
        "create",
        ("record", {"agentcore_runtime_arn": "arn:r1"}),
        "check",
        (
            "record",
            {
                "runtime_host": "agentcore",
                "agentcore_runtime_arn": "arn:r1",
                "status": "running",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_the_freshly_created_arn_record_is_not_conditioned_on_no_arn_yet(monkeypatch):
    """Fix round 2 (reverts fix round 1's Important #1 follow-up): this write must NOT carry
    expected={"agentcore_runtime_arn": None} -- it is also reached by the M6-R43 recovery path,
    where the row's OWN agentcore_runtime_arn still names a dead/DELETING runtime (only the
    LOCAL `arn` variable is reset to None; the row field is never cleared). Conditioning on
    "no arn yet" there would fail against that still-present value,
    and strand the row on the dead ARN forever -- see
    test_fresh_arn_record_succeeds_against_real_conditional_semantics_when_row_holds_a_dead_arn
    for the regression proof against real conditional semantics. Runtime names are unique per
    owner, so two concurrent creators for the same owner converge on the SAME arn anyway -- the
    condition would guard nothing real."""
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:r1")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _noop_async)
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(None))
    _no_runtime_found_by_name(monkeypatch)

    calls = []

    async def _spy_update(owner_id, fields, **kwargs):
        calls.append((fields, kwargs))

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _spy_update)

    await hermes_provisioning.provision_hermes_runtime("org_a")

    record_call = calls[0]
    assert record_call[0] == {"agentcore_runtime_arn": "arn:r1"}
    assert "expected" not in record_call[1]


# --- Writer failure handling: catch per agent, name the variable, don't half-provision silently


def _patch_write_profiles_collaborators(
    monkeypatch,
    *,
    provider_choice="chatgpt_oauth",
    byo_provider=None,
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

    async def _fake_ensure_paperclip_api_key(*, owner_id, paperclip_agent_id, admin_client):
        return paperclip_api_key

    monkeypatch.setattr("core.services.container_provision.resolve_provider_choice", _fake_resolve_provider_choice)
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers",
        _fake_build_connected_integration_servers,
    )
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
        await hermes_provisioning.write_profiles("org_a")


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

    await hermes_provisioning.write_profiles("org_a")
    assert captured["byo_provider"] == "anthropic"
    assert captured["byo_api_key"] == "sk-real-byo-key"


@pytest.mark.asyncio
async def test_write_profiles_never_reads_a_byo_key_for_a_non_byo_owner(monkeypatch):
    _patch_write_profiles_collaborators(
        monkeypatch,
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        read_byo_key=_explode,
    )

    # Real write_agent_profile runs (not mocked) -- proves the non-BYO path produces a normal
    # profile with byo_api_key=None, with no read ever attempted.
    await hermes_provisioning.write_profiles("org_a")


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

    async def _counting_resolve(owner_id):
        calls.append(owner_id)
        return await real_resolve(owner_id)

    monkeypatch.setattr(hermes_provisioning, "resolve_profile_inputs", _counting_resolve)

    await hermes_provisioning.write_profiles("org_a")

    assert calls == ["org_a"]


@pytest.mark.asyncio
async def test_write_profiles_refuses_an_empty_roster(monkeypatch):
    """An empty roster wrote nothing and returned success, so reconcile reported 'updated' on a
    workspace whose profiles never moved (prod, 2026-09-23). Empty means broken: raise."""
    _patch_write_profiles_collaborators(monkeypatch)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[]))

    with pytest.raises(hermes_provisioning.HermesProfileError, match="roster is empty"):
        await hermes_provisioning.write_profiles("org_a")


@pytest.mark.asyncio
async def test_two_concurrent_write_profiles_calls_produce_byte_identical_config_and_env(monkeypatch):
    """#12: provision_hermes_runtime's whole path -- including write_profiles -- must converge
    for two concurrent provision runs of the same owner, not just the runtime-create step.
    write_agent_profile/build_profile_config/build_profile_env are pure functions of their
    arguments (no clock, no random id), and Task 1 already made every credential that feeds
    those arguments (the boot secret's hook/gateway/bearer tokens) converge at the source --
    so two independent write_profiles calls with the same converged inputs (provider
    choice, paperclip api key, integrations, env vars) must render byte-identical config.yaml
    and .env for every agent, regardless of which of the two concurrent runs' S3 PUT lands
    last."""
    roster = [
        {"id": f"1111111{i}-1111-1111-1111-111111111111", "name": f"Agent{i}", "role": "general"} for i in range(6)
    ]
    _patch_write_profiles_collaborators(monkeypatch, paperclip_api_key="pcp_key_fixed")
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))

    uploaded: dict[int, dict[str, tuple[str, str]]] = {0: {}, 1: {}}

    def _make_capturing_upload(run_index):
        async def _capture(owner_id, agent_id, files):
            uploaded[run_index][agent_id] = (files.config_yaml, files.dotenv)

        return _capture

    for run_index in range(2):
        monkeypatch.setattr("core.services.hermes_profiles.upload_profile", _make_capturing_upload(run_index))
        await hermes_provisioning.write_profiles("org_a")

    assert set(uploaded[0]) == set(uploaded[1]) == {row["id"] for row in roster}
    for agent_id in uploaded[0]:
        assert uploaded[0][agent_id] == uploaded[1][agent_id], f"profile diverged across runs for agent {agent_id}"


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
    _patch_write_profiles_collaborators(monkeypatch, provider_choice="chatgpt_oauth", mock_facts_staging=False)
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

    await hermes_provisioning.write_profiles("org_a")

    assert len(uploaded) == 6
    seen_ids = [agent_ids[0] for _owner, agent_ids, _facts in uploaded]
    assert seen_ids == [row["id"] for row in roster]
    for owner_id, agent_ids, facts_json in uploaded:
        assert owner_id == "org_a"
        assert len(agent_ids) == 1
        assert '"provider_choice": "chatgpt_oauth"' in facts_json


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
        AsyncMock(return_value={"subscription_status": "active", "provider_choice": "chatgpt_oauth"}),
    )
    monkeypatch.setattr("core.services.owner_facts._lookup_email_address", AsyncMock(return_value=None))
    monkeypatch.setattr("core.services.owner_facts._lookup_provisioning", AsyncMock(return_value=[]))

    captured = {}
    monkeypatch.setattr(
        "core.services.hermes_profiles.write_facts_to_profiles",
        lambda owner_id, agent_ids, facts_json: captured.update(facts_json=facts_json),
    )

    await hermes_provisioning.write_profiles("org_a")

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

    await hermes_provisioning.write_profiles("org_a")  # must not raise


# --- write_profile_for_agent (M6-R23) -----------------------------------------------------------
# Moved, unchanged in behaviour, from catalog_service._build_and_upload_agent_profile -- the
# shared per-agent "write this agent's profile" seam write_profiles (above) now delegates to,
# and runtime_rpc's agents.create door for an AgentCore owner calls directly (Q9a row #1).


@pytest.mark.asyncio
async def test_write_profile_for_agent_writes_and_uploads(monkeypatch):
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        byo_api_key=None,
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

    await hermes_provisioning.write_profile_for_agent("owner_1", agent_row)

    assert written["owner_id"] == "owner_1"
    assert written["agent"] is agent_row
    assert written["kwargs"]["provider_choice"] == "chatgpt_oauth"
    assert written["kwargs"]["paperclip_api_key"] == "pk-123"
    upload_mock.assert_awaited_once()
    assert upload_mock.await_args.args[0] == "owner_1"
    assert upload_mock.await_args.args[1] == "agent_new"
    assert upload_mock.await_args.args[2].soul_md == "ORIGINAL"
    stage_facts.assert_awaited_once_with("owner_1", ["agent_new"], inputs, refresh_roster=True)


@pytest.mark.asyncio
async def test_write_profile_for_agent_stages_facts_for_the_new_agent(monkeypatch):
    """Minor (predeploy review, M6-R39 housekeeping): before this fix, only the six-agent
    provision fan-out staged lightbulb-facts.json -- a hired/created agent installed through
    catalog_service's single-agent path or runtime_rpc's agents.create door had no facts file
    at all until the next billing event re-wrote it."""
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        byo_api_key=None,
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
    monkeypatch.setattr("core.services.owner_facts._lookup_owner_timezone", AsyncMock(return_value="UTC"))
    monkeypatch.setattr("core.services.owner_facts._lookup_connected_integrations", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.services.owner_facts._lookup_teammates", AsyncMock(return_value=[]))
    # A lone create/hire also refreshes the OTHER agents' facts (their `teammates` gained it).
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": "agent_old"}, {"id": "agent_new"}]),
    )
    staged = {}
    monkeypatch.setattr(
        "core.services.hermes_profiles.write_facts_to_profiles",
        lambda owner_id, agent_ids, facts_json: staged.update(owner_id=owner_id, agent_ids=agent_ids),
    )

    await hermes_provisioning.write_profile_for_agent("owner_1", {"id": "agent_new"})

    assert staged == {"owner_id": "owner_1", "agent_ids": ["agent_new", "agent_old"]}


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
        await hermes_provisioning.write_profile_for_agent("owner_1", {"id": "agent_new"})

    write_mock.assert_not_called()
    upload_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_profile_for_agent_raises_when_paperclip_key_mint_fails(monkeypatch):
    """A failed/empty Paperclip key mint must never ship a profile whose paperclip skill
    silently 401s on every call."""
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        byo_api_key=None,
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
        await hermes_provisioning.write_profile_for_agent("owner_1", {"id": "agent_new"})

    write_mock.assert_not_called()
    upload_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_profile_for_agent_soul_md_override_replaces_the_derived_soul(monkeypatch):
    from core.services.hermes_provisioning import ProfileInputs

    inputs = ProfileInputs(
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        byo_api_key=None,
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

    await hermes_provisioning.write_profile_for_agent("owner_1", {"id": "agent_new"}, soul_md_override="CUSTOM SOUL")

    assert upload_mock.await_args.args[2].soul_md == "CUSTOM SOUL"


# --- resolve_profile_inputs (fix round 3, M6-R10/M6-R11/M6-R13) ----------------------------------


def _patch_resolve_profile_inputs_collaborators(
    monkeypatch,
    *,
    billing_row=None,
    read_byo_key=None,
    integrations=None,
):
    """Patches the underlying collaborators resolve_profile_inputs composes
    (NOT resolve_provider_choice itself) so provider_choice_from_row's real
    trial_local/byo_provider logic actually runs -- that logic is exactly
    what this function's own tests need to exercise."""

    async def _fake_get_billing_row(owner_id, *, consistent_read=False):
        return billing_row

    async def _fake_build_connected_integration_servers(owner_id):
        return integrations or {}

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", _fake_get_billing_row)
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers",
        _fake_build_connected_integration_servers,
    )
    if read_byo_key is not None:
        monkeypatch.setattr("core.services.key_service.read_byo_key", read_byo_key)


@pytest.mark.asyncio
async def test_resolve_profile_inputs_returns_managed_choice_for_a_trial_local_owner(monkeypatch):
    """M6-R10, divergence (1): catalog_service used to skip
    provider_choice_from_row's trial_local branch entirely -- a card-less
    trial owner's marketplace deploy would get the full paid model ladder.
    resolve_profile_inputs is the one place both callers now go through, so
    this proves the real provider_choice_from_row logic fires for real."""
    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row={"subscription_status": "trial_local"})

    inputs = await hermes_provisioning.resolve_profile_inputs("org_trial")

    assert inputs.provider_choice == "openrouter_managed"
    assert inputs.byo_provider is None


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

    inputs = await hermes_provisioning.resolve_profile_inputs("org_a")
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
        await hermes_provisioning.resolve_profile_inputs("org_a")


@pytest.mark.asyncio
async def test_resolve_profile_inputs_defaults_to_managed_when_billing_row_missing(monkeypatch):
    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row=None)

    inputs = await hermes_provisioning.resolve_profile_inputs("org_a")
    assert inputs.provider_choice == "openrouter_managed"
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
    the provisioner takes the create path WITHOUT waiting out the delete: a still-in-flight
    delete surfaces as a transient name conflict that retries cover. The row write replaces
    the stale ARN."""
    _configure_hermes_settings(monkeypatch)
    monkeypatch.setattr(
        hermes_provisioning.container_repo,
        "get_by_owner_id",
        _row({"agentcore_runtime_arn": "arn:rolled-back", "status": "provisioning"}),
    )
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role",
        _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
    )
    monkeypatch.setattr(hermes_provisioning, "write_profiles", _explode)  # ARN row: profiles already in S3
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", _explode)
    monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)

    def _gone(arn):
        raise hermes_provisioning._RuntimeGone(arn)

    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", _gone)
    waited = []
    monkeypatch.setattr(hermes_provisioning, "_wait_runtime_gone", lambda arn: waited.append(arn) or True)
    monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:fresh")
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")

    writes = []

    async def _spy_update(owner_id, fields, **kwargs):
        writes.append(dict(fields))

    monkeypatch.setattr(hermes_provisioning.container_repo, "update_fields", _spy_update)

    arn = await hermes_provisioning.provision_hermes_runtime("org_a")
    assert arn == "arn:fresh"
    assert waited == ["arn:rolled-back"]  # waited for the old delete BEFORE creating
    assert writes[0] == {"agentcore_runtime_arn": "arn:fresh"}  # stale ARN replaced before the status check
    assert writes[-1]["status"] == "running" and writes[-1]["agentcore_runtime_arn"] == "arn:fresh"


@pytest.mark.asyncio
async def test_fresh_arn_record_succeeds_against_real_conditional_semantics_when_row_holds_a_dead_arn(monkeypatch):
    """Fix round 2 regression: the freshly-created ARN record must NOT be conditioned on
    expected={"agentcore_runtime_arn": None}. That write is also reached by this exact M6-R43
    recovery path -- the row's OWN agentcore_runtime_arn still names the dead/DELETING runtime
    (``_RuntimeGone`` resets only the LOCAL `arn` variable to None; the ROW field is never
    cleared). A bare-equality `expected=None` -- or even the OR-absent form -- would fail against
    that still-present non-null value and strand the row on the dead ARN forever. The sibling
    test above uses a spy that ignores `expected=` and therefore cannot catch this; this one runs
    the REAL container_repo.update_fields against a moto-backed table so the write's actual
    conditional semantics are exercised."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", resource)
        table = resource.Table("test-containers")
        # Seeded exactly like the live M6-R43 scenario: a dead/DELETING runtime's ARN is still
        # stamped on the row.
        table.put_item(
            Item={
                "owner_id": "org_a",
                "status": "provisioning",
                "agentcore_runtime_arn": "arn:rolled-back",
            }
        )

        _configure_hermes_settings(monkeypatch)
        monkeypatch.setattr(
            "core.billing.bedrock_aip.ensure_owner_bedrock_role",
            _noop_async_returns("arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"),
        )
        monkeypatch.setattr(hermes_provisioning, "write_profiles", _explode)
        monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", _explode)
        monkeypatch.setattr(hermes_provisioning, "_update_runtime_image", _explode)

        def _gone(arn):
            raise hermes_provisioning._RuntimeGone(arn)

        monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", _gone)
        monkeypatch.setattr(hermes_provisioning, "_wait_runtime_gone", lambda arn: True)
        monkeypatch.setattr(hermes_provisioning, "_create_runtime", lambda req: "arn:fresh")
        monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: "READY")

        arn = await hermes_provisioning.provision_hermes_runtime("org_a")

        assert arn == "arn:fresh"
        row = table.get_item(Key={"owner_id": "org_a"})["Item"]
        assert row["agentcore_runtime_arn"] == "arn:fresh"
        assert row["status"] == "running"


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


class _ConflictException(Exception):
    pass


class _ResourceNotFoundException(Exception):
    pass


class _CreateClient:
    exceptions = SimpleNamespace(
        ValidationException=_ValidationException,
        ConflictException=_ConflictException,
        ResourceNotFoundException=_ResourceNotFoundException,
    )

    def __init__(self, failures, *, runtime_status="READY"):
        self.failures = list(failures)
        self.calls = 0
        # Backs the liveness check the Conflict handler runs on an adopted ARN
        # (_runtime_image_uri) -- READY by default so every pre-existing test in this group,
        # which never cared about it, keeps adopting a healthy runtime unchanged.
        self.runtime_status = runtime_status

    def create_agent_runtime(self, **request):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"agentRuntimeArn": "arn:created"}

    def get_agent_runtime(self, agentRuntimeId):
        return {
            "status": self.runtime_status,
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "img"}},
        }


def test_create_runtime_adopts_existing_arn_on_conflict(monkeypatch):
    """Two concurrent provision runs both find no ARN on the row and both call
    CreateAgentRuntime for the same (owner-derived, unique-per-account) name.
    The loser must adopt the winner's runtime via the same by-name lookup
    ``provision_hermes_runtime`` already uses for a crash-recovered ARN,
    instead of failing the whole provision (owner ruling: no leases, both
    runs converge)."""
    import boto3

    client = _CreateClient([_ConflictException("already exists")])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:existing")

    arn = hermes_provisioning._create_runtime({"agentRuntimeName": "x"})

    assert arn == "arn:existing"
    assert client.calls == 1


def test_create_runtime_reraises_conflict_when_no_existing_runtime_is_found(monkeypatch):
    """If the by-name lookup can't find what caused the conflict, the original
    error must still surface rather than being swallowed into a None ARN."""
    import boto3

    client = _CreateClient([_ConflictException("already exists")])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: None)

    with pytest.raises(_ConflictException):
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})


def test_create_runtime_refuses_to_adopt_a_deleting_runtime(monkeypatch):
    """Fix round 1 (Important #1): the by-name lookup a Conflict adopts from also lists
    DELETING runtimes (M6-R43), and DELETING is not a terminal status this module ever sees on
    its own row -- so without a liveness check, the race where run A saw CREATE_FAILED, deleted
    runtime X and cleared the row's ARN, while run B's CreateAgentRuntime hits Conflict because
    the name is STILL held by the deleting X, ends with B adopting X and recording a runtime
    that is about to not exist as a SUCCESS. The ORIGINAL ConflictException must surface instead
    (classified transient, retried) rather than the adopt succeeding."""
    import boto3

    client = _CreateClient([_ConflictException("already exists")], runtime_status="DELETING")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:deleting")
    monkeypatch.setattr(hermes_provisioning, "_DELETE_WAIT_TIMEOUT_S", 0.0)  # delete never finishes

    with pytest.raises(_ConflictException):
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})


def test_create_runtime_refuses_to_adopt_a_runtime_that_is_already_gone(monkeypatch):
    """Same fix, the other _RuntimeGone trigger: the adopted ARN 404s outright (deleted between
    the list and the describe)."""
    import boto3

    client = _CreateClient([_ConflictException("already exists")])

    def _gone(agentRuntimeId):
        raise _ResourceNotFoundException("gone")

    client.get_agent_runtime = _gone
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:gone")

    # Gone between the list and the describe: the name is free again, so the retry creates.
    assert hermes_provisioning._create_runtime({"agentRuntimeName": "x"}) == "arn:created"
    assert client.calls == 2


def test_create_runtime_waits_out_a_deleting_runtime_then_creates_once(monkeypatch):
    """Teardown immediately followed by a provision: the name is held by a DELETING runtime."""
    import boto3

    client = _CreateClient([_ConflictException("already exists")], runtime_status="DELETING")
    polls = {"n": 0}
    real_get = client.get_agent_runtime

    def _get(agentRuntimeId):
        polls["n"] += 1
        if polls["n"] > 3:  # 1 liveness check + 2 DELETING polls, then gone
            raise _ResourceNotFoundException("gone")
        return real_get(agentRuntimeId)

    client.get_agent_runtime = _get
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:old/rt-old")
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    assert hermes_provisioning._create_runtime({"agentRuntimeName": "x"}) == "arn:created"
    assert client.calls == 2 and len(slept) == 2


def test_create_runtime_gives_up_after_one_conflict_retry(monkeypatch):
    import boto3

    client = _CreateClient([_ConflictException("a"), _ConflictException("b")])
    states = iter(["DELETING", "gone", "DELETING", "gone"])

    def _get(agentRuntimeId):
        if next(states) == "gone":
            raise _ResourceNotFoundException("gone")
        return {"status": "DELETING"}

    client.get_agent_runtime = _get
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:old/rt-old")

    with pytest.raises(_ConflictException):
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})
    assert client.calls == 2


def test_conflict_on_the_last_propagation_attempt_still_gets_one_post_wait_create(monkeypatch):
    import boto3

    n = hermes_provisioning._ROLE_PROPAGATION_ATTEMPTS
    rejection = _ValidationException("Role validation failed")
    client = _CreateClient([rejection] * (n - 1) + [_ConflictException("already exists")], runtime_status="DELETING")
    states = iter(["DELETING", "gone"])

    def _get(agentRuntimeId):
        if next(states) == "gone":
            raise _ResourceNotFoundException("gone")
        return {"status": "DELETING"}

    client.get_agent_runtime = _get
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:old/rt-old")

    assert hermes_provisioning._create_runtime({"agentRuntimeName": "x"}) == "arn:created"
    assert client.calls == n + 1


def _deleting_then_gone_client(failures):
    client = _CreateClient(failures, runtime_status="DELETING")
    states = iter(["DELETING", "gone"])

    def _get(agentRuntimeId):
        if next(states) == "gone":
            raise _ResourceNotFoundException("gone")
        return {"status": "DELETING"}

    client.get_agent_runtime = _get
    return client


def test_post_wait_create_retries_role_validation_then_succeeds(monkeypatch):
    import boto3

    role = _ValidationException("Role validation failed")
    client = _deleting_then_gone_client([_ConflictException("exists"), role, role])
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:old/rt-old")

    assert hermes_provisioning._create_runtime({"agentRuntimeName": "x"}) == "arn:created"
    assert client.calls == 4


def test_post_wait_role_validation_exhaustion_is_retryable(monkeypatch):
    import boto3

    from core.repositories.container_repo import classify_provision_error

    n = hermes_provisioning._ROLE_PROPAGATION_ATTEMPTS
    role = _ValidationException("Role validation failed")
    client = _deleting_then_gone_client([_ConflictException("exists")] + [role] * n)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(hermes_provisioning, "_find_existing_runtime_arn", lambda name: "arn:old/rt-old")

    with pytest.raises(RuntimeError) as ei:
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})
    assert classify_provision_error(ei.value)[1] is True


def test_exhausted_create_loop_raises_a_retryable_error_not_assertion(monkeypatch):
    import boto3

    from core.repositories.container_repo import classify_provision_error

    monkeypatch.setattr(hermes_provisioning, "_ROLE_PROPAGATION_ATTEMPTS", 0)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _CreateClient([]))
    with pytest.raises(RuntimeError) as ei:
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})
    assert not isinstance(ei.value, AssertionError) and classify_provision_error(ei.value)[1] is True


def test_wait_runtime_gone_returns_false_for_a_live_runtime(monkeypatch):
    import boto3

    client = _CreateClient([], runtime_status="READY")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    assert hermes_provisioning._wait_runtime_gone("arn:x/rt") is False


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

    with pytest.raises(RuntimeError):
        hermes_provisioning._create_runtime({"agentRuntimeName": "x"})
    assert client.calls == hermes_provisioning._ROLE_PROPAGATION_ATTEMPTS


@pytest.mark.asyncio
async def test_write_profiles_never_overwrites_an_owner_edited_soul_md(monkeypatch):
    """M6-R48 on the re-render fan-out (PATCH /debug/provision, a provider switch): a SOUL.md
    that lost the generated marker is the owner's and is carried through verbatim; one still
    carrying the marker (or absent/empty) is regenerated from the roster row."""
    from core.services import hermes_profiles

    owner_text = "# Ada\n\nI wrote this myself.\n"
    stale = hermes_profiles.build_soul_md({"id": "x", "name": "Old", "role": "general"})
    stored = {"a-owner": owner_text, "a-gen": stale, "a-none": None, "a-empty": ""}
    roster = [{"id": aid, "name": f"New {aid}", "role": "general", "capabilities": "does things"} for aid in stored]
    _patch_write_profiles_collaborators(monkeypatch)
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=roster))
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(hermes_profiles, "read_soul_md", lambda owner_id, agent_id: stored[agent_id])
    uploaded = {}

    async def _capture(owner_id, agent_id, files):
        uploaded[agent_id] = files.soul_md

    monkeypatch.setattr(hermes_profiles, "upload_profile", _capture)

    await hermes_provisioning.write_profiles("org_a")

    assert uploaded["a-owner"] == owner_text
    for aid in ("a-gen", "a-none", "a-empty"):
        assert hermes_profiles.is_generated_soul(uploaded[aid])
        assert f"# New {aid}" in uploaded[aid]
        assert "## What you do\n\ndoes things" in uploaded[aid]


def test_a_runtime_still_carrying_the_retired_bedrock_env_reads_as_drift():
    """The Bedrock env keys are no longer set, so the desired env lacks them. They must still
    read as drift on a runtime that carries them, or ``roll_hermes_runtimes --respec`` would
    never UpdateAgentRuntime and the fleet would keep them forever."""
    desired = {"AWS_REGION": "us-east-1"}
    assert hermes_provisioning._env_drifted({**desired, "ANTHROPIC_MODEL": "arn:aip:sonnet"}, desired)
    assert not hermes_provisioning._env_drifted(dict(desired), desired)


async def _front_desk_profile(monkeypatch, line, override=None):
    """The SOUL.md ``write_profile_for_agent`` uploads for agent_fd, with ``line`` as the workspace's text line."""
    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", AsyncMock(return_value=line))
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None))
    )
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers", AsyncMock(return_value={})
    )
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pk"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    upload = AsyncMock()
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", upload)
    monkeypatch.setattr(hermes_provisioning, "_stage_owner_facts", AsyncMock())
    await hermes_provisioning.write_profile_for_agent(
        "owner_1", {"id": "agent_fd", "name": "Desk", "role": "general"}, soul_md_override=override
    )
    return upload.await_args.args[2].soul_md


@pytest.mark.asyncio
async def test_profile_for_the_line_agent_carries_the_section(monkeypatch):
    from core.services import hermes_profiles

    soul = await _front_desk_profile(monkeypatch, {"address": "+15551234567", "agent_id": "agent_fd"})
    base = hermes_profiles.build_soul_md({"id": "agent_fd", "name": "Desk", "role": "general"})
    assert soul.startswith(base + "\n\n## Front desk") and "+15551234567" in soul
    assert "A text that reaches you directly is from a customer." in soul
    assert hermes_profiles.is_generated_soul(soul)  # a rename still regenerates it
    # an owner-edited override is never touched
    line = {"address": "+15551234567", "agent_id": "agent_fd"}
    assert await _front_desk_profile(monkeypatch, line, override="MINE") == "MINE"


@pytest.mark.asyncio
async def test_other_agents_do_not(monkeypatch):
    soul = await _front_desk_profile(monkeypatch, {"address": "+1555", "agent_id": "someone_else"})
    assert "## Front desk" not in soul


@pytest.mark.asyncio
async def test_no_line_no_section(monkeypatch):
    assert "## Front desk" not in await _front_desk_profile(monkeypatch, None)


@pytest.mark.asyncio
async def test_the_roster_fan_out_reads_the_line_once(monkeypatch):
    line = AsyncMock(return_value={"address": "+1555", "agent_id": "a1"})
    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", line)
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None))
    )
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": f"a{i}", "name": f"A{i}", "role": "general"} for i in range(6)]),
    )
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "")
    write = AsyncMock()
    monkeypatch.setattr(hermes_provisioning, "write_profile_for_agent", write)
    await hermes_provisioning.write_profiles("owner_1")
    line.assert_awaited_once_with("owner_1")
    numbers = [c.kwargs["inputs"].front_desk_number_for(c.args[1]["id"]) for c in write.await_args_list]
    assert numbers == [None, "+1555", None, None, None, None]


@pytest.mark.asyncio
async def test_a_fresh_provision_mints_the_network_key_and_sessions_json(monkeypatch):
    """prepare_hermes_workspace makes every workspace a network member: the boot secret gets a
    member key and ``<owner>/network/sessions.json`` is written (empty roster before step 0
    seeds it; write_profiles republishes it after)."""
    import json

    import boto3
    from moto import mock_aws

    _patch_owner_aws(monkeypatch, existing_row=None)
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(hermes_provisioning.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr("core.services.hermes_profiles.put_owner_object", lambda k, b: objects.__setitem__(k, b))
    monkeypatch.setattr("core.services.hermes_profiles.get_owner_object", lambda k: objects.get(k))
    monkeypatch.setattr(
        "core.services.hermes_profiles.owner_object_size", lambda k: len(objects[k]) if k in objects else None
    )
    monkeypatch.setattr("core.services.clerk_admin.get_organization", AsyncMock(return_value={"name": "Acme"}))
    monkeypatch.setattr("core.services.messaging.load_agent_roster", AsyncMock(return_value=[]))
    wake = {"url": "https://api/x", "token": "org_a.t"}  # the doorbell's own tests: test_network_wake.py
    monkeypatch.setattr("core.services.network_member._wake", AsyncMock(return_value=wake))
    # The members list the sessions file carries (no Paperclip workspace in this test).
    monkeypatch.setattr("core.services.website_logins._humans_to_ask", AsyncMock(return_value=[]))

    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        assert await hermes_provisioning.prepare_hermes_workspace("org_a") is True
        boot = json.loads(sm.get_secret_value(SecretId=hermes_provisioning.boot_secret_name("org_a"))["SecretString"])
        assert boot["networkKey"]
        assert json.loads(objects["org_a/network/sessions.json"]) == {
            "workspace": "Acme",
            "sessions": [],
            "wake": wake,
            "members": [],
            "revoked_keys": [],
        }

        # A reconcile keeps the key and refreshes sessions.json (#4359: workspaces always carry members).
        objects["org_a/network/sessions.json"] = b'{"workspace": "Acme", "sessions": [], "kept": true}'
        assert await hermes_provisioning.prepare_hermes_workspace("org_a") is False
        again = json.loads(sm.get_secret_value(SecretId=hermes_provisioning.boot_secret_name("org_a"))["SecretString"])
        assert again["networkKey"] == boot["networkKey"]
        assert "kept" not in json.loads(objects["org_a/network/sessions.json"])
