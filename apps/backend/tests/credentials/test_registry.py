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


def test_posthog_oauth_requests_full_scope_set():
    # The hosted PostHog MCP probes GET /api/users/@me/ on startup (needs user:read)
    # and gates each tool's scope at call time, so we request its complete published
    # scope set — openid + every read + every write — to cover all tools in one
    # consent. A list missing user:read is the documented startup insufficient_scope.
    p = registry.get_provider("posthog")
    scopes = p.oauth.scopes
    assert "user:read" in scopes  # the scope whose absence produced the 403
    assert "openid" in scopes
    reads = [s for s in scopes if s.endswith(":read")]
    writes = [s for s in scopes if s.endswith(":write")]
    assert len(reads) == 92
    assert len(writes) == 92
    # read/write are paired per resource: every read scope has a write twin.
    assert {s[: -len(":read")] for s in reads} == {s[: -len(":write")] for s in writes}
    assert len(scopes) == 185
    assert len(scopes) == len(set(scopes))  # no duplicate scope slipped into the yaml


def test_google_is_byo_required():
    from core.services.credentials import registry

    g = registry.get_provider("google")
    assert g.byo_required is True
    assert g.byo_setup_url == "https://console.cloud.google.com/apis/credentials"
    assert not hasattr(g, "allow_byo_app")


def test_only_google_and_x_are_byo_required():
    from core.services.credentials import registry

    byo = [k for k, v in registry.all_providers().items() if v.byo_required]
    assert byo == ["google", "x"]


def test_x_provider_shape():
    p = registry.get_provider("x")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "cli"
    assert p.refresh_owner == "actor"
    assert p.byo_required is True
    assert p.byo_setup_url == "https://developer.x.com/en/portal/dashboard"
    assert p.requires.bins == ["xurl"]
    assert p.oauth.authorization_url == "https://x.com/i/oauth2/authorize"
    assert p.oauth.token_url == "https://api.x.com/2/oauth2/token"
    assert "tweet.write" in p.oauth.scopes
    assert "offline.access" in p.oauth.scopes
    assert p.wiring.materializer == "xurl_cli"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None


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


# Linear, Notion, and Sentry deliver their hosted MCP over OAuth rather than a
# local npx stdio process, so they ride the mcp_oauth materializer rail (their
# remote-server metadata under mcp_remote), like Attio.
@pytest.mark.parametrize(
    "key,host",
    [
        ("notion", "https://mcp.notion.com"),
        ("linear", "https://mcp.linear.app"),
        ("sentry", "https://mcp.sentry.dev"),
        ("intercom", "https://mcp.intercom.com"),
    ],
)
def test_hosted_oauth_mcp_provider_shape(key, host):
    from core.services.credentials.registry import all_providers, get_provider

    assert key in all_providers()
    p = get_provider(key)
    assert p.acquisition == "direct_oauth"
    assert p.actor == "mcp"
    assert p.refresh_owner == "actor"
    # Rides the mcp_oauth materializer rail; remote-server metadata under mcp_remote.
    assert p.wiring.materializer == "mcp_oauth"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    assert p.mcp_remote is not None
    assert p.mcp_remote.key == key
    assert p.mcp_remote.url == f"{host}/mcp"
    assert p.mcp_remote.transport == "streamable-http"
    # DCR mints a public PKCE client; not BYO, no local validate spec.
    assert p.oauth.registration_url is not None
    assert p.oauth.pkce is True
    assert p.oauth.scopes  # non-empty
    assert p.byo_required is False
    assert p.validation is None


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


def test_posthog_mcp_remote_uses_cli_mode_and_list_timeout():
    # PostHog's raw toolset (500+ tools, ~657K tokens of schemas) overflows a
    # 272K-context model on its own, so the registry MUST pin ?mode=cli — the
    # server then exposes one exec tool with on-demand schema discovery. The
    # published block must also carry requestTimeoutMs: without an explicit
    # request timeout OpenClaw caps catalog tools/list at 1.5s, which silently
    # excludes the server under CPU contention.
    p = registry.get_provider("posthog")
    assert p.mcp_remote is not None
    assert p.mcp_remote.url == "https://mcp.posthog.com/mcp?mode=cli"
    assert p.mcp_remote.server_block() == {
        "transport": "streamable-http",
        "url": "https://mcp.posthog.com/mcp?mode=cli",
        "auth": "oauth",
        "requestTimeoutMs": 60000,
    }


def test_skill_spec_model_shape():
    # A SkillSpec carries what a paste-token CLI integration's materializer needs:
    # the skills.entries.<key> it owns, the env var the credential is injected as,
    # and the EXACT-pinned npm package the skill's SKILL.md tells the agent to npx.
    from core.services.credentials.registry import SkillSpec

    s = SkillSpec(key="vercel", primaryEnv="VERCEL_TOKEN", package="vercel@39.1.3")
    assert s.key == "vercel"
    # The `primaryEnv` alias (YAML camelCase) populates the `primary_env` field.
    assert s.primary_env == "VERCEL_TOKEN"
    assert s.package == "vercel@39.1.3"
    # populate_by_name also accepts the field name directly (Python construction).
    s2 = SkillSpec(key="vercel", primary_env="VERCEL_TOKEN", package="vercel@39.1.3")
    assert s2.primary_env == "VERCEL_TOKEN"


def test_provider_carries_optional_skill_block():
    # A paste-token CLI integration that delivers a bundled skill rides the
    # skill_cli materializer and carries its SkillSpec at the provider level.
    from core.services.credentials.registry import Provider

    p = Provider(
        key="vercel_cli",
        display_name="Vercel",
        acquisition="paste_token",
        actor="skill",
        refresh_owner="none",
        wiring={"materializer": "skill_cli"},
        skill={"key": "vercel", "primaryEnv": "VERCEL_TOKEN", "package": "vercel@39.1.3"},
        validate={"method": "GET", "url": "https://api.vercel.com/v2/user"},
    )
    assert p.skill is not None
    assert p.skill.key == "vercel"
    assert p.skill.primary_env == "VERCEL_TOKEN"
    assert p.skill.package == "vercel@39.1.3"
    # Validate by API call, never by token prefix.
    assert p.validation is not None
    assert p.validation.url == "https://api.vercel.com/v2/user"
    assert p.validation.token_prefix is None


def test_provider_skill_defaults_to_none():
    # Providers without a bundled skill leave the field unset.
    p = registry.get_provider("stripe")
    assert p.skill is None


def test_vercel_cli_is_skill_paste_provider():
    p = registry.get_provider("vercel_cli")
    assert p.acquisition == "paste_token"
    assert p.actor == "skill"
    assert p.refresh_owner == "none"
    assert p.wiring.materializer == "skill_cli"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    assert p.skill is not None
    assert p.skill.key == "vercel"
    assert p.skill.primary_env == "VERCEL_TOKEN"
    assert p.skill.package.startswith("vercel@")
    assert p.validation is not None
    assert p.validation.url == "https://api.vercel.com/v2/user"
    assert p.validation.token_prefix is None


def test_aws_provider_shape():
    # AWS read-only rides the aws_sts acquisition: the owner runs a CloudFormation
    # stack that grants a cross-account role, and the baked `aws` CLI delivers the
    # assumed-role credentials via the aws_cli materializer rail. The managed
    # ViewOnly/ReadOnly policy ARNs cap the role at read access.
    p = registry.get_provider("aws")
    assert p.acquisition == "aws_sts"
    assert p.actor == "cli"
    assert p.refresh_owner == "none"
    assert p.requires.bins == ["aws"]
    assert p.wiring.materializer == "aws_cli"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    assert p.aws_sts is not None
    assert p.aws_sts.view_only_policy_arn == "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
    assert p.aws_sts.read_only_policy_arn == "arn:aws:iam::aws:policy/ReadOnlyAccess"
    assert p.aws_sts.power_user_policy_arn == "arn:aws:iam::aws:policy/PowerUserAccess"
    assert p.aws_sts.default_region == "us-east-1"
    assert p.aws_sts.cfn_template_url is None


def test_aws_sts_requires_aws_sts_block():
    # Mirrors test_direct_oauth_requires_oauth_block: an aws_sts provider with no
    # aws_sts spec block is a misconfiguration and must fail to construct.
    from pydantic import ValidationError

    from core.services.credentials.registry import Provider, Wiring

    with pytest.raises(ValidationError):
        Provider(
            key="x",
            display_name="X",
            acquisition="aws_sts",
            actor="cli",
            refresh_owner="none",
            wiring=Wiring(materializer="aws_cli"),
            aws_sts=None,
        )  # no aws_sts block


# Materializers a provider block already declares but whose implementation is
# not registered yet. Such names are exempt from the materializer consistency
# guard below until their implementation ships. Empty today: every declared
# materializer (including `aws_cli`) is now registered.
_PENDING_MATERIALIZERS: set[str] = set()


def test_registry_materializers_and_oauth_are_self_consistent():
    from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS

    for key, p in registry.all_providers().items():
        if p.acquisition == "direct_oauth":
            assert p.oauth and p.oauth.scopes, f"{key}: direct_oauth missing oauth scopes"
        mat = p.wiring.materializer
        if mat and mat not in _PENDING_MATERIALIZERS:
            assert mat in MATERIALIZERS, f"{key}: unknown materializer {mat!r}"
            # A materializer without a matching remover would silently skip
            # on-disk cleanup at disconnect, leaving credential residue on EFS.
            assert mat in MATERIALIZER_REMOVERS, f"{key}: materializer {mat!r} has no remover"


def test_pending_materializers_are_genuinely_unimplemented():
    # Self-cleaning guard: once a pending materializer is actually registered it
    # must be dropped from _PENDING_MATERIALIZERS so the consistency check above
    # starts covering it. This assertion trips the moment that cleanup is missed.
    from core.services.credentials.materializers import MATERIALIZERS

    for mat in _PENDING_MATERIALIZERS:
        assert mat not in MATERIALIZERS, f"{mat!r} is now implemented; drop it from _PENDING_MATERIALIZERS"
