"""S3 profile sync (the AWS reference's workspace_sync, cut down)."""
from __future__ import annotations

import fnmatch
import json
import logging
import sqlite3
import time
from pathlib import Path

from bridge import HOME, S3_BUCKET, S3_PREFIX

log = logging.getLogger("bridge")

_restored: set[str] = set()

# Hermes writes sessions/request_dump_*.json on EVERY API error with no config switch
# (report 01 §1). The body is redacted but the file must never reach S3; logs/ likewise;
# .lightbulb-turn/ holds the per-turn local-session JWT (Task 11). WAL/SHM are skipped
# because save_profile snapshots SQLite through the backup API instead.
EXCLUDE_GLOBS = (
    "sessions/request_dump_*.json",
    "logs/*",
    "logs/**/*",
    ".lightbulb-turn/*",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite-wal",
    "*.sqlite-shm",
    # Task 25: attachments materialized locally for a turn (media/messaging/attachments/...)
    # are re-fetched from the backend on the next process, not synced to S3.
    "media/messaging/*",
    "media/messaging/**/*",
)


def is_excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in EXCLUDE_GLOBS)


def load_boot_secrets(secret_id: str, client=None) -> dict[str, str]:
    """The runtime's environmentVariables are plaintext to anyone with GetAgentRuntime
    (spec §4.9), so they carry identifiers only and the real secrets are fetched here with
    the execution role."""
    if client is None:
        import boto3

        client = boto3.client("secretsmanager")
    return json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])


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
    """Upload the agent's profile dir. SQLite files go through the backup API; excluded
    paths (request dumps, logs, per-turn context, WAL/SHM) never leave the microVM."""
    if not S3_BUCKET:
        return 0.0
    t0 = time.time()
    src = _profile_dir(agent_id)
    prefix = f"{S3_PREFIX}/{agent_id}/"
    s3 = _s3()
    n = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src).as_posix()
        if not path.is_file() or is_excluded(rel):
            continue
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


# --- lane B: config RPC helpers (Task 17) -------------------------------------------------------
CONFIG_NAME = "config.yaml"


def deep_merge(base: dict, patch: dict) -> dict:
    """Dict-into-dict recursion; a list or scalar replaces wholesale. Same semantics as the
    EFS config patcher, so relay/provisioning patches behave identically on Hermes — which
    is also why a deny-list patch must carry every entry that stays denied."""
    out = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_profile_config(agent_id: str) -> dict:
    import yaml

    path = _profile_dir(agent_id) / CONFIG_NAME
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_profile_config(agent_id: str, config: dict) -> None:
    import yaml

    directory = _profile_dir(agent_id)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{CONFIG_NAME}.tmp"
    tmp.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    tmp.replace(directory / CONFIG_NAME)  # atomic: a torn config.yaml bricks the agent's next turn


def delete_profile_objects(agent_id: str) -> int:
    """Drop the profile's S3 prefix. Without this a deleted agent comes back on the next
    cold start, because restore reads the prefix, not the local dir."""
    if not S3_BUCKET:
        return 0
    s3, prefix, removed = _s3(), f"{S3_PREFIX}/{agent_id}/", 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": keys})
            removed += len(keys)
    return removed


def _aux_defaults() -> dict:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    return DEFAULT_CONFIG["auxiliary"]


def auxiliary_pin(aux_defaults: dict, model: str) -> dict:
    """Every auxiliary task on bedrock, with the auto-chain disarmed.

    Hermes's auto-chain probes OpenRouter/Nous for any aux task, which means our prompt text
    leaving for a third party from inside the owner's microVM. The task list is derived from
    Hermes's OWN defaults, so a Hermes upgrade that adds a task cannot silently reopen that
    lane. bedrock.discovery.enabled defaults true and AccessDenies on the owner role.
    Until M4's config writer exists, the bridge writes this before every turn.
    """
    tasks = {name: {"provider": "bedrock", "model": model, "fallback_chain": []}
             for name, block in aux_defaults.items() if isinstance(block, dict) and "provider" in block}
    return {"auxiliary": {**tasks, "free_only": True},
            "bedrock": {"discovery": {"enabled": False}}}


def pin_profile_defaults(agent_id: str, model: str) -> dict:
    config = deep_merge(read_profile_config(agent_id), auxiliary_pin(_aux_defaults(), model))
    write_profile_config(agent_id, config)
    return config


# ---- attachment materialization for the turn (Task 25) -----------------------------------
# `HOOK_TOKEN` is mutable module state on `bridge` (bridge/__init__.py overwrites it in place
# after the boot-secrets fetch), and this module is imported DURING bridge/__init__.py's own
# execution (for load_boot_secrets), before that fetch runs. A top-level `from bridge import
# HOOK_TOKEN`/`API`/`OWNER` here would freeze the stale pre-fetch value forever (Python only
# executes a module's top level once); server.py/turn.py can capture them at their own
# top-level import because they are imported only after bridge/__init__.py has fully finished
# (see the comment there). So this reads `bridge.API`/`bridge.OWNER`/`bridge.HOOK_TOKEN`
# lazily, at call time, via the module object.
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
        "application/pdf": ".pdf"}


def _get_backend_bytes(path: str) -> bytes:
    import bridge
    import urllib.request

    req = urllib.request.Request(f"{bridge.API}/api/v1/internal/messaging/{path}",
                                 headers={"Authorization": f"Bearer {bridge.HOOK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def materialize_attachments(body: dict) -> list[str]:
    """Download the attachments a turn can see into the plugin-era layout; skip files
    already present (a turn revisiting the same history does not re-fetch them). One
    attachment's fetch failing (network blip, deleted object, ...) must not fail the whole
    turn -- it is skipped and logged, not raised."""
    import bridge

    room_id = (body.get("room") or {}).get("id")
    out: list[str] = []
    for m in [body.get("message") or {}] + list(body.get("recentHistory", [])):
        for att in m.get("attachments") or []:
            target = HOME / "media" / "messaging" / "attachments" / room_id / f"{att['id']}{_EXT.get(att.get('contentType'), '')}"
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(_get_backend_bytes(f"attachments/{bridge.OWNER}/{room_id}/{att['id']}"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("attachment fetch failed room=%s attachment=%s: %s", room_id, att.get("id"), exc)
                    continue
            out.append(str(target))
    return out
