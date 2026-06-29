"""Materializer for a CLI delivered as a bundled SKILL (e.g. Vercel).

``write`` does two things at connect time:
  (a) enables ``skills.entries.<key>`` with the pasted token under ``apiKey`` via
      the config patcher (hot-reloaded). OpenClaw injects ``apiKey`` as the skill's
      ``primaryEnv`` into the gateway-host exec env, so a baked CLI reads it at runtime.
  (b) seeds the skill's SKILL.md onto the owner's EFS, substituting the ``{{PKG}}``
      placeholder with the exact pinned package spec (e.g. ``vercel@54.18.2``) so the
      agent always ``npx``-es the version the integration was tested against.

``remove`` reverses both: disables the config entry and deletes the seeded skill dir.

The token lives ONLY in ``skills.entries.<key>.apiKey`` (scrubbed from the marketplace
slice) — never in any ``tools.*`` field."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from core.services import config_patcher
from core.services.credentials import registry
from core.services.credentials.materializers.base import atomic_write, owner_efs_root

# The SKILL.md templates ship next to the materializers package (sibling
# ``skills/`` dir), keyed by the skill's registry key.
_SKILL_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "skills"

# Placeholder in the shipped SKILL.md the materializer rewrites to the exact pinned
# package spec at seed time (sourced from ``provider.skill.package``).
_PKG_PLACEHOLDER = "{{PKG}}"


def _run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive an async config-patcher coroutine from this synchronous materializer.

    The materializer rail offloads materializers via ``run_in_thread`` (see the
    disconnect path in ``connections.py``), so the common case is a worker thread
    with no running loop and a bare ``asyncio.run`` suffices. A caller on a live
    loop is a programming error here — mirror ``mcp_oauth``'s intent of running off
    the request loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("skill_cli materializer must run off the request loop (use run_in_thread)")


def _skill_dir(owner_id: str, key: str) -> Path:
    return owner_efs_root(owner_id) / "skills" / key


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    spec = registry.get_provider(provider_key).skill
    if spec is None:
        raise ValueError(f"{provider_key}: skill_cli requires a `skill` block")
    token = credential["token"]

    # (b) seed SKILL.md, substituting the pinned package spec for the placeholder.
    template = (_SKILL_TEMPLATE_ROOT / spec.key / "SKILL.md").read_text()
    body = template.replace(_PKG_PLACEHOLDER, spec.package)
    atomic_write(_skill_dir(owner_id, spec.key) / "SKILL.md", body.encode())

    # (a) enable the skill + carry the token as apiKey. The leaf write replaces the
    # whole entry so a re-connect refreshes the token; deep-merge can't express that.
    _run_sync(
        config_patcher.set_openclaw_config_path(
            owner_id, ["skills", "entries", spec.key], {"enabled": True, "apiKey": token}
        )
    )


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    spec = registry.get_provider(provider_key).skill
    if spec is None:
        return
    _run_sync(config_patcher.delete_openclaw_config_path(owner_id, ["skills", "entries", spec.key]))
    # ignore_errors keeps disconnect idempotent — a missing dir is a success.
    shutil.rmtree(_skill_dir(owner_id, spec.key), ignore_errors=True)
