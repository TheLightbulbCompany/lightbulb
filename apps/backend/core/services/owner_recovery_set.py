"""Create a version-pinned, verified pre-migration recovery set for one owner.

The caller owns ECS orchestration.  This provider accepts only evidence for one
captured source task that is already ``STOPPED`` with its service at 0/0/0.  At
that point Litestream's supervised shutdown has completed its final sync, so a
stable S3 inventory and the EFS tree can be checkpointed without inventing a
rollback point from a live replica prefix.

Recovery artifacts live under the existing, versioned marketplace-artifacts
bucket.  Every returned locator includes an S3 VersionId; the CDK bucket policy
prevents the backend role from permanently deleting versions below
``recovery-sets/``.  A canonical manifest is written only after every copied
replica restores and every EFS byte is accounted for.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import boto3
from botocore.exceptions import ClientError

from core.config import settings

PROVIDER_NAME = "lightbulb-owner-recovery-set"
PROVIDER_VERSION = "1"
MANIFEST_SCHEMA_VERSION = 1
PINNED_LITESTREAM_VERSION = "0.5.15"
LITESTREAM_SYNC_INTERVAL_SECONDS = 10
STABILITY_WAIT_SECONDS = 11
_MULTIPART_PART_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_COPY_METADATA_SOURCE_ETAG = "source-etag"
_COPY_METADATA_SOURCE_SIZE = "source-size"
_COPY_METADATA_SOURCE_KEY_SHA = "source-key-sha256"
_ARCHIVE_METADATA_TREE_SHA = "tree-manifest-sha256"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LTX_TAIL = r"(?P<level>[0-9]{4})/(?P<txid>[0-9A-Fa-f]{8,16}-[0-9A-Fa-f]{8,16})\.ltx"


class OwnerRecoverySetUnavailable(RuntimeError):
    """The provider could not prove a complete, coherent recovery set."""


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OwnerRecoverySetUnavailable("recovery evidence is not canonical JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso8601(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise OwnerRecoverySetUnavailable(f"{field} is unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OwnerRecoverySetUnavailable(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise OwnerRecoverySetUnavailable(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _aws_time(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise OwnerRecoverySetUnavailable("replica LastModified is not timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    return _iso8601(value, "replica LastModified")


def _etag(value: object, field: str = "ETag") -> str:
    if not isinstance(value, str) or not value.strip('"'):
        raise OwnerRecoverySetUnavailable(f"{field} is unavailable")
    return value.strip('"')


def _required_string(mapping: Mapping[str, object], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise OwnerRecoverySetUnavailable(f"{field} is unavailable")
    return value


def _positive_int(mapping: Mapping[str, object], field: str) -> int:
    value = mapping.get(field)
    if type(value) is not int or value <= 0:
        raise OwnerRecoverySetUnavailable(f"{field} is invalid")
    return value


def _zero(mapping: Mapping[str, object], field: str) -> None:
    if type(mapping.get(field)) is not int or mapping[field] != 0:
        raise OwnerRecoverySetUnavailable("source service is not fully stopped")


def validate_recovery_inputs(
    owner_id: str,
    update_id: str,
    release: Mapping[str, object],
    source: Mapping[str, object],
    drain_evidence: Mapping[str, object],
) -> None:
    """Reject caller claims that do not prove the captured source is stopped."""

    if not _SAFE_ID_RE.fullmatch(owner_id) or not _SAFE_ID_RE.fullmatch(update_id):
        raise OwnerRecoverySetUnavailable("owner or update id is unsafe")
    required_release = {"release_id", "image_digest", "build_id", "schema_id"}
    required_source = {
        "service_arn",
        "task_arn",
        "task_definition_arn",
        "image_digest",
        "access_point_id",
        "file_system_id",
        "efs_root",
        "shared_schema_version",
        "agent_schema_version",
    }
    required_drain = {
        "observed_at",
        "service_arn",
        "service_desired_count",
        "service_running_count",
        "service_pending_count",
        "stopped_tasks",
    }
    if set(release) != required_release or set(source) != required_source or set(drain_evidence) != required_drain:
        raise OwnerRecoverySetUnavailable("recovery input fields are incomplete or ambiguous")
    for field in required_release:
        _required_string(release, field)
    for field in required_source - {"shared_schema_version", "agent_schema_version"}:
        _required_string(source, field)
    if not _IMAGE_DIGEST_RE.fullmatch(str(release["image_digest"])) or not _IMAGE_DIGEST_RE.fullmatch(
        str(source["image_digest"])
    ):
        raise OwnerRecoverySetUnavailable("source or target image digest is invalid")
    if not re.fullmatch(r"fsap-[0-9a-f]{17}", str(source["access_point_id"])):
        raise OwnerRecoverySetUnavailable("source access point id is invalid")
    if not re.fullmatch(r"fs-[0-9a-f]{17}", str(source["file_system_id"])):
        raise OwnerRecoverySetUnavailable("source file system id is invalid")
    if source["efs_root"] != f"/users/{owner_id}":
        raise OwnerRecoverySetUnavailable("source EFS root does not belong to owner")
    _positive_int(source, "shared_schema_version")
    _positive_int(source, "agent_schema_version")
    _iso8601(drain_evidence.get("observed_at"), "drain observed_at")
    if drain_evidence.get("service_arn") != source.get("service_arn"):
        raise OwnerRecoverySetUnavailable("drain service does not match source service")
    for field in ("service_desired_count", "service_running_count", "service_pending_count"):
        _zero(drain_evidence, field)
    tasks = drain_evidence.get("stopped_tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise OwnerRecoverySetUnavailable("exactly one STOPPED source task is required")
    task = tasks[0]
    if set(task) != {"task_arn", "last_status", "task_definition_arn", "image_digest"}:
        raise OwnerRecoverySetUnavailable("source task drain evidence is incomplete")
    if task.get("last_status") != "STOPPED":
        raise OwnerRecoverySetUnavailable("source task has not reached STOPPED")
    if task.get("task_arn") != source.get("task_arn"):
        raise OwnerRecoverySetUnavailable("drain evidence does not identify the source task")
    if task.get("task_definition_arn") != source.get("task_definition_arn"):
        raise OwnerRecoverySetUnavailable("drained task definition does not match source")
    if task.get("image_digest") != source.get("image_digest"):
        raise OwnerRecoverySetUnavailable("drained task image digest does not match source")


@dataclass(frozen=True, order=True)
class ReplicaObject:
    key: str
    etag: str
    size: int
    last_modified: str

    @classmethod
    def from_aws(cls, value: Mapping[str, object], owner_id: str) -> "ReplicaObject":
        key = value.get("Key")
        size = value.get("Size")
        if not isinstance(key, str) or not key.startswith(f"{owner_id}/"):
            raise OwnerRecoverySetUnavailable("replica inventory escaped the owner prefix")
        if type(size) is not int or size < 0:
            raise OwnerRecoverySetUnavailable("replica object size is invalid")
        return cls(
            key=key,
            etag=_etag(value.get("ETag")),
            size=size,
            last_modified=_aws_time(value.get("LastModified")),
        )

    def as_aws_dict(self) -> dict:
        return {"Key": self.key, "ETag": f'"{self.etag}"', "Size": self.size, "LastModified": self.last_modified}

    def manifest_dict(self) -> dict:
        return {
            "key": self.key,
            "etag": self.etag,
            "size": self.size,
            "last_modified": self.last_modified,
        }


@dataclass(frozen=True)
class ReplicaGroups:
    shared: tuple[ReplicaObject, ...]
    agents: dict[str, tuple[ReplicaObject, ...]]
    tombstoned_agents: tuple[str, ...]


@dataclass(frozen=True)
class StableReplicaInventory:
    objects: tuple[ReplicaObject, ...]
    inventory_sha256: str
    first_observed_at: str
    second_observed_at: str
    wait_seconds: int


def group_replica_objects(owner_id: str, objects: Sequence[ReplicaObject]) -> ReplicaGroups:
    owner = re.escape(owner_id)
    shared_re = re.compile(rf"^{owner}/state/openclaw\.sqlite/{_LTX_TAIL}$")
    agent_re = re.compile(
        rf"^{owner}/agents/(?P<agent>[A-Za-z0-9][A-Za-z0-9._-]{{0,127}})/agent/openclaw-agent\.sqlite/{_LTX_TAIL}$"
    )
    tombstone_re = re.compile(
        rf"^{owner}/agents/(?P<agent>[A-Za-z0-9][A-Za-z0-9._-]{{0,127}})/\.replica-deleted$"
    )
    tombstoned: set[str] = set()
    for obj in objects:
        match = tombstone_re.fullmatch(obj.key)
        if match:
            tombstoned.add(match.group("agent"))
    shared: list[ReplicaObject] = []
    agents: dict[str, list[ReplicaObject]] = {}
    for obj in objects:
        if tombstone_re.fullmatch(obj.key):
            continue
        if shared_re.fullmatch(obj.key):
            shared.append(obj)
            continue
        match = agent_re.fullmatch(obj.key)
        if match:
            agent_id = match.group("agent")
            if agent_id not in tombstoned:
                agents.setdefault(agent_id, []).append(obj)
            continue
        raise OwnerRecoverySetUnavailable(f"unrecognized replica object under owner prefix: {obj.key}")
    if not shared:
        raise OwnerRecoverySetUnavailable("shared database replica is unavailable")
    return ReplicaGroups(
        shared=tuple(sorted(shared)),
        agents={agent: tuple(sorted(values)) for agent, values in sorted(agents.items())},
        tombstoned_agents=tuple(sorted(tombstoned)),
    )


def inspect_restored_database(
    path: Path,
    *,
    expected_role: str,
    expected_schema: int,
    expected_agent_id: str | None = None,
) -> dict:
    if expected_role not in {"global", "agent"}:
        raise OwnerRecoverySetUnavailable("expected database role is invalid")
    if not path.is_file() or path.is_symlink():
        raise OwnerRecoverySetUnavailable("restored database file is unavailable")
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            quick = [row[0] for row in db.execute("PRAGMA quick_check").fetchall()]
            version = db.execute("PRAGMA user_version").fetchone()
            metadata = db.execute(
                "SELECT role, schema_version, agent_id FROM schema_meta WHERE meta_key = 'primary'"
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise OwnerRecoverySetUnavailable("restored database could not be inspected") from exc
    if quick != ["ok"]:
        raise OwnerRecoverySetUnavailable("restored database failed quick_check")
    if not version or type(version[0]) is not int or version[0] != expected_schema:
        raise OwnerRecoverySetUnavailable("restored database has the wrong schema version")
    if not metadata or metadata[0] != expected_role or metadata[1] != expected_schema:
        raise OwnerRecoverySetUnavailable("restored database schema metadata is invalid")
    observed_agent = metadata[2]
    if expected_role == "global" and observed_agent is not None:
        raise OwnerRecoverySetUnavailable("shared database carries an agent identity")
    if expected_role == "agent" and observed_agent != expected_agent_id:
        raise OwnerRecoverySetUnavailable("restored database has the wrong agent identity")
    return {
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
        "quick_check": "ok",
        "user_version": version[0],
        "role": metadata[0],
        "schema_version": metadata[1],
        "agent_id": observed_agent,
    }


def _file_digest(file_descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(file_descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _manifest_stat(path: str, kind: str, value: os.stat_result) -> dict:
    return {
        "path": path,
        "type": kind,
        "mode": stat.S_IMODE(value.st_mode),
        "uid": value.st_uid,
        "gid": value.st_gid,
        "mtime_ns": value.st_mtime_ns,
    }


def _scan_directory(directory_fd: int, relative: PurePosixPath, entries: list[dict]) -> None:
    try:
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise OwnerRecoverySetUnavailable("EFS directory could not be enumerated") from exc
    for child in children:
        child_relative = relative / child.name if str(relative) != "." else PurePosixPath(child.name)
        path = child_relative.as_posix()
        try:
            observed = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(observed.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
                child_fd = os.open(child.name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
                        raise OwnerRecoverySetUnavailable("EFS directory changed while scanning")
                    entries.append(_manifest_stat(path, "directory", opened))
                    _scan_directory(child_fd, child_relative, entries)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(observed.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                child_fd = os.open(child.name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                        observed.st_dev,
                        observed.st_ino,
                    ):
                        raise OwnerRecoverySetUnavailable("EFS file changed while scanning")
                    digest, size = _file_digest(child_fd)
                    after = os.fstat(child_fd)
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ) or size != after.st_size:
                        raise OwnerRecoverySetUnavailable("EFS file changed while scanning")
                    entry = _manifest_stat(path, "file", after)
                    entry.update({"size": size, "sha256": digest})
                    entries.append(entry)
                finally:
                    os.close(child_fd)
            elif stat.S_ISLNK(observed.st_mode):
                target = os.readlink(child.name, dir_fd=directory_fd)
                entry = _manifest_stat(path, "symlink", observed)
                entry["link_target"] = target
                entries.append(entry)
            else:
                raise OwnerRecoverySetUnavailable(f"unsupported special file in EFS tree: {path}")
        except OSError as exc:
            raise OwnerRecoverySetUnavailable(f"EFS entry changed while scanning: {path}") from exc


def scan_efs_tree(root: Path) -> tuple[dict, ...]:
    try:
        root_lstat = root.lstat()
    except OSError as exc:
        raise OwnerRecoverySetUnavailable("owner EFS root is unavailable") from exc
    if not stat.S_ISDIR(root_lstat.st_mode) or root.is_symlink():
        raise OwnerRecoverySetUnavailable("owner EFS root is not a real directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise OwnerRecoverySetUnavailable("owner EFS root could not be opened") from exc
    try:
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (root_lstat.st_dev, root_lstat.st_ino):
            raise OwnerRecoverySetUnavailable("owner EFS root changed while scanning")
        entries = [_manifest_stat(".", "directory", opened)]
        _scan_directory(root_fd, PurePosixPath("."), entries)
        return tuple(entries)
    finally:
        os.close(root_fd)


def _open_beneath(root_fd: int, relative: PurePosixPath, *, directory: bool) -> int:
    if relative.is_absolute() or ".." in relative.parts or str(relative) in {"", "."}:
        raise OwnerRecoverySetUnavailable("archive path is unsafe")
    current = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= os.O_DIRECTORY
        return os.open(relative.parts[-1], flags, dir_fd=current)
    except OSError as exc:
        raise OwnerRecoverySetUnavailable(f"EFS entry changed while archiving: {relative.as_posix()}") from exc
    finally:
        os.close(current)


def _readlink_beneath(root_fd: int, relative: PurePosixPath) -> str:
    if relative.is_absolute() or ".." in relative.parts or str(relative) in {"", "."}:
        raise OwnerRecoverySetUnavailable("archive symlink path is unsafe")
    current = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return os.readlink(relative.parts[-1], dir_fd=current)
    except OSError as exc:
        raise OwnerRecoverySetUnavailable(f"EFS symlink changed while archiving: {relative.as_posix()}") from exc
    finally:
        os.close(current)


class _HashingReader(io.RawIOBase):
    def __init__(self, file_descriptor: int):
        self._stream = os.fdopen(file_descriptor, "rb", closefd=True)
        self._digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        self._digest.update(chunk)
        self.size += len(chunk)
        return chunk

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def close(self) -> None:
        self._stream.close()
        super().close()


def _tar_info(entry: Mapping[str, object]) -> tarfile.TarInfo:
    name = str(entry["path"])
    info = tarfile.TarInfo(name=name)
    info.mode = int(entry["mode"])
    info.uid = int(entry["uid"])
    info.gid = int(entry["gid"])
    info.mtime = int(int(entry["mtime_ns"]) / 1_000_000_000)
    info.uname = ""
    info.gname = ""
    kind = entry["type"]
    if kind == "directory":
        info.type = tarfile.DIRTYPE
        info.size = 0
    elif kind == "symlink":
        info.type = tarfile.SYMTYPE
        info.linkname = str(entry["link_target"])
        info.size = 0
    elif kind == "file":
        info.type = tarfile.REGTYPE
        info.size = int(entry["size"])
    else:
        raise OwnerRecoverySetUnavailable("archive manifest contains an unsupported entry")
    return info


def write_efs_tar(root: Path, entries: Sequence[Mapping[str, object]], sink: BinaryIO) -> None:
    """Stream a deterministic tar without following any symlink."""

    expected = tuple(dict(entry) for entry in entries)
    if scan_efs_tree(root) != expected:
        raise OwnerRecoverySetUnavailable("EFS tree changed while archiving")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with tarfile.open(fileobj=sink, mode="w|") as archive:
            for entry in expected:
                relative = PurePosixPath(str(entry["path"]))
                info = _tar_info(entry)
                if str(relative) == ".":
                    archive.addfile(info)
                    continue
                if entry["type"] == "directory":
                    fd = _open_beneath(root_fd, relative, directory=True)
                    try:
                        observed = os.fstat(fd)
                        if stat.S_IMODE(observed.st_mode) != entry["mode"]:
                            raise OwnerRecoverySetUnavailable("EFS directory changed while archiving")
                    finally:
                        os.close(fd)
                    archive.addfile(info)
                elif entry["type"] == "symlink":
                    if _readlink_beneath(root_fd, relative) != entry["link_target"]:
                        raise OwnerRecoverySetUnavailable("EFS symlink changed while archiving")
                    archive.addfile(info)
                else:
                    reader = _HashingReader(_open_beneath(root_fd, relative, directory=False))
                    try:
                        archive.addfile(info, reader)
                        if reader.size != entry["size"] or reader.sha256 != entry["sha256"]:
                            raise OwnerRecoverySetUnavailable("EFS file changed while archiving")
                    finally:
                        reader.close()
    finally:
        os.close(root_fd)
    if scan_efs_tree(root) != expected:
        raise OwnerRecoverySetUnavailable("EFS tree changed while archiving")


class _MultipartSink:
    def __init__(self, s3, bucket: str, key: str, metadata: dict[str, str]):
        self._s3 = s3
        self.bucket = bucket
        self.key = key
        try:
            response = s3.create_multipart_upload(Bucket=bucket, Key=key, Metadata=metadata)
            self._upload_id = response["UploadId"]
        except (ClientError, KeyError) as exc:
            raise OwnerRecoverySetUnavailable("EFS archive upload could not start") from exc
        self._buffer = bytearray()
        self._parts: list[dict] = []
        self._digest = hashlib.sha256()
        self.size = 0
        self._finished = False

    def write(self, value: bytes) -> int:
        if self._finished:
            raise OwnerRecoverySetUnavailable("EFS archive upload is already closed")
        data = bytes(value)
        self._digest.update(data)
        self.size += len(data)
        self._buffer.extend(data)
        while len(self._buffer) >= _MULTIPART_PART_BYTES:
            part = bytes(self._buffer[:_MULTIPART_PART_BYTES])
            del self._buffer[:_MULTIPART_PART_BYTES]
            self._upload_part(part)
        return len(data)

    def tell(self) -> int:
        return self.size

    def flush(self) -> None:
        return None

    def _upload_part(self, body: bytes) -> None:
        try:
            response = self._s3.upload_part(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self._upload_id,
                PartNumber=len(self._parts) + 1,
                Body=body,
            )
            etag = response["ETag"]
        except (ClientError, KeyError) as exc:
            raise OwnerRecoverySetUnavailable("EFS archive part upload failed") from exc
        self._parts.append({"ETag": etag, "PartNumber": len(self._parts) + 1})

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def complete(self) -> dict:
        if self._finished:
            raise OwnerRecoverySetUnavailable("EFS archive upload already completed")
        if self._buffer or not self._parts:
            self._upload_part(bytes(self._buffer))
            self._buffer.clear()
        try:
            response = self._s3.complete_multipart_upload(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts},
            )
        except ClientError as exc:
            raise OwnerRecoverySetUnavailable("EFS archive upload could not complete") from exc
        self._finished = True
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise OwnerRecoverySetUnavailable("EFS archive destination has no immutable VersionId")
        return {"version_id": version_id, "etag": _etag(response.get("ETag"), "archive ETag")}

    def abort(self) -> None:
        if self._finished:
            return
        try:
            self._s3.abort_multipart_upload(
                Bucket=self.bucket,
                Key=self.key,
                UploadId=self._upload_id,
            )
        except ClientError:
            # Preserve the original fail-closed archive error. A completed
            # upload cannot be aborted, and an incomplete orphan is harmless:
            # no verified manifest references it.
            pass
        finally:
            self._finished = True


class OwnerRecoverySetProvider:
    def __init__(
        self,
        *,
        s3=None,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
        litestream_binary: str = "/usr/local/bin/litestream",
    ):
        self.s3 = s3 or boto3.client("s3")
        self.wait = wait
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.litestream_binary = litestream_binary

    def _now_iso(self) -> str:
        value = self.now()
        if value.tzinfo is None:
            raise OwnerRecoverySetUnavailable("provider clock is not timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    def _list_replica_objects(self, owner_id: str) -> tuple[ReplicaObject, ...]:
        paginator = self.s3.get_paginator("list_objects_v2")
        objects: dict[str, ReplicaObject] = {}
        try:
            pages = paginator.paginate(Bucket=settings.LITESTREAM_BUCKET, Prefix=f"{owner_id}/")
            for page in pages:
                for raw in page.get("Contents") or []:
                    observed = ReplicaObject.from_aws(raw, owner_id)
                    if observed.key in objects:
                        raise OwnerRecoverySetUnavailable("replica inventory contains a duplicate key")
                    objects[observed.key] = observed
        except ClientError as exc:
            raise OwnerRecoverySetUnavailable("replica inventory could not be listed") from exc
        return tuple(sorted(objects.values()))

    async def stable_replica_inventory(self, owner_id: str) -> StableReplicaInventory:
        first_at = self._now_iso()
        first = await asyncio.to_thread(self._list_replica_objects, owner_id)
        await self.wait(STABILITY_WAIT_SECONDS)
        second_at = self._now_iso()
        second = await asyncio.to_thread(self._list_replica_objects, owner_id)
        if first != second:
            raise OwnerRecoverySetUnavailable("replica inventory changed after drain")
        group_replica_objects(owner_id, second)
        inventory_sha = sha256_bytes(canonical_json([obj.manifest_dict() for obj in second]))
        return StableReplicaInventory(second, inventory_sha, first_at, second_at, STABILITY_WAIT_SECONDS)

    @staticmethod
    def _versioned_head_evidence(response: Mapping[str, object], *, key: str, version_id: str) -> dict:
        length = response.get("ContentLength")
        if type(length) is not int or length < 0:
            raise OwnerRecoverySetUnavailable("recovery object size is invalid")
        observed_version = response.get("VersionId")
        if observed_version != version_id or version_id == "null":
            raise OwnerRecoverySetUnavailable("recovery object VersionId is unavailable")
        return {"key": key, "version_id": version_id, "etag": _etag(response.get("ETag")), "size": length}

    def copy_replica_object(
        self,
        source: ReplicaObject,
        source_bucket: str,
        destination_bucket: str,
        destination_key: str,
    ) -> dict:
        metadata = {
            _COPY_METADATA_SOURCE_ETAG: source.etag,
            _COPY_METADATA_SOURCE_SIZE: str(source.size),
            _COPY_METADATA_SOURCE_KEY_SHA: sha256_bytes(source.key.encode()),
        }
        try:
            existing = self.s3.head_object(Bucket=destination_bucket, Key=destination_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                raise OwnerRecoverySetUnavailable("recovery replica destination could not be inspected") from exc
        else:
            version_id = existing.get("VersionId")
            if (
                isinstance(version_id, str)
                and version_id != "null"
                and existing.get("Metadata") == metadata
                and existing.get("ContentLength") == source.size
            ):
                evidence = self._versioned_head_evidence(existing, key=destination_key, version_id=version_id)
                return {
                    **evidence,
                    "source_key": source.key,
                    "source_etag": source.etag,
                    "source_size": source.size,
                    "source_last_modified": source.last_modified,
                }
            raise OwnerRecoverySetUnavailable("recovery replica destination conflicts with a prior attempt")
        try:
            copied = self.s3.copy_object(
                Bucket=destination_bucket,
                Key=destination_key,
                CopySource={"Bucket": source_bucket, "Key": source.key},
                CopySourceIfMatch=f'"{source.etag}"',
                Metadata=metadata,
                MetadataDirective="REPLACE",
            )
        except ClientError as exc:
            raise OwnerRecoverySetUnavailable("replica changed or failed while copying") from exc
        version_id = copied.get("VersionId")
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise OwnerRecoverySetUnavailable("replica recovery destination has no immutable VersionId")
        try:
            head = self.s3.head_object(Bucket=destination_bucket, Key=destination_key, VersionId=version_id)
        except ClientError as exc:
            raise OwnerRecoverySetUnavailable("copied replica version could not be verified") from exc
        evidence = self._versioned_head_evidence(head, key=destination_key, version_id=version_id)
        if head.get("Metadata") != metadata or evidence["size"] != source.size:
            raise OwnerRecoverySetUnavailable("copied replica version does not match its source")
        return {
            **evidence,
            "source_key": source.key,
            "source_etag": source.etag,
            "source_size": source.size,
            "source_last_modified": source.last_modified,
        }

    def _litestream_version(self) -> str:
        try:
            result = subprocess.run(
                [self.litestream_binary, "version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OwnerRecoverySetUnavailable("pinned Litestream binary is unavailable") from exc
        version = result.stdout.strip()
        version_pattern = rf"(?:^|\s)v?{re.escape(PINNED_LITESTREAM_VERSION)}(?:\s|$)"
        if (
            result.returncode != 0
            or not version
            or len(version) > 200
            or re.search(version_pattern, version) is None
        ):
            raise OwnerRecoverySetUnavailable("pinned Litestream version could not be verified")
        return version

    def _restore_database(
        self,
        *,
        replica_uri: str,
        output: Path,
        expected_role: str,
        expected_schema: int,
        expected_agent_id: str | None = None,
    ) -> dict:
        try:
            result = subprocess.run(
                [
                    self.litestream_binary,
                    "restore",
                    "-integrity-check",
                    "quick",
                    "-o",
                    str(output),
                    replica_uri,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OwnerRecoverySetUnavailable("copied database could not be restored") from exc
        if result.returncode != 0:
            raise OwnerRecoverySetUnavailable("copied database failed Litestream restore")
        return inspect_restored_database(
            output,
            expected_role=expected_role,
            expected_schema=expected_schema,
            expected_agent_id=expected_agent_id,
        )

    def _copy_database(
        self,
        *,
        objects: Sequence[ReplicaObject],
        owner_id: str,
        recovery_prefix: str,
        relative_db_prefix: str,
        expected_role: str,
        expected_schema: int,
        temporary_directory: Path,
        expected_agent_id: str | None = None,
    ) -> dict:
        destination_prefix = f"{recovery_prefix}/replicas/{relative_db_prefix}"
        copied_objects = []
        source_db_prefix = f"{owner_id}/{relative_db_prefix}/"
        for source in objects:
            if not source.key.startswith(source_db_prefix):
                raise OwnerRecoverySetUnavailable("replica group escaped its database prefix")
            suffix = source.key[len(source_db_prefix) :]
            copied_objects.append(
                self.copy_replica_object(
                    source,
                    settings.LITESTREAM_BUCKET,
                    settings.MARKETPLACE_ARTIFACTS_BUCKET,
                    f"{destination_prefix}/{suffix}",
                )
            )
        inventory_sha = sha256_bytes(canonical_json(copied_objects))
        output_name = (
            "shared.sqlite"
            if expected_agent_id is None
            else f"agent-{sha256_bytes(expected_agent_id.encode())[:16]}.sqlite"
        )
        restored = self._restore_database(
            replica_uri=f"s3://{settings.MARKETPLACE_ARTIFACTS_BUCKET}/{destination_prefix}",
            output=temporary_directory / output_name,
            expected_role=expected_role,
            expected_schema=expected_schema,
            expected_agent_id=expected_agent_id,
        )
        generation = sha256_bytes(canonical_json({"inventory_sha256": inventory_sha, "restored": restored}))
        return {
            "source_prefix": source_db_prefix.rstrip("/"),
            "recovery_prefix": destination_prefix,
            "inventory_sha256": inventory_sha,
            "generation": generation,
            "objects": copied_objects,
            "restored": restored,
        }

    def _read_version_bytes(self, bucket: str, key: str, version_id: str, *, limit: int | None = None) -> bytes:
        try:
            response = self.s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
            if response.get("VersionId") != version_id:
                raise OwnerRecoverySetUnavailable("S3 returned a different recovery object version")
            body = response["Body"]
            try:
                data = body.read() if limit is None else body.read(limit + 1)
            finally:
                body.close()
        except (ClientError, KeyError, OSError) as exc:
            raise OwnerRecoverySetUnavailable("recovery object version could not be read") from exc
        if limit is not None and len(data) > limit:
            raise OwnerRecoverySetUnavailable("recovery manifest is too large")
        return data

    def _hash_version(self, bucket: str, key: str, version_id: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        try:
            response = self.s3.get_object(Bucket=bucket, Key=key, VersionId=version_id)
            if response.get("VersionId") != version_id:
                raise OwnerRecoverySetUnavailable("S3 returned a different archive version")
            body = response["Body"]
            try:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            finally:
                body.close()
        except (ClientError, KeyError, OSError) as exc:
            raise OwnerRecoverySetUnavailable("EFS archive version could not be verified") from exc
        return digest.hexdigest(), size

    def _archive_efs(self, owner_root: Path, recovery_prefix: str) -> dict:
        entries = scan_efs_tree(owner_root)
        tree_manifest_sha = sha256_bytes(canonical_json(entries))
        key = f"{recovery_prefix}/efs/owner-root.tar"
        sink = _MultipartSink(
            self.s3,
            settings.MARKETPLACE_ARTIFACTS_BUCKET,
            key,
            {_ARCHIVE_METADATA_TREE_SHA: tree_manifest_sha, "provider-version": PROVIDER_VERSION},
        )
        try:
            write_efs_tar(owner_root, entries, sink)  # type: ignore[arg-type]
            completed = sink.complete()
        except Exception:
            sink.abort()
            raise
        try:
            head = self.s3.head_object(
                Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET,
                Key=key,
                VersionId=completed["version_id"],
            )
        except ClientError as exc:
            raise OwnerRecoverySetUnavailable("EFS archive version could not be inspected") from exc
        evidence = self._versioned_head_evidence(head, key=key, version_id=completed["version_id"])
        if (
            evidence["size"] != sink.size
            or evidence["etag"] != completed["etag"]
            or head.get("Metadata", {}).get(_ARCHIVE_METADATA_TREE_SHA) != tree_manifest_sha
        ):
            raise OwnerRecoverySetUnavailable("EFS archive head does not match the uploaded bytes")
        observed_sha, observed_size = self._hash_version(
            settings.MARKETPLACE_ARTIFACTS_BUCKET, key, completed["version_id"]
        )
        if observed_sha != sink.sha256 or observed_size != sink.size:
            raise OwnerRecoverySetUnavailable("EFS archive version failed its content digest")
        return {
            **evidence,
            "sha256": sink.sha256,
            "tree_manifest_sha256": tree_manifest_sha,
            "tree_entries": list(entries),
        }

    def _manifest_summary(self, manifest: Mapping[str, object], key: str, version_id: str, digest: str) -> dict:
        try:
            replicas = manifest["replicas"]
            efs = manifest["efs"]
            source = manifest["source"]
            release = manifest["release"]
            drain = manifest["drain_evidence"]
            inventory = manifest["stable_replica_inventory"]
            provider = manifest["provider"]
            tools = manifest["tools"]
            if not all(
                isinstance(value, dict)
                for value in (replicas, efs, source, release, drain, inventory, provider, tools)
            ):
                raise TypeError
            shared = replicas["shared"]
            agents = replicas["agents"]
            archive = efs["archive"]
            if not isinstance(shared, dict) or not isinstance(agents, dict) or not isinstance(archive, dict):
                raise TypeError
            agent_generations = {}
            for agent, value in agents.items():
                if not isinstance(agent, str) or not isinstance(value, dict):
                    raise TypeError
                agent_generations[agent] = value["generation"]
            summary = {
                "recovery_set_id": manifest["recovery_set_id"],
                "owner_id": manifest["owner_id"],
                "update_id": manifest["update_id"],
                "status": manifest["status"],
                "manifest_bucket": settings.MARKETPLACE_ARTIFACTS_BUCKET,
                "manifest_key": key,
                "manifest_version_id": version_id,
                "manifest_sha256": digest,
                "release_id": release["release_id"],
                "release_image_digest": release["image_digest"],
                "source_service_arn": source["service_arn"],
                "source_task_arn": source["task_arn"],
                "source_task_definition_arn": source["task_definition_arn"],
                "source_image_digest": source["image_digest"],
                "access_point_id": efs["access_point_id"],
                "file_system_id": efs["file_system_id"],
                "efs_root": efs["root"],
                "drain_observed_at": drain["observed_at"],
                "drain_evidence_sha256": sha256_bytes(canonical_json(drain)),
                "stable_replica_inventory_sha256": inventory["sha256"],
                "shared_db_generation": shared["generation"],
                "agent_db_generations": agent_generations,
                "efs_archive_key": archive["key"],
                "efs_archive_version_id": archive["version_id"],
                "efs_archive_etag": archive["etag"],
                "efs_archive_size": archive["size"],
                "efs_archive_sha256": archive["sha256"],
                "efs_tree_manifest_sha256": archive["tree_manifest_sha256"],
                "provider": dict(provider),
                "tools": dict(tools),
                "verified_at": manifest["verified_at"],
            }
        except (KeyError, TypeError) as exc:
            raise OwnerRecoverySetUnavailable("recovery manifest evidence is incomplete") from exc
        for field in (
            "recovery_set_id",
            "owner_id",
            "update_id",
            "manifest_version_id",
            "manifest_sha256",
            "release_id",
            "release_image_digest",
            "source_service_arn",
            "source_task_arn",
            "source_task_definition_arn",
            "source_image_digest",
            "access_point_id",
            "file_system_id",
            "efs_root",
            "drain_observed_at",
            "drain_evidence_sha256",
            "stable_replica_inventory_sha256",
            "shared_db_generation",
            "efs_archive_key",
            "efs_archive_version_id",
            "efs_archive_etag",
            "efs_archive_sha256",
            "efs_tree_manifest_sha256",
            "verified_at",
        ):
            if not isinstance(summary[field], str) or not summary[field]:
                raise OwnerRecoverySetUnavailable("recovery manifest summary is invalid")
        digest_fields = (
            "manifest_sha256",
            "drain_evidence_sha256",
            "stable_replica_inventory_sha256",
            "shared_db_generation",
            "efs_archive_sha256",
            "efs_tree_manifest_sha256",
        )
        if not all(_SHA256_RE.fullmatch(summary[field]) for field in digest_fields):
            raise OwnerRecoverySetUnavailable("recovery manifest digest is invalid")
        if (
            summary["status"] != "verified"
            or type(summary["efs_archive_size"]) is not int
            or summary["efs_archive_size"] <= 0
        ):
            raise OwnerRecoverySetUnavailable("recovery manifest summary is invalid")
        if not all(
            isinstance(agent, str) and isinstance(generation, str) and _SHA256_RE.fullmatch(generation)
            for agent, generation in agent_generations.items()
        ):
            raise OwnerRecoverySetUnavailable("recovery manifest agent generations are invalid")
        return summary

    def _verify_manifest_artifact_versions(self, manifest: Mapping[str, object]) -> None:
        """Prove a stale retry still names every immutable artifact version."""

        try:
            replicas = manifest["replicas"]
            efs = manifest["efs"]
            if not isinstance(replicas, dict) or not isinstance(efs, dict):
                raise TypeError
            databases = [replicas["shared"]]
            agents = replicas["agents"]
            if not isinstance(agents, dict):
                raise TypeError
            databases.extend(agents.values())
            artifacts: list[Mapping[str, object]] = []
            for database in databases:
                if not isinstance(database, dict) or not isinstance(database.get("objects"), list):
                    raise TypeError
                artifacts.extend(database["objects"])
            archive = efs["archive"]
            if not isinstance(archive, dict):
                raise TypeError
            artifacts.append(archive)
        except (KeyError, TypeError) as exc:
            raise OwnerRecoverySetUnavailable("recovery manifest artifact evidence is incomplete") from exc
        try:
            owner_id = _required_string(manifest, "owner_id")
            recovery_set_id = _required_string(manifest, "recovery_set_id")
        except OwnerRecoverySetUnavailable as exc:
            raise OwnerRecoverySetUnavailable("recovery manifest artifact scope is invalid") from exc
        expected_prefix = f"recovery-sets/{owner_id}/{recovery_set_id}/"
        observed_versions: set[tuple[str, str]] = set()
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise OwnerRecoverySetUnavailable("recovery manifest artifact evidence is invalid")
            key = artifact.get("key")
            version_id = artifact.get("version_id")
            if not isinstance(key, str) or not isinstance(version_id, str) or not version_id or version_id == "null":
                raise OwnerRecoverySetUnavailable("recovery manifest artifact version is invalid")
            if not key.startswith(expected_prefix) or (key, version_id) in observed_versions:
                raise OwnerRecoverySetUnavailable("recovery manifest artifact scope is invalid")
            observed_versions.add((key, version_id))
            try:
                head = self.s3.head_object(
                    Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET,
                    Key=key,
                    VersionId=version_id,
                )
            except ClientError as exc:
                raise OwnerRecoverySetUnavailable("recovery manifest artifact version is unavailable") from exc
            observed = self._versioned_head_evidence(head, key=key, version_id=version_id)
            if observed["etag"] != artifact.get("etag") or observed["size"] != artifact.get("size"):
                raise OwnerRecoverySetUnavailable("recovery manifest artifact version has changed")

    def _reuse_manifest(
        self,
        *,
        key: str,
        recovery_set_id: str,
        owner_id: str,
        update_id: str,
        release: Mapping[str, object],
        source: Mapping[str, object],
        drain_evidence: Mapping[str, object],
    ) -> dict | None:
        try:
            head = self.s3.head_object(Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise OwnerRecoverySetUnavailable("existing recovery manifest could not be inspected") from exc
        version_id = head.get("VersionId")
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise OwnerRecoverySetUnavailable("existing recovery manifest is not version-pinned")
        data = self._read_version_bytes(
            settings.MARKETPLACE_ARTIFACTS_BUCKET,
            key,
            version_id,
            limit=_MAX_MANIFEST_BYTES,
        )
        digest = sha256_bytes(data)
        try:
            manifest = json.loads(data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise OwnerRecoverySetUnavailable("existing recovery manifest is invalid") from exc
        if not isinstance(manifest, dict) or canonical_json(manifest) != data:
            raise OwnerRecoverySetUnavailable("existing recovery manifest is not canonical")
        expected = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "verified",
            "provider": {"name": PROVIDER_NAME, "version": PROVIDER_VERSION},
            "recovery_set_id": recovery_set_id,
            "owner_id": owner_id,
            "update_id": update_id,
            "release": dict(release),
            "source": dict(source),
            "drain_evidence": dict(drain_evidence),
        }
        if any(manifest.get(field) != value for field, value in expected.items()):
            raise OwnerRecoverySetUnavailable("existing recovery manifest belongs to different inputs")
        self._verify_manifest_artifact_versions(manifest)
        return self._manifest_summary(manifest, key, version_id, digest)

    async def create(
        self,
        *,
        owner_id: str,
        update_id: str,
        release: Mapping[str, object],
        source: Mapping[str, object],
        drain_evidence: Mapping[str, object],
    ) -> dict:
        validate_recovery_inputs(owner_id, update_id, release, source, drain_evidence)
        if not settings.LITESTREAM_BUCKET or not settings.MARKETPLACE_ARTIFACTS_BUCKET:
            raise OwnerRecoverySetUnavailable("recovery buckets are not configured")
        if settings.LITESTREAM_BUCKET == settings.MARKETPLACE_ARTIFACTS_BUCKET:
            raise OwnerRecoverySetUnavailable("live and recovery buckets must be distinct")
        recovery_identity = owner_id.encode() + b"\0" + update_id.encode()
        recovery_set_id = f"rs-{sha256_bytes(recovery_identity)[:32]}"
        recovery_prefix = f"recovery-sets/{owner_id}/{recovery_set_id}"
        manifest_key = f"{recovery_prefix}/manifest.json"
        reused = await asyncio.to_thread(
            self._reuse_manifest,
            key=manifest_key,
            recovery_set_id=recovery_set_id,
            owner_id=owner_id,
            update_id=update_id,
            release=release,
            source=source,
            drain_evidence=drain_evidence,
        )
        if reused is not None:
            return reused

        inventory = await self.stable_replica_inventory(owner_id)
        groups = group_replica_objects(owner_id, inventory.objects)
        owner_root = Path(settings.EFS_MOUNT_PATH) / owner_id
        litestream_version = await asyncio.to_thread(self._litestream_version)
        with tempfile.TemporaryDirectory(prefix="lightbulb-recovery-") as temporary:
            temporary_path = Path(temporary)
            shared = await asyncio.to_thread(
                self._copy_database,
                objects=groups.shared,
                owner_id=owner_id,
                recovery_prefix=recovery_prefix,
                relative_db_prefix="state/openclaw.sqlite",
                expected_role="global",
                expected_schema=int(source["shared_schema_version"]),
                temporary_directory=temporary_path,
            )
            agents = {}
            for agent_id, objects in groups.agents.items():
                if not _AGENT_ID_RE.fullmatch(agent_id):
                    raise OwnerRecoverySetUnavailable("agent replica identity is invalid")
                agents[agent_id] = await asyncio.to_thread(
                    self._copy_database,
                    objects=objects,
                    owner_id=owner_id,
                    recovery_prefix=recovery_prefix,
                    relative_db_prefix=f"agents/{agent_id}/agent/openclaw-agent.sqlite",
                    expected_role="agent",
                    expected_schema=int(source["agent_schema_version"]),
                    temporary_directory=temporary_path,
                    expected_agent_id=agent_id,
                )
        efs_archive = await asyncio.to_thread(self._archive_efs, owner_root, recovery_prefix)
        verified_at = self._now_iso()
        manifest = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "status": "verified",
            "provider": {"name": PROVIDER_NAME, "version": PROVIDER_VERSION},
            "recovery_set_id": recovery_set_id,
            "owner_id": owner_id,
            "update_id": update_id,
            "release": dict(release),
            "source": dict(source),
            "drain_evidence": dict(drain_evidence),
            "stable_replica_inventory": {
                "sha256": inventory.inventory_sha256,
                "first_observed_at": inventory.first_observed_at,
                "second_observed_at": inventory.second_observed_at,
                "wait_seconds": inventory.wait_seconds,
                "object_count": len(inventory.objects),
            },
            "replicas": {
                "shared": shared,
                "agents": agents,
                "tombstoned_agents": list(groups.tombstoned_agents),
            },
            "efs": {
                "file_system_id": source["file_system_id"],
                "access_point_id": source["access_point_id"],
                "root": source["efs_root"],
                "archive": efs_archive,
            },
            "tools": {"litestream": litestream_version, "sqlite": sqlite3.sqlite_version},
            "verified_at": verified_at,
        }
        manifest_bytes = canonical_json(manifest)
        if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
            raise OwnerRecoverySetUnavailable("recovery manifest is too large")
        manifest_sha = sha256_bytes(manifest_bytes)
        try:
            uploaded = await asyncio.to_thread(
                self.s3.put_object,
                Bucket=settings.MARKETPLACE_ARTIFACTS_BUCKET,
                Key=manifest_key,
                Body=manifest_bytes,
                ContentType="application/json",
                Metadata={"manifest-sha256": manifest_sha, "provider-version": PROVIDER_VERSION},
            )
        except ClientError as exc:
            raise OwnerRecoverySetUnavailable("verified recovery manifest could not be written") from exc
        version_id = uploaded.get("VersionId")
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise OwnerRecoverySetUnavailable("recovery manifest has no immutable VersionId")
        observed = await asyncio.to_thread(
            self._read_version_bytes,
            settings.MARKETPLACE_ARTIFACTS_BUCKET,
            manifest_key,
            version_id,
            limit=_MAX_MANIFEST_BYTES,
        )
        if observed != manifest_bytes or sha256_bytes(observed) != manifest_sha:
            raise OwnerRecoverySetUnavailable("recovery manifest version failed verification")
        return self._manifest_summary(manifest, manifest_key, version_id, manifest_sha)


async def create_verified_owner_recovery_set(
    *,
    owner_id: str,
    update_id: str,
    release: Mapping[str, object],
    source: Mapping[str, object],
    drain_evidence: Mapping[str, object],
) -> dict:
    """Public fail-closed entry point used after the release executor drains ECS."""

    return await OwnerRecoverySetProvider().create(
        owner_id=owner_id,
        update_id=update_id,
        release=release,
        source=source,
        drain_evidence=drain_evidence,
    )


__all__ = [
    "ClientError",
    "OwnerRecoverySetProvider",
    "OwnerRecoverySetUnavailable",
    "ReplicaObject",
    "canonical_json",
    "create_verified_owner_recovery_set",
    "group_replica_objects",
    "inspect_restored_database",
    "scan_efs_tree",
    "sha256_bytes",
    "validate_recovery_inputs",
    "write_efs_tar",
]
