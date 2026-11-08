"""Cross-module proof of the bridge/__init__.py boot-fetch wiring.

test_sync.py only unit-tests load_boot_secrets/is_excluded in isolation. What actually
matters -- every reader (turn.py's HOOK_TOKEN, server.py's GATEWAY_TOKEN) observes the
boot-fetched value, not the plain env-var default it would otherwise capture at its own
`from bridge import ...` -- is only true because bridge/__init__.py's boot-fetch runs
before those submodules are ever imported. This test forces a real, fresh import of the
whole `bridge` package (as an actual process boot would do) with LIGHTBULB_SECRET_ID set,
to prove that ordering holds, instead of asserting it only in an uncommitted script. A
future refactor that reorders bridge/__init__.py (e.g. moves the boot-fetch above where
HOOK_TOKEN/GATEWAY_TOKEN are first assigned, or adds an earlier import of bridge.turn)
would fail this test.
"""
import importlib
import json
import os
import sys
import types

_BRIDGE_MODULES = ("bridge.server", "bridge.turn", "bridge.website_logins", "bridge.sync", "bridge")


def test_the_boot_fetch_reaches_every_reader_module_on_a_fresh_import(monkeypatch):
    fake_boto3 = types.ModuleType("boto3")

    class FakeSecretsManager:
        def get_secret_value(self, SecretId):
            assert SecretId == "lightbulb/dev/hermes/org_test"
            # providerSwitchFrozen/providerSwitchGeneration: an OLD backend may still write
            # these into the boot secret (Task 11 retired the gate they used to drive) --
            # they must be accepted and ignored, not raise on an unrecognized key.
            return {"SecretString": json.dumps(
                {"hookToken": "boot-hook", "gatewayToken": "boot-gw", "bedrockBearer": "boot-bearer",
                 "vaultToken": "boot-vault",
                 "providerChoice": "openrouter_managed", "providerSwitchFrozen": True,
                 "providerSwitchGeneration": "gen-new"}
            )}

    fake_boto3.client = lambda name: FakeSecretsManager()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("LIGHTBULB_SECRET_ID", "lightbulb/dev/hermes/org_test")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    # Force a real re-import of the whole package graph -- a cached `bridge` module (from
    # an earlier test file's `from bridge import server`) would never re-run the boot-fetch.
    saved = {name: sys.modules.get(name) for name in _BRIDGE_MODULES}
    for name in _BRIDGE_MODULES:
        sys.modules.pop(name, None)
    try:
        bridge = importlib.import_module("bridge")
        turn = importlib.import_module("bridge.turn")
        server = importlib.import_module("bridge.server")

        assert bridge.HOOK_TOKEN == "boot-hook"
        assert bridge.GATEWAY_TOKEN == "boot-gw"
        assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "boot-bearer"
        # An old boot secret's providerSwitchFrozen/providerSwitchGeneration are ignored
        # outright (Task 11): no env export, and turns still admit.
        assert "LIGHTBULB_PROVIDER_SWITCH_GENERATION" not in os.environ
        assert "LIGHTBULB_PROVIDER_SWITCH_FROZEN" not in os.environ
        assert turn.reserve_turn() is True
        turn.release_turn()
        # M2-R12: lightbulb_node reads OPENCLAW_HOOK_TOKEN off the process env directly (it has
        # no per-turn context file -- exec isn't turn-scoped), so the boot fetch must reach that
        # env var too, not just the `bridge.HOOK_TOKEN` module attribute.
        assert os.environ["OPENCLAW_HOOK_TOKEN"] == "boot-hook"
        assert "GATEWAY_TOKEN" not in os.environ   # the shell must not inherit it (audit P1-3)
        # An old boot secret's vaultToken is ignored: the execution role can read that secret
        # (and the shell the role, over the metadata service). The token rides the backend's
        # invokes instead (test_vault_token.py).
        assert bridge.VAULT_TOKEN == ""
        assert "boot-vault" not in os.environ.values()
    finally:
        # Leave every other test's already-imported bridge modules exactly as they were.
        for name in _BRIDGE_MODULES:
            sys.modules.pop(name, None)
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod
        os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
        os.environ.pop("LIGHTBULB_PROVIDER_SWITCH_FROZEN", None)
        os.environ.pop("LIGHTBULB_PROVIDER_SWITCH_GENERATION", None)
        os.environ.pop("OPENCLAW_HOOK_TOKEN", None)
        os.environ.pop("GATEWAY_TOKEN", None)


def test_the_gate_bearer_resolves_for_config_but_never_reaches_a_shell_child(home, monkeypatch):
    """Audit P1-3: every secret-scope name was registered for terminal passthrough, the owner's
    gate bearer included -- exfiltrated, it spends managed credits from anywhere."""
    import types

    from bridge import turn

    scope = {}
    fake = types.ModuleType("agent.secret_scope")
    fake.build_profile_secret_scope = lambda path: {"PAPERCLIP_API_KEY": "pk"}
    fake.set_secret_scope = lambda secrets: scope.update(secrets) or "tok"
    fake.reset_secret_scope = lambda token: None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "owner-bearer")
    declared, allowed = [], []
    monkeypatch.setattr(turn, "_declare_child_env_names", lambda names: declared.extend(names))
    monkeypatch.setattr(turn, "_allow_child_env_names", lambda names: allowed.extend(names))
    with turn._secret_scope("ceo"):
        pass
    assert scope["LIGHTBULB_GATE_BEARER"] == "owner-bearer"          # ${LIGHTBULB_GATE_BEARER} still resolves
    assert "LIGHTBULB_GATE_BEARER" not in declared + allowed
    assert "PAPERCLIP_API_KEY" in declared and "PAPERCLIP_API_KEY" in allowed
