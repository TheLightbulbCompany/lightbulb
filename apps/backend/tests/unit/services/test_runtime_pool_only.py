"""The current product cannot revive an obsolete owned runtime by losing pool config."""

from unittest.mock import AsyncMock

import httpx
import pytest

from core.config import settings
from core.services import runtime_transport as rt, hermes_provisioning as hp

POOL = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/pool-AbCd"
OLD = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/old-XyZ"


def test_missing_pool_never_routes_to_owned_or_saved_runtime(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    for row in (
        {"status": "running", "agentcore_runtime_arn": OLD},
        {"status": "running", "agentcore_shared_runtime_arn": POOL},
    ):
        with pytest.raises(RuntimeError, match="HERMES_SHARED_RUNTIME_ARN"):
            rt.runtime_arn(row)


@pytest.mark.asyncio
async def test_live_legacy_writer_refuses_instead_of_invoking_it_or_starting_a_second_writer(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", POOL)
    monkeypatch.setattr(hp.container_repo, "get_session_sync", lambda *a, **k: {"arn": OLD, "warm_until": 10**10})
    with pytest.raises(httpx.ConnectError, match="legacy runtime"):
        await rt.request({"owner_id": "org_a", "status": "running", "agentcore_runtime_arn": OLD}, "/x", json_body={})


@pytest.mark.asyncio
async def test_missing_pool_refuses_preparation_before_any_identity_mutation(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    tokens = AsyncMock(side_effect=AssertionError("identity mutated"))
    monkeypatch.setattr(hp.container_repo, "ensure_owner_tokens", tokens)
    with pytest.raises(RuntimeError, match="HERMES_SHARED_RUNTIME_ARN"):
        await hp.prepare_hermes_workspace("org_a")
    tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_pool_never_adopts_or_creates_per_owner_runtime(monkeypatch):
    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", "")
    monkeypatch.setattr(settings, "AGENTCORE_HERMES_IMAGE", "candidate")
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", "profiles")
    monkeypatch.setattr(
        hp.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"agentcore_runtime_arn": OLD, "status": "running"}),
    )
    with pytest.raises(RuntimeError, match="HERMES_SHARED_RUNTIME_ARN"):
        await hp.provision_hermes_runtime("org_a")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
async def test_real_rpc_cause_chain_never_treats_legacy_refusal_as_idle(monkeypatch, legacy):
    import json
    import time

    monkeypatch.setattr(settings, "HERMES_SHARED_RUNTIME_ARN", POOL)
    row = {"owner_id": "org_a", "status": "running", "agentcore_runtime_arn": OLD, "gateway_token": "test-gateway"}
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(
        hp.container_repo, "get_session_sync", lambda *a, **k: {"arn": OLD if legacy else POOL, "warm_until": 10**10}
    )
    invoked = []

    def invoke(arn, owner, envelope, timeout, task_id=None):
        invoked.append(arn)
        return {"statusCode": 200}, json.dumps({"status": 200, "body": {"ok": True, "payload": {"busy": 0}}})

    monkeypatch.setattr(rt, "_invoke_envelope", invoke)
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(hp, "stop_runtime_session", stop)
    monkeypatch.setattr("core.billing.bedrock_gate_tokens.expire_superseded_gate_tokens", AsyncMock())
    monkeypatch.setattr(hp, "ROLL_IDLE_WAIT_S", 0)

    # Exercise real transport -> RPC ApiProblem cause -> lifecycle idle guard, not an idle RPC mock.
    assert await hp._idle_payload("org_a", time.monotonic() - 1) == (None if legacy else {"busy": 0})
    assert await hp._stop_when_idle("org_a", OLD if legacy else POOL, was_warm=False, updated_at=None, write=False) is (
        not legacy
    )
    if legacy:
        assert invoked == []
        stop.assert_not_awaited()
    else:
        assert invoked == [POOL, POOL]
        stop.assert_awaited_once_with("org_a", POOL)
