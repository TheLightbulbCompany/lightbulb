"""App source archives (tar.gz from the builder) in the owner state bucket."""

import asyncio
import gzip
import hashlib
import io
import tarfile
import zlib
from collections.abc import Iterator

import boto3
from botocore.exceptions import ClientError

from core.config import settings

# R55: API Gateway (HTTP API) refuses payloads over 10 MB; the CLI refuses at MAX_BYTES too.
MAX_BYTES = 9 * 1024 * 1024
MAX_UNCOMPRESSED = 100 * 1024 * 1024
MAX_MEMBERS = 20_000


class SourceTooLarge(Exception):
    pass


class SourceRefused(Exception):
    pass


class SourceNotFound(Exception):
    pass


class _Capped:
    """Reader that refuses once more than `limit` inflated bytes have been read, so
    no tar header payload (GNU long name, PAX) can allocate past it."""

    def __init__(self, raw: io.BufferedIOBase, limit: int):
        self.raw, self.left = raw, limit

    def read(self, n: int = -1) -> bytes:
        if n < 0 or n > self.left + 1:
            n = self.left + 1
        data = self.raw.read(n)
        self.left -= len(data)
        if self.left < 0:
            raise SourceRefused("archive too large when unpacked")
        return data


def _check_id(value: str) -> str:
    if not value or "/" in value or ".." in value:
        raise ValueError(f"invalid id: {value!r}")
    return value


def _prefix(owner_id: str, app_id: str) -> str:
    return f"apps/{_check_id(owner_id)}/{_check_id(app_id)}/"


def _members(data: bytes, *, forbid: bool) -> Iterator[tuple[str, tarfile.TarInfo, tarfile.TarFile]]:
    # headers/padding get slack on top of the member-size cap
    reader = _Capped(gzip.GzipFile(fileobj=io.BytesIO(data)), MAX_UNCOMPRESSED + MAX_MEMBERS * 1024)
    count = total = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=reader, mode="r|") as tf:
            for m in tf:
                count += 1
                total += max(m.size, 0)
                if count > MAX_MEMBERS or total > MAX_UNCOMPRESSED:
                    raise SourceRefused("archive too large when unpacked")
                if not (m.isreg() or m.isdir()):
                    raise SourceRefused(f"unsupported entry type: {m.name}")
                parts = m.name.removeprefix("./").split("/")
                if m.name.startswith("/") or ".." in parts:
                    raise SourceRefused(f"unsafe path: {m.name}")
                if forbid and any(p in ("node_modules", ".git") or p.startswith(".env") for p in parts):
                    raise SourceRefused(f"not allowed: {m.name.removeprefix('./')}")
                path = "/".join(p for p in parts if p)
                if forbid and m.isreg():
                    if path in seen:
                        raise SourceRefused(f"duplicate path: {path}")
                    seen.add(path)
                yield path, m, tf
        # tar end-of-archive is zero blocks; anything else after it is smuggled data
        while chunk := reader.read(65536):
            if chunk.strip(b"\0"):
                raise SourceRefused("trailing data after archive")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise SourceRefused(f"unreadable archive: {e}") from e


def iter_files(data: bytes) -> Iterator[tuple[str, bytes]]:
    for path, m, tf in _members(data, forbid=False):
        # macOS tar adds a binary ``._name`` (AppleDouble) beside each file with extended attributes.
        if m.isreg() and not path.rpartition("/")[2].startswith("._"):
            yield path, tf.extractfile(m).read()


def content_hash(files: dict[str, bytes]) -> str:
    """What a tree holds, from an already unpacked (so size-limited) archive. ``source_id`` hashes the
    .tar.gz, and bsdtar writes the current time into the gzip header, so an unchanged tree gets a new id."""
    digest = hashlib.sha256()
    for path in sorted(files):
        name, body = path.encode(), files[path]
        digest.update(b"%d:%d:" % (len(name), len(body)) + name + body)
    return digest.hexdigest()[:16]


def _s3():
    return boto3.client("s3")


def _validate(data: bytes) -> str:
    for _ in _members(data, forbid=True):
        pass
    return hashlib.sha256(data).hexdigest()[:16]


async def put(owner_id: str, app_id: str, *, commit: str, data: bytes) -> str:
    prefix = _prefix(owner_id, app_id)
    if len(data) > MAX_BYTES:
        raise SourceTooLarge(f"archive is {len(data)} bytes, limit {MAX_BYTES}")
    if not commit.isascii() or len(commit) > 200:
        raise SourceRefused("commit must be ASCII, at most 200 chars")
    sid = await asyncio.to_thread(_validate, data)
    await asyncio.to_thread(
        _s3().put_object,
        Bucket=settings.OWNER_STATE_BUCKET,
        Key=f"{prefix}src/{sid}.tar.gz",
        Body=data,
        Metadata={"commit": commit},
    )
    return sid


async def get(owner_id: str, app_id: str, source_id: str) -> bytes:
    key = f"{_prefix(owner_id, app_id)}src/{_check_id(source_id)}.tar.gz"
    try:
        resp = await asyncio.to_thread(_s3().get_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise SourceNotFound(key) from e
        raise
    return await asyncio.to_thread(resp["Body"].read)


def _delete_all(prefix: str) -> None:
    s3 = _s3()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.OWNER_STATE_BUCKET, Prefix=prefix):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            resp = s3.delete_objects(Bucket=settings.OWNER_STATE_BUCKET, Delete={"Objects": objs})
            if resp.get("Errors"):
                raise RuntimeError(f"delete failed: {resp['Errors'][0]}")


async def delete_all(owner_id: str, app_id: str) -> None:
    await asyncio.to_thread(_delete_all, _prefix(owner_id, app_id))
