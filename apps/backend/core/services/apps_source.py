"""App source archives (tar.gz from the builder) in the owner state bucket."""

import asyncio
import hashlib
import io
import tarfile
from collections.abc import Iterator

import boto3

from core.config import settings

MAX_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED = 500 * 1024 * 1024
MAX_MEMBERS = 20_000


class SourceTooLarge(Exception):
    pass


class SourceRefused(Exception):
    pass


def _check_id(value: str) -> str:
    if not value or "/" in value or ".." in value:
        raise ValueError(f"invalid id: {value!r}")
    return value


def _prefix(owner_id: str, app_id: str) -> str:
    return f"apps/{_check_id(owner_id)}/{_check_id(app_id)}/"


def _members(data: bytes, *, forbid: bool) -> Iterator[tuple[str, tarfile.TarInfo, tarfile.TarFile]]:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise SourceRefused(f"unreadable archive: {e}") from e
    count = total = 0
    with tf:
        try:
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
                yield "/".join(p for p in parts if p), m, tf
        except (tarfile.TarError, OSError, EOFError) as e:
            raise SourceRefused(f"unreadable archive: {e}") from e


def iter_files(data: bytes) -> Iterator[tuple[str, bytes]]:
    for path, m, tf in _members(data, forbid=False):
        if m.isreg():
            yield path, tf.extractfile(m).read()


def _s3():
    return boto3.client("s3")


async def put(owner_id: str, app_id: str, *, commit: str, data: bytes) -> str:
    prefix = _prefix(owner_id, app_id)
    if len(data) > MAX_BYTES:
        raise SourceTooLarge(f"archive is {len(data)} bytes, limit {MAX_BYTES}")
    for _ in _members(data, forbid=True):
        pass
    sid = hashlib.sha256(data).hexdigest()[:16]
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
    resp = await asyncio.to_thread(_s3().get_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key)
    return await asyncio.to_thread(resp["Body"].read)


def _delete_all(prefix: str) -> None:
    s3 = _s3()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.OWNER_STATE_BUCKET, Prefix=prefix):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=settings.OWNER_STATE_BUCKET, Delete={"Objects": objs})


async def delete_all(owner_id: str, app_id: str) -> None:
    await asyncio.to_thread(_delete_all, _prefix(owner_id, app_id))
