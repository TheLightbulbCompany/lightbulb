"""roll_runtime_image (M6 Task 3 flip tooling): update on image drift, wait READY, stop the warm
session; no-op when current; dry-run touches nothing; refuses non-AgentCore rows."""

import pytest

from core.services import container_rpc, hermes_provisioning
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
def _no_billing_row(monkeypatch):
    """Provisioning resolves the provider from billing on every path; default to no row
    (bedrock_claude). Tests that care patch billing_repo themselves, which wins."""
    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=None))


IMAGE = "acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-new"
ROW = {"owner_id": "org_a", "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:rt", "status": "running"}


def _row(value):
    async def _get(owner_id, *, consistent=False):
        return value

    return _get


async def _roles(owner_id):
    return "arn:aws:iam::877352799272:role/lightbulb-dev-owner-bedrock-org_a"


def _wire(monkeypatch, *, current_image, row=ROW):
    monkeypatch.setattr(hermes_provisioning.settings, "AGENTCORE_HERMES_IMAGE", IMAGE)
    monkeypatch.setattr(hermes_provisioning.container_repo, "get_by_owner_id", _row(row))
    monkeypatch.setattr(
        hermes_provisioning,
        "_runtime_description",
        lambda arn: {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": current_image}}},
    )
    monkeypatch.setattr("core.billing.bedrock_aip.ensure_owner_bedrock_role", _roles)
    calls = []
    # The roll is how an already-provisioned workspace gets its first Browser profile, so
    # the request it builds carries the id ensure_browser_profile just resolved -- echoed
    # into the fake request so the drift test can assert on it.
    monkeypatch.setattr(
        hermes_provisioning,
        "build_runtime_request_for_owner",
        lambda o, r, *, browser_profile_id="", provider_choice, max_concurrent_turns=None, network_workers=False: {
            "req": o,
            "profile": browser_profile_id,
        },
    )

    async def _ensure_profile(owner_id):
        return f"{owner_id}-ab12cd34ef"

    monkeypatch.setattr(hermes_provisioning.browser_sessions, "ensure_browser_profile", _ensure_profile)
    monkeypatch.setattr(
        hermes_provisioning, "_update_runtime_image", lambda arn, req: calls.append(("update", arn, req))
    )
    # Optimistic roll: a single status read replaces the READY poll. The "ready"
    # tag keeps the sequence assertions below unchanged.
    monkeypatch.setattr(hermes_provisioning, "_runtime_status", lambda arn: calls.append(("ready", arn)) or "READY")
    monkeypatch.setattr(hermes_provisioning, "_stop_runtime_session", lambda arn, sid: calls.append(("stop", arn, sid)))

    # The busy probe is part of the sequence now (Hazard 2). Stubbed here so these tests never
    # traverse the real container_rpc -> runtime_transport stack and attempt an outbound call;
    # the busy BEHAVIOUR is asserted in test_hermes_provisioning_roll_busy.py, where it is the
    # subject. An idle answer keeps every assertion below about update/ready/stop.
    async def _health(owner_id, method, params=None, **kwargs):
        calls.append(("health", owner_id))
        return {"busy": 0}

    monkeypatch.setattr(container_rpc, "call", _health)
    monkeypatch.setattr(hermes_provisioning, "_session_warm", AsyncMock(return_value=False))  # a parked workspace
    return calls


@pytest.mark.asyncio
async def test_roll_updates_waits_and_stops_the_session_on_drift(monkeypatch):
    calls = _wire(monkeypatch, current_image="acct.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-old")
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "health", "stop"]
    assert calls[0] == ("update", "arn:rt", {"req": "org_a", "profile": "org_a-ab12cd34ef"})
    assert calls[3][1] == "arn:rt" and calls[3][2].startswith("lb-org_a-")


@pytest.mark.asyncio
async def test_a_roll_wakes_again_a_session_that_was_in_use(monkeypatch):
    """Dev walk 16: the roll stopped a session the walk was using, and the next message waited 39 s for the cold
    start. A session warm before the roll is woken on the new spec once stopped; a parked one stays parked (above)."""
    calls = _wire(monkeypatch, current_image="x:old")
    monkeypatch.setattr(hermes_provisioning, "_session_warm", AsyncMock(return_value=True))
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "health", "stop", "health"]


@pytest.mark.asyncio
async def test_roll_keeps_managed_provider_in_replacement_spec(monkeypatch):
    from unittest.mock import AsyncMock

    calls = _wire(monkeypatch, current_image="x:old")
    monkeypatch.setattr(hermes_provisioning.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=("openrouter_managed", None)),
    )
    monkeypatch.setattr(
        hermes_provisioning, "build_runtime_request_for_owner", lambda *a, **kw: {"provider": kw["provider_choice"]}
    )
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert calls[0][2]["provider"] == "openrouter_managed"


@pytest.mark.asyncio
async def test_roll_keeps_the_owners_network_workers_flag_from_the_row(monkeypatch):
    calls = _wire(monkeypatch, current_image="x:old", row={**ROW, "network_workers": True})
    monkeypatch.setattr(
        hermes_provisioning, "build_runtime_request_for_owner", lambda *a, **kw: {"workers": kw["network_workers"]}
    )
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert calls[0][2] == {"workers": True}


@pytest.mark.asyncio
async def test_roll_carries_the_owners_turn_cap_from_the_row(monkeypatch):
    calls = _wire(monkeypatch, current_image="x:old", row={**ROW, "max_concurrent_turns": 6})
    monkeypatch.setattr(
        hermes_provisioning, "build_runtime_request_for_owner", lambda *a, **kw: {"cap": kw["max_concurrent_turns"]}
    )
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert calls[0][2] == {"cap": 6}


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
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "StopRuntimeSession")

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


@pytest.mark.asyncio
async def test_respec_on_a_matching_spec_rewrites_profiles_after_the_stop_without_an_update(monkeypatch):
    # The env diff already catches a model flip (roll_runtime_image re-specs on it); --respec adds the
    # profile rewrite, after the stop, and forces the idle stop even when nothing drifted.
    calls = _wire(monkeypatch, current_image=IMAGE)

    async def _write(owner_id):
        calls.append(("profiles", owner_id))

    monkeypatch.setattr(hermes_provisioning, "write_profiles", _write)
    assert await hermes_provisioning.roll_runtime_image("org_a", respec=True) == "updated"
    assert [c[0] for c in calls] == ["health", "stop", "profiles"]


@pytest.mark.asyncio
async def test_an_env_drift_rewrites_profiles_after_the_stop_without_respec(monkeypatch):
    # A managed-model flip moves LIGHTBULB_MANAGED_MODEL_ID in the env; bridge sync.py refuses an
    # env/profile pair that disagrees, so the plain roll must carry the profiles with it.
    calls = _wire(monkeypatch, current_image=IMAGE)
    monkeypatch.setattr(
        hermes_provisioning,
        "build_runtime_request_for_owner",
        lambda o, r, **kw: {"environmentVariables": {"LIGHTBULB_MANAGED_MODEL_ID": "new/model"}},
    )

    async def _write(owner_id):
        calls.append(("profiles", owner_id))

    monkeypatch.setattr(hermes_provisioning, "write_profiles", _write)
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert [c[0] for c in calls] == ["update", "ready", "health", "stop", "profiles"]


@pytest.mark.asyncio
async def test_a_lifecycle_change_on_the_same_image_updates_the_runtime(monkeypatch):
    # IDLE_TIMEOUT_S moved 4 h -> 30 min: without this a live runtime on the current image kept 4 h forever.
    calls = _wire(monkeypatch, current_image=IMAGE)
    life = {"idleRuntimeSessionTimeout": 1800, "maxLifetime": 28800}
    monkeypatch.setattr(
        hermes_provisioning, "build_runtime_request_for_owner", lambda o, r, **kw: {"lifecycleConfiguration": life}
    )
    monkeypatch.setattr(
        hermes_provisioning,
        "_runtime_description",
        lambda arn: {
            "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": IMAGE}},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 14400, "maxLifetime": 28800},
        },
    )
    assert await hermes_provisioning.roll_runtime_image("org_a") == "updated"
    assert calls[0] == ("update", "arn:rt", {"lifecycleConfiguration": life})
