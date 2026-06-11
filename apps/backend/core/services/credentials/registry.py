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
    command: str
    args: list[str] = Field(default_factory=list)
    # env var name -> credential source ("input.token", "credentials.access_token"),
    # resolved at delivery time via the same _resolve_source the openclaw_paths rail uses.
    env: dict[str, str] = Field(default_factory=dict)


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
    # Hidden from the public /connections catalog unless the owner is on the
    # internal allowlist (config.INTERNAL_INTEGRATION_OWNERS). For providers that
    # are built but not yet reliably shippable to every user — e.g. Google while
    # its OAuth app is still pending Google verification.
    internal: bool = False
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
