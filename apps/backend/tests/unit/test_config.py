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

from core.config import Settings, apex_domain  # noqa: E402


@patch.dict(os.environ, {"GOG_KEYRING_PASSWORD": "s3cr3t-keyring-pw"})
def test_gog_keyring_password_env_var_loaded():
    """The gog_cli materializer reads GOG_KEYRING_PASSWORD to encrypt the
    owner's Google OAuth keyring with the same value the per-user containers
    receive; must be loadable from the env CDK injects it through."""
    settings = Settings()
    assert settings.GOG_KEYRING_PASSWORD == "s3cr3t-keyring-pw"


def test_workspace_price_settings_have_expected_defaults():
    s = Settings()
    assert s.STRIPE_WORKSPACE_PRICE_ID == ""
    assert s.WORKSPACE_PRICE_USD == 50
    assert s.ULTRA_PRICE_USD == 150


def test_apex_domain_collapses_subdomains():
    """The cookie scope, the same-site return-URL allowlist and the SES parent
    identity all key on the registrable apex, not on a brand literal."""
    assert apex_domain("isol8.co") == "isol8.co"
    assert apex_domain("dev.isol8.co") == "isol8.co"
    assert apex_domain("company-dev.isol8.co") == "isol8.co"
    assert apex_domain("A.B.Example.AI") == "example.ai"
    assert apex_domain("  dev.isol8.co.  ") == "isol8.co"
    assert apex_domain("") == ""
    assert apex_domain("localhost") == "localhost"


def test_root_domain_is_the_apex_of_frontend_url():
    """Same apex in every env — dev serves from a subdomain of it, prod from it."""
    dev = Settings(FRONTEND_URL="https://dev.isol8.co")
    prod = Settings(FRONTEND_URL="https://isol8.co")
    assert (dev.frontend_host, dev.root_domain) == ("dev.isol8.co", "isol8.co")
    assert (prod.frontend_host, prod.root_domain) == ("isol8.co", "isol8.co")


def test_owner_state_bucket_reads_its_own_env_var():
    """ONE name for the owner-state bucket. It used to fall back to ``LITESTREAM_BUCKET``
    because CDK set only that one; CDK now sets both to the same value, so the alias and its
    validator are deleted -- and PR-6 drops the CDK line."""
    assert Settings(OWNER_STATE_BUCKET="lightbulb-dev-openclaw-replicas").OWNER_STATE_BUCKET == (
        "lightbulb-dev-openclaw-replicas"
    )
    assert Settings().OWNER_STATE_BUCKET == ""
