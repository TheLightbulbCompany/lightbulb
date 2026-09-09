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
from dataclasses import dataclass
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
_FORK_TAG_RE = re.compile(r"v[0-9]{4}\.[0-9]+\.[0-9]+-isol8\.[0-9]+\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_BUILD_ID_RE = re.compile(r"[0-9a-f]{7}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PinnedReleaseIdentity:
    """Expected immutable identity for the current environment's pin.

    This is configuration evidence only. It does not observe a running task or
    qualify native-room behavior.
    """

    fork_tag: str
    fork_commit: str
    base_image_digest: str
    gateway_build_id: str
    extended_tag: str
    extended_artifact_digest: str
    runtime_manifest_digest: str


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


def _env_or_pinned(name: str, pinned) -> str:
    value = os.environ[name] if name in os.environ else pinned
    return value if isinstance(value, str) else ""


def pinned_release_identity() -> PinnedReleaseIdentity | None:
    """Validated expected release identity for this environment.

    CDK injects every field in deployed backend tasks. Local development and
    CI fall back to the same repo manifest. Any missing, malformed, or
    internally inconsistent field returns ``None`` so future qualification
    callers fail closed instead of accepting partial identity evidence.
    """
    pinned = _load_pinned()
    section = "prod" if (os.getenv("ENVIRONMENT") or "").lower().startswith("prod") else "dev"
    selected = pinned.get(section)
    selected = selected if isinstance(selected, dict) else {}

    fork_tag = _env_or_pinned("OPENCLAW_FORK_TAG", pinned.get("forkTag"))
    fork_commit = _env_or_pinned("OPENCLAW_FORK_COMMIT", pinned.get("forkCommit"))
    base_image_digest = _env_or_pinned("OPENCLAW_BASE_IMAGE_DIGEST", pinned.get("baseImageDigest"))
    gateway_build_id = _env_or_pinned("OPENCLAW_GATEWAY_BUILD_ID", pinned.get("gatewayBuildId"))
    extended_tag = _env_or_pinned(
        "OPENCLAW_PROD_TAG" if section == "prod" else "OPENCLAW_DEV_TAG",
        selected.get("tag"),
    )
    extended_artifact_digest = _env_or_pinned(
        "OPENCLAW_EXTENDED_ARTIFACT_DIGEST",
        selected.get("artifactDigest"),
    )
    runtime_manifest_digest = _env_or_pinned(
        "OPENCLAW_RUNTIME_MANIFEST_DIGEST",
        selected.get("runtimeManifestDigest"),
    )

    valid = (
        _FORK_TAG_RE.fullmatch(fork_tag) is not None
        and _COMMIT_RE.fullmatch(fork_commit) is not None
        and _DIGEST_RE.fullmatch(base_image_digest) is not None
        and _BUILD_ID_RE.fullmatch(gateway_build_id) is not None
        and gateway_build_id == fork_commit[:7]
        and re.fullmatch(rf"{re.escape(fork_tag[1:])}-[0-9a-f]{{7}}", extended_tag) is not None
        and _DIGEST_RE.fullmatch(extended_artifact_digest) is not None
        and _DIGEST_RE.fullmatch(runtime_manifest_digest) is not None
        and extended_artifact_digest != runtime_manifest_digest
    )
    if not valid:
        return None
    return PinnedReleaseIdentity(
        fork_tag=fork_tag,
        fork_commit=fork_commit,
        base_image_digest=base_image_digest,
        gateway_build_id=gateway_build_id,
        extended_tag=extended_tag,
        extended_artifact_digest=extended_artifact_digest,
        runtime_manifest_digest=runtime_manifest_digest,
    )


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


def version_at_least(version: str, target: str) -> bool:
    """True when dotted ``version`` >= dotted ``target``.

    Split out of :func:`deployed_upstream_at_least` so a caller that already
    knows which image a config will run on can compare against THAT rather
    than against this environment's pin. The pin is only correct for a fresh
    provision; a live container can lag it, and during a promotion or a
    rollback it lags it by a whole upstream version.
    """
    return _version_tuple(version) >= _version_tuple(target)


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
    return version_at_least(deployed_upstream_version(), target)


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


def deployed_fork_at_least(base: str, patch: int, *, tag: str | None = None) -> bool:
    """True when the image a config will run on carries a fork patch that
    first shipped as ``<base>-isol8.<patch>``.

    The fork patch counter restarts on every new upstream base
    (``2026.7.2-isol8.8`` → ``2026.8.1-isol8.1``) and picks are carried
    forward, so the test is: newer base ⇒ yes; same base ⇒ patch level
    ``>= patch``; older base ⇒ no.

    ``tag`` is the tag of the image that will READ the config when the caller
    knows it (a regen or roll of an existing container) — pass ``""`` when a
    container exists but its image could not be read, which gates false.
    ``None`` means no container exists yet, so the environment pin
    (:func:`deployed_tag`) is what the new task will run. The distinction
    matters because live containers lag the pin: gating a fork-only key on
    the pin alone would write it onto an older image whose strict schema then
    refuses to boot.
    """
    t = deployed_tag() if tag is None else (tag or "")
    if not t:
        return False
    upstream = _version_tuple(t.split("-", 1)[0])
    wanted = _version_tuple(base)
    if upstream > wanted:
        return True
    if upstream < wanted:
        return False
    return isol8_patch_level(t) >= patch
