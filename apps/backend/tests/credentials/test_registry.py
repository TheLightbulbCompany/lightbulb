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


def test_validate_key_maps_to_validation_field():
    p = registry.get_provider("linear")
    assert p.validation is not None
    assert p.validation.method == "GET"
    assert p.validation.url == "https://api.linear.app/graphql"


def test_validate_auth_scheme_is_per_provider():
    # Linear personal API keys are sent as a BARE Authorization header (no "Bearer").
    assert registry.get_provider("linear").validation.auth_scheme == ""
    # Stripe accepts the default Bearer scheme.
    assert registry.get_provider("stripe").validation.auth_scheme == "Bearer"


def test_wiring_rejects_neither_or_both():
    from pydantic import ValidationError
    from core.services.credentials.registry import Wiring, OpenclawPath

    with pytest.raises(ValidationError):
        Wiring()  # neither
    with pytest.raises(ValidationError):
        Wiring(
            openclaw_paths=[OpenclawPath(path="a.b", source="input.token")],
            materializer="x",
        )  # both


def test_direct_oauth_requires_oauth_block():
    from pydantic import ValidationError
    from core.services.credentials.registry import Provider, Wiring

    with pytest.raises(ValidationError):
        Provider(
            key="bad",
            display_name="Bad",
            acquisition="direct_oauth",
            actor="cli",
            refresh_owner="actor",
            wiring=Wiring(materializer="x"),
        )  # no oauth


def test_registry_materializers_and_oauth_are_self_consistent():
    from core.services.credentials.materializers import MATERIALIZERS

    for key, p in registry.all_providers().items():
        if p.acquisition == "direct_oauth":
            assert p.oauth and p.oauth.scopes, f"{key}: direct_oauth missing oauth scopes"
        if p.wiring.materializer:
            assert p.wiring.materializer in MATERIALIZERS, f"{key}: unknown materializer {p.wiring.materializer!r}"
