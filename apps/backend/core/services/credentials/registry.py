"""Integration registry: the single source of truth for how each third-party
provider is acquired, who acts with the credential, and exactly where it lands.
Loaded + validated once at import."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from core.config import settings

_YAML = Path(__file__).parent / "providers.yaml"

Acquisition = Literal["paste_token", "direct_oauth", "nango", "agentcore", "aws_sts", "secrets_manager"]
Actor = Literal["cli", "mcp", "skill", "nango_proxy"]
RefreshOwner = Literal["none", "actor", "isol8_rematerialize", "nango_proxy"]


class OpenclawPath(BaseModel):
    path: str
    source: str


class McpServer(BaseModel):
    key: str  # the mcp.servers.<key> this integration owns
    # A stdio server bakes its own process: `command` (+ `args`) is required and
    # the credential rides into the process env. A remote server is reached over
    # the network instead: `transport` (e.g. "streamable-http") + `url` are set
    # and there is no command. Exactly one of the two shapes must be present.
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    transport: str | None = None
    url: str | None = None
    # env var name -> credential source ("input.token", "credentials.access_token"),
    # resolved at delivery time via the same _resolve_source the openclaw_paths rail uses.
    env: dict[str, str] = Field(default_factory=dict)
    # OpenClaw's default MCP connect timeout is 30s, but a stdio server spawned via
    # npx takes 13s+ even with a warm cache on a 0.5-vCPU container — and the npx
    # cache lives on ephemeral /tmp, so every task restart is a cold start that
    # reliably exceeds 30s. 120s gives the spawn room; once connected the session
    # is reused, so the cost is only paid on genuine cold starts.
    connection_timeout_ms: int = Field(120_000, alias="connectionTimeoutMs")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _stdio_or_remote(self) -> "McpServer":
        is_stdio = bool(self.command)
        is_remote = bool(self.transport and self.url)
        if is_stdio == is_remote:
            raise ValueError("mcp_server must be either stdio (command) or remote (transport+url), not neither/both")
        return self


class McpRemote(BaseModel):
    """Remote MCP server metadata for a provider that delivers its OAuth tokens
    via the ``mcp_oauth`` materializer rather than the ``mcp_server`` wiring rail.

    The ``mcp_server`` wiring rail can't carry this provider because its credential
    is not an env var fed to a baked stdio process — it is a token file the MCP
    SDK's OAuthClientProvider reads + self-refreshes. So the provider rides the
    materializer rail (which writes that token file) and ALSO needs to publish the
    live ``mcp.servers.<key>`` block pointing the runtime at the remote endpoint.
    That block is described HERE, at the provider level, so the ``materializer``
    wiring stays the single declared rail and the "exactly one of
    openclaw_paths | materializer | mcp_server" invariant is preserved.
    """

    key: str  # the mcp.servers.<key> this integration owns
    url: str
    transport: str
    # OpenClaw's catalog build caps each server's tools/list at a hardcoded 1.5s
    # UNLESS the server block carries an explicit request timeout — and a remote
    # server's list can exceed 1.5s whenever the 0.5-vCPU container is under CPU
    # pressure (e.g. a sibling stdio server's npx cold-start), which silently
    # excludes the server from the catalog ("failed to start server … -32001").
    # 60s matches the MCP SDK's default per-request timeout, so runtime tool-call
    # behavior is unchanged; only the list budget is lifted.
    request_timeout_ms: int = Field(60_000, alias="requestTimeoutMs")

    model_config = {"populate_by_name": True}

    def server_block(self) -> dict[str, object]:
        """The mcp.servers.<key> block published into the live openclaw.json.

        ``auth: "oauth"`` is load-bearing: OpenClaw only attaches the MCP SDK's
        OAuthClientProvider (which reads + self-refreshes the seeded token file)
        when the raw server config carries ``auth === "oauth"`` — without it the
        runtime treats the endpoint as a plain remote server, ignores the token
        store, and the server 401s. The materializer write and the reprovision
        rehydration both build the block from here, so they stay identical."""
        return {
            "transport": self.transport,
            "url": self.url,
            "auth": "oauth",
            "requestTimeoutMs": self.request_timeout_ms,
        }


class SkillSpec(BaseModel):
    """A bundled skill delivered by a paste-token CLI integration. The
    materializer enables ``skills.entries.<key>`` and injects the credential as
    the env var ``primary_env`` (OpenClaw's native skill env injection). ``package``
    is the EXACT-pinned npm spec the skill's SKILL.md tells the agent to ``npx``."""

    key: str  # skills.entries.<key> this integration owns
    primary_env: str = Field(alias="primaryEnv")
    package: str  # e.g. "vercel@<x.y.z>" — exact version, never a range/tag

    model_config = {"populate_by_name": True}


class Wiring(BaseModel):
    openclaw_paths: list[OpenclawPath] | None = None
    materializer: str | None = None
    mcp_server: McpServer | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Wiring":
        # Truthiness check: an empty `openclaw_paths: []` is intentionally treated as "not set".
        if sum(bool(x) for x in (self.openclaw_paths, self.materializer, self.mcp_server)) != 1:
            raise ValueError("wiring must set exactly one of openclaw_paths | materializer | mcp_server")
        return self


class OAuthSpec(BaseModel):
    authorization_url: str
    token_url: str
    scopes: list[str]
    scope_separator: str = " "
    pkce: bool = True
    extra_authorize_params: dict[str, str] = Field(default_factory=dict)
    # How a CONFIDENTIAL client proves itself at the token endpoint. Every provider
    # here accepted the secret in the form body until X, which classifies a Web App
    # / Automated App or Bot as confidential and 401s a posted secret — it requires
    # HTTP Basic (RFC 6749 §2.3.1). A public/PKCE-only client has no secret, so this
    # is inert for the DCR providers.
    token_endpoint_auth_method: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post"
    # Dynamic Client Registration endpoint. When set and the owner has no BYO app
    # stored, the dance registers a fresh public OAuth client at this URL (RFC 7591)
    # and caches it, so a headless owner never needs to pre-create an OAuth app.
    registration_url: str | None = None
    # Platform-hosted OAuth app: the name of the Isol8-owned client stored at
    # ``isol8/<env>/integrations/platform-oauth/<name>`` in Secrets Manager. Every
    # owner OAuths against this ONE reviewed app (the Buffer/Hootsuite model —
    # Meta app review approves the app once, each user grants access to their own
    # assets). Mutually exclusive with BYO (byo_required) and DCR
    # (registration_url): when set, the dance never consults the per-owner app
    # store for the client.
    platform_app: str | None = None
    # Provider-specific post-exchange hook. "ig_exchange_token" upgrades
    # Instagram's short-lived (1h) token to a long-lived (60d) one immediately
    # after the code exchange — the upgrade needs the client_secret, so it must
    # run in the dance (backend), never in a materializer or the container.
    long_lived_exchange: Literal["ig_exchange_token"] | None = None


class ValidateSpec(BaseModel):
    method: str
    url: str
    auth_header: str = "Authorization"
    # Auth scheme prefix for the validate header value (e.g. "Bearer"). Some
    # providers (Linear personal API keys) require a bare key with no scheme —
    # set auth_scheme: "" for those.
    auth_scheme: str = "Bearer"
    # Optional JSON request body. GraphQL endpoints (Linear) require a POST with
    # a query body — a bare GET rejects otherwise-valid keys.
    body: dict | None = None
    # Static request headers merged in alongside the auth header. Some providers
    # require a fixed header on every call (e.g. Notion's `Notion-Version`) — a
    # request without it is rejected even with a valid token.
    headers: dict[str, str] = Field(default_factory=dict)
    # Dotted JSON path that must resolve to a truthy value for the token to count
    # as valid. Some providers answer HTTP 200 even for a bad token and signal
    # validity only in the body (Slack `auth.test` -> {"ok": false}; Linear
    # GraphQL -> 200 with `errors`). A missing/falsy path means invalid.
    success_json_field: str | None = None
    # Required token prefix, checked BEFORE the HTTP call. Some validate endpoints
    # accept more token shapes than the wired env var can use — Slack's auth.test
    # says `ok` for a bot `xoxb-` token, but the server is given the user-token
    # slot, so only an `xoxp-` token actually works once spawned.
    token_prefix: str | None = None


class AwsStsSpec(BaseModel):
    """AWS access via STS AssumeRole. The owner runs a CloudFormation stack
    that creates a connector role Isol8's backend can assume (trust = our
    account root + per-connection ExternalId), capped at the tier the owner
    picked from the three AWS-managed policy ARNs below. The aws_cli
    materializer delivers a credential_process bundle; the container's CLI
    pulls short-lived credentials from the backend on demand."""

    view_only_policy_arn: str = "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"
    read_only_policy_arn: str = "arn:aws:iam::aws:policy/ReadOnlyAccess"
    power_user_policy_arn: str = "arn:aws:iam::aws:policy/PowerUserAccess"
    default_region: str = "us-east-1"
    cfn_template_url: str | None = None


class Requires(BaseModel):
    bins: list[str] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)


class InboundEvent(BaseModel):
    id: str
    label: str


class InboundTemplate(BaseModel):
    title: str
    suggested_agent: str
    starter_prompt: str


class Inbound(BaseModel):
    """Declares this provider as a 1-click Automations trigger source. Consumed
    by the seed service (`create_routine` + `create_routine_trigger`) and the
    Integrations card's "Can trigger" line — see the Automations Phase 2 plan.
    """

    # "guided_paste" — the provider has no register-a-webhook API reachable
    # with what we hold, so the owner pastes the proxy URL into the provider's
    # own dashboard (Stripe). (An "auto" shape — Isol8 registers the webhook
    # itself via the provider's API — returns with the generic-rail follow-up.)
    webhook_registration: Literal["guided_paste"]
    # Registry-level marker interpreted by the trigger proxy + seed service —
    # NOT a Paperclip signing enum. "stripe_edge" verifies HMAC at the Isol8
    # edge (the Paperclip trigger's own signingMode stays "none").
    signing_mode: str
    events: list[InboundEvent]
    template: InboundTemplate


class Provider(BaseModel):
    key: str
    display_name: str
    acquisition: Acquisition
    actor: Actor
    refresh_owner: RefreshOwner
    wiring: Wiring
    requires: Requires = Requires()
    oauth: OAuthSpec | None = None
    # BYO-required: this provider has no Isol8-managed OAuth app, so the owner
    # MUST connect with their own OAuth app (client_id/secret). It is visible to
    # every owner and gated only by "have you stored your app yet". Today only
    # google: its scopes are Google *restricted* scopes that an unverified shared
    # app cannot serve, and the owner's own app carries its own Limited-Use
    # compliance.
    byo_required: bool = False
    # Help link shown in the BYO setup panel (the provider's OAuth-app console).
    byo_setup_url: str | None = None
    # Remote MCP endpoint published into mcp.servers.<key> by the mcp_oauth
    # materializer. Only set for OAuth-protected remote MCP servers (e.g. Attio);
    # see McpRemote for why it lives at the provider level rather than in Wiring.
    mcp_remote: McpRemote | None = None
    # Restrict this provider to specific environments (e.g. ["local", "dev"]).
    # None = available in every environment. Used to ship a provider to dev for
    # testing before it's cleared for prod — e.g. Instagram, pending Meta App
    # Review + the platform-secret seed. Enforced in routers/connections.py:
    # gated providers are neither listed nor connectable outside their envs.
    environments: list[str] | None = None
    # Extra owners who get this provider even OUTSIDE `environments` — a targeted
    # allowlist for prod-dogfooding a provider that's env-gated off prod for
    # everyone else. E.g. Instagram pre-App-Review: dev-open for all dev owners,
    # plus the founder's prod container here, while every other prod user stays
    # dark (no dead "Connect" button they can't complete pre-review). Owner ids
    # are Clerk org_/user_ ids. None/empty = no extra owners.
    owner_allowlist: list[str] | None = None
    # What a gate MEANS for this provider when it excludes an owner.
    #
    # False (default) — HIDDEN. The provider is absent from the owner's
    #   connections list and from the public platform-info catalog, exactly as
    #   before `requestable` existed. This is what keeps a half-wired provider
    #   gated `environments: [local, dev]` off prod entirely: dropping this
    #   default would publish it on an unauthenticated route and have agents
    #   pitch it, with no knob left to suppress it short of deleting the entry.
    #
    # True — REQUESTABLE. The provider is listed with `requestable: true` so the
    #   Integrations tab can offer "Request access" (emails support) instead of a
    #   Connect button whose authorize leg would 404. Opt in ONLY for providers
    #   we can actually grant by hand — i.e. a Meta integration where an operator
    #   adds the owner as a provider tester and to `owner_allowlist`.
    #
    # Flip this AFTER the extended image carrying the matching isol8 SKILL.md is
    # promoted for the env (openclaw-version.json). Containers bake the skill, so
    # a backend-first flip leaves live agents on copy that has never heard of
    # `requestable` and still says "connect it in the Integrations tab".
    offer_request_access: bool = False
    # Bundled skill delivered by a paste-token CLI integration (see SkillSpec).
    skill: SkillSpec | None = None
    # AWS STS rail config. Only set for the `aws` provider (acquisition
    # aws_sts); carries the managed-policy ARNs that cap the assumed role + the
    # default region.
    aws_sts: AwsStsSpec | None = None
    validation: ValidateSpec | None = Field(default=None, alias="validate")
    # 1-click Automations trigger source declaration. None = this provider is
    # not (yet) wired as a webhook trigger source. See Inbound docstring.
    inbound: Inbound | None = None

    # populate_by_name lets us pass either the `validate` alias (YAML) or the
    # `validation` field name (Python construction) when building a Provider.
    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _oauth_present_when_needed(self) -> "Provider":
        if self.acquisition == "direct_oauth" and self.oauth is None:
            raise ValueError(f"{self.key}: direct_oauth requires an `oauth` block")
        if self.acquisition == "aws_sts" and self.aws_sts is None:
            raise ValueError(f"{self.key}: aws_sts requires an aws_sts block")
        # A platform app IS the client — a provider can't simultaneously demand a
        # BYO app or register one dynamically. Catching the contradiction at load
        # time keeps _oauth_client's resolution order unambiguous.
        if self.oauth and self.oauth.platform_app and (self.byo_required or self.oauth.registration_url):
            raise ValueError(f"{self.key}: platform_app is mutually exclusive with byo_required/registration_url")
        return self


@lru_cache(maxsize=1)
def _load() -> dict[str, Provider]:
    raw = yaml.safe_load(_YAML.read_text()) or {}
    out: dict[str, Provider] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            raise ValueError(f"provider {k!r} has an empty or malformed definition")
        out[k] = Provider(key=k, **v)
    return out


def all_providers() -> dict[str, Provider]:
    return _load()


def get_provider(key: str) -> Provider:
    providers = _load()
    if key not in providers:
        raise KeyError(f"unknown integration provider: {key!r}")
    return providers[key]


def current_env() -> str:
    # Empty ENVIRONMENT (local dev) is treated as "dev", matching the
    # integration_secret_store / provider-secret naming convention.
    return settings.ENVIRONMENT or "dev"


def visible_in_env(p: Provider) -> bool:
    """Env half of the availability gate.

    This is the only half an *ownerless* caller can evaluate — notably the
    public platform-info catalog, which has no auth and so cannot check
    `owner_allowlist`. Keep it here rather than in a router so every surface
    that advertises providers agrees on what this environment offers.
    """
    return p.environments is None or current_env() in p.environments


def is_listable(p: Provider, owner_id: str | None) -> bool:
    """Whether this provider appears in the owner's connections list at all.

    Connectable providers always list. A gated one lists only if it opts into
    `offer_request_access` — otherwise a gate still means HIDDEN, which is what
    keeps a provider that is merely half-built out of prod.
    """
    return available_for(p, owner_id) or p.offer_request_access


def available_for(p: Provider, owner_id: str | None) -> bool:
    """Whether this provider is exposed to this owner in the running environment.

    Available when it's not env-gated, OR the current env is in its
    `environments`, OR this owner is on its `owner_allowlist` (the prod-dogfood
    escape hatch — a dev-gated provider still reachable by a named founder
    container on prod).
    """
    if visible_in_env(p):
        return True
    return bool(owner_id and p.owner_allowlist and owner_id in p.owner_allowlist)
