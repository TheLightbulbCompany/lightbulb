"""Integration registry: the single source of truth for how each third-party
provider is acquired, who acts with the credential, and exactly where it lands.
Loaded + validated once at import."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

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

    def server_block(self) -> dict[str, str]:
        """The mcp.servers.<key> block published into the live openclaw.json.

        ``auth: "oauth"`` is load-bearing: OpenClaw only attaches the MCP SDK's
        OAuthClientProvider (which reads + self-refreshes the seeded token file)
        when the raw server config carries ``auth === "oauth"`` — without it the
        runtime treats the endpoint as a plain remote server, ignores the token
        store, and the server 401s. The materializer write and the reprovision
        rehydration both build the block from here, so they stay identical."""
        return {"transport": self.transport, "url": self.url, "auth": "oauth"}


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
    # Dynamic Client Registration endpoint. When set and the owner has no BYO app
    # stored, the dance registers a fresh public OAuth client at this URL (RFC 7591)
    # and caches it, so a headless owner never needs to pre-create an OAuth app.
    registration_url: str | None = None


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


class Requires(BaseModel):
    bins: list[str] = Field(default_factory=list)
    config: list[str] = Field(default_factory=list)


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
    # Bundled skill delivered by a paste-token CLI integration (see SkillSpec).
    skill: SkillSpec | None = None
    validation: ValidateSpec | None = Field(default=None, alias="validate")

    # populate_by_name lets us pass either the `validate` alias (YAML) or the
    # `validation` field name (Python construction) when building a Provider.
    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _oauth_present_when_needed(self) -> "Provider":
        if self.acquisition == "direct_oauth" and self.oauth is None:
            raise ValueError(f"{self.key}: direct_oauth requires an `oauth` block")
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
