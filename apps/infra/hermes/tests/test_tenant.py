"""The tenant comes from each invoke (X-Lightbulb-Tenant), and a session serves one tenant."""
import os
import sys

import pytest

import bridge
from bridge import runs, server, sync, turn

AUTH = {"authorization": "Bearer gw-test"}
NOPE = "/plugins/lightbulb-channel/nope"  # past every gate, then 404: proves the invoke was let in


def invoke(tenant=None):
    headers = dict(AUTH, **({bridge.TENANT_HEADER: tenant} if tenant else {}))
    return server.handle_plugin(NOPE, "POST", headers, {})


@pytest.fixture
def shared(monkeypatch):
    """A runtime booted with no owner (a shared one): every module's copy of OWNER is empty."""
    monkeypatch.delenv("S3_PREFIX", raising=False)
    monkeypatch.setenv("ISOL8_OWNER_ID", "")
    for name, mod in list(sys.modules.items()):
        if (name == "bridge" or name.startswith("bridge.")) and mod is not None:
            if hasattr(mod, "OWNER"):
                monkeypatch.setattr(mod, "OWNER", "")
            if hasattr(mod, "S3_PREFIX"):
                monkeypatch.setattr(mod, "S3_PREFIX", "hermes")


def test_a_per_owner_runtime_is_unchanged():
    assert bridge.OWNER == "org_test"
    assert invoke()[0] == 404  # an older backend sends no tenant
    assert invoke("org_test")[0] == 404
    assert bridge.OWNER == "org_test" and sync.S3_PREFIX == "org_test/hermes"


def test_a_per_owner_runtime_refuses_another_tenant():
    assert invoke("org_other") == (403, {"error": "tenant_mismatch"})
    assert bridge.OWNER == "org_test"


def test_an_unauthorized_invoke_never_binds(shared):
    assert server.handle_plugin(NOPE, "POST", {bridge.TENANT_HEADER: "org_a"}, {})[0] == 401
    assert bridge.OWNER == ""


def test_a_shared_runtime_takes_the_first_tenant_everywhere(shared):
    assert invoke("org_a")[0] == 404
    assert bridge.OWNER == turn.OWNER == runs.OWNER == server.OWNER == "org_a"
    assert sync.S3_PREFIX == bridge.S3_PREFIX == "org_a/hermes"
    assert os.environ["ISOL8_OWNER_ID"] == "org_a"
    assert invoke("org_a")[0] == 404
    assert invoke("org_b") == (403, {"error": "tenant_mismatch"})
    assert bridge.OWNER == "org_a" and sync.S3_PREFIX == "org_a/hermes"


def test_a_run_for_another_owner_is_refused_after_binding(shared):
    invoke("org_a")
    with pytest.raises(runs.Refused) as refused:
        runs.validate_identity({
            "runId": "11111111-1111-1111-1111-111111111111", "taskId": "22222222-2222-2222-2222-222222222222",
            "agentId": "33333333-3333-3333-3333-333333333333", "ownerId": "org_b", "attempt": 1, "mode": "worker",
            "session": {"kind": "workspace"},
            "sessionKey": "agent:33333333-3333-3333-3333-333333333333:lightbulb:task:22222222-2222-2222-2222-222222222222",
        })
    assert refused.value.reply == (403, {"error": "owner_mismatch"})
