"""Server-owned Paperclip adapter configuration.

There is ONE adapter shape: the native ``hermes_gateway``, pointed at the hook-token
authenticated backend facade. The device-paired ``openclaw_gateway`` synthesizer this module
also built — its ws URL allowlist, its per-agent operator keypair, its claimed-api-key path,
its fallback session key — went with the OpenClaw gateway, and so did the ~20 tests over it.
``adapter_type_for`` went too: there is nothing to choose between.
"""

import pytest

from core.config import settings
from core.services import paperclip_adapter_config as cfg_module
from core.services.paperclip_adapter_config import AdapterConfigError, HERMES_GATEWAY_TYPE


def test_the_canonical_adapter_type_is_the_native_one():
    assert HERMES_GATEWAY_TYPE == "hermes_gateway"


def test_the_config_points_at_the_native_backend_facade(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api-dev.flickmeon.ai")
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.internal:3100")
    built = cfg_module.build_hermes_gateway_adapter_config("org_1", hook_token="hook-abc")
    assert built == {
        "apiBaseUrl": "https://api-dev.flickmeon.ai/api/v1/internal/paperclip/hermes/org_1",
        "apiKey": "hook-abc",
        "sessionKeyStrategy": "issue",
        "timeoutSec": 900,
        # Both sit at the native adapter's own clamp ceiling on purpose: every poll and every
        # reconnect is an InvokeAgentRuntime into the owner's microVM, and the adapter's
        # defaults (2s / 1s) would cost ~900 invokes on one 900s run instead of ~90.
        "eventReconnectMs": 30_000,
        "pollIntervalMs": 10_000,
        "paperclipApiUrl": "http://paperclip.internal:3100",
    }


def test_the_config_rejects_an_empty_hook_token(monkeypatch):
    """M4-R33: a seed racing ahead of prepare_hermes_workspace would otherwise mint an adapter
    that sends a bare bearer and 401s at the facade on EVERY run -- reported by Paperclip as a
    per-run failure rather than as a broken agent. It must fail loudly at WIRING time."""
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "http://paperclip.internal:3100")
    with pytest.raises(AdapterConfigError, match="hook_token"):
        cfg_module.build_hermes_gateway_adapter_config("org_1", hook_token="")


def test_the_config_strips_a_trailing_slash_on_the_base_url(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api-dev.flickmeon.ai/")
    monkeypatch.setattr(settings, "PAPERCLIP_INTERNAL_URL", "")
    built = cfg_module.build_hermes_gateway_adapter_config("org_1", hook_token="hook-abc")
    assert built["apiBaseUrl"] == "https://api-dev.flickmeon.ai/api/v1/internal/paperclip/hermes/org_1"
    assert "paperclipApiUrl" not in built


def test_the_module_offers_no_transport_choice():
    """A second adapter type or a chooser coming back would mean a branch with one live side."""
    for gone in ("synthesize_openclaw_adapter", "adapter_type_for", "OPENCLAW_GATEWAY_TYPE", "build_gateway_url_re"):
        assert not hasattr(cfg_module, gone), gone
