"""Materializer for a CLI delivered as a bundled SKILL (e.g. Vercel).

``write`` seeds the skill's SKILL.md, substituting the ``{{PKG}}`` placeholder with the
exact pinned package spec (e.g. ``vercel@54.18.2``) so the agent always ``npx``-es the
version the integration was tested against. ``remove`` deletes the seeded skill dir.

NOT DISPATCHED TODAY. The one materializer dispatch is ``credentials.hermes_cli.project``,
which gates on ``OWNED_PATHS``; this rail is absent from it, so ``materializer_root()`` raises
if anything ever calls ``write``/``remove``. That is deliberate — a silent write to a path no
runtime reads is worse. Wiring it up means adding it to ``hermes_cli.OWNED_PATHS``/``ROOT_DIR``
and giving the credential a delivery the microVM's shell actually reads.

KNOWN GAP (carried by deletion PR-5, not introduced by it): the pasted token used to ride
``skills.entries.<key>.apiKey``, which OpenClaw injected into the exec env. There is no such
config on Hermes, so wiring this rail up also needs the token delivered through
``hermes_cli.CLI_ENV_FILE`` (the shell-env store) rather than a skill field."""

from __future__ import annotations

import shutil
from pathlib import Path

from core.services.credentials import registry
from core.services.credentials.materializers.base import atomic_write, materializer_root

# The SKILL.md templates ship next to the materializers package (sibling
# ``skills/`` dir), keyed by the skill's registry key.
_SKILL_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "skills"

# Placeholder in the shipped SKILL.md the materializer rewrites to the exact pinned
# package spec at seed time (sourced from ``provider.skill.package``).
_PKG_PLACEHOLDER = "{{PKG}}"


def _skill_dir(owner_id: str, key: str) -> Path:
    return materializer_root() / "skills" / key


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    spec = registry.get_provider(provider_key).skill
    if spec is None:
        raise ValueError(f"{provider_key}: skill_cli requires a `skill` block")
    # Seed SKILL.md, substituting the pinned package spec for the placeholder.
    template = (_SKILL_TEMPLATE_ROOT / spec.key / "SKILL.md").read_text()
    body = template.replace(_PKG_PLACEHOLDER, spec.package)
    atomic_write(_skill_dir(owner_id, spec.key) / "SKILL.md", body.encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    spec = registry.get_provider(provider_key).skill
    if spec is None:
        return
    # ignore_errors keeps disconnect idempotent — a missing dir is a success.
    shutil.rmtree(_skill_dir(owner_id, spec.key), ignore_errors=True)
