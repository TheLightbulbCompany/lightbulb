"""roll_runtime_image (M6 Task 3 flip tooling): update on image drift, wait READY, stop the warm
session; no-op when current; dry-run touches nothing; refuses non-AgentCore rows."""

import pytest

from core.services import container_rpc, hermes_provisioning

IMAGE = "acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-new"
ROW = {"owner_id": "org_a", "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:rt", "status": "running"}


def _row(value):
    async def _get(owner_id, *, consistent=False):
        return value

    return _get


async def _roles(owner_id):
    return "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a", {"sonnet": "arn:aip"}


def _wire(monkeypatch, *, current_image, row=ROW):
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", IMAGE)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(row))
    monkeypatch.setattr(hermes_provisioning, "_runtime_image_uri", lambda arn: current_image)
    monkeypatch.setattr(hermes_provisioning, "_load_role_and_aips", _roles)
    calls = []
    monkeypatch.setattr(hermes_provisioning, "build_runtime_request_for_owner", lambda o, r, a: {"req": o})
    monkeypatch.setattr(
        hermes_provisioning, "_update_runtime_image", lambda arn, req: calls.append(("update", arn, req))
    )
    monkeypatch.setattr(hermes_provisioning, "_wait_for_runtime_ready", lambda arn: calls.append(("ready", arn)))
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda arn, sid: calls.append(("stop", arn, sid)))

    # The busy probe is part of the sequence now (Hazard 2). Stubbed here so these tests never
    # traverse the real container_rpc -> runtime_transport stack and attempt an outbound call;
    # the busy BEHAVIOUR is asserted in test_hermes_provisioning_roll_busy.py, where it is the
    # subject. An idle answer keeps every assertion below about update/ready/stop.
    async def _health(owner_id, method, params=None, **kwargs):
        calls.append(("health", owner_id))
        return {"busy": 0}

    monkeypatch.setattr(container_rpc, "call", _health)
    return calls


@pytest.mark.asyncio
async def test_roll_updates_waits_and_stops_the_session_on_drift(monkeypatch):
    calls = _wire(monkeypatch, current_image="acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-old")
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "health", "stop"]
    assert calls[0] == ("update", "arn:rt", {"req": "org_a"})
    assert calls[3][1] == "arn:rt" and calls[3][2].startswith("lb-org_a-")


@pytest.mark.asyncio
async def test_roll_is_a_noop_when_the_runtime_is_already_on_the_image(monkeypatch):
    calls = _wire(monkeypatch, current_image=IMAGE)
    assert await hermes_provisioning.roll_runtime_image("org_a") == "current"
    assert calls == []


@pytest.mark.asyncio
async def test_roll_dry_run_reports_without_touching_the_runtime(monkeypatch):
    calls = _wire(monkeypatch, current_image="x:old")
    assert await hermes_provisioning.roll_runtime_image("org_a", dry_run=True) == "would-update"
    assert calls == []  # no update, no stop -- and no busy probe, which invokes the runtime


@pytest.mark.asyncio
async def test_roll_tolerates_no_warm_session(monkeypatch):
    calls = _wire(monkeypatch, current_image="x:old")

    def _no_session(arn, sid):
        raise RuntimeError("Session not found or has been terminated")

    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", _no_session)
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "health"]


@pytest.mark.asyncio
async def test_roll_refuses_an_ecs_row(monkeypatch):
    calls = _wire(
        monkeypatch,
        current_image="x:old",
        row={"owner_id": "org_a", "runtime_host": "ecs", "service_name": "openclaw-x"},
    )
    with pytest.raises(ValueError):
        await hermes_provisioning.roll_runtime_image("org_a")
    assert calls == []
