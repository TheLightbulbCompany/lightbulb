"""Integration registry: the single source of truth for how each third-party
provider is acquired, who acts with the credential, and exactly where it lands.
Loaded + validated once at import."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from core.config import settings

_YAML = Path(__file__).parent / "providers.yaml"

Acquisition = Literal["paste_token", "direct_oauth", "nango", "agentcore", "aws_sts", "secrets_manager"]
Actor = Literal["cli", "mcp", "skill", "nango_proxy"]
RefreshOwner = Literal["none", "actor", "backend", "nango_proxy"]


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

    def server_block(self, *, brokered: bool = False) -> dict[str, object]:
        """Hermes MCP config: native per-profile OAuth or brokered bearer auth."""
        if brokered:
            env_key = f"MCP_{re.sub(r'[^A-Za-z0-9]', '_', self.key).upper()}_ACCESS_TOKEN"
            return {
                "url": self.url,
                "headers": {"Authorization": f"Bearer ${{{env_key}}}"},
                "requestTimeoutMs": self.request_timeout_ms,
            }
        return {
            "transport": self.transport,
            "url": self.url,
            "auth": "oauth",
            "requestTimeoutMs": self.request_timeout_ms,
        }


class McpStdio(BaseModel):
    """Stdio MCP server metadata for a provider whose credential is delivered as a
    FILE by a materializer rather than as env values on the ``mcp_server`` rail.

    ``mcp_server`` (the wiring rail) resolves each env var from the retained
    Secrets Manager secret, so it cannot carry a provider whose ``refresh_owner``
    is ``actor`` — those retain no secret, and ``build_connected_integration_servers``
    skips them, silently dropping the server block on the next reprovision. This
    block is the stdio twin of ``McpRemote``: the materializer writes the token
    store onto EFS, and the published server config carries only the PATH to it.

    Because the block holds no secret, it is re-derivable from the registry alone —
    which is what makes it survive a wholesale openclaw.json rewrite.
    """

    key: str  # the mcp.servers.<key> this integration owns
    command: str
    args: list[str] = Field(default_factory=list)
    # Env var naming the on-EFS token store. The server must read this from its
    # HOST PROCESS env (i.e. this block) rather than from the store file itself.
    token_store_env: str = Field(alias="tokenStoreEnv")
    # Same rationale as McpServer.connection_timeout_ms: a stdio server on a
    # 0.5-vCPU container needs well past OpenClaw's 30s default to spawn.
    connection_timeout_ms: int = Field(120_000, alias="connectionTimeoutMs")

    model_config = {"populate_by_name": True}

    def server_block(self, token_store_path: str) -> dict[str, object]:
        """The mcp.servers.<key> block published into the live openclaw.json.

        Built identically by the materializer write and by the reprovision
        rehydration, so the two can never drift."""
        return {
            "command": self.command,
            "args": list(self.args),
            "env": {self.token_store_env: token_store_path},
            "connectionTimeoutMs": self.connection_timeout_ms,
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
    # A credential the BACKEND reads and the container never receives. Explicit
    # rather than "no target set", so it can never happen by a typo.
    #
    # This exists because openclaw.json is NOT durable for arbitrary keys: a
    # reprovision rebuilds it wholesale and re-injects only the
    # `integration_mcp_servers` merge and `provisioning_landing.build_provisioned_env`,
    # which selects Projects rows by key prefix AND actor stamp. Anything else
    # written into the config is silently dropped on the next image roll while
    # the integration row still reads "connected" — a credential that vanishes
    # without telling anyone. So a backend-read key does not go there at all;
    # Secrets Manager (via `_retains_secret`) is its only custodian. A backend_only
    # provider with refresh_owner: backend (microsoft365) still reaches the agent,
    # but only as a short-lived access token vended per turn by
    # routers/runtime_rpc.runtime_credentials — never the stored grant.
    backend_only: bool = False

    @model_validator(mode="after")
    def _exactly_one(self) -> "Wiring":
        # Truthiness check: an empty `openclaw_paths: []` is intentionally treated as "not set".
        targets = sum(bool(x) for x in (self.openclaw_paths, self.materializer, self.mcp_server))
        if self.backend_only:
            if targets:
                raise ValueError("backend_only wiring must not set a delivery target")
            return self
        if targets != 1:
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
    # Provider-specific post-exchange hook upgrading Meta's short-lived
    # code-exchange token to the ~60-day one, immediately after the exchange. The
    # upgrade needs the client_secret, so it must run in the dance (backend),
    # never in a materializer or the container.
    #   "ig_exchange_token" — Business Login for Instagram.
    #   "fb_exchange_token" — Facebook Login for Business (the Meta Ads MCP path).
    # A Meta provider WITHOUT this is broken by construction, not merely
    # short-lived: Meta issues no refresh_token at any tier, so nothing downstream
    # can renew and the grant dies within hours of the connect.
    long_lived_exchange: Literal["ig_exchange_token", "fb_exchange_token"] | None = None


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
    # Per-account API host. A provider whose admin API lives on the OWNER'S OWN
    # domain (Shopify: https://<shop>.myshopify.com/…) can have no static validate
    # URL, but the paste rail carries exactly one string — so the owner pastes
    # "<host>|<token>" and `url` interpolates the left half as {host}. This
    # pattern is what declares that two-value format AND pins which hosts may be
    # interpolated: an owner-supplied host in a request the backend itself issues
    # is an SSRF primitive, so a host it doesn't full-match is refused outright
    # rather than fetched. Unset (the norm) = the whole paste is the credential.
    host_pattern: str | None = None
    # Client-credentials exchange run BEFORE the check, for a provider whose pasted
    # credential is an app's "<client_id>:<client_secret>" rather than a token
    # (Shopify Dev Dashboard apps). POSTed as an OAuth client_credentials form to this
    # URL ({host} as above); the returned access_token is what the check then sends.
    token_grant_url: str | None = None


class PasteField(BaseModel):
    """One labelled input of a multi-field paste provider (see Provider.paste_fields)."""

    key: str
    label: str
    placeholder: str | None = None
    secret: bool = False
    # A host the owner may paste as a URL (https://Store.myshopify.com/admin/): reduced to its
    # lowercased host before the join. Never set on an id/secret field -- those are case-sensitive.
    host: bool = False


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
    # Provider-side event names to subscribe when Isol8 auto-registers the
    # webhook (auto_github: the GitHub hook's `events` array; auto_vercel:
    # the Vercel webhook's `events`). One trigger may carry several events, in
    # which case the UNION of their names is what gets registered on the one
    # hook. None for guided-paste providers — the
    # owner picks events in the provider's own dashboard, so there is nothing
    # to subscribe programmatically.
    provider_events: list[str] | None = None
    # Per-EVENT registration override. Events within one provider can differ
    # in how they're wired (google: calendar/drive ride push channels, Gmail
    # polling rides a schedule trigger, Gmail push needs the owner's own
    # Pub/Sub topic), so registration granularity lives on the event:
    #   None          — inherit the block-level webhook_registration.
    #   "poll"        — no webhook at all; the seed attaches an enabled
    #                   schedule trigger and the agent checks on a cadence.
    #   "owner_setup" — the owner does a one-time setup in THEIR OWN
    #                   provider project (gmail.message: a Pub/Sub topic +
    #                   push subscription); the seed registers our side and
    #                   returns the setup instructions.
    registration: Literal["poll", "owner_setup"] | None = None
    # Per-event starter-prompt override of template.starter_prompt — a
    # scheduled inbox check and a push notification need different opening
    # lines. Same placeholders as the template ({event_label}, {account}).
    starter_prompt: str | None = None


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
    # own dashboard (Stripe/Sentry/Linear/Attio). "auto_github" — the owner's
    # connected gh token can create repo webhooks itself, so the seed registers
    # the hook (armed with the trigger's own one-time secret) and the
    # automation is live with no paste step; the caller must supply the
    # "owner/repo" resource to hook. "auto_vercel" — same auto shape but the
    # secret flows the other way: Vercel GENERATES the webhook secret at
    # create time, so the seed registers the webhook and then ARMS the trigger
    # with Vercel's value; the caller supplies the project id to hook.
    # "auto_google_watch" — the seed opens a Google push CHANNEL through the
    # owner's gog keyring (Calendar events.watch / Drive changes.watch) whose
    # token is the trigger's own secret, verified by Paperclip's bearer mode
    # via the proxy's X-Goog-Channel-Token mapping; the caller supplies a
    # connected Google account email as the resource. Channels expire ≤7 days
    # and the routine renews its own: the seed attaches a twice-weekly
    # maintenance schedule trigger and an EFS state file the agent re-arms
    # from (see inbound_triggers) — no platform-side renewal job.
    # "auto_meta" — the only APP-LEVEL (fan-in) shape: Meta configures ONE
    # callback URL for the whole app, so every owner's deliveries land on the
    # same ownerless route (routers/meta_webhook.py) and are routed by the
    # asset id in the payload. The seed binds the owner's asset with
    # POST /{asset-id}/subscribed_apps and records the asset↔trigger binding
    # on the integrations row; the caller supplies the asset id as the
    # resource. signing_mode is "bearer" because Meta's signature is over the
    # APP secret (verified at the edge), not anything Paperclip holds.
    webhook_registration: Literal["guided_paste", "auto_github", "auto_vercel", "auto_google_watch", "auto_meta"]
    # A NATIVE Paperclip signingMode ("hmac_sha256" | "hmac_sha1" |
    # "github_hmac" | "bearer" | "none"), passed straight through to the
    # created trigger — Paperclip verifies the provider's signature natively
    # against the trigger's secret (armed with the provider's own value via
    # PUT .../webhook-secret or the auto_vercel arm). Stripe uses
    # "hmac_sha256" (its `{t}.{body}` scheme); the trigger proxy only
    # normalizes Stripe's combined signature header into what that mode reads.
    signing_mode: str
    # The provider's own signature header for raw-body HMAC modes (e.g.
    # "Sentry-Hook-Signature"). The trigger proxy copies its value into
    # X-Paperclip-Signature so Paperclip's raw-body HMAC modes (github_hmac,
    # hmac_sha1) can read it — the proxy collects these from the registry, so
    # wiring a new raw-body-HMAC provider is YAML-only. Leave unset when the
    # provider already sends a header Paperclip reads natively (GitHub's
    # X-Hub-Signature-256) or needs bespoke adaptation in the proxy (Stripe's
    # combined t=,v1= header).
    signature_header: str | None = None
    # Provider-specific guided-paste copy shown after seeding, overriding the
    # generic "add a webhook endpoint in your <provider> dashboard" sentence —
    # each dashboard buries webhooks somewhere different (Sentry: an internal
    # integration; Linear: Settings → API). Supports {proxy_url} plus
    # {event_labels}/{event_ids} — the human labels / provider ids of EVERY
    # event armed on the trigger, joined ("a, b and c"). {event_label} and
    # {event_id} are the older singular spellings, bound to the same rendering
    # so a template written before events became multi-select still names them
    # all. Write the copy so it reads for one event or ten.
    instructions: str | None = None
    # Placeholder for the paste-the-signing-secret input. Stripe secrets look
    # like "whsec_…"; Sentry's is an integration Client Secret — a wrong hint
    # reads as "you're pasting the wrong thing", so name the provider's term.
    secret_hint: str | None = None
    # Can the AGENT tell which event fired from the delivery BODY alone?
    #
    # This gates multi-event triggers. One trigger fires for several events and
    # the agent is handed only the payload: the proxy forwards the body
    # byte-verbatim (HMAC is over the raw bytes) and the fork's
    # `firePublicTrigger` takes `payload: req.body` — NO headers reach the run
    # context. So a provider that names its event only in an HTTP header
    # (GitHub's X-GitHub-Event, Sentry's Sentry-Hook-Resource) cannot be
    # multi-armed: the agent would wake with no way to know which of the armed
    # events it is looking at.
    #
    # False by default, so a newly wired provider is single-event until someone
    # has actually checked its payload. Set it only after confirming the body
    # carries the event type (Stripe `type`, Linear `type`, Attio
    # `events[].event_type`, Vercel `type`, Meta `entry[].changes[].field`).
    # Isol8AI/isol8#1916 is the durable fix that lifts this for the rest.
    event_in_body: bool = False
    # Ordering is load-bearing, by convention rather than schema: the first
    # entries are the curated "popular" set — the frontend event picker pins
    # the first 3 as its Popular group — and the remainder is alphabetized by
    # id. See the matching comment atop the stripe events list in providers.yaml.
    events: list[InboundEvent]
    template: InboundTemplate


class Provider(BaseModel):
    key: str
    display_name: str
    acquisition: Acquisition
    actor: Actor
    refresh_owner: RefreshOwner
    # Route an actor-owned remote OAuth MCP provider through the workspace
    # broker in the declared environments, keeping refresh grants out of runtimes.
    brokered_environments: list[str] | None = None
    # Opt-in override for actor-refresh providers: retain the exchanged
    # credential in Secrets Manager anyway, for backend-side reads (posthog:
    # HogQL rollups). Non-actor providers retain regardless — see
    # routers.connections._retains_secret.
    retain_secret: bool = False
    # Super-agent token custody (spec 13 T6): an actor-refreshed provider whose rotating grant the
    # backend may take over, per integration row (row ``custody`` attribute, see refresh_owner_for).
    custody_migratable: bool = False
    # How long before expiry the backend refreshes a token it holds (seconds).
    refresh_window_s: int = 60
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
    # Where a paste_token provider's key is minted. The OAuth rail needs no such
    # link (the provider's own consent screen is the flow), but an API-key row is
    # just a blank box: without this the owner has to go find the right settings
    # page themselves, which is the difference between connecting now and
    # connecting later. Rendered next to the paste field, not in the BYO panel —
    # deliberately separate from byo_setup_url, which points at an OAuth-app
    # console and answers a different question.
    token_setup_url: str | None = None
    # Placeholder for the paste field, for the provider whose one input is not a
    # bare key. Only the two-value paste rail needs it (see
    # ValidateSpec.host_pattern): the generic "Paste API key" is a lie when the
    # box wants "<host>|<token>", and that format is otherwise undiscoverable —
    # token_setup_url points at the provider's own docs, which have never heard
    # of our packing convention.
    token_paste_hint: str | None = None
    # Labelled inputs for a provider whose credential is several values (Shopify:
    # store domain, client id, client secret). The Connections form renders one
    # input per field and posts them as `fields`; the backend joins them with
    # `paste_format` (str.format over the field keys) into the single string the
    # paste rail stores and validates, so no owner ever types the packed form.
    paste_fields: list[PasteField] | None = None
    paste_format: str | None = None
    # Remote MCP endpoint published into mcp.servers.<key> by the mcp_oauth
    # materializer. Only set for OAuth-protected remote MCP servers (e.g. Attio);
    # see McpRemote for why it lives at the provider level rather than in Wiring.
    mcp_remote: McpRemote | None = None
    mcp_stdio: McpStdio | None = None
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
    # Registrable domains of this service's own website. An agent's ask for a website login on
    # one of them, while this connector is connected, carries a hint that the password is only
    # for the browser (website_logins.connector_hint). ``browser_name`` is the name that hint
    # uses when it differs from display_name.
    browser_domains: list[str] = Field(default_factory=list)
    browser_name: str | None = None

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
        if bool(self.paste_fields) != bool(self.paste_format):
            raise ValueError(f"{self.key}: paste_fields and paste_format go together")
        if self.brokered_environments and (not self.mcp_remote or self.refresh_owner != "actor"):
            raise ValueError(f"{self.key}: brokered_environments requires actor-owned remote OAuth MCP")
        return self


def refresh_owner_for(provider: Provider, row: dict | None = None) -> RefreshOwner:
    """Resolve the effective token custodian for the current environment.

    An integrations ``row`` carrying ``custody`` wins (spec 13: custody is data, set by a
    switch-on connect or the cutover script); a row without it -- every legacy row -- keeps
    today's rule."""
    if row and row.get("custody") in ("backend", "actor"):
        return row["custody"]
    if provider.brokered_environments and current_env() in provider.brokered_environments:
        return "backend"
    return provider.refresh_owner


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
