"""S3 profile sync (the AWS reference's workspace_sync, cut down)."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from bridge import HOME, S3_BUCKET, S3_PREFIX

log = logging.getLogger("bridge")

_restored: set[str] = set()


def _s3():
    import boto3

    return boto3.client("s3")


def _profile_dir(agent_id: str) -> Path:
    return HOME / "profiles" / agent_id


def restore_profile(agent_id: str) -> float:
    """Download the agent's profile dir from S3 once per process. Returns seconds taken."""
    if agent_id in _restored or not S3_BUCKET:
        _profile_dir(agent_id).mkdir(parents=True, exist_ok=True)
        _restored.add(agent_id)
        return 0.0
    t0 = time.time()
    dest = _profile_dir(agent_id)
    dest.mkdir(parents=True, exist_ok=True)
    prefix = f"{S3_PREFIX}/{agent_id}/"
    s3 = _s3()
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(S3_BUCKET, obj["Key"], str(target))
            n += 1
    _restored.add(agent_id)
    took = time.time() - t0
    log.info("restored profile %s: %d objects in %.1fs", agent_id, n, took)
    return took


def save_profile(agent_id: str) -> float:
    """Upload the agent's profile dir. SQLite files go through the backup API; WAL/SHM are skipped."""
    if not S3_BUCKET:
        return 0.0
    t0 = time.time()
    src = _profile_dir(agent_id)
    prefix = f"{S3_PREFIX}/{agent_id}/"
    s3 = _s3()
    n = 0
    for path in src.rglob("*"):
        if not path.is_file() or path.suffix in (".db-wal", ".db-shm") or path.name.endswith((".sqlite-wal", ".sqlite-shm")):
            continue
        rel = path.relative_to(src).as_posix()
        upload = path
        if path.suffix in (".db", ".sqlite"):
            snap = path.with_name(path.name + ".s3snap")
            with sqlite3.connect(path) as conn, sqlite3.connect(snap) as out:
                conn.backup(out)
            upload = snap
        s3.upload_file(str(upload), S3_BUCKET, prefix + rel)
        if upload is not path:
            upload.unlink(missing_ok=True)
        n += 1
    took = time.time() - t0
    log.info("saved profile %s: %d objects in %.1fs", agent_id, n, took)
    return took
