"""A shared runtime (no owner, no boot secret): the binding invoke brings the owner's boot values,
and the session serves that owner only."""
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import bridge
from bridge import browser_control, network, runs, server, sync, turn

NOPE = "/plugins/lightbulb-channel/nope"  # past every gate, then 404: proves the invoke was let in
BOOT_A = {"hookToken": "hook-a", "gatewayToken": "gw-a", "networkKey": "net-a", "bedrockBearer": "bearer-a",
          "browserProfileId": "prof-a", "networkWorkers": True, "maxConcurrentTurns": 6}
BOOT_B = {"hookToken": "hook-b", "gatewayToken": "gw-b", "networkKey": "net-b", "bedrockBearer": "bearer-b"}
_ENV = ("ISOL8_OWNER_ID", "OPENCLAW_HOOK_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "AGENTCORE_PROFILE_ID",
        "AGENT_BROWSER_EXECUTABLE_PATH", "AGENT_BROWSER_PROVIDER", "BROWSER_CDP_URL", "HERMES_CUA_DRIVER_CMD",
        "LIGHTBULB_NETWORK_WORKERS", "S3_PREFIX")


def _creds(key, hours=1.0):
    return json.dumps({"accessKeyId": key, "secretAccessKey": "s", "sessionToken": "t",
                       "expiration": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()})


def invoke(tenant, boot=None, token=None, creds=None):
    headers = {"authorization": f"Bearer {token or (boot or {}).get('gatewayToken', '')}", bridge.TENANT_HEADER: tenant}
    if boot:
        headers[bridge.BOOT_HEADER] = json.dumps(boot)
    if creds:
        headers[bridge.AWS_SESSION_HEADER] = creds
    return server.handle_plugin(NOPE, "POST", headers, {})


@pytest.fixture
def shared(monkeypatch):
    """Booted with no owner and no boot secret: every module's OWNER and tokens are empty."""
    for key in _ENV:  # setenv first: a delenv of an absent key records nothing to undo
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    for name, mod in list(sys.modules.items()):
        if (name == "bridge" or name.startswith("bridge.")) and mod is not None:
            for attr, empty in (("OWNER", ""), ("S3_PREFIX", "hermes"), ("HOOK_TOKEN", ""), ("GATEWAY_TOKEN", "")):
                if attr in vars(mod):
                    monkeypatch.setattr(mod, attr, empty)
    monkeypatch.setattr(bridge, "SHARED", True)
    monkeypatch.setattr(bridge, "NETWORK_KEY", "")
    monkeypatch.setattr(bridge, "AWS_SESSION", {})
    for attr in ("MAX_CONCURRENT_TURNS", "_turn_slots", "_compaction_slots"):
        monkeypatch.setattr(turn, attr, getattr(turn, attr))
    monkeypatch.setattr(network, "ensure_started", lambda **_: None)


def test_the_binding_invoke_brings_the_owners_boot_values(shared):
    assert invoke("org_a", BOOT_A)[0] == 404
    assert bridge.OWNER == "org_a" and sync.S3_PREFIX == "org_a/hermes"
    assert bridge.HOOK_TOKEN == "hook-a" and bridge.GATEWAY_TOKEN == "gw-a"
    assert bridge.NETWORK_KEY == "net-a"
    assert os.environ["OPENCLAW_HOOK_TOKEN"] == "hook-a"
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "bearer-a"
    assert os.environ["AGENTCORE_PROFILE_ID"] == "prof-a"
    assert os.environ["BROWSER_CDP_URL"] == "ws://127.0.0.1:9222"
    assert os.environ["LIGHTBULB_NETWORK_WORKERS"] == "1"
    assert turn.MAX_CONCURRENT_TURNS == 6 and turn._turn_slots._initial_value == 6
    assert "GATEWAY_TOKEN" not in os.environ and "gw-a" not in json.dumps(dict(os.environ))  # the shell never sees it


def test_later_invokes_must_present_the_bound_gateway_token(shared):
    invoke("org_a", BOOT_A)
    assert invoke("org_a", token="gw-a")[0] == 404
    assert invoke("org_a", token="forged")[0] == 401
    assert invoke("org_a", dict(BOOT_A, gatewayToken="forged"))[0] == 401  # the boot is not re-read
    assert bridge.GATEWAY_TOKEN == "gw-a"


def test_another_owner_never_reaches_the_bound_session(shared):
    """Owner B's invoke (its own boot and scoped credentials) is refused before anything of it is kept,
    so A's session never holds B's tokens or B's S3/memory credentials."""
    invoke("org_a", BOOT_A, creds=_creds("ASIA-A"))
    assert invoke("org_b", BOOT_B, token="gw-b", creds=_creds("ASIA-B", hours=2))[0] == 401
    assert invoke("org_b", BOOT_B, token="gw-a", creds=_creds("ASIA-B", hours=2)) == (403, {"error": "tenant_mismatch"})
    assert bridge.OWNER == "org_a" and bridge.HOOK_TOKEN == "hook-a" and bridge.NETWORK_KEY == "net-a"
    assert bridge.aws_session_kwargs()["aws_access_key_id"] == "ASIA-A"


def test_a_shared_runtime_without_boot_values_lets_nothing_in_and_stays_unbound(shared):
    assert server.handle_plugin(NOPE, "POST", {"authorization": "Bearer ", bridge.TENANT_HEADER: "org_a"}, {})[0] == 401
    malformed = {"authorization": "Bearer ", bridge.TENANT_HEADER: "org_a", bridge.BOOT_HEADER: '["x"]'}
    assert server.handle_plugin(NOPE, "POST", malformed, {})[0] == 401
    assert bridge.OWNER == ""
    assert invoke("org_a", BOOT_A)[0] == 404  # the next good invoke still binds


def test_a_per_owner_runtime_ignores_the_boot_header(monkeypatch):
    monkeypatch.setattr(network, "ensure_started", lambda **_: None)
    assert invoke("org_test", dict(BOOT_A, gatewayToken="gw-test"))[0] == 404
    assert bridge.HOOK_TOKEN == "hook-test" and bridge.GATEWAY_TOKEN == "gw-test"


def test_an_older_tasks_credentials_never_replace_newer_ones(monkeypatch):
    monkeypatch.setattr(bridge, "AWS_SESSION", {})
    assert bridge.aws_session_due()
    bridge.set_aws_session(_creds("NEW", hours=1))
    bridge.set_aws_session(_creds("OLD", hours=0.5))
    assert bridge.aws_session_kwargs()["aws_access_key_id"] == "NEW"
    assert not bridge.aws_session_due()
    bridge.set_aws_session(_creds("SOON", hours=2))
    monkeypatch.setattr(bridge, "AWS_SESSION", {**bridge.AWS_SESSION, "expires": datetime.now(timezone.utc) + timedelta(minutes=10)})
    assert bridge.aws_session_due()


def test_a_long_runs_heartbeat_asks_for_fresh_credentials(monkeypatch):
    posted = []
    monkeypatch.setattr(bridge, "AWS_SESSION", {})
    monkeypatch.setattr(runs, "_post_event", lambda identity, body: posted.append(body))
    monkeypatch.setattr(turn, "durable_run_status", lambda *a, **k: {"status": "running"} if not posted else {"status": "completed"})
    monkeypatch.setattr(runs, "HEARTBEAT_EVERY_S", 0)
    stop = types.SimpleNamespace(wait=lambda _s: len(posted) > 1)
    runs._heartbeats({"runId": "r"}, stop)
    assert posted == [{"kind": "heartbeat", "status": "running", "recentTools": [], "credentialsDue": True}]


def test_the_screen_driver_uses_the_scoped_credentials(monkeypatch):
    built = {}

    class Client:
        def invoke_browser(self, **_):
            return {"result": {"screenshot": {"data": b"x"}}}

    fake = types.ModuleType("boto3")
    fake.client = lambda service, **kw: (built.update(kw), Client())[1]
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setattr(browser_control, "session_ref", lambda: {"browserIdentifier": "b", "sessionId": "s"})
    monkeypatch.setattr(bridge, "AWS_SESSION", {})
    bridge.set_aws_session(_creds("ASIA-A"))
    browser_control.invoke({"screenshot": {}})
    assert built["aws_access_key_id"] == "ASIA-A"


def test_a_shared_runtime_refuses_an_invoke_that_names_no_tenant(shared):
    """Unbound and untenanted it would run as owner "" -- network keys at /network/..., outside every prefix."""
    assert server.handle_plugin(NOPE, "POST", {"authorization": "Bearer "}, {})[0] in (401, 403)
    invoke("org_a", BOOT_A)
    assert bridge.bind_tenant(None) is True  # bound: the session is org_a's whatever the header


def test_an_unbound_shared_runtime_refuses_untenanted_even_with_a_boot(shared):
    headers = {"authorization": "Bearer gw-a", bridge.BOOT_HEADER: json.dumps(BOOT_A)}
    assert server.handle_plugin(NOPE, "POST", headers, {}) == (401, {"error": "unauthorized"})
    assert bridge.OWNER == "" and bridge.GATEWAY_TOKEN == ""


def test_a_shared_runtime_never_falls_back_to_its_execution_role(shared, monkeypatch):
    monkeypatch.setattr(sync, "_S3_CLIENT", None)
    fake = types.ModuleType("boto3")
    fake.client = lambda *a, **k: pytest.fail("a client on the execution role")
    monkeypatch.setitem(sys.modules, "boto3", fake)
    with pytest.raises(bridge.NoScopedCredentials):
        sync._s3()
    bridge.set_aws_session(_creds("OLD", hours=-0.1))  # expired: the same refusal
    with pytest.raises(bridge.NoScopedCredentials):
        bridge.aws_session_kwargs()


def test_a_per_owner_runtime_still_falls_back_to_its_role(monkeypatch):
    monkeypatch.setattr(bridge, "AWS_SESSION", {})
    assert bridge.SHARED is False and bridge.aws_session_kwargs() == {}
