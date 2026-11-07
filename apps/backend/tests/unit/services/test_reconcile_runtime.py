"""reconcile_runtime: one idempotent "make the runtime match billing".

A provider switch is a profile rewrite + ``.revision`` bump (the bridge re-reads it every
turn), so nothing here stops a session unless a BOOT-time input changed: the image, the
runtime env block, or the boot secret's tokens/bearer.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import boto3
import pytest
import yaml
from moto import mock_aws

from core.services import agentcore_runtime, container_rpc
from core.services import hermes_provisioning as hp

IMAGE = "acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-v1"
ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/org_a"
AGENT = "11111111-1111-1111-1111-111111111111"
MANAGED_GATE = "https://openrouter-gate.example/v1"

BILLING = {
    "chatgpt_oauth": {
        "subscription_status": "active",
        "provider_choice": "chatgpt_oauth",
        "oauth_connector_user_id": "user_conn",
    },
    "byo_anthropic": {"subscription_status": "active", "provider_choice": "byo_key", "byo_provider": "anthropic"},
    "byo_openai": {"subscription_status": "active", "provider_choice": "byo_key", "byo_provider": "openai"},
    "openrouter_managed": {"subscription_status": "active", "provider_choice": "openrouter_managed"},
    "trial": {"subscription_status": "trial_local"},
}
EXPECTED_PROVIDER = {
    "chatgpt_oauth": "openai-codex",
    "byo_anthropic": "anthropic",
    "byo_openai": "openai-api",
    "openrouter_managed": "custom",
    "trial": "custom",
}


class World:
    """Every AWS/Paperclip edge reconcile touches, as observable state."""

    def __init__(self, monkeypatch, *, billing, row=None, runtime_env=None):
        self.billing = billing
        self.row = {"owner_id": "org_a", "id": "row-1", "agentcore_runtime_arn": ARN, "status": "running"}
        self.row.update(row or {})
        self.profiles: dict[str, str] = {}
        self.calls: list = []
        self.runtime_env = runtime_env  # None = "whatever the desired request says"
        mp = monkeypatch

        mp.setattr(hp.settings, "AGENTCORE_HERMES_IMAGE", IMAGE)
        mp.setattr(hp.settings, "OWNER_STATE_BUCKET", "bucket")
        mp.setattr(hp.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
        mp.setattr("core.billing.openrouter_gate_url.gate_url", lambda: MANAGED_GATE)

        async def _billing(owner_id, **kwargs):
            return dict(self.billing)

        mp.setattr("core.repositories.billing_repo.get_by_owner_id", _billing)

        async def _get_row(owner_id, *, consistent=False):
            return dict(self.row)

        async def _ensure_tokens(owner_id, *, hook_token, gateway_token):
            await asyncio.sleep(0)  # let a concurrent reconcile interleave
            self.row.setdefault("hook_token", hook_token)
            self.row.setdefault("gateway_token", gateway_token)
            return dict(self.row)

        mp.setattr(hp.container_repo, "get_by_owner_id", _get_row)
        mp.setattr(hp.container_repo, "ensure_owner_tokens", _ensure_tokens)
        mp.setattr(hp.container_repo, "update_fields", AsyncMock(side_effect=AssertionError("no row write")))
        mp.setattr(
            hp.container_repo,
            "mark_provision_error",
            AsyncMock(side_effect=AssertionError("no error flip")),
            raising=False,
        )

        import core.billing.bedrock_aip as aip
        import core.billing.bedrock_gate_tokens as gate

        self.ensure_role = AsyncMock(return_value="arn:aws:iam::877352799272:role/owner-org_a")
        self.mint = AsyncMock(
            side_effect=lambda owner_id, *, row_id, generation: gate.derive_gate_bearer(owner_id, row_id, generation)
        )
        mp.setattr(aip, "ensure_owner_bedrock_role", self.ensure_role)
        mp.setattr(gate, "mint_gate_token", self.mint)
        mp.setattr(hp, "ensure_agentcore_trust", AsyncMock(return_value=True))
        self.ensure_or_key = AsyncMock(return_value="hash")
        mp.setattr("core.services.openrouter_keys.ensure_owner_key_for_settings", self.ensure_or_key)
        self.browser_profile = ""

        async def _ensure_browser(owner_id):
            return self.browser_profile

        mp.setattr(hp.browser_sessions, "ensure_browser_profile", _ensure_browser)

        async def _roster(owner_id):
            return [{"id": AGENT, "name": "Ada", "role": "ceo"}]

        async def _upload(owner_id, agent_id, files):
            await asyncio.sleep(0)
            self.profiles[agent_id] = files.config_yaml
            self.souls[agent_id] = files.soul_md

        mp.setattr("core.services.messaging.load_agent_roster", _roster)
        mp.setattr("core.services.hermes_profiles.upload_profile", _upload)
        self.souls: dict[str, str] = {}
        mp.setattr("core.services.hermes_profiles.read_soul_md", lambda owner_id, agent_id: self.souls.get(agent_id))
        mp.setattr(
            "core.services.credentials.mcp_server.build_connected_integration_servers", AsyncMock(return_value={})
        )
        mp.setattr("core.services.key_service.read_byo_key", AsyncMock(return_value="sk-byo-key"))
        mp.setattr(hp, "_stage_owner_facts", AsyncMock())
        mp.setattr(
            "core.services.oauth_service.get_token_row", AsyncMock(return_value={"tokens": {"access_token": "a"}})
        )
        self.staged = []
        mp.setattr(
            "core.services.container_auth.stage_codex_auth",
            lambda owner_id, tokens, **kw: self.staged.append((owner_id, kw)),
        )

        def _describe(arn):
            if self.runtime_env is None:  # a runtime already on today's desired spec
                self.runtime_env = dict((self.last_request or {}).get("environmentVariables") or {})
            return {
                "status": "READY",
                "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
                "environmentVariables": dict(self.runtime_env),
                "lifecycleConfiguration": (self.last_request or {}).get("lifecycleConfiguration"),
            }

        def _update(arn, req):
            self.calls.append(("update", req))
            self.runtime_env = dict(req["environmentVariables"])

        self.last_request = None
        real_build = hp.build_runtime_request_for_owner

        def _build(*args, **kwargs):
            self.last_request = real_build(*args, **kwargs)
            return self.last_request

        mp.setattr(hp, "build_runtime_request_for_owner", _build)
        mp.setattr(hp, "_runtime_description", _describe)
        mp.setattr(hp, "_update_runtime_image", _update)
        mp.setattr(hp, "_runtime_status", lambda arn: "READY")
        # Edges added to the roll/provision path after this suite was written: without these stubs they hit
        # moto's empty DynamoDB/S3 (ResourceNotFound/NoSuchBucket) and every roll reads as "failed".
        mp.setattr(hp, "_session_warm", AsyncMock(return_value=False))
        mp.setattr(hp, "_ensure_network_member", AsyncMock())
        mp.setattr(hp, "_stop_runtime_session", lambda arn, sid: self.calls.append(("stop", arn)))

        async def _health(owner_id, method, params=None, **kwargs):
            return {"busy": 0}

        mp.setattr(container_rpc, "call", _health)
        self.metrics = []
        mp.setattr(hp, "put_metric", lambda name, *a, **kw: self.metrics.append(name))
        self.expired = []

        async def _expire(owner_id):
            self.expired.append(owner_id)

        mp.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", _expire)
        # The billing-derived duties outside the runtime (``_reconcile_entitlements``).
        self.metronome = AsyncMock(return_value="cust")
        self.arm = AsyncMock(return_value="armed")
        mp.setattr("core.billing.metronome_provisioning.ensure_metronome_customer", self.metronome)
        mp.setattr("core.services.proactive.ensure", self.arm)

    def provider_of_profiles(self) -> set[str]:
        return {yaml.safe_load(cfg)["model"]["provider"] for cfg in self.profiles.values()}


@pytest.fixture
def boot_secrets():
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-east-1")


def _boot(sm) -> dict:
    return json.loads(sm.get_secret_value(SecretId=agentcore_runtime.boot_secret_name("org_a"))["SecretString"])


@pytest.mark.asyncio
@pytest.mark.parametrize("to", list(BILLING))
@pytest.mark.parametrize("frm", list(BILLING))
async def test_switch_matrix_rewrites_profiles_without_stopping_the_session(monkeypatch, boot_secrets, frm, to):
    if frm == to:
        pytest.skip("not a switch")
    world = World(monkeypatch, billing=BILLING[frm])
    await hp.reconcile_runtime("org_a")  # first write of the boot secret: a boot-time change
    assert world.provider_of_profiles() == {EXPECTED_PROVIDER[frm]}
    world.calls.clear()

    world.billing = BILLING[to]
    assert await hp.reconcile_runtime("org_a") == "current"

    assert world.provider_of_profiles() == {EXPECTED_PROVIDER[to]}
    assert world.calls == []  # no UpdateAgentRuntime, no StopRuntimeSession on a pure switch
    assert set(_boot(boot_secrets)) == {"hookToken", "gatewayToken", "bedrockBearer", "networkKey"}


@pytest.mark.asyncio
async def test_reconcile_re_ensures_role_and_gate_row(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    await hp.reconcile_runtime("org_a")

    world.ensure_role.assert_awaited()
    world.mint.assert_awaited()
    assert world.mint.await_args.kwargs == {"row_id": "row-1", "generation": 0}


@pytest.mark.asyncio
async def test_switching_into_chatgpt_stages_the_grant_only_if_absent(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["chatgpt_oauth"])
    await hp.reconcile_runtime("org_a")
    assert world.staged == [("org_a", {"only_if_absent": True})]


@pytest.mark.asyncio
async def test_two_concurrent_reconciles_converge_on_one_secret_and_profile_set(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["byo_anthropic"])
    await hp.reconcile_runtime("org_a")
    single = (_boot(boot_secrets), dict(world.profiles))

    world.profiles.clear()
    results = await asyncio.gather(hp.reconcile_runtime("org_a"), hp.reconcile_runtime("org_a"))

    assert results == ["current", "current"]
    assert (_boot(boot_secrets), dict(world.profiles)) == single


@pytest.mark.asyncio
async def test_retired_switch_keys_in_the_boot_secret_are_dropped_without_a_restart(monkeypatch, boot_secrets):
    """A pre-T8c secret still carries providerSwitchGeneration/providerSwitchFrozen/providerChoice.
    No bridge reads them, so the reconcile rewrites the credentials-only shape and stops nothing."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    await hp.reconcile_runtime("org_a")
    stale = {
        **_boot(boot_secrets),
        "providerSwitchGeneration": "gen-old",
        "providerSwitchFrozen": True,
        "providerChoice": "openrouter_managed",
    }
    boot_secrets.put_secret_value(SecretId=agentcore_runtime.boot_secret_name("org_a"), SecretString=json.dumps(stale))
    world.calls.clear()

    await hp.reconcile_runtime("org_a")
    assert world.calls == []
    assert set(_boot(boot_secrets)) == {"hookToken", "gatewayToken", "bedrockBearer", "networkKey"}


@pytest.mark.asyncio
async def test_env_diff_rolls_a_runtime_missing_the_managed_vars(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    await hp.reconcile_runtime("org_a")
    env_without_managed = {
        k: v for k, v in world.last_request["environmentVariables"].items() if not k.startswith("LIGHTBULB_MANAGED")
    }
    world.runtime_env = env_without_managed
    world.calls.clear()

    assert await hp.reconcile_runtime("org_a") == "updated"

    kinds = [c[0] for c in world.calls]
    assert kinds == ["update", "stop"]
    env = world.calls[0][1]["environmentVariables"]
    assert env["LIGHTBULB_MANAGED_GATE_URL"] == MANAGED_GATE
    assert env["LIGHTBULB_MANAGED_MODEL_ID"] == "candidate/model"


@pytest.mark.asyncio
async def test_a_reconcile_failure_leaves_a_running_row_running_and_emits_the_metric(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    world.ensure_role.side_effect = RuntimeError("iam down")

    assert await hp.reconcile_runtime("org_a") == "failed"
    assert world.metrics == ["provision.reconcile.failed"]
    assert world.row["status"] == "running"
    assert world.calls == []


@pytest.mark.asyncio
async def test_a_workspace_with_no_live_runtime_goes_through_the_provision_funnel(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["openrouter_managed"], row={"status": "provisioning"})
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", dispatch)

    assert await hp.reconcile_runtime("org_a") == "dispatched"
    dispatch.assert_awaited_once_with("org_a")
    assert world.profiles == {}


@pytest.mark.asyncio
async def test_a_first_provision_on_chatgpt_billing_writes_codex_profiles_with_no_switch(monkeypatch, boot_secrets):
    world = World(
        monkeypatch,
        billing=BILLING["chatgpt_oauth"],
        row={"agentcore_runtime_arn": None, "status": "provisioning"},
    )
    monkeypatch.setattr(hp, "_find_existing_runtime_arn", lambda name: None)
    monkeypatch.setattr(hp, "_create_runtime", lambda request: ARN)
    writes = []
    monkeypatch.setattr(hp.container_repo, "update_fields", AsyncMock(side_effect=lambda o, f, **kw: writes.append(f)))

    await hp.prepare_hermes_workspace("org_a")
    assert await hp.provision_hermes_runtime("org_a") == ARN

    assert world.provider_of_profiles() == {"openai-codex"}
    assert ("stop", ARN) not in world.calls
    assert writes[-1]["status"] == "running"


# --- fix round 1 ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superseded_bearers_expire_only_after_a_confirmed_session_stop(monkeypatch, boot_secrets):
    """C1: the stop is what makes a superseded bearer unpresentable; a busy session keeps it."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    await hp.reconcile_runtime("org_a")  # first secret write: boot change -> idle stop
    assert world.calls == [("stop", ARN)] and world.expired == ["org_a"]

    world.calls.clear(), world.expired.clear()
    world.runtime_env["AGENTCORE_PROFILE_ID"] = "drifted"
    monkeypatch.setattr(hp, "_wait_for_idle_session", AsyncMock(return_value=False))
    assert await hp.reconcile_runtime("org_a") == "updated-session-busy"
    assert world.expired == []

    monkeypatch.setattr(hp, "_wait_for_idle_session", AsyncMock(return_value=True))
    world.runtime_env["AGENTCORE_PROFILE_ID"] = "drifted"

    def _stop_fails(arn, sid):
        raise RuntimeError("throttled")

    monkeypatch.setattr(hp, "_stop_runtime_session", _stop_fails)
    await hp.reconcile_runtime("org_a")
    assert world.expired == []  # an unconfirmed stop keeps every superseded bearer


@pytest.mark.asyncio
async def test_an_unchanged_owner_reconciles_to_current(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["byo_openai"])
    await hp.reconcile_runtime("org_a")
    world.calls.clear()
    assert await hp.reconcile_runtime("org_a") == "current"
    assert world.calls == []


@pytest.mark.asyncio
async def test_a_transient_browser_error_does_not_re_spec(monkeypatch, boot_secrets):
    """I1: ensure_browser_profile answers "" on any error; the row's recorded id stands in."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"], row={"browser_profile_id": "prof-1"})
    world.browser_profile = "prof-1"
    await hp.reconcile_runtime("org_a")
    assert world.runtime_env["AGENTCORE_PROFILE_ID"] == "prof-1"
    world.calls.clear()

    world.browser_profile = ""  # the transient failure
    assert await hp.reconcile_runtime("org_a") == "current"
    assert world.calls == []


@pytest.mark.asyncio
async def test_a_key_aws_adds_to_the_env_is_not_drift(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    await hp.reconcile_runtime("org_a")
    world.calls.clear()
    world.runtime_env["AWS_SOMETHING_INJECTED"] = "x"
    assert await hp.reconcile_runtime("org_a") == "current"
    assert world.calls == []


@pytest.mark.asyncio
async def test_an_owner_edited_soul_survives_a_reconcile(monkeypatch, boot_secrets):
    """I2: the Instructions tab strips the generated marker; a reconcile keeps that file."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    world.souls[AGENT] = "You are Ada, the owner's own words."
    await hp.reconcile_runtime("org_a")
    assert world.souls[AGENT] == "You are Ada, the owner's own words."


@pytest.mark.asyncio
async def test_a_generated_soul_is_still_regenerated(monkeypatch, boot_secrets):
    from core.services.hermes_profiles import SOUL_GENERATED_MARKER, is_generated_soul

    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    world.souls[AGENT] = f"{SOUL_GENERATED_MARKER}\nstale"
    await hp.reconcile_runtime("org_a")
    assert is_generated_soul(world.souls[AGENT]) and "stale" not in world.souls[AGENT]


@pytest.mark.asyncio
async def test_billing_that_moves_mid_reconcile_is_rendered_again(monkeypatch, boot_secrets):
    """I3: an overlapping reconcile that finishes last must not leave the older provider."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    real_upload = hp.write_profiles
    runs = []

    async def _write_profiles(owner_id):
        await real_upload(owner_id)
        runs.append(world.provider_of_profiles())
        if len(runs) == 1:
            world.billing = BILLING["chatgpt_oauth"]  # a switch committed while we rendered

    monkeypatch.setattr(hp, "write_profiles", _write_profiles)
    await hp.reconcile_runtime("org_a")
    assert runs == [{"custom"}, {"openai-codex"}]
    assert world.provider_of_profiles() == {"openai-codex"}


@pytest.mark.asyncio
async def test_a_concurrent_update_is_not_a_reconcile_failure(monkeypatch, boot_secrets):
    """I3: a runtime already UPDATING (another roll) answers ConflictException."""
    from botocore.exceptions import ClientError

    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    await hp.reconcile_runtime("org_a")
    world.runtime_env["AGENTCORE_PROFILE_ID"] = "drifted"

    def _conflict(arn, req):
        raise ClientError({"Error": {"Code": "ConflictException", "Message": "updating"}}, "UpdateAgentRuntime")

    monkeypatch.setattr(hp, "_update_runtime_image", _conflict)
    assert await hp.reconcile_runtime("org_a") == "update-in-progress"
    assert world.metrics == []


@pytest.mark.asyncio
async def test_a_failed_row_read_is_a_counted_reconcile_failure(monkeypatch, boot_secrets):
    """M1: every exception, the row read included, is logged and counted."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb")))
    assert await hp.reconcile_runtime("org_a") == "failed"
    assert world.metrics == ["provision.reconcile.failed"]


# --- entitlements: the duties the /container/status poll used to self-heal ----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [{}, {"status": "provisioning"}])
async def test_reconcile_ensures_the_metronome_customer_and_the_proactive_layer(monkeypatch, boot_secrets, row):
    """A Stripe re-entitlement (trial->paid, re-subscribe) reaches both through its reconcile --
    whether the runtime is live or the reconcile hands off to the provision funnel."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"], row=row)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_provision", AsyncMock(return_value=True))

    await hp.reconcile_runtime("org_a")

    world.metronome.assert_awaited_once_with(owner_id="org_a")
    world.arm.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
@pytest.mark.parametrize("billing", ["chatgpt_oauth", "byo_anthropic"])
async def test_a_chatgpt_or_byo_owner_gets_no_metronome_customer(monkeypatch, boot_secrets, billing):
    """Inference we never bill: no customer, contract or low-balance alerts."""
    world = World(monkeypatch, billing=BILLING[billing])

    await hp.reconcile_runtime("org_a")

    world.metronome.assert_not_awaited()
    world.arm.assert_awaited_once_with("org_a")


@pytest.mark.asyncio
async def test_a_metronome_failure_fails_the_reconcile_after_the_runtime_work(monkeypatch, boot_secrets):
    """So the reconcile execution is retried; the runtime half still ran (profiles written)."""
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    world.metronome.side_effect = RuntimeError("metronome down")

    assert await hp.reconcile_runtime("org_a") == "failed"
    assert world.metrics == ["metronome.provision.error"]
    assert world.profiles


@pytest.mark.asyncio
async def test_a_proactive_failure_does_not_fail_the_reconcile(monkeypatch, boot_secrets):
    world = World(monkeypatch, billing=BILLING["openrouter_managed"])
    world.arm.side_effect = RuntimeError("paperclip down")

    assert await hp.reconcile_runtime("org_a") != "failed"
    assert world.metrics == []
    world.arm.assert_awaited_once_with("org_a")


@pytest.fixture(autouse=True)
def _no_text_line(monkeypatch):
    """Profile writes read the workspace's texting line (the front-desk SOUL section); none here."""
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", _AsyncMock(return_value=None))
