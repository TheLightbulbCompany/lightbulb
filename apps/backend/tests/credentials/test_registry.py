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
    assert p.wiring.materializer == "gog_cli"


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        registry.get_provider("does-not-exist")


def test_validate_key_maps_to_validation_field():
    p = registry.get_provider("linear")
    assert p.validation is not None
    # Linear's GraphQL endpoint requires POST + a query body — a bare GET rejects
    # valid personal API keys.
    assert p.validation.method == "POST"
    assert p.validation.url == "https://api.linear.app/graphql"
    assert p.validation.body == {"query": "{ viewer { id } }"}


def test_stripe_validate_has_no_body():
    p = registry.get_provider("stripe")
    assert p.validation.method == "GET"
    assert p.validation.body is None


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


def test_mcp_server_wiring_parses():
    from core.services.credentials.registry import Provider

    p = Provider(
        key="stripe",
        display_name="Stripe",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring={
            "mcp_server": {
                "key": "stripe",
                "command": "npx",
                "args": ["-y", "@stripe/mcp", "--tools=all"],
                "env": {"STRIPE_SECRET_KEY": "input.token"},
            }
        },
    )
    assert p.wiring.mcp_server.command == "npx"
    assert p.wiring.mcp_server.env == {"STRIPE_SECRET_KEY": "input.token"}
    assert p.wiring.openclaw_paths is None and p.wiring.materializer is None


def test_wiring_accepts_exactly_one_of_three():
    from pydantic import ValidationError

    from core.services.credentials.registry import McpServer, Wiring

    # zero set → reject
    with pytest.raises(ValidationError):
        Wiring()
    # two set → reject (this is the case the OLD 2-way validator could not catch)
    with pytest.raises(ValidationError):
        Wiring(
            materializer="gog_cli",
            mcp_server=McpServer(key="x", command="npx", args=[], env={"K": "input.token"}),
        )


def test_registry_materializers_and_oauth_are_self_consistent():
    from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS

    for key, p in registry.all_providers().items():
        if p.acquisition == "direct_oauth":
            assert p.oauth and p.oauth.scopes, f"{key}: direct_oauth missing oauth scopes"
        if p.wiring.materializer:
            assert p.wiring.materializer in MATERIALIZERS, f"{key}: unknown materializer {p.wiring.materializer!r}"
            # A materializer without a matching remover would silently skip
            # on-disk cleanup at disconnect, leaving credential residue on EFS.
            assert p.wiring.materializer in MATERIALIZER_REMOVERS, (
                f"{key}: materializer {p.wiring.materializer!r} has no remover"
            )
