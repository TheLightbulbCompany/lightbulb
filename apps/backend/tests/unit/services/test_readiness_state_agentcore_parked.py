"""AgentCore rows never get a health RPC: AWS parks/wakes the microVM invisibly, so a running
AgentCore row is "ready" outright, and a probe would wake a box the poll should leave alone."""

from core.services.readiness_state import resolve_runtime_phase


def test_a_live_agentcore_row_is_ready_without_a_health_rpc():
    row = {"runtime_host": "agentcore", "status": "running"}
    # The microVM may be parked; AgentCore wakes it on invoke, so a failed probe is not "starting".
    assert resolve_runtime_phase(row, gateway_connected=False) == "ready"


# NOTE: `test_an_ecs_row_still_needs_the_gateway_probe` was deleted -- `host_for` is pinned
# to "agentcore" fleet-wide now (deletion PR-1), so a `runtime_host: "ecs"` row is reported
# "ready" without a health probe exactly like an agentcore row; there is no ECS row left that
# still needs one.


def test_a_provisioning_agentcore_row_is_still_provisioning():
    assert resolve_runtime_phase({"runtime_host": "agentcore", "status": "provisioning"}, False) == "provisioning"
