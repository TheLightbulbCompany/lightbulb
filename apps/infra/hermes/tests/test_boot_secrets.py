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

_BRIDGE_MODULES = ("bridge.server", "bridge.turn", "bridge.sync", "bridge")


def test_the_boot_fetch_reaches_every_reader_module_on_a_fresh_import(monkeypatch):
    fake_boto3 = types.ModuleType("boto3")

    class FakeSecretsManager:
        def get_secret_value(self, SecretId):
            assert SecretId == "lightbulb/dev/hermes/org_test"
            return {"SecretString": json.dumps(
                {"hookToken": "boot-hook", "gatewayToken": "boot-gw", "bedrockBearer": "boot-bearer"}
            )}

    fake_boto3.client = lambda name: FakeSecretsManager()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("LIGHTBULB_SECRET_ID", "lightbulb/dev/hermes/org_test")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)

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
        assert turn.HOOK_TOKEN == "boot-hook", "turn.py captured a stale value, not the boot fetch"
        assert server.GATEWAY_TOKEN == "boot-gw", "server.py captured a stale value, not the boot fetch"
        assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "boot-bearer"
        assert os.environ["OPENCODE_ZEN_API_KEY"] == "boot-bearer"
        # M2-R12: lightbulb_node reads OPENCLAW_HOOK_TOKEN off the process env directly (it has
        # no per-turn context file -- exec isn't turn-scoped), so the boot fetch must reach that
        # env var too, not just the `bridge.HOOK_TOKEN` module attribute.
        assert os.environ["OPENCLAW_HOOK_TOKEN"] == "boot-hook"
        assert os.environ["GATEWAY_TOKEN"] == "boot-gw"
    finally:
        # Leave every other test's already-imported bridge modules exactly as they were.
        for name in _BRIDGE_MODULES:
            sys.modules.pop(name, None)
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod
        os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
        os.environ.pop("OPENCODE_ZEN_API_KEY", None)
        os.environ.pop("OPENCLAW_HOOK_TOKEN", None)
        os.environ.pop("GATEWAY_TOKEN", None)
