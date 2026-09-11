"""Manifest construction and safe tar/untar helpers for catalog packages."""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO


@dataclass
class CatalogPackage:
    """A packed catalog item ready for upload to S3.

    `format`: "openclaw" (full agent bundle from EFS) or — historically —
              "skillmd" (a SKILL.md adapter package). The skillmd adapter
              was removed in the Isol8-internal v0 reduction; the type
              still exists for forward compatibility.
    `manifest`: dict written as manifest.json alongside the tarball.
    `openclaw_slice`: openclaw.json patch (agent + plugins entries) the
              deploy flow merges into the buyer's config. Empty for
              non-openclaw formats.
    `tarball_bytes`: gzipped tar of the workspace contents.
    `tarball_contents`: sorted list of relative paths inside the tarball
              (used for catalog list views and audit logs).
    `pruned_paths`: workspace-relative dirs the export dropped as nested
              repo checkouts (reported to the seller, never persisted).
    """

    format: str
    manifest: dict[str, Any]
    openclaw_slice: dict[str, Any]
    tarball_bytes: bytes
    tarball_contents: list[str] = field(default_factory=list)
    pruned_paths: list[str] = field(default_factory=list)


def tar_directory(src: Path) -> bytes:
    """Tar (gzip) a directory's contents with paths relative to src."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(src, arcname=".")
    return buf.getvalue()


def untar_to_directory(tar_stream: BinaryIO, dst: Path) -> None:
    """Extract a tar.gz stream into dst, rejecting any member whose resolved
    path escapes dst (absolute paths, `..` traversal, or link members whose
    linkname could point outside dst). Raises ValueError on a suspicious
    member.
    """
    dst_resolved = dst.resolve()
    with tarfile.open(fileobj=tar_stream, mode="r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            # Link members (symlinks/hardlinks) have a `linkname` that bypasses
            # the name-based traversal check — reject them outright.
            if m.issym() or m.islnk():
                raise ValueError(f"tar member is a symlink or hardlink: {m.name!r} -> {m.linkname!r}")
            if m.name.startswith("/"):
                raise ValueError(f"tar member has absolute path: {m.name!r}")
            target = (dst / m.name).resolve()
            try:
                target.relative_to(dst_resolved)
            except ValueError as exc:
                raise ValueError(f"tar member escapes extraction directory: {m.name!r}") from exc
        tf.extractall(dst)
