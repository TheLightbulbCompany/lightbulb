"""S3 profile sync (the AWS reference's workspace_sync, cut down)."""
from __future__ import annotations

import fnmatch
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from bridge import HOME, S3_BUCKET, S3_PREFIX

log = logging.getLogger("bridge")

_restored: set[str] = set()

# Fix round (minor 12): two turns for the SAME agent in different rooms run concurrently in
# one process (the FIFO group is room+agent), and both call save_profile on the one profile
# dir at the end. Without a lock, two concurrent uploads interleave and the last writer wins
# on S3 -- one turn's session rows can be lost on the next cold start.
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def _save_lock(agent_id: str) -> threading.Lock:
    with _save_locks_guard:
        return _save_locks.setdefault(agent_id, threading.Lock())

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
    # Task 13 (M4-R20): backend-owned. Written by hermes_profiles.upload_profile /
    # patch_profile_config / set_profile_config, re-pulled by ensure_profile() on a changed
    # .revision. A warm microVM's local copy can go stale the instant a hot patch lands, and
    # save_profile racing that patch would silently revert it -- fix round 1's C4 flagged this
    # exact clobber before this exclusion existed.
    "config.yaml",
    ".env",
    "SOUL.md",
    ".revision",
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
    paths (request dumps, logs, per-turn context, WAL/SHM) never leave the microVM.

    Serialized per agent (see _save_lock) -- two turns for the same agent in different rooms
    can finish concurrently, and an interleaved pair of uploads would let the later-finishing
    one silently clobber files the earlier one already wrote for a different room's session
    rows."""
    if not S3_BUCKET:
        return 0.0
    with _save_lock(agent_id):
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


# ---- Task 13b: hot-patch re-pull -----------------------------------------------------------
# The backend is the ONLY writer of the four backend-owned files (config.yaml, .env, SOUL.md,
# .revision -- see hermes_profiles.upload_profile/patch_profile_config/set_profile_config on the
# backend, and save_profile's EXCLUDE_GLOBS entries above). A patch bumps .revision; this is how
# the bridge notices without a watcher or a restart.
BACKEND_OWNED_FILES = ("config.yaml", ".env", "SOUL.md", ".revision")

_REVISIONS: dict[str, str] = {}


def _read_revision(agent_id: str) -> str | None:
    """The profile's ``.revision`` marker in S3, or None when it cannot be read."""
    try:
        key = f"{S3_PREFIX}/{agent_id}/.revision"
        return _s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode().strip()
    except Exception:  # noqa: BLE001 -- a transient S3 blip must not tear down a warm profile
        return None


def _download_profile(agent_id: str) -> None:
    """Re-pull exactly the backend-owned files -- NOT a full restore_profile-style tree walk.
    One object missing (a profile mid-provision, or one that predates Task 7) must not abort
    the rest."""
    dest = _profile_dir(agent_id)
    dest.mkdir(parents=True, exist_ok=True)
    s3 = _s3()
    for name in BACKEND_OWNED_FILES:
        try:
            s3.download_file(S3_BUCKET, f"{S3_PREFIX}/{agent_id}/{name}", str(dest / name))
        except Exception:  # noqa: BLE001 -- one missing object must not abort the other three
            log.warning("ensure_profile: failed to re-pull %s for %s", name, agent_id)


def ensure_profile(agent_id: str) -> None:
    """Pull the backend-owned files if we have never seen this agent, or if the backend bumped
    the revision since our last look.

    One GetObject-sized read per turn buys hot config patches (Task 13a) with no watcher, no
    lock, no restart. An unreadable revision leaves a WARM profile alone: a transient S3 error
    must not re-download (and momentarily blank) a profile that is already working mid-turn.
    """
    if not S3_BUCKET:
        return
    revision = _read_revision(agent_id)
    known = _REVISIONS.get(agent_id)
    if known is not None and (revision is None or revision == known):
        return
    _download_profile(agent_id)
    if revision is not None:
        _REVISIONS[agent_id] = revision


# ---- Task 15b: ChatGPT OAuth — the root auth.json's own save/restore leg -----------------------
# Report 06 §2.2 / hermes_cli/auth_codex.py: a profile with no providers.openai-codex block reads
# the ROOT home's grant via the fallback, and a refresh under that profile write-throughs to
# ROOT's chain. So the root auth.json needs the SAME restore-once / save-after-turn treatment as
# a profile dir, keyed at S3_PREFIX directly (no per-agent segment) to match
# hermes_profiles.root_home_prefix on the backend side.
def _download_object(key: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _s3().download_file(S3_BUCKET, f"{S3_PREFIX}/{key}", str(dest))


def _upload_object(src: Path, key: str) -> None:
    _s3().upload_file(str(src), S3_BUCKET, f"{S3_PREFIX}/{key}")


_ROOT_AUTH_RESTORED: set[str] = set()


def restore_root_auth() -> None:
    """Pull ``<owner>/hermes/auth.json`` into the root home, once per process."""
    if not S3_BUCKET or "done" in _ROOT_AUTH_RESTORED:
        return
    try:
        _download_object("auth.json", HOME / "auth.json")
    except Exception as exc:  # noqa: BLE001 -- no root auth.json yet (never connected) is not an error
        log.debug("restore_root_auth: no root auth.json to restore: %s", exc)
    _ROOT_AUTH_RESTORED.add("done")


def save_root_auth() -> None:
    """Push the root ``auth.json`` back after a turn — Hermes rotates the refresh token in
    place, and the next microVM must start from the rotated chain, not a replayed one.

    No existence guard here: an owner who has never connected ChatGPT OAuth has no local
    ``auth.json`` at all, and this raises in that case (``upload_file`` needs a real source
    file) — the caller (``turn.py``, same pattern as ``save_profile``) treats it as best-effort.
    """
    if not S3_BUCKET:
        return
    _upload_object(HOME / "auth.json", "auth.json")


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


LIGHTBULB_PLUGINS = ("lightbulb", "lightbulb_node")


def plugins_pin(existing: dict) -> dict:
    """Opt the Lightbulb directory plugins in. Hermes loads a non-backend plugin only when
    plugins.enabled names it (hermes_cli/plugins_discovery.py gate_manifest); bundled
    backends/platforms auto-load, so this allow-list touches nothing else. deep_merge
    replaces lists wholesale, so the union is computed from the existing config here."""
    current = (existing.get("plugins") or {}).get("enabled") or []
    enabled = list(dict.fromkeys([*current, *LIGHTBULB_PLUGINS]))
    return {"plugins": {"enabled": enabled}}


def pin_profile_defaults(agent_id: str, model: str) -> dict:
    """Fill-missing, not force-pin (fix round, M4-R2).

    M4's config writer (``hermes_profiles.build_profile_config``) is now the source of truth for
    a profile's aux/bedrock pins, written once at provision and kept current by Task 13's
    ``ensure_profile()`` re-pull on a changed ``.revision`` -- this no longer needs to
    force-overwrite them on every turn the way it did before that writer existed (see this
    function's own history: "Until M4's config writer exists, the bridge writes this before
    every turn"). ``deep_merge(defaults, existing)`` makes EXISTING win on any actual conflict
    (an agent-edited or legacy value survives untouched), with the defaults only filling
    whatever the profile's own config.yaml is missing -- e.g. a profile M4 never wrote, or one
    written before Task 7 shipped.

    This is a deliberate authority handoff, not a fence removal: ``bedrock.discovery.enabled``
    stays enforced regardless, via the MANAGED overlay (Task 11's baked ``/etc/hermes/config.yaml``,
    which wins AFTER env expansion and cannot be undone from inside a profile's own config.yaml).
    The per-task auxiliary provider pin has no such backstop -- an already-pinned task's own
    value, correct or not, is trusted once M4 has written it.

    ``plugins.enabled`` stays a UNION regardless of merge direction: ``plugins_pin`` already
    computes it from whatever ``existing`` holds, and this second merge line is unchanged.
    """
    existing = read_profile_config(agent_id)
    config = deep_merge(auxiliary_pin(_aux_defaults(), model), existing)
    config = deep_merge(config, plugins_pin(existing))
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
