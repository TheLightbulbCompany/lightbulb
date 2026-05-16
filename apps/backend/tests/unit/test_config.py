"""Tests for core.config Settings.

Each test instantiates a fresh `Settings()` locally rather than reloading the
`core.config` module. Reloading mutates the module-level `settings` singleton
that other tests in the same pytest session import; that pollution caused
unrelated suites (e.g. test_workspace_catalog_helpers) to fail when they ran
after this file. Local-instantiation keeps the global pristine.
"""

import os

# Match the codebase pattern (e.g. test_admin_org_resolution.py): seed CLERK_ISSUER
# before any `core.*` import so Settings() instantiation in conftest doesn't fail
# when this file is run in isolation.
os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import patch  # noqa: E402

from core.config import Settings  # noqa: E402


@patch.dict(os.environ, {"MARKETPLACE_LISTINGS_TABLE": "isol8-dev-marketplace-listings"})
def test_marketplace_listings_table_env_var_loaded():
    settings = Settings()
    assert settings.MARKETPLACE_LISTINGS_TABLE == "isol8-dev-marketplace-listings"


@patch.dict(os.environ, {"MARKETPLACE_ARTIFACTS_BUCKET": "isol8-dev-marketplace-artifacts"})
def test_marketplace_artifacts_bucket_env_var_loaded():
    settings = Settings()
    assert settings.MARKETPLACE_ARTIFACTS_BUCKET == "isol8-dev-marketplace-artifacts"


@patch.dict(os.environ, {"STRIPE_CONNECT_REFRESH_URL": "https://example.com/refresh"})
def test_stripe_connect_refresh_url_loaded():
    settings = Settings()
    assert settings.STRIPE_CONNECT_REFRESH_URL == "https://example.com/refresh"


def test_ws_gateway_url_default_is_wss():
    """WS_GATEWAY_URL must default to a wss:// endpoint so desktop pair flow
    returns a usable URL even when the env var is unset."""
    # Ensure env is clean for this assertion — fresh Settings() reads from env.
    env = {k: v for k, v in os.environ.items() if k != "WS_GATEWAY_URL"}
    with patch.dict(os.environ, env, clear=True):
        settings = Settings()
        assert settings.WS_GATEWAY_URL.startswith("wss://")


@patch.dict(os.environ, {"WS_GATEWAY_URL": "wss://override.example.com"})
def test_ws_gateway_url_overrides_via_env():
    """WS_GATEWAY_URL is consumed by POST /api/v1/desktop/pair to tell desktop
    clients which gateway endpoint to connect to; must be overridable via env."""
    settings = Settings()
    assert settings.WS_GATEWAY_URL == "wss://override.example.com"
