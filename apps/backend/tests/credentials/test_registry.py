import pytest
from core.services.credentials import registry


def test_loads_known_providers():
    providers = registry.all_providers()
    assert "linear" in providers
    assert "google" in providers


def test_paste_key_provider_shape():
    p = registry.get_provider("linear")
    assert p.acquisition == "paste_token"
    assert p.actor == "mcp"
    assert p.refresh_owner == "none"
    assert p.wiring.openclaw_paths[0].path == "mcp.servers.linear.env.LINEAR_API_KEY"
    assert p.wiring.openclaw_paths[0].source == "input.token"


def test_direct_oauth_provider_shape():
    p = registry.get_provider("google")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "cli"
    assert p.refresh_owner == "actor"
    assert p.oauth.token_url == "https://oauth2.googleapis.com/token"
    assert "https://www.googleapis.com/auth/gmail.modify" in p.oauth.scopes
    assert p.wiring.materializer == "gog_keyring"


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        registry.get_provider("does-not-exist")
