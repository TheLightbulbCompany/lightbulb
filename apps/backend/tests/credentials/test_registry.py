import pytest
from core.services.credentials import registry


def test_loads_known_providers():
    providers = registry.all_providers()
    assert "stripe" in providers
    assert "google" in providers


def test_paste_key_provider_shape():
    p = registry.get_provider("stripe")
    assert p.acquisition == "paste_token"
    assert p.actor == "mcp"
    assert p.refresh_owner == "none"
    assert p.wiring.mcp_server.key == "stripe"
    assert p.wiring.mcp_server.env == {"STRIPE_SECRET_KEY": "input.token"}


def test_parallel_openclaw_paths_provider_shape():
    p = registry.get_provider("parallel")
    assert p.acquisition == "paste_token"
    assert p.refresh_owner == "none"
    # web-search provider plugin — rides the openclaw_paths rail, not mcp/cli.
    assert p.wiring.mcp_server is None
    assert p.wiring.materializer is None
    assert {op.path: op.source for op in (p.wiring.openclaw_paths or [])} == {
        "tools.web.search.provider": "literal:parallel-free",
        "tools.web.search.parallel.apiKey": "input.token",
    }
    # No validate endpoint (key checked at first search); public + ungated.
    assert p.validation is None


def test_direct_oauth_provider_shape():
    p = registry.get_provider("google")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "cli"
    assert p.refresh_owner == "actor"
    assert p.oauth.token_url == "https://oauth2.googleapis.com/token"
    assert "https://www.googleapis.com/auth/gmail.modify" in p.oauth.scopes
    assert p.wiring.materializer == "gog_cli"


def test_google_is_byo_required():
    from core.services.credentials import registry

    g = registry.get_provider("google")
    assert g.byo_required is True
    assert g.byo_setup_url == "https://console.cloud.google.com/apis/credentials"
    assert not hasattr(g, "allow_byo_app")


def test_no_provider_is_byo_required_except_google():
    from core.services.credentials import registry

    byo = [k for k, v in registry.all_providers().items() if v.byo_required]
    assert byo == ["google"]


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        registry.get_provider("does-not-exist")


def test_validate_key_maps_to_validation_field():
    p = registry.get_provider("stripe")
    assert p.validation is not None
    assert p.validation.method == "GET"
    assert p.validation.url == "https://api.stripe.com/v1/account"


def test_stripe_validate_has_no_body():
    p = registry.get_provider("stripe")
    assert p.validation.method == "GET"
    assert p.validation.body is None


def test_validate_auth_scheme_is_per_provider():
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


def test_stripe_is_mcp_server():
    from core.services.credentials.registry import get_provider

    w = get_provider("stripe").wiring
    assert w.mcp_server is not None and w.mcp_server.key == "stripe"
    assert w.mcp_server.env == {"STRIPE_SECRET_KEY": "input.token"}


# Linear re-enters the registry on the mcp_server rail with a real command, so
# the previous "Linear is gone" expectation no longer holds — it must load and
# carry the bare-key (no-Bearer) validate scheme its personal API tokens need.
def test_linear_is_back_on_mcp_server_rail():
    from core.services.credentials.registry import all_providers, get_provider

    assert "linear" in all_providers()
    p = get_provider("linear")
    assert p.wiring.mcp_server is not None
    assert p.wiring.mcp_server.command == "npx"
    assert p.validation.auth_scheme == ""


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


# The four curated mcp_server integrations share a shape: paste-token / mcp /
# no-refresh, one mcp.servers.<key> they own, and a validate spec. The per-row
# expectations below pin the bits that are unique per provider (env keys, the
# success-field/header/scheme that a wrong value would silently break).
_CURATED_MCP = {
    "notion": {
        "mcp_key": "notion",
        "env_keys": {"NOTION_TOKEN"},
        "success_json_field": None,
        "headers": {"Notion-Version": "2022-06-28"},
        "auth_scheme": "Bearer",
    },
    "sentry": {
        "mcp_key": "sentry",
        "env_keys": {"SENTRY_ACCESS_TOKEN"},
        "success_json_field": None,
        "headers": {},
        "auth_scheme": "Bearer",
    },
    "linear": {
        "mcp_key": "linear",
        "env_keys": {"LINEAR_API_TOKEN"},
        "success_json_field": "data.viewer.id",
        "headers": {},
        "auth_scheme": "",
    },
}


@pytest.mark.parametrize("key", sorted(_CURATED_MCP))
def test_curated_mcp_provider_shape(key):
    expected = _CURATED_MCP[key]
    p = registry.get_provider(key)
    assert p.acquisition == "paste_token"
    assert p.actor == "mcp"
    assert p.refresh_owner == "none"
    assert p.wiring.mcp_server is not None
    assert p.wiring.mcp_server.key == expected["mcp_key"]
    assert set(p.wiring.mcp_server.env) == expected["env_keys"]
    assert p.validation is not None
    assert p.validation.success_json_field == expected["success_json_field"]
    assert p.validation.headers == expected["headers"]
    assert p.validation.auth_scheme == expected["auth_scheme"]


def test_github_is_on_materializer_rail():
    # GitHub uses the gh CLI (baked into the image) via the materializer rail —
    # a paste-token PAT seeded into gh's hosts.yml, not an mcp_server.
    p = registry.get_provider("github")
    assert p.acquisition == "paste_token"
    assert p.actor == "cli"
    assert p.refresh_owner == "none"
    assert p.requires.bins == ["gh"]
    assert p.wiring.materializer == "gh_cli"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    # A bad PAT 401s, so status-based validate (no success_json_field) is enough;
    # the pinned API-version header must still ride every call.
    assert p.validation is not None
    assert p.validation.url == "https://api.github.com/user"
    assert p.validation.auth_scheme == "Bearer"
    assert p.validation.success_json_field is None
    assert p.validation.headers == {"X-GitHub-Api-Version": "2022-11-28"}


def test_mcp_server_accepts_remote_shape():
    # A remote streamable-http server is expressible with transport+url and no command.
    from core.services.credentials.registry import McpServer

    s = McpServer(key="attio", transport="streamable-http", url="https://mcp.attio.com/mcp")
    assert s.command is None
    assert s.transport == "streamable-http"
    assert s.url == "https://mcp.attio.com/mcp"


def test_mcp_server_rejects_neither_stdio_nor_remote():
    from pydantic import ValidationError

    from core.services.credentials.registry import McpServer

    # No command and no transport+url — neither shape is satisfied.
    with pytest.raises(ValidationError):
        McpServer(key="x")
    # A url without a transport is still not a complete remote shape.
    with pytest.raises(ValidationError):
        McpServer(key="x", url="https://h/mcp")


def test_mcp_server_rejects_both_stdio_and_remote():
    from pydantic import ValidationError

    from core.services.credentials.registry import McpServer

    with pytest.raises(ValidationError):
        McpServer(key="x", command="npx", transport="streamable-http", url="https://h/mcp")


def test_attio_is_remote_mcp_oauth_provider():
    # Attio rides the materializer rail (mcp_oauth) with its remote-server metadata
    # under mcp_remote, keeping the exactly-one-of-three wiring invariant intact.
    p = registry.get_provider("attio")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "mcp"
    assert p.refresh_owner == "actor"
    assert p.wiring.materializer == "mcp_oauth"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    # DCR endpoint + public-client scopes.
    assert p.oauth.registration_url == "https://app.attio.com/oauth/register"
    assert p.oauth.scopes == ["openid", "offline_access", "mcp"]
    assert p.oauth.pkce is True
    # Not BYO — DCR mints the client, so no owner-provided app is required.
    assert p.byo_required is False
    # Remote-server metadata lives at the provider level.
    assert p.mcp_remote is not None
    assert p.mcp_remote.key == "attio"
    assert p.mcp_remote.url == "https://mcp.attio.com/mcp"
    assert p.mcp_remote.transport == "streamable-http"


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
