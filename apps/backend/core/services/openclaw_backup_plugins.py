"""Re-install an owner's non-bundled OpenClaw plugins from a backup.

Mechanism (verified live on the fleet 2026-06-12, see project memory
``plugin-efs-install-feasible``): the plugin source lands under the owner's
EFS root at ``extensions/<id>/`` (``/home/node/.openclaw/extensions`` in the
container — EFS, not a local volume), and ``plugins.entries.<id>`` is patched
into openclaw.json. The gateway discovers it at the next start; code
registration (``register(api)``) needs a restart, which the import router
triggers once at the end of confirm.

The backend has no Node, so dependencies cannot be installed here. A plugin
whose package.json declares ``dependencies`` is copied and enabled anyway and
the owner gets a warning naming the one command to run inside the container.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from core.containers import get_workspace
from core.services.config_patcher import patch_openclaw_config
from core.services.openclaw_backup_import import PluginCandidate

_SKIP = ("node_modules", ".git", "__pycache__", ".DS_Store")


@dataclass
class PluginApplyResult:
    installed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    patch: dict = field(default_factory=dict)


def _chown_tree(path: Path) -> None:
    """OpenClaw runs as uid 1000; the backend writes as root."""
    for root, dirs, files in os.walk(path):
        for n in (*dirs, *files):
            try:
                os.chown(os.path.join(root, n), 1000, 1000)
            except OSError:
                pass
    try:
        os.chown(path, 1000, 1000)
    except OSError:
        pass


async def apply_plugins(*, owner_id: str, plugins: list[PluginCandidate], chosen: set[str] | None) -> PluginApplyResult:
    res = PluginApplyResult()
    root = get_workspace().user_path(owner_id)
    entries: dict[str, dict] = {}
    for p in plugins:
        if chosen is not None and p.id not in chosen:
            continue
        if p.source_dir is None:
            res.warnings.append(
                f"plugin {p.id!r}: no source in the backup — run `openclaw plugins install {p.package_name}@{p.version}` inside the container"
            )
            continue
        dest = root / "extensions" / p.id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(p.source_dir, dest, symlinks=False, ignore=shutil.ignore_patterns(*_SKIP))
        _chown_tree(dest)
        entries[p.id] = {**p.config, "enabled": True}
        res.installed.append(p.id)
        if p.has_dependencies:
            res.warnings.append(
                f"plugin {p.id!r} needs its dependencies — run `openclaw plugins install {p.package_name}@{p.version}` inside the container (ask Friday)"
            )
    if entries:
        res.patch = {"plugins": {"entries": entries}}
        await patch_openclaw_config(owner_id, res.patch)
    return res
