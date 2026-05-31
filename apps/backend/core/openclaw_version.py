"""Single source of truth for the pinned OpenClaw image versions/tags.

The canonical source is ``openclaw-version.json`` at the repo root. It has two
kinds of reader:

1. **AWS CDK at synth time** (``apps/infra/lib/stacks/*-stack.ts``) reads the
   file directly and injects the resolved values into the backend container as
   env vars. The backend image's Docker build context is ``apps/backend/`` (see
   ``service-stack.ts`` ``DockerImageAsset``), so ``openclaw-version.json`` —
   which lives at the repo root — is outside the context and cannot be
   ``COPY``-d into the image. Env injection is how the running container learns
   the pinned tags.

2. **This module at runtime.** It prefers the CDK-injected env vars; in local
   dev / CI (where the whole repo is checked out) it falls back to reading the
   JSON directly. Either way there is one source of truth and no hand-mirrored
   constants to keep in sync — bumping ``openclaw-version.json`` is the only
   edit a version change needs.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

# Last-resort default for the upstream version ONLY. The deploy path never uses
# it (CDK injects OPENCLAW_UPSTREAM_VERSION from openclaw-version.json) and local
# dev / CI read the repo JSON. It exists solely so a non-CDK runner of the
# backend image (the Dockerfile's documented `docker run --env-file .env` flow,
# where the JSON is outside the build context and the env var may be unset) still
# gets a valid, non-empty meta.lastTouchedVersion — otherwise an empty string
# reintroduces OpenClaw's auto-restore/"config from the future" warning. Keep it
# roughly current on upstream bumps; it is a safety floor, not a drift-checked
# mirror, so exact lockstep is not required.
_FALLBACK_UPSTREAM_VERSION = "2026.5.12"


@lru_cache(maxsize=1)
def _load_pinned() -> dict:
    """Read openclaw-version.json by searching upward from this file.

    Returns ``{}`` when the file is not found (the normal case inside the
    deployed backend image, whose Docker build context is ``apps/backend/`` so
    the repo-root JSON is absent — the injected env vars are authoritative
    there). We walk ancestors instead of assuming a fixed depth: in the image
    ``__file__`` is ``/app/core/openclaw_version.py`` (only 3 ancestors), so a
    hardcoded ``parents[3]`` would raise IndexError and crash this function
    before the env/fallback resolution downstream could take over.
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "openclaw-version.json"
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except (OSError, ValueError):
                return {}
    return {}


def upstream_version() -> str:
    """Pinned upstream OpenClaw version (``openclaw-version.json#tag``).

    Used by the meta shim in ``core/containers/config.py`` that records
    ``meta.lastTouchedVersion`` so OpenClaw's auto-restore does not fire.
    Resolution order: CDK-injected env var → repo JSON → hardcoded fallback.
    The fallback guarantees a non-empty version even on a non-CDK
    ``docker run`` of the backend image where neither source is available.
    """
    return os.getenv("OPENCLAW_UPSTREAM_VERSION") or _load_pinned().get("tag") or _FALLBACK_UPSTREAM_VERSION


def dev_tag() -> str:
    """Extended-image tag deployed to dev (``openclaw-version.json#dev.tag``)."""
    env = os.getenv("OPENCLAW_DEV_TAG")
    if env:
        return env
    dev = _load_pinned().get("dev")
    return dev.get("tag", "") if isinstance(dev, dict) else ""


def prod_tag() -> str:
    """Extended-image tag deployed to prod (``openclaw-version.json#prod.tag``)."""
    env = os.getenv("OPENCLAW_PROD_TAG")
    if env:
        return env
    prod = _load_pinned().get("prod")
    return prod.get("tag", "") if isinstance(prod, dict) else ""
