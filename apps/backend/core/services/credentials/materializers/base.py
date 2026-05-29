"""Materializers write a CLI's own credential file onto the owner's EFS workspace
(0600, owned by uid/gid 1000, atomic) so a baked CLI reads it at runtime. This
generalizes core/containers/workspace.py:700-786 (the ChatGPT auth-profile rail)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from core.config import settings

NODE_UID = 1000
NODE_GID = 1000


class Materializer(Protocol):
    def __call__(self, owner_id: str, provider_key: str, credential: dict) -> None: ...


def owner_efs_root(owner_id: str) -> Path:
    return Path(settings.EFS_MOUNT_PATH) / owner_id


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        if os.geteuid() == 0:  # only in-container (root); tests run non-root and skip chown
            os.chown(tmp, NODE_UID, NODE_GID)
        os.replace(tmp, path)
        if os.geteuid() == 0:
            os.chown(path.parent, NODE_UID, NODE_GID)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
