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
import re
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
_FALLBACK_UPSTREAM_VERSION = "2026.5.28"


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


def deployed_upstream_version() -> str:
    """Upstream OpenClaw version of the extended image actually DEPLOYED to
    this environment — the value for ``meta.lastTouchedVersion``.

    The per-env extended tag is ``<upstream>-<shortsha>`` (e.g.
    ``2026.5.12-0654e00``); we take the ``<upstream>`` prefix so the stamped
    meta tracks the container the gateway is actually running, not the
    build-target ``tag`` — which can be bumped ahead of a per-env promotion
    (a provision in that window would otherwise stamp a "from the future"
    version that trips OpenClaw's warnIfConfigFromFuture). Falls back to
    :func:`upstream_version` when the deployed tag is unset (non-CDK / local).
    """
    deployed = deployed_tag()
    prefix = deployed.split("-", 1)[0] if deployed else ""
    return prefix or upstream_version()


def deployed_tag() -> str:
    """Extended-image tag pinned for THIS environment (``prod.tag`` in prod,
    ``dev.tag`` otherwise) — what a freshly provisioned container will run."""
    env = (os.getenv("ENVIRONMENT") or "").lower()
    return prod_tag() if env.startswith("prod") else dev_tag()


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a dotted version (e.g. ``2026.6.1``) into a comparable int tuple.

    A non-numeric segment collapses to 0 so an unparseable value sorts low —
    a version we can't read will not satisfy a ``>=`` gate, which is the safe
    default (don't emit a newer-only key when the deployed version is unknown).
    """
    parts: list[int] = []
    for segment in v.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def deployed_upstream_at_least(target: str) -> bool:
    """True when the extended image deployed to THIS environment is at least
    ``target`` (an upstream version such as ``2026.6.1``).

    Gates emission of config keys that exist only in newer OpenClaw. The
    gateway validates ``openclaw.json`` against a strict schema at boot, so a
    key the running image doesn't recognize crash-loops the container. Comparing
    against :func:`deployed_upstream_version` (the per-env ``dev.tag``/``prod.tag``
    prefix, not the build-target ``tag``) means a newer-only key is emitted only
    once this environment's deployed image actually supports it, and flips
    per-environment as each tag is promoted.
    """
    return _version_tuple(deployed_upstream_version()) >= _version_tuple(target)


# Extended tags are ``<upstream>-isol8.<n>-<shortsha>`` (e.g.
# ``2026.7.2-isol8.8-8f700f0``); ``<n>`` is the Isol8 fork patch level, which
# is what decides whether a fork-only config key exists — the upstream prefix
# is identical across every isol8.N of the same base.
_ISOL8_PATCH_RE = re.compile(r"-isol8\.(\d+)(?:-|$)")


def isol8_patch_level(tag: str | None) -> int:
    """Fork patch level carried by an extended-image tag; 0 when the tag is
    empty, unknown, or carries no ``isol8.N`` segment (a value we can't read
    must never satisfy a ``>=`` gate)."""
    m = _ISOL8_PATCH_RE.search(tag or "")
    return int(m.group(1)) if m else 0


def deployed_isol8_patch_at_least(target: int, *, tag: str | None = None) -> bool:
    """True when the image a config will run on carries fork patch level
    ``>= target``.

    ``tag`` is the owner's ACTUAL running image tag when the caller knows it
    (a regen or roll of an existing container) — pass ``""`` when a container
    exists but its image could not be read, which gates false. ``None`` means
    no container exists yet, so the environment pin (:func:`deployed_tag`) is
    what the new task will run. The distinction matters because live
    containers lag the pin: gating a fork-only key on the pin alone would
    write it onto an older image whose strict schema then refuses to boot.
    """
    return isol8_patch_level(deployed_tag() if tag is None else tag) >= target
