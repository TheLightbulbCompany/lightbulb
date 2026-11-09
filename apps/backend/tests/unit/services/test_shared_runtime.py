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


def test_a_pool_workspace_keeps_its_recorded_route_when_the_setting_is_cleared(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    assert rt.runtime_arn({"status": "running", "agentcore_shared_runtime_arn": SHARED}) == SHARED


@pytest.mark.parametrize("route_field", ["agentcore_runtime_arn", "agentcore_shared_runtime_arn"])
def test_a_failed_drain_read_never_starts_a_second_session_on_the_pool(shared, monkeypatch, route_field):
    from botocore.exceptions import ClientError

    def unavailable(*args, **kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")

    monkeypatch.setattr(hp.container_repo, "get_session_sync", unavailable)
    with pytest.raises(ClientError):
        rt.live_arn({"owner_id": "org_a", route_field: OWN, "status": "running"})


@pytest.mark.asyncio
async def test_an_invoke_goes_to_the_shared_runtime_under_the_owners_own_session(shared, monkeypatch):
    sent = []

    class Client:
        def invoke_agent_runtime(self, **kw):
            sent.append(kw)
            return {"statusCode": 200, "response": b'{"status":200,"body":{}}'}

    monkeypatch.setattr("boto3.client", lambda *a, **kw: Client())
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: {})  # no pre-pool session to drain
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
    assert writes == [{"runtime_host": "agentcore", "agentcore_shared_runtime_arn": SHARED, "status": "running"}]
    assert "agentcore_runtime_arn" not in writes[0]  # saved route, never an owned runtime to delete

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
async def test_a_running_pool_workspace_records_its_route_without_rewriting_profiles(shared, monkeypatch):
    row = {"owner_id": "org_new", "id": "row-1", "status": "running"}
    monkeypatch.setattr(hp.container_repo, "update_fields", AsyncMock())
    monkeypatch.setattr(hp, "write_profiles", AsyncMock(side_effect=AssertionError("rewrote a live profile")))
    monkeypatch.setattr(hp, "_browser_profile_id", AsyncMock(side_effect=AssertionError("recreated live browser")))
    assert await hp._join_shared_runtime("org_new", row) == SHARED
    hp.container_repo.update_fields.assert_awaited_once_with("org_new", {"agentcore_shared_runtime_arn": SHARED})


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


@pytest.mark.asyncio
async def test_a_missed_heartbeat_inside_the_grace_window_keeps_the_poll_on_its_own_runtime(shared, monkeypatch):
    """One late or missed heartbeat must not send a live run's poll to the pool (RunLost): the session
    counts as live on its own runtime for ``LIVE_GRACE_S`` past ``warm_until``."""
    import time

    from core.services import runtime_runs

    row = {"owner_id": "org_a", "id": "row-1", "agentcore_runtime_arn": OWN, "status": "running", "gateway_token": "g"}
    now = int(time.time())
    session = {"arn": OWN, "warm_until": now - 60, "started_at": 1}  # a heartbeat is a minute late
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
        status = await runtime_runs.get_run("org_a", identity)  # no RunLost
        assert status.status == "running" and sent == [OWN]
        session["warm_until"] = now - rt.LIVE_GRACE_S - 1  # past the grace window: idle, the pool takes it
        with pytest.raises(runtime_runs.RunLost):
            await runtime_runs.get_run("org_a", identity)
        assert sent == [OWN, SHARED]


@pytest.mark.asyncio
async def test_a_shared_roll_stops_again_while_a_microvm_older_than_the_update_answers(shared, monkeypatch):
    """_stop_rolled_session on the pool: "older than the update" is the runtime's own lastUpdatedAt (the
    update was CDK's deploy, not this process), so a split session's old microVM answering the rewake is
    drained and stopped too, and a fresh one ends it."""
    import time

    updated = datetime.fromtimestamp(time.time() - 600, tz=timezone.utc)  # the deploy, 10 min ago
    row = {"owner_id": "org_a", "status": "running"}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp, "_runtime_description", lambda arn: {"lastUpdatedAt": updated})
    stops = []
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda arn, sid: stops.append(arn))
    monkeypatch.setattr(hp.container_repo, "clear_session", AsyncMock())
    monkeypatch.setattr(hp, "ROLL_IDLE_POLL_S", 0)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", AsyncMock())
    # idle probe, rewake (an old VM: up 1 h), idle probe, rewake (a fresh VM: up 5 s)
    answers = iter([{"busy": 0}, {"uptimeS": 3600}, {"busy": 0, "uptimeS": 3600}, {"uptimeS": 5}])
    monkeypatch.setattr(container_rpc, "call", AsyncMock(side_effect=lambda *a, **k: next(answers)))
    warm = {"warm_until": 10**10, "arn": SHARED, "started_at": int(updated.timestamp()) - 60}
    with patch.object(hp.container_repo, "get_session_sync", lambda owner_id, **k: warm):
        assert await hp.roll_runtime_image("org_a") == "updated"
    assert stops == [SHARED, SHARED]


@pytest.mark.asyncio
async def test_a_shared_roll_moves_a_session_still_warm_on_its_own_runtime_onto_the_pool(shared, monkeypatch):
    """At the first pool deploy, warm sessions are on each workspace's OWN (old-image) runtime: the roll
    stops that one at idle, so the next invoke starts on the pool."""
    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp, "_runtime_description", lambda arn: pytest.fail("no pool update to compare"))
    stops = []
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda arn, sid: stops.append(arn))
    monkeypatch.setattr(hp.container_repo, "clear_session", AsyncMock())
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", AsyncMock())
    monkeypatch.setattr(container_rpc, "call", AsyncMock(return_value={"busy": 0, "uptimeS": 5}))
    warm = {"warm_until": 10**10, "arn": OWN, "started_at": 1}
    with patch.object(hp.container_repo, "get_session_sync", lambda owner_id, **k: warm):
        assert await hp.roll_runtime_image("org_a", dry_run=True) == "would-update"
        assert await hp.roll_runtime_image("org_a") == "updated"
    assert stops == [OWN]


@pytest.mark.asyncio
async def test_the_roll_and_transport_agree_a_session_in_the_drain_grace_is_still_live(shared, monkeypatch):
    import time

    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    warm = {"warm_until": int(time.time()) - 1, "arn": OWN, "started_at": 1}
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: warm)
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    assert rt.live_arn(row) == OWN
    assert await hp.roll_runtime_image("org_a", dry_run=True) == "would-update"


@pytest.mark.asyncio
async def test_a_saved_pool_route_is_never_respecced_as_an_owned_runtime(shared, monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    row = {"owner_id": "org_a", "agentcore_shared_runtime_arn": SHARED, "status": "running"}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp, "_shared_session_to_roll", AsyncMock(return_value=(SHARED, None)))
    monkeypatch.setattr(
        "core.billing.bedrock_aip.ensure_owner_bedrock_role", AsyncMock(side_effect=AssertionError("per-owner role"))
    )
    assert await hp.provision_hermes_runtime("org_a") == SHARED
    assert await hp.roll_runtime_image("org_a") == "current"


def test_the_drain_outlasts_agentcores_idle_timeout(shared):
    """live_arn leaves the own runtime only once its microVM has surely idled out: ``warm_until`` can lag
    the last invoke by WARM_EXTEND_STEP_S, so warm + grace must pass the last invoke + IDLE_TIMEOUT_S."""
    from core.services.agentcore_runtime import IDLE_TIMEOUT_S

    worst_warm = IDLE_TIMEOUT_S - rt.WARM_MARGIN_S - rt.WARM_EXTEND_STEP_S  # after the last invoke
    assert worst_warm + rt.LIVE_GRACE_S > IDLE_TIMEOUT_S


def test_the_fleet_grid_reports_a_shared_runtime_workspace(shared, monkeypatch):
    import asyncio

    from core.services.ops import fleet

    monkeypatch.setattr(
        fleet.container_repo, "scan_all", AsyncMock(return_value=[{"owner_id": "o", "status": "running"}])
    )
    monkeypatch.setattr(fleet, "_display_names", AsyncMock(return_value={}))
    [cell] = asyncio.run(fleet._build_grid())
    assert (cell["runtime_arn"], cell["status"]) == (SHARED, "running")


@pytest.mark.asyncio
async def test_a_network_reload_reaches_a_session_still_warm_on_its_own_runtime(shared, monkeypatch):
    from core.services import network_member

    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    with patch.object(hp.container_repo, "get_session_sync", lambda owner_id, **k: {"arn": OWN, "warm_until": 10**10}):
        assert await network_member._session_is_warm("org_a")


@pytest.mark.asyncio
async def test_a_network_revocation_reloads_the_old_runtime_through_the_drain_grace(shared, monkeypatch):
    import time

    from core.services import network_member

    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    warm = {"arn": OWN, "warm_until": int(time.time()) - 1}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: warm)
    assert await network_member._session_is_warm("org_a")


@pytest.mark.asyncio
async def test_a_late_teardown_spares_a_reprovisioned_shared_workspace(shared, monkeypatch):
    """Both incarnations have no ARN on the pool: the row id tells them apart."""
    monkeypatch.setattr(
        hp.container_repo, "get_by_owner_id", AsyncMock(return_value={"id": "row-new", "status": "running"})
    )
    assert await hp._superseded("org_a", {"id": "row-old"})
    assert not await hp._superseded("org_a", {"id": "row-new"})
    assert "status" in hp._TEARDOWN_ROW_KEYS  # the captured row still routes to the pool (runtime_arn)


@pytest.mark.parametrize("saved_route", [False, True])
def test_rollback_keeps_a_migrated_legacy_workspace_on_its_live_pool_session(monkeypatch, saved_route):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    if saved_route:
        row["agentcore_shared_runtime_arn"] = SHARED
    session = {"arn": SHARED, "warm_until": 10**10}
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: session)
    assert rt.live_arn(row) == SHARED
    session["warm_until"] = 0
    assert rt.live_arn(row) == OWN


@pytest.mark.asyncio
async def test_legacy_migration_records_the_shared_route_without_rewriting_profiles(shared, monkeypatch):
    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    monkeypatch.setattr(hp.container_repo, "update_fields", AsyncMock())
    monkeypatch.setattr(hp, "write_profiles", AsyncMock(side_effect=AssertionError("rewrote live profile")))
    assert await hp._join_shared_runtime("org_a", row) == SHARED
    hp.container_repo.update_fields.assert_awaited_once_with("org_a", {"agentcore_shared_runtime_arn": SHARED})


@pytest.mark.asyncio
async def test_custody_fence_stops_the_live_legacy_vm_before_taking_the_wake_lease(shared, monkeypatch):
    from core.services import token_custody

    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: {"arn": OWN, "warm_until": 10**10})
    events = []

    async def stop(owner, arn):
        events.append(("stop", arn))
        return True

    def take(owner, **kw):
        events.append(("lease", kw["arn"]))
        return True

    monkeypatch.setattr(hp, "stop_runtime_session", stop)
    monkeypatch.setattr(hp.container_repo, "try_take_wake_lease_sync", take)
    monkeypatch.setattr(hp.container_repo, "finish_wake_sync", lambda *a, **k: None)
    async with token_custody.fenced_session("org_a") as held:
        assert held
        assert events == [("stop", OWN), ("lease", OWN)]


@pytest.mark.asyncio
async def test_debug_restart_stops_the_live_legacy_vm(shared, monkeypatch):
    from types import SimpleNamespace
    from routers import debug

    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: {"arn": OWN, "warm_until": 10**10})
    monkeypatch.setattr(debug, "resolve_owner_id", lambda auth: "org_a")
    monkeypatch.setattr(debug, "get_owner_type", lambda auth: "org")
    monkeypatch.setattr(hp, "write_profiles", AsyncMock())
    monkeypatch.setattr(hp, "stop_runtime_session", AsyncMock(return_value=True))
    await debug.redeploy_container(SimpleNamespace(is_org_context=False))
    hp.stop_runtime_session.assert_awaited_once_with("org_a", OWN)


@pytest.mark.asyncio
async def test_rollback_revokes_a_pool_session_still_inside_drain_grace(monkeypatch):
    import time
    from core.services import network_member

    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    row = {
        "owner_id": "org_a",
        "agentcore_runtime_arn": OWN,
        "agentcore_shared_runtime_arn": SHARED,
        "status": "running",
    }
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(
        hp.container_repo, "get_session_sync", lambda *a, **k: {"arn": SHARED, "warm_until": int(time.time()) - 1}
    )
    assert await network_member._session_is_warm("org_a")


@pytest.mark.asyncio
async def test_teardown_stops_the_live_vm_before_deleting_its_owned_runtime(shared, monkeypatch):
    from core.services import browser_control

    row = {"owner_id": "org_a", "agentcore_runtime_arn": OWN, "status": "running"}
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: {"arn": OWN, "warm_until": 10**10})
    monkeypatch.setattr(browser_control, "teardown", AsyncMock())
    monkeypatch.setattr(hp, "stop_runtime_session", AsyncMock(return_value=True))
    monkeypatch.setattr(hp, "_delete_runtime_and_bearer", AsyncMock())
    monkeypatch.setattr(hp, "_delete_boot_secret", lambda *a: None)
    monkeypatch.setattr(hp, "_delete_prefixes", lambda *a: None)
    await hp.teardown_hermes_runtime("org_a", container_row=row, delete_profiles=False)
    hp.stop_runtime_session.assert_awaited_once_with("org_a", OWN)
    hp._delete_runtime_and_bearer.assert_awaited_once_with("org_a", row)
