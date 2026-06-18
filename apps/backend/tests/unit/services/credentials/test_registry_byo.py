import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.credentials import registry  # noqa: E402


def test_google_allows_byo_app():
    google = registry.get_provider("google")
    assert google.allow_byo_app is True
    assert google.byo_setup_url  # a help link is set


def test_byo_defaults_false_for_other_providers():
    assert registry.get_provider("stripe").allow_byo_app is False
