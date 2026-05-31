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


@lru_cache(maxsize=1)
def _load_pinned() -> dict:
    """Read openclaw-version.json from the repo root if it is reachable.

    Returns ``{}`` when the file is absent (the normal case inside the deployed
    backend image, where the injected env vars are authoritative instead).
    """
    # apps/backend/core/openclaw_version.py -> repo root is parents[3].
    candidate = Path(__file__).resolve().parents[3] / "openclaw-version.json"
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
    """
    return os.getenv("OPENCLAW_UPSTREAM_VERSION") or _load_pinned().get("tag", "")


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
