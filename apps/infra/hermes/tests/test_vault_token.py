"""The website-login vault token reaches the bridge only on the backend's gateway-authenticated
invokes, and lives in module memory -- never os.environ, where the agent's shell would see it."""
import os

import bridge
from bridge import server, website_logins


def test_an_authorized_invoke_hands_the_bridge_its_vault_token(monkeypatch):
    monkeypatch.setattr(bridge, "VAULT_TOKEN", "")
    unauth = {"Authorization": "Bearer wrong", "X-Lightbulb-Vault": "forged"}
    assert server.handle_plugin("/plugins/lightbulb-channel/nope", "POST", unauth, {})[0] == 401
    assert bridge.VAULT_TOKEN == ""

    auth = {"authorization": f"Bearer {bridge.GATEWAY_TOKEN}", "x-lightbulb-vault": "vault-1"}
    server.handle_plugin("/plugins/lightbulb-channel/nope", "POST", auth, {})
    assert bridge.VAULT_TOKEN == "vault-1"
    assert "vault-1" not in os.environ.values()

    # An invoke without the header leaves the held token alone.
    server.handle_plugin("/plugins/lightbulb-channel/nope", "POST", {"Authorization": auth["authorization"]}, {})
    assert bridge.VAULT_TOKEN == "vault-1"


def test_backend_calls_present_the_token_held_at_call_time(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(website_logins.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bridge, "VAULT_TOKEN", "vault-2")
    website_logins._call("GET", "/requests/x")
    assert seen["auth"] == "Bearer vault-2"
