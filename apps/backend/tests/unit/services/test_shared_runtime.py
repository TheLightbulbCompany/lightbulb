"""The tier's one shared Hermes runtime (HERMES_SHARED_RUNTIME_ARN): every provisioned workspace's
sessions run there, each under its own owner session id; a new workspace gets no runtime, role or
boot secret of its own; a roll moves warm sessions that predate the runtime's last update."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.services import container_rpc, hermes_provisioning as hp, runtime_transport as rt

SHARED = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/lightbulb_dev_hermes_pool-AbCd"
OWN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/lb_org_a-XyZ"


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", SHARED)
    monkeypatch.setattr(settings, "AGENTCORE_HERMES_IMAGE", "img:h-new")
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", "bucket")


def test_the_shared_runtime_serves_every_provisioned_workspace(shared):
    assert rt.runtime_arn({"agentcore_runtime_arn": OWN, "status": "error"}) == SHARED  # had its own
    assert rt.runtime_arn({"status": "running"}) == SHARED  # provisioned onto the shared one
    assert rt.runtime_arn({"status": "provisioning"}) is None  # not yet: nothing to wake


def test_without_the_setting_each_workspace_keeps_its_own(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    assert rt.runtime_arn({"agentcore_runtime_arn": OWN, "status": "running"}) == OWN
    assert rt.runtime_arn({"status": "running"}) is None


@pytest.mark.asyncio
async def test_an_invoke_goes_to_the_shared_runtime_under_the_owners_own_session(shared, monkeypatch):
    sent = []

    class Client:
        def invoke_agent_runtime(self, **kw):
            sent.append(kw)
            return {"statusCode": 200, "response": b'{"status":200,"body":{}}'}

    monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
    for owner in ("org_a", "org_b"):
        await rt.request({"owner_id": owner, "agentcore_runtime_arn": OWN, "status": "running"}, "/x", json_body={})
    assert [kw["agentRuntimeArn"] for kw in sent] == [SHARED, SHARED]
    assert [kw["runtimeSessionId"] for kw in sent] == [rt.session_id_for("org_a"), rt.session_id_for("org_b")]
    assert [json.loads(kw["payload"])["headers"][rt.TENANT_HEADER] for kw in sent] == ["org_a", "org_b"]


@pytest.mark.asyncio
async def test_a_new_workspace_gets_no_runtime_role_or_boot_secret(shared, monkeypatch):
    rows = {"owner_id": "org_new", "id": "row-1", "status": "provisioning"}
    writes = []
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=rows))
    monkeypatch.setattr(hp.container_repo, "update_fields", AsyncMock(side_effect=lambda o, f, **k: writes.append(f)))
    monkeypatch.setattr(hp, "_browser_profile_id", AsyncMock(return_value="prof"))
    monkeypatch.setattr(hp, "write_profiles", AsyncMock())
    nothing = AsyncMock(side_effect=AssertionError("per-owner infra on the shared runtime"))
    monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", nothing)
    monkeypatch.setattr(hp, "_create_runtime", lambda req: pytest.fail("CreateAgentRuntime"))
    assert await hp.provision_hermes_runtime("org_new") == SHARED
    hp.write_profiles.assert_awaited_once_with("org_new")
    assert writes == [{"runtime_host": "agentcore", "status": "running"}]  # no ARN on the row

    # prepare: tokens and the gate bearer, no role, no trust, no boot secret
    monkeypatch.setattr(
        hp.container_repo,
        "ensure_owner_tokens",
        AsyncMock(return_value={**rows, "hook_token": "h", "gateway_token": "g"}),
    )
    monkeypatch.setattr(hp, "ensure_agentcore_trust", nothing)
    monkeypatch.setattr(hp, "write_boot_secret", nothing)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.mint_gate_token", AsyncMock(return_value="bearer"))
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice", AsyncMock(return_value=("bedrock_claude", None))
    )
    monkeypatch.setattr(hp, "_ensure_network_member", AsyncMock())
    assert await hp.prepare_hermes_workspace("org_new") is False


@pytest.mark.asyncio
async def test_a_roll_moves_only_a_warm_session_that_predates_the_update(shared, monkeypatch):
    updated = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        hp.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "org_a", "status": "running"})
    )
    monkeypatch.setattr(hp, "_runtime_description", lambda arn: {"lastUpdatedAt": updated})
    monkeypatch.setattr(hp, "_update_runtime_image", lambda *a: pytest.fail("the shared runtime is CDK's"))
    stops = []
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda arn, sid: stops.append((arn, sid)))
    monkeypatch.setattr(container_rpc, "call", AsyncMock(return_value={"busy": 0}))
    monkeypatch.setattr(hp, "_session_warm", AsyncMock(return_value=False))
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", AsyncMock())

    def session(started):
        warm = {"warm_until": 10**10, "arn": SHARED, "started_at": int(started)}
        return patch.object(hp.container_repo, "get_session_sync", lambda owner_id, **k: warm)

    with session(updated.timestamp() + 60):
        assert await hp.roll_runtime_image("org_a") == "current"
    with patch.object(hp.container_repo, "get_session_sync", lambda owner_id, **k: {}):
        assert await hp.roll_runtime_image("org_a") == "current"  # parked: starts on the new version
    with session(updated.timestamp() - 60):
        assert await hp.roll_runtime_image("org_a", dry_run=True) == "would-update"
        assert await hp.roll_runtime_image("org_a") == "updated"
    assert stops == [(SHARED, rt.session_id_for("org_a"))]


@pytest.mark.asyncio
async def test_at_the_cut_over_a_run_still_live_on_its_own_runtime_is_never_polled_on_the_pool(shared, monkeypatch):
    """The drain: a workspace whose session is live on its own runtime stays there until it idles.
    The run is unknown to a pool session, so a poll there would be a 404, RunLost, and a retry."""
    from core.services import runtime_runs

    row = {"owner_id": "org_a", "id": "row-1", "agentcore_runtime_arn": OWN, "status": "running", "gateway_token": "g"}
    session = {"arn": OWN, "warm_until": 10**10, "started_at": 1}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    sent = []

    class Client:
        def invoke_agent_runtime(self, **kw):
            sent.append(kw["agentRuntimeArn"])
            body = {"run_id": "r1", "status": "running"} if kw["agentRuntimeArn"] == OWN else {"error": "run_not_found"}
            status = 200 if kw["agentRuntimeArn"] == OWN else 404
            return {"statusCode": 200, "response": json.dumps({"status": status, "body": body})}

    monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
    identity = runtime_runs.RunIdentity("r1", "org_a", "t1", 1, "agent-1")
    with patch.object(hp.container_repo, "get_session_sync", lambda owner_id, **k: session):
        status = await runtime_runs.get_run("org_a", identity)  # no RunLost: it went to its own runtime
        assert status.status == "running" and sent == [OWN]
        session["warm_until"] = 0  # idled out (no heartbeat): the next session starts on the pool
        with pytest.raises(runtime_runs.RunLost):
            await runtime_runs.get_run("org_a", identity)
        assert sent == [OWN, SHARED]


def test_a_runs_heartbeat_keeps_its_session_live_on_its_own_runtime(monkeypatch):
    """A Worker run sends no invoke; its heartbeat is what keeps the drain estimate current."""
    session = {"arn": OWN, "warm_until": 0, "started_at": 10**9}
    extended = []
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda owner_id, **k: session)
    monkeypatch.setattr(
        hp.container_repo, "extend_session_warm_sync", lambda owner_id, *, warm_until, **k: extended.append(warm_until)
    )
    rt.note_alive("org_a")
    assert extended and extended[0] > 10**9
