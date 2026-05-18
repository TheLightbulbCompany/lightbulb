"""Shared request/response schemas for config-patch routes.

Three routers (`routers/admin.py`, `routers/config.py`, `routers/updates.py`)
each accept the same `{patch: dict}` body to deep-merge into a user's
`openclaw.json`. Keeping the schema in one place prevents the name drift
we already saw (`ConfigPatchBody` vs `ConfigPatchRequest`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConfigPatchRequest(BaseModel):
    patch: dict = Field(..., description="Config patch to deep-merge into openclaw.json")
