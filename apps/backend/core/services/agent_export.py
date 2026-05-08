"""Snapshot a seller's OpenClaw agent from EFS into a marketplace artifact.

Path B for the marketplace publish flow: an Isol8 paid user picks one of
their existing agents and we tar the agent's EFS directory directly into a
CatalogPackage with format="openclaw". No container interaction — we read
the agents/ tree even when the container is scaled to zero.

Snapshot at call time. Subsequent edits to the seller's agent do not affect
already-published versions; the buyer always installs the bytes that were
in S3 at upload time.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from pathlib import Path

from core.containers import get_workspace
from core.containers.workspace import WorkspaceError
from core.observability.metrics import put_metric
from core.services.catalog_package import CatalogPackage
from core.services.catalog_slice import (
    extract_agent_slice,
    filter_cron_jobs_for_agent,
)


_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$")


# Top-level dirs we never want to ship — caches, transient state, VCS metadata.
_SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".cache",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "node_modules",
        ".DS_Store",
    }
)


class AgentNotFoundError(Exception):
    """Seller has no agent at agents/{agent_id} on EFS."""


class InvalidAgentIdError(Exception):
    """agent_id failed format validation or path-traversal guard."""


class AgentNotInConfigError(Exception):
    """agent_id has a workspace dir on EFS but no entry in the seller's openclaw.json
    agents.list — can't synthesize a slice without the agent's identity/skills/tools.
    """


def _resolve_agent_dir(seller_id: str, agent_id: str) -> Path:
    """Validate agent_id format, resolve the EFS path, and assert it stays
    within the seller's agents directory.

    Emits ``marketplace.path_traversal_attempt`` and raises
    InvalidAgentIdError on any traversal attempt.
    """
    if not _AGENT_ID_RE.match(agent_id):
        raise InvalidAgentIdError(f"agent_id must be alphanumeric (with . _ -), 3-64 chars: {agent_id!r}")

    workspace = get_workspace()
    try:
        seller_root = workspace.user_path(seller_id).resolve()
    except WorkspaceError as exc:
        raise InvalidAgentIdError(str(exc)) from exc

    agents_root = seller_root / "agents"
    candidate = (agents_root / agent_id).resolve()

    try:
        candidate.relative_to(agents_root.resolve())
    except ValueError:
        put_metric("marketplace.path_traversal_attempt")
        raise InvalidAgentIdError(f"agent_id resolves outside seller's agents directory: {agent_id!r}")

    return candidate


def _build_tarball(agent_dir: Path) -> tuple[bytes, list[str]]:
    """Tar (gzip) an agent directory, skipping junk dirs and dotfiles at the
    top level. File mtimes are zeroed so two snapshots of unchanged content
    produce identical bytes (deterministic SHA-256).
    """
    if not agent_dir.exists() or not agent_dir.is_dir():
        raise AgentNotFoundError(f"agent dir not found: {agent_dir}")

    contents: list[str] = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:

        def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
            # Skip junk dirs anywhere in the tree.
            parts = tarinfo.name.split("/")
            if any(p in _SKIP_DIR_NAMES for p in parts):
                return None
            # Skip hidden files / hidden dirs anywhere in the tree. The
            # leading "." in tarinfo.name is the arcname root (we pass
            # arcname="."), so ignore it; any other path segment starting
            # with "." is a dotfile/dotdir. Without this, .env, .ssh/*,
            # .aws/credentials, .openclaw/secrets — anything a seller had
            # in their workspace — would ship in the marketplace artifact.
            for p in parts:
                if p in (".", ""):
                    continue
                if p.startswith("."):
                    return None
            # Reject symlinks defensively (catalog_package.untar already does
            # the receiving-side check, but rejecting here keeps the artifact
            # auditable).
            if tarinfo.issym() or tarinfo.islnk():
                return None
            # Determinism: zero mtimes + uid/gid so identical content → identical bytes.
            tarinfo.mtime = 0
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            if not tarinfo.isdir():
                contents.append(tarinfo.name)
            return tarinfo

        # arcname="." gives us paths like "./openclaw.json" — same convention
        # as catalog_package.tar_directory uses for catalog uploads.
        tf.add(str(agent_dir), arcname=".", filter=_filter)

    return buf.getvalue(), sorted(contents)


def _summarize_openclaw_config(agent_dir: Path) -> dict:
    """Extract a small summary from openclaw.json if present.

    Best-effort; returns minimal dict on missing / invalid file.
    """
    cfg_path = agent_dir / "openclaw.json"
    if not cfg_path.exists():
        return {"name": agent_dir.name, "description": ""}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"name": agent_dir.name, "description": ""}

    identity = data.get("identity", {}) if isinstance(data, dict) else {}
    return {
        "name": identity.get("name") or agent_dir.name,
        "description": identity.get("description") or identity.get("vibe") or "",
    }


def export_agent_from_efs(seller_id: str, agent_id: str) -> CatalogPackage:
    """Read the seller's agent dir from EFS, tar it, return a CatalogPackage.

    The CatalogPackage carries an ``openclaw_slice`` synthesized from the
    seller's per-user openclaw.json so the buyer-side deploy can register
    the new agent in their config (matching the admin-catalog publish
    behavior). Without the slice, deploy_from_artifact extracts files but
    leaves the buyer's openclaw.json untouched.

    Args:
        seller_id: Clerk user id of the seller (also the EFS dir owner).
        agent_id: Subdirectory name under ``agents/`` to publish.

    Raises:
        InvalidAgentIdError: agent_id failed format / path-traversal check.
        AgentNotFoundError: agent_id not found on EFS for this seller.
        AgentNotInConfigError: agent dir exists but agent_id isn't registered
            in the seller's openclaw.json (or the config file is missing).
    """
    agent_dir = _resolve_agent_dir(seller_id=seller_id, agent_id=agent_id)
    tarball_bytes, contents = _build_tarball(agent_dir)

    workspace = get_workspace()
    config = workspace.read_openclaw_config(seller_id)
    if not config:
        raise AgentNotInConfigError(
            f"seller {seller_id!r} has no openclaw.json — publish the agent from a container that has been provisioned"
        )
    try:
        slice_ = extract_agent_slice(config, agent_id)
    except KeyError as exc:
        raise AgentNotInConfigError(
            f"agent {agent_id!r} is not registered in seller's openclaw.json agents.list"
        ) from exc
    slice_["cron_jobs"] = filter_cron_jobs_for_agent(workspace.read_cron_jobs(seller_id), agent_id)

    summary = _summarize_openclaw_config(agent_dir)
    manifest = {
        "name": summary["name"],
        "description": summary["description"],
        "format": "openclaw",
        "exported_at": int(time.time()),
        "agent_id": agent_id,
        "file_count": len(contents),
    }

    return CatalogPackage(
        format="openclaw",
        manifest=manifest,
        openclaw_slice=slice_,
        tarball_bytes=tarball_bytes,
        tarball_contents=contents,
    )
