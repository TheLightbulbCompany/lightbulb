"""Integration registry: the single source of truth for how each third-party
provider is acquired, who acts with the credential, and exactly where it lands.
Loaded + validated once at import."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

_YAML = Path(__file__).parent / "providers.yaml"

Acquisition = Literal["paste_token", "direct_oauth", "nango", "agentcore", "aws_sts", "secrets_manager"]
Actor = Literal["cli", "mcp", "skill", "nango_proxy"]
RefreshOwner = Literal["none", "actor", "isol8_rematerialize", "nango_proxy"]


class OpenclawPath(BaseModel):
    path: str
    source: str


class Wiring(BaseModel):
    openclaw_paths: list[OpenclawPath] | None = None
    materializer: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Wiring":
        if bool(self.openclaw_paths) == bool(self.materializer):
            raise ValueError("wiring must set exactly one of openclaw_paths | materializer")
        return self


class OAuthSpec(BaseModel):
    authorization_url: str
    token_url: str
    scopes: list[str]
    scope_separator: str = " "
    pkce: bool = True
    extra_authorize_params: dict[str, str] = {}


class ValidateSpec(BaseModel):
    method: str
    url: str
    auth_header: str = "Authorization"


class Requires(BaseModel):
    bins: list[str] = []
    config: list[str] = []


class Provider(BaseModel):
    key: str
    display_name: str
    acquisition: Acquisition
    actor: Actor
    refresh_owner: RefreshOwner
    wiring: Wiring
    requires: Requires = Requires()
    oauth: OAuthSpec | None = None
    validation: ValidateSpec | None = None   # YAML key is `validate`; aliased below

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
        # YAML uses `validate` as the key; map it to the `validation` field.
        if "validate" in v:
            v = {**v, "validation": v.pop("validate")}
        out[k] = Provider(key=k, **v)
    return out


def all_providers() -> dict[str, Provider]:
    return _load()


def get_provider(key: str) -> Provider:
    providers = _load()
    if key not in providers:
        raise KeyError(f"unknown integration provider: {key!r}")
    return providers[key]
