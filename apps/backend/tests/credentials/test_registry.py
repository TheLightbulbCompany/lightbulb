import json
from pathlib import Path

import pytest
from core.services.credentials import registry


def test_loads_known_providers():
    providers = registry.all_providers()
    assert "stripe" in providers
    assert "google" in providers


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
    # The validate spec is pinned by test_registry_validate_parallel.py.


def test_direct_oauth_provider_shape():
    p = registry.get_provider("google")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "cli"
    assert p.refresh_owner == "actor"
    assert p.oauth.token_url == "https://oauth2.googleapis.com/token"
    assert p.oauth.scopes == [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/drive.file",
    ]
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
    assert len(reads) == 100
    assert len(writes) == 100
    # read/write are paired per resource: every read scope has a write twin.
    assert {s[: -len(":read")] for s in reads} == {s[: -len(":write")] for s in writes}
    assert len(scopes) == 203
    assert len(scopes) == len(set(scopes))  # no duplicate scope slipped into the yaml


def test_google_is_a_platform_app_not_byo():
    g = registry.get_provider("google")
    assert g.byo_required is False
    assert g.byo_setup_url is None
    assert g.oauth.platform_app == "google"


def test_no_provider_is_byo_required_except_x():
    byo = [k for k, v in registry.all_providers().items() if v.byo_required]
    assert byo == ["x"]


def test_x_is_byo_direct_oauth_cli_provider():
    p = registry.get_provider("x")
    assert p.display_name == "X"
    assert p.acquisition == "direct_oauth"
    assert p.actor == "cli"
    assert p.refresh_owner == "actor"
    assert p.byo_required is True
    assert p.byo_setup_url == "https://developer.x.com/en/portal/dashboard"
    assert p.requires.bins == ["xurl"]
    assert p.oauth.authorization_url == "https://x.com/i/oauth2/authorize"
    assert p.oauth.token_url == "https://api.x.com/2/oauth2/token"
    assert p.oauth.pkce is True
    assert p.oauth.scope_separator == " "
    assert {"tweet.read", "tweet.write", "users.read", "offline.access", "dm.read", "dm.write", "media.write"}.issubset(
        set(p.oauth.scopes)
    )
    assert p.wiring.materializer == "xurl_cli"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        registry.get_provider("does-not-exist")


def test_validate_key_maps_to_validation_field():
    p = registry.get_provider("github")
    assert p.validation is not None
    assert p.validation.method == "GET"
    assert p.validation.url == "https://api.github.com/user"


def test_plain_get_validate_has_no_body():
    p = registry.get_provider("github")
    assert p.validation.method == "GET"
    assert p.validation.body is None


def test_validate_auth_scheme_is_per_provider():
    # GitHub accepts the default Bearer scheme.
    assert registry.get_provider("github").validation.auth_scheme == "Bearer"


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


# Linear, Notion, Sentry, Intercom, and Stripe deliver their hosted MCP over
# OAuth rather than a local npx stdio process, so they ride the mcp_oauth
# materializer rail (their remote-server metadata under mcp_remote), like Attio.
# Stripe's URL is pathless on purpose — it must match the advertised
# protected-resource value exactly for RFC 8707 token audiencing.
@pytest.mark.parametrize(
    "key,url",
    [
        ("notion", "https://mcp.notion.com/mcp"),
        ("linear", "https://mcp.linear.app/mcp"),
        ("sentry", "https://mcp.sentry.dev/mcp"),
        ("intercom", "https://mcp.intercom.com/mcp"),
        ("stripe", "https://mcp.stripe.com"),
        ("apollo", "https://mcp.apollo.io/mcp"),
        ("cloudflare", "https://mcp.cloudflare.com/mcp"),
    ],
)
def test_hosted_oauth_mcp_provider_shape(key, url):
    from core.services.credentials.registry import all_providers, get_provider

    assert key in all_providers()
    p = get_provider(key)
    assert p.acquisition == "direct_oauth"
    assert p.actor == "mcp"
    assert p.refresh_owner == ("backend" if key == "sentry" else "actor")
    # Rides the mcp_oauth materializer rail; remote-server metadata under mcp_remote.
    assert p.wiring.materializer == "mcp_oauth"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    assert p.mcp_remote is not None
    assert p.mcp_remote.key == key
    assert p.mcp_remote.url == url
    assert p.mcp_remote.transport == "streamable-http"
    # DCR mints a public PKCE client; not BYO, no local validate spec.
    assert p.oauth.registration_url is not None
    assert p.oauth.pkce is True
    if key == "cloudflare":
        # Cloudflare's 2026-08-23 OAuth metadata advertises no scope strings;
        # permissions are selected in Cloudflare during OAuth/account consent.
        assert p.oauth.scopes == []
    else:
        assert p.oauth.scopes  # non-empty
    assert p.byo_required is False
    assert p.validation is None


def test_apollo_mcp_requests_read_oriented_scopes_only():
    p = registry.get_provider("apollo")
    scopes = set(p.oauth.scopes)
    assert {
        "read_user_profile",
        "mixed_people_api_search",
        "organizations_search",
        "contacts_search",
        "contact_read",
    }.issubset(scopes)
    assert not any(s.endswith("_write") or s.endswith("_create") or s.endswith("_approve") for s in scopes)
    assert "emailer_messages_send_now" not in scopes
    assert "emailer_campaigns_add_contact_ids" not in scopes


def test_apollo_prospect_source_contract_documents_isoa188_payload():
    schema_path = Path(__file__).parents[4] / "docs" / "integrations" / "apollo-prospect-source.schema.json"
    schema = json.loads(schema_path.read_text())

    assert schema["required"] == ["campaign_id", "source_run_id", "prospects"]
    assert schema["properties"]["prospects"]["maxItems"] == 10

    prospect = schema["$defs"]["prospect"]
    assert {
        "apollo_person_id",
        "apollo_organization_id",
        "company_domain",
        "contact",
        "company",
        "evidence",
        "suppression_hints",
        "enriched_at",
        "quality",
    }.issubset(prospect["properties"])
    assert "apollo_person_id" in prospect["required"]
    assert "company_domain" in prospect["required"]

    contact = prospect["properties"]["contact"]["properties"]
    assert {"name", "role", "business_email", "linkedin_url", "profile_url"}.issubset(contact)

    quality = prospect["properties"]["quality"]["properties"]
    assert {"confidence", "flags", "rejection_reason"}.issubset(quality)


def test_cloudflare_mcp_uses_hosted_oauth_metadata_without_declared_scopes():
    p = registry.get_provider("cloudflare")
    assert p.oauth.authorization_url == "https://mcp.cloudflare.com/authorize"
    assert p.oauth.token_url == "https://mcp.cloudflare.com/token"
    assert p.oauth.registration_url == "https://mcp.cloudflare.com/register"
    assert p.oauth.scopes == []


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
    # the platform OAuth app's token seeded into gh's hosts.yml, not an mcp_server.
    p = registry.get_provider("github")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "cli"
    # OAuth App tokens don't expire — nothing owns refresh.
    assert p.refresh_owner == "none"
    assert p.requires.bins == ["gh"]
    assert p.wiring.materializer == "gh_cli"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    # One Isol8-owned app per env, no BYO and no DCR.
    assert p.oauth is not None
    assert p.oauth.platform_app == "github"
    assert not p.byo_required and p.oauth.registration_url is None
    assert p.oauth.authorization_url == "https://github.com/login/oauth/authorize"
    assert p.oauth.token_url == "https://github.com/login/oauth/access_token"
    assert set(p.oauth.scopes) == {"repo", "read:org", "gist", "workflow"}
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


def test_actor_remote_mcp_can_be_brokered_per_environment(monkeypatch):
    # brokered_environments: [dev, prod] routes the actor-owned remote OAuth MCP
    # providers through the workspace broker in both environments, keeping refresh
    # grants out of runtimes.
    p = registry.get_provider("notion")
    assert p.refresh_owner == "actor"
    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "dev")
    assert registry.refresh_owner_for(p) == "backend"
    monkeypatch.setattr(registry.settings, "ENVIRONMENT", "prod")
    assert registry.refresh_owner_for(p) == "backend"


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


# Materializer names a provider block declares with no implementation behind
# them. `mcp_oauth` is a rail marker: remote MCP grants ride the broker/native
# MCP rails, `hermes_cli.OWNED_PATHS` never lists it, so nothing dispatches it.
_PENDING_MATERIALIZERS: set[str] = {"mcp_oauth"}


def test_registry_materializers_and_oauth_are_self_consistent():
    import re

    from core.services.credentials.materializers import MATERIALIZER_REMOVERS, MATERIALIZERS

    for key, p in registry.all_providers().items():
        # A host_pattern's match is interpolated into a URL the BACKEND fetches,
        # so a loose one (`.*`) silently turns the paste box into an SSRF
        # primitive. Every such pattern must at minimum refuse link-local and
        # loopback, whatever else it allows.
        if p.validation and p.validation.host_pattern:
            for host in ("169.254.169.254", "localhost"):
                assert not re.fullmatch(p.validation.host_pattern, host), (key, host)
        if p.acquisition == "direct_oauth":
            assert p.oauth, f"{key}: direct_oauth missing oauth spec"
            if key == "cloudflare":
                # Cloudflare hosted MCP metadata advertises no scope strings;
                # permissions are chosen during Cloudflare OAuth/account consent.
                assert p.oauth.scopes == []
            else:
                assert p.oauth.scopes, f"{key}: direct_oauth missing oauth scopes"
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


def test_meta_ads_rides_platform_app_not_dcr():
    """Meta's hosted ads MCP authenticates with OUR Meta app id as the OAuth client.

    Its discovery document advertises a `registration_endpoint`, which makes the
    Notion/Sentry/Linear DCR shape look right — but that endpoint answers
    400 invalid_client_metadata for every caller Meta hasn't allowlisted, so a
    `registration_url` here would ship a Connect button that cannot complete.
    Pin the platform_app shape (and the absence of registration_url) so the
    tempting-but-broken variant can't come back.
    """
    p = registry.get_provider("meta_ads")
    assert p.acquisition == "direct_oauth"
    assert p.actor == "mcp"
    # NOT "actor". That would claim the MCP SDK's OAuthClientProvider self-refreshes,
    # which it cannot: Meta issues no refresh_token at any tier, so mcp_oauth writes a
    # token store with no `refresh_token` key and the SDK has nothing to renew from.
    # `none` is honest AND keeps the credential in Secrets Manager, so a reprovision
    # re-materializes instead of forcing a reconnect.
    assert p.refresh_owner == "none"
    # The only thing making the grant outlive the afternoon: the code exchange alone
    # yields a token measured in hours.
    assert p.oauth.long_lived_exchange == "fb_exchange_token"
    assert p.oauth.platform_app == "meta-ads"
    assert p.oauth.registration_url is None
    # NOT meta-instagram: that secret holds the Instagram app id, a different app
    # from the Meta app the ads MCP authenticates against.
    assert p.oauth.platform_app != "meta-instagram"


def test_meta_ads_requests_every_documented_scope():
    """ads_mcp_management is what gates the MCP endpoint itself — dropping it (or
    any of the seven Meta's Get Started lists) yields a token the server refuses.

    pages_manage_metadata is the eighth, and is NOT from that list: it is what
    Meta requires to POST {page-id}/subscribed_apps, which is how a Page trigger
    binds to our webhook (inbound_triggers._meta_pages). pages_show_list only
    lists Pages; it does not authorize binding one. Pinned here because dropping
    it breaks Page triggers for every owner at subscribe time, and the failure is
    nowhere near this file."""
    p = registry.get_provider("meta_ads")
    assert set(p.oauth.scopes) == {
        "ads_mcp_management",
        "ads_read",
        "ads_management",
        "business_management",
        "catalog_management",
        "pages_show_list",
        "pages_manage_metadata",
        "instagram_basic",
    }
    assert p.oauth.scope_separator == " "


def test_meta_ads_publishes_the_remote_mcp_block():
    p = registry.get_provider("meta_ads")
    assert p.wiring.materializer == "mcp_oauth"
    assert p.wiring.mcp_server is None
    assert p.mcp_remote.key == "meta_ads"
    assert p.mcp_remote.url == "https://mcp.facebook.com/ads"
    assert p.mcp_remote.transport == "streamable-http"


def test_meta_inbound_blocks_are_app_level_and_well_formed():
    """Meta Ads + Instagram are the only APP-LEVEL trigger sources: one webhook
    callback for the whole app, so the seed's only per-owner act is subscribing
    the asset, and the trigger is `bearer` because Meta signs with the APP
    secret (verified at the edge, not by Paperclip)."""
    for key, expected_events in (
        ("meta_ads", {"leadgen", "feed", "messages"}),
        ("instagram", {"comments", "mentions", "messages"}),
    ):
        p = registry.get_provider(key)
        assert p.inbound is not None, f"{key} lost its inbound block"
        assert p.inbound.webhook_registration == "auto_meta"
        assert p.inbound.signing_mode == "bearer"
        assert {e.id for e in p.inbound.events} == expected_events
        # provider_events ARE the Meta field names subscribed_apps takes — an
        # event without them would subscribe the curated id by accident.
        for event in p.inbound.events:
            assert event.provider_events, f"{key}.{event.id} has no provider_events"
        assert p.inbound.template.suggested_agent == "friday"
        assert "{event_label}" in p.inbound.template.title
        # The app-level ingress derives the app secrets it accepts from these
        # providers' platform apps — no platform_app, no verifiable delivery.
        assert p.oauth is not None and p.oauth.platform_app


def test_meta_providers_stay_waitlisted_off_prod():
    """The `environments` gate is the Meta App Review waitlist. Wiring these two
    as trigger sources must not be the thing that un-gates them in prod."""
    for key in ("meta_ads", "instagram"):
        assert registry.get_provider(key).environments == ["local", "dev"]


def test_shopify_is_a_three_field_client_credentials_provider():
    # Shopify ended admin-created custom apps on 2026-01-01: a new store's app comes from the
    # Dev Dashboard with only a Client ID + Client secret, and an Admin API token is a 24 h
    # client-credentials grant. The owner fills three labelled inputs; the server joins them
    # into the "<host>|<token>" shape the two-value paste rail already parses.
    p = registry.get_provider("shopify")
    assert p.acquisition == "paste_token"
    assert p.actor == "skill"
    assert p.wiring.materializer == "shopify_cli"
    assert [f.key for f in p.paste_fields] == ["shop", "client_id", "client_secret"]
    assert [f.secret for f in p.paste_fields] == [False, False, True]
    assert p.paste_format == "{shop}|{client_id}:{client_secret}"
    assert p.token_setup_url
    v = p.validation
    assert v.token_grant_url == "https://{host}/admin/oauth/access_token"
    assert v.url == "https://{host}/admin/api/2026-07/graphql.json"
    assert v.auth_header == "X-Shopify-Access-Token" and v.auth_scheme == ""
    # The minted token is 32 hex chars, not shpat_ — a prefix gate would refuse every store.
    assert v.token_prefix is None
    assert v.success_json_field == "data.shop.name"
    # Shopify signs webhooks base64 HMAC; Paperclip compares hex. No inbound block.
    assert p.inbound is None


def test_shopify_host_pattern_pins_the_validate_host():
    # host_pattern is an SSRF fence: its match is interpolated into URLs the BACKEND fetches.
    import re

    pattern = registry.get_provider("shopify").validation.host_pattern
    assert re.fullmatch(pattern, "mystore.myshopify.com")
    assert re.fullmatch(pattern, "my-store-2.myshopify.com")
    for bad in (
        "169.254.169.254",
        "localhost",
        "evil.com",
        "mystore.myshopify.com.evil.com",
        "mystore.myshopify.com/admin",
        "mystore.myshopify.com:8080",
        "",
    ):
        assert not re.fullmatch(pattern, bad), bad


def test_paste_fields_require_a_paste_format():
    with pytest.raises(ValueError):
        registry.Provider(
            key="x2",
            display_name="X2",
            acquisition="paste_token",
            actor="skill",
            refresh_owner="none",
            wiring={"materializer": "shopify_cli"},
            paste_fields=[{"key": "a", "label": "A"}],
        )
