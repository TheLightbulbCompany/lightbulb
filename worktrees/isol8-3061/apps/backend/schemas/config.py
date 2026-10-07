"""Shared request/response schema for the config-patch routes.

Two routers (`routers/config.py`, `routers/admin.py`) accept the same `{patch: dict}` body,
deep-merged into every roster profile's `config.yaml` by `runtime_rpc.config_patch_roster`.
Keeping the schema in one place prevents the name drift we already saw (`ConfigPatchBody`
vs `ConfigPatchRequest`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConfigPatchRequest(BaseModel):
    patch: dict = Field(..., description="Config patch to deep-merge into each agent profile's config.yaml")
