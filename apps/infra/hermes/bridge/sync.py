"""S3 profile sync (the AWS reference's workspace_sync, cut down).

Ruling M6-R51: **the profile in S3 is the source of truth for every file the turn did not
touch.** The backend writes more than its five ``BACKEND_OWNED_FILES`` into a profile prefix --
``memories/USER.md`` and ``AGENTS.md`` from the Instructions tab, any Files-panel edit,
``memories/`` seeded by the migration job -- so a ``.revision`` bump re-pulls by RECENCY (every
object S3 has touched since we last pulled or pushed our own copy of it) and ``save_profile``
uploads only what the turn actually changed. Before that rule a warm microVM saw none of those
writes and then silently reverted them by uploading its whole stale local tree (task-10b
review, finding C1).

Neither direction propagates a DELETE (unchanged from before this ruling): a file the agent
removes locally stays in S3 and comes back on the next cold restore.

Concurrency is last-writer-wins -- when the backend and the agent change the SAME file inside
one turn, whichever write reaches S3 last is the one that survives. Per-agent saves stay
serialized (``_save_lock``) so two concurrent turns cannot interleave their uploads.
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from bridge import HOME, S3_BUCKET, S3_PREFIX

log = logging.getLogger("bridge")

_restored: set[str] = set()

#: Ruling M6-R51, per agent beside _REVISIONS.
#: _SYNCED: the S3 ``LastModified`` (epoch seconds) of the copy we hold, per relative path --
#: the per-file "as of" instant the recency re-pull compares against. An upload records the
#: local wall clock instead (S3 hands back an ETag, not a LastModified), which makes this the
#: one place a local clock is compared with S3's; the microVM is NTP-synced and the cost of a
#: skew is a redundant re-download of bytes we just uploaded, not a lost write. Per FILE rather
#: than one instant per agent so that a save advances it ONLY for what the save uploaded: a
#: single agent-wide instant would either re-pull the turn's own uploads on the next bump or
#: swallow a backend write that landed mid-turn.
#: _MTIMES: the local mtime of each file as of the last restore/pull/save, so save_profile can
#: tell what this turn changed.
_SYNCED: dict[str, dict[str, float]] = {}
_MTIMES: dict[str, dict[str, int]] = {}

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
    # Row #15 / Ruling M6-R30: also backend-owned, written by
    # hermes_profiles.write_facts_to_profiles (owner_facts.write_owner_facts's AgentCore
    # branch) on every plan/provider change -- same clobber risk as the four above.
    "lightbulb-facts.json",
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


#: Minor 2 (fix round 2 continuation): one lazily-built boto3 client per process, not one per
#: call -- restore_root_auth/save_root_auth alone now cost 2-3 of these per turn
#: (_root_auth_revision on both restore and save, plus the actual get/put).
_S3_CLIENT = None


def _s3():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3

        _S3_CLIENT = boto3.client("s3")
    return _S3_CLIENT


def _profile_dir(agent_id: str) -> Path:
    return HOME / "profiles" / agent_id


def _modified_at(obj: dict) -> float | None:
    """Epoch seconds for a ``list_objects_v2`` entry, or None when the listing carried no
    LastModified. Real S3 always carries it; "unknown" has to fail OPEN (download) because
    skipping is how a backend write goes missing, and a redundant download only costs bytes."""
    when = obj.get("LastModified")
    return when.timestamp() if when is not None else None


def _change_stamp(path: Path) -> int:
    """``path``'s mtime -- but for a SQLite db, the newest of the db and its ``-wal``/``-shm``
    sidecars. In WAL mode a write lands in the sidecar and the main file's mtime does not move
    until a checkpoint, so mtime alone would let save_profile drop a whole turn's session rows.

    Every stat is suppressed: SQLite deletes ``-wal``/``-shm`` on the last connection's close, so
    a checkpoint landing between the walk and the stat would otherwise raise and abort the whole
    save -- every remaining file in the turn included. A vanished file reads as 0, which differs
    from any recorded stamp, so the caller re-uploads rather than skipping.
    """
    candidates = [path]
    if path.suffix in (".db", ".sqlite"):
        candidates += [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
    stamps = []
    for candidate in candidates:
        with contextlib.suppress(OSError):
            stamps.append(candidate.stat().st_mtime_ns)
    return max(stamps, default=0)


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
    synced = _SYNCED.setdefault(agent_id, {})
    # M6-R51: the baseline save_profile diffs against -- without it the first save of the process
    # would upload the entire tree it just downloaded. Recorded per object as the loop writes it,
    # not by a second rglob afterwards: cold-restore latency sits in front of the first reply.
    mtimes = _MTIMES.setdefault(agent_id, {})
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel or rel.endswith("/"):
                continue  # directory marker: downloading it onto the directory raises
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(S3_BUCKET, obj["Key"], str(target))
            modified = _modified_at(obj)
            if modified is not None:
                synced[rel] = modified
            mtimes[rel] = _change_stamp(target)
            n += 1
    _restored.add(agent_id)
    took = time.time() - t0
    log.info("restored profile %s: %d objects in %.1fs", agent_id, n, took)
    # Minor 12 (fix round 1 continuation): the tree walk above just downloaded .revision (if the
    # profile has one) along with everything else -- seed _REVISIONS from that local copy so
    # ensure_profile's very next call doesn't immediately think a hot patch landed and re-pull
    # the same four files this just restored.
    revision_file = dest / ".revision"
    if revision_file.is_file():
        _REVISIONS[agent_id] = revision_file.read_text(encoding="utf-8").strip()
    return took


def save_profile(agent_id: str) -> float:
    """Upload what THIS turn changed. SQLite files go through the backup API; excluded
    paths (request dumps, logs, per-turn context, WAL/SHM) never leave the microVM.

    Ruling M6-R51: only files that are new or whose change stamp moved since the last
    restore/pull/save go up. Uploading the whole tree (what this did before) silently reverted
    every backend write the turn had not touched -- an Instructions-tab edit, a Files-panel
    save, migration-seeded ``memories/``. A file the agent DELETED locally is still not deleted
    in S3; that has never been propagated and is not now.

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
        mtimes = _MTIMES.setdefault(agent_id, {})
        synced = _SYNCED.setdefault(agent_id, {})
        n = 0
        # Materialized, not lazy: the .s3snap temp below is created inside src and a live
        # rglob can yield it back as a file of its own.
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src).as_posix()
            if not path.is_file() or is_excluded(rel):
                continue
            stamp = _change_stamp(path)
            if mtimes.get(rel) == stamp:
                continue  # untouched by this turn: S3's copy is the source of truth
            upload = path
            if path.suffix in (".db", ".sqlite"):
                snap = path.with_name(path.name + ".s3snap")
                with sqlite3.connect(path) as conn, sqlite3.connect(snap) as out:
                    conn.backup(out)
                upload = snap
            # I1: the instant is read BEFORE the PUT, never after. Reading it after would put it
            # past the object's real LastModified by the PUT's own latency plus any clock skew,
            # and a backend write landing inside that gap would then be <= synced[rel] FOREVER --
            # skipped on every later bump, C1 re-opened for that one file. Under-recording is the
            # safe rounding: it costs at most a redundant re-download of our own identical bytes.
            now = time.time()
            s3.upload_file(str(upload), S3_BUCKET, prefix + rel)
            if upload is not path:
                upload.unlink(missing_ok=True)
            mtimes[rel] = stamp
            synced[rel] = now
            n += 1
        took = time.time() - t0
        log.info("saved profile %s: %d changed objects in %.1fs", agent_id, n, took)
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
# The backend is the ONLY writer of these backend-owned files (config.yaml, .env, SOUL.md,
# .revision -- see hermes_profiles.upload_profile/patch_profile_config/set_profile_config on the
# backend, and save_profile's EXCLUDE_GLOBS entries above -- plus lightbulb-facts.json, written by
# hermes_profiles.write_facts_to_profiles, Row #15 / Ruling M6-R30). A patch bumps .revision; this
# is how the bridge notices without a watcher or a restart.
BACKEND_OWNED_FILES = ("config.yaml", ".env", "SOUL.md", ".revision", "lightbulb-facts.json")

_REVISIONS: dict[str, str] = {}


def _read_revision(agent_id: str) -> str | None:
    """The profile's ``.revision`` marker in S3, or None when it cannot be read."""
    try:
        key = f"{S3_PREFIX}/{agent_id}/.revision"
        return _s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode().strip()
    except Exception:  # noqa: BLE001 -- a transient S3 blip must not tear down a warm profile
        return None


def _is_absent(exc: Exception) -> bool:
    """A 404/NoSuchKey, as opposed to a throttle, a timeout or a permission error. A profile
    mid-provision (or one that predates Task 7) is legitimately missing some backend-owned
    object, and that must not be reported as a failed pull forever."""
    response = getattr(exc, "response", None)
    code = (response or {}).get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
    return code in ("404", "NoSuchKey", "NotFound")


def _download_profile(agent_id: str) -> bool:
    """Re-pull the backend-owned files by name, then everything else the backend has touched
    since we last pulled or pushed it (Ruling M6-R51) -- still NOT a full restore_profile-style
    tree walk. One object missing (a profile mid-provision, or one that predates Task 7) must
    not abort the rest.

    Returns False when anything we meant to pull did not land, so ``ensure_profile`` can leave
    ``_REVISIONS`` alone and retry on the next turn (I2): a bump announces content, and there is
    no second bump until the next backend write, so swallowing one throttled listing would leave
    the agent on stale instructions until the microVM recycles. A legitimately ABSENT object is
    not a failure -- it would pin the revision forever on a profile that simply has no SOUL.md.

    The named loop stays even though recency would also catch these five: they are in
    EXCLUDE_GLOBS, which the recency walk honours, and an unconditional GetObject also heals a
    local copy that was truncated or never written in the first place."""
    dest = _profile_dir(agent_id)
    dest.mkdir(parents=True, exist_ok=True)
    s3 = _s3()
    complete = True
    for name in BACKEND_OWNED_FILES:
        try:
            s3.download_file(S3_BUCKET, f"{S3_PREFIX}/{agent_id}/{name}", str(dest / name))
        except Exception as exc:  # noqa: BLE001 -- one missing object must not abort the other four
            log.warning("ensure_profile: failed to re-pull %s for %s: %s", name, agent_id, exc)
            complete = complete and _is_absent(exc)
    # `and complete` LAST so the walk always runs, whatever the named loop reported.
    complete = _pull_changed_objects(agent_id) and complete
    # Deliberately not part of the verdict: the mcp-tokens seed is best-effort by design
    # (seeded-only, F2 / M6-R35) and its failure mode is a dead tool, not stale instructions.
    _pull_new_mcp_token_files(agent_id)
    return complete


def _pull_changed_objects(agent_id: str) -> bool:
    """Ruling M6-R51: download every object in the profile prefix that S3 has touched since we
    last pulled or pushed our own copy of it. Returns False when the listing or any object's
    download failed, so the caller can decline to advance ``_REVISIONS`` and retry next turn.

    This is the half of the ruling that makes a ``.revision`` bump mean what the backend's own
    comments already claimed it meant ("so the bridge re-pulls the file on the next turn"). Only
    five names used to be re-pulled, so an Instructions-tab edit, a Files-panel save or a
    migration-seeded ``memories/`` was invisible to a warm microVM -- and then reverted by its
    next save.

    EXCLUDE_GLOBS is skipped on the way down for the same reasons it is skipped on the way up;
    that also covers the five names the caller has just pulled unconditionally. ``mcp-tokens/``
    is left to _pull_new_mcp_token_files: its seeded-only rule (F2 / M6-R35) is NOT redundant
    under recency, because Hermes rotates a token file IN PLACE and a newer backend object would
    otherwise replace a rotated local copy the container is the sole owner of.

    Two things this walk will NOT overwrite (I3 -- it can reach every key in the prefix, where
    the old five-name re-pull could only touch files no turn holds open):

    * a live SQLite file. The backend has no business writing one, but the Files-panel write
      route takes any relative path, and renaming a foreign file over the inode a concurrent
      turn has open is textbook corruption.
    * a file whose local copy has moved since we last recorded it -- i.e. the agent has unsaved
      changes to it. Last-writer-wins stays honest that way: the turn's own save is the later
      write, so it wins, instead of the pull destroying in-flight work mid-run. The next bump
      after that save picks the backend's object up normally.

    Best-effort, like the mcp-tokens pull below: a blip must not abort ensure_profile and tear
    down a warm profile mid-turn. Each object is wrapped on its own, so one unreadable key
    cannot mask every key after it -- which, with the verdict this returns, would otherwise be
    permanent and silent."""
    dest = _profile_dir(agent_id)
    synced = _SYNCED.setdefault(agent_id, {})
    mtimes = _MTIMES.setdefault(agent_id, {})
    prefix = f"{S3_PREFIX}/{agent_id}/"
    complete = True
    try:
        s3 = _s3()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix):]
                # a trailing "/" is a directory marker, not a file: renaming onto an existing
                # directory raises, and it is the realistic "one poison key" instance.
                if not rel or rel.endswith("/") or rel.startswith("mcp-tokens/") or is_excluded(rel):
                    continue
                if rel.endswith((".db", ".sqlite")):
                    continue
                modified = _modified_at(obj)
                if modified is not None and modified <= synced.get(rel, 0.0):
                    continue
                target = dest / rel
                if target.exists() and _change_stamp(target) != mtimes.get(rel):
                    continue  # unsaved local changes: this turn's own save is the later write
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    s3.download_file(S3_BUCKET, obj["Key"], str(target))
                except Exception as exc:  # noqa: BLE001 -- one bad key must not mask the rest
                    log.warning("ensure_profile: failed to pull %s for %s: %s",
                                obj["Key"], agent_id, exc)
                    complete = False
                    continue
                if modified is not None:
                    synced[rel] = modified
                # the pull is not a local change to push back up on this turn's save
                mtimes[rel] = _change_stamp(target)
    except Exception:  # noqa: BLE001 -- a listing blip must not abort ensure_profile
        log.warning("ensure_profile: recency re-pull failed for %s", agent_id)
        return False
    return complete


def _pull_new_mcp_token_files(agent_id: str) -> None:
    """F2 (predeploy review, M6-R35): seed a newly-connected OAuth MCP token file on a revision
    bump, seeded-only. ``restore_profile`` already pulls ``mcp-tokens/`` once at cold start, but
    a same-session integration connect (the microVM stays warm) never re-runs it, so
    ``hermes_profiles._write_mcp_token_files``'s freshly-seeded object never reaches an already-
    warm profile -- that server's tools stay dead until the session idles out (up to 4h).

    Only downloads a name this microVM has never seen: after the seed, Hermes's own
    ``OAuthClientProvider`` refreshes the token IN PLACE (report 06 §4.4) -- the container is the
    sole rotator from then on, so overwriting an existing local file here would clobber an
    already-rotated live token with a stale copy. One failed listing/download must not abort
    ``ensure_profile``'s own re-pull of the four/five files above."""
    dest_dir = _profile_dir(agent_id) / "mcp-tokens"
    prefix = f"{S3_PREFIX}/{agent_id}/mcp-tokens/"
    try:
        s3 = _s3()
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix):]
                if not rel:
                    continue
                target = dest_dir / rel
                if target.exists():
                    continue  # already seeded (or since rotated) -- never overwrite
                target.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(S3_BUCKET, obj["Key"], str(target))
    except Exception:  # noqa: BLE001 -- a listing/download blip must not abort ensure_profile
        log.warning("ensure_profile: mcp-tokens re-pull failed for %s", agent_id)


def ensure_profile(agent_id: str) -> None:
    """Pull the backend-owned files if we have never seen this agent, or if the backend bumped
    the revision since our last look.

    One GetObject-sized read per turn buys hot config patches (Task 13a) with no watcher, no
    lock, no restart. An unreadable revision leaves a WARM profile alone: a transient S3 error
    must not re-download (and momentarily blank) a profile that is already working mid-turn.

    The revision only advances when the re-pull actually completed (I2). A bump announces
    content and there is no second bump until the next backend write, so recording a revision
    whose content never landed strands the agent on stale instructions for the life of the
    microVM -- the very symptom this rule exists to remove. The cost of the other direction is
    that a persistently failing pull re-lists the prefix every turn, loudly, which is the right
    way round. (A legacy profile with no ``.revision`` in S3 at all pays the same per-turn list
    for the same reason: nothing can ever be recorded for it.)
    """
    if not S3_BUCKET:
        return
    revision = _read_revision(agent_id)
    known = _REVISIONS.get(agent_id)
    if known is not None and (revision is None or revision == known):
        return
    complete = _download_profile(agent_id)
    if revision is not None and complete:
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


def _upload_object(src: Path, key: str, *, if_match: str) -> str:
    """Conditionally replace a small root object and return its new ETag."""
    response = _s3().put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_PREFIX}/{key}",
        Body=src.read_bytes(),
        IfMatch=if_match,
    )
    etag = response.get("ETag")
    if not etag:
        raise RuntimeError(f"S3 replaced {key} without returning an ETag")
    return etag


def _root_auth_revision() -> str | None:
    """The owner's ``<owner>/hermes/auth.revision`` marker in S3, or ``None`` when it cannot be
    read (no root auth.json ever staged is not an error — same convention as a profile's own
    ``_read_revision``)."""
    try:
        key = f"{S3_PREFIX}/auth.revision"
        return _s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode().strip()
    except Exception:  # noqa: BLE001 -- a transient S3 blip must not tear down a warm profile
        return None


def _root_auth_etag() -> str | None:
    """The actual auth object's ETag — both the change detector and stale-writer fence."""
    try:
        return _s3().head_object(Bucket=S3_BUCKET, Key=f"{S3_PREFIX}/auth.json")["ETag"]
    except Exception:  # noqa: BLE001 -- absent/unreadable is handled like an unreadable revision
        return None


def _local_auth_hash() -> str | None:
    path = HOME / "auth.json"
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: C3 (fix round 1 continuation, M4-R44): the root auth.json gets the SAME per-turn revision
#: treatment a profile's config.yaml already has (Task 13a/13b) instead of a restore-ONCE-per-
#: process flag, which could never see a grant hermes_profiles.stage_codex_auth staged after
#: this microVM's first turn.
_ROOT_AUTH_REVISION: str | None = None
#: The ETag of the exact auth.json restored/saved locally. The revision marker wakes older images;
#: this value fences the actual object and catches marker-write reordering.
_ROOT_AUTH_ETAG: str | None = None
#: The sha256 of the local auth.json as of the last restore/save -- lets save_root_auth tell
#: "Hermes rotated the token this turn" apart from "nothing touched it".
_ROOT_AUTH_HASH: str | None = None


#: Sentinel _ROOT_AUTH_REVISION value meaning "checked once, found absent" -- distinct from the
#: real `None` starting value so the short-circuit below can tell "never checked" (must try the
#: download once) apart from "checked and there is nothing there" (must stop trying).
_ROOT_AUTH_ABSENT = ""


def restore_root_auth() -> None:
    """Pull ``<owner>/hermes/auth.json`` into the root home when either the compatibility
    ``auth.revision`` marker or the auth object's own ETag has moved since our last restore.
    The ETag is also retained as ``save_root_auth``'s conditional stale-writer fence.

    Almost every owner never connects ChatGPT OAuth, so ``auth.revision`` never exists. Recording
    the sentinel ``_ROOT_AUTH_ABSENT`` after the first check (instead of leaving
    ``_ROOT_AUTH_REVISION`` at ``None``) lets an unchanged missing object short-circuit on later
    turns. A marker or auth object that later appears still differs from the recorded state and
    triggers the download.
    """
    global _ROOT_AUTH_REVISION, _ROOT_AUTH_ETAG, _ROOT_AUTH_HASH
    if not S3_BUCKET:
        return
    revision = _root_auth_revision()
    etag = _root_auth_etag()
    marker_unchanged = revision is None or revision == _ROOT_AUTH_REVISION
    object_unchanged = etag is None or etag == _ROOT_AUTH_ETAG
    if _ROOT_AUTH_REVISION is not None and marker_unchanged and object_unchanged:
        return
    try:
        _download_object("auth.json", HOME / "auth.json")
    except Exception as exc:  # noqa: BLE001 -- no root auth.json yet (never connected) is not an error
        log.debug("restore_root_auth: no root auth.json to restore: %s", exc)
    _ROOT_AUTH_REVISION = revision if revision is not None else _ROOT_AUTH_ABSENT
    _ROOT_AUTH_ETAG = etag
    _ROOT_AUTH_HASH = _local_auth_hash()


def save_root_auth() -> None:
    """Push the root ``auth.json`` back only when THIS turn actually changed it locally
    (Hermes rotates the refresh token in place via ``hermes_cli/auth_codex.py``'s write-through)
    AND the backend hasn't staged a newer grant since our last restore — uploading over a
    fresher staged grant would silently discard it (C3).

    Silently a no-op (minor 4) when there is no local ``auth.json`` at all (an owner who has
    never connected ChatGPT OAuth) or when this turn never touched it — neither is worth a
    warning-level log; only the caller's own best-effort try/except (``turn.py``, same pattern
    as ``save_profile``) is left for a genuine S3 failure.

    The save verifies the revision and actual auth-object ETag observed at restore, then sends that
    ETag as S3 ``PutObject.IfMatch``. A disconnect or reconnect that lands after the checks wins:
    S3 rejects this warm writer rather than letting it resurrect or overwrite the grant.
    """
    global _ROOT_AUTH_REVISION, _ROOT_AUTH_ETAG, _ROOT_AUTH_HASH
    if not S3_BUCKET:
        return
    local_hash = _local_auth_hash()
    if local_hash is None or local_hash == _ROOT_AUTH_HASH:
        return  # no local auth.json, or this turn never touched it
    current_revision = _root_auth_revision()
    current_etag = _root_auth_etag()
    if (
        not _ROOT_AUTH_ETAG
        or current_etag != _ROOT_AUTH_ETAG
        or (_ROOT_AUTH_REVISION is not None and current_revision != _ROOT_AUTH_REVISION)
    ):
        log.info("save_root_auth: root auth changed mid-turn — skipping upload")
        return
    new_etag = _upload_object(HOME / "auth.json", "auth.json", if_match=_ROOT_AUTH_ETAG)
    new_revision = uuid.uuid4().hex
    _s3().put_object(Bucket=S3_BUCKET, Key=f"{S3_PREFIX}/auth.revision", Body=new_revision.encode())
    _ROOT_AUTH_REVISION = new_revision
    _ROOT_AUTH_ETAG = new_etag
    _ROOT_AUTH_HASH = local_hash


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


def profile_child_env_names() -> list[str]:
    """The names this profile's SANDBOXED CHILDREN (terminal, execute_code) may see.

    ONE dict defines it — the profile's active secret scope, which is the profile's own
    ``.env`` merged with whatever the bridge supplies for this turn. Nothing here is
    lane-specific: a lane that needs a shell command to see something puts it in the
    profile env (statically, via ``hermes_profiles.build_profile_env``) or in the per-turn
    scope overlay, and it appears here automatically.

    This is the list Hermes's ``terminal.env_passthrough`` wants. Its child envs strip
    secrets by default (``tools/environments/local.py::_filter_secret_env``) and consult
    this allowlist for the exceptions, resolving each name's VALUE back through the profile
    secret scope (``tools/env_passthrough.py::resolve_passthrough_value``) rather than the
    process env — so listing a name here grants that profile's child its OWN value and
    never another profile's.

    Empty outside the Hermes image (no ``agent.secret_scope``) and empty with no scope
    installed, which is the fail-closed direction: an unlisted name simply never reaches a
    shell.
    """
    try:
        from agent.secret_scope import current_secret_scope
    except ImportError:  # dev/test environments outside the Hermes image
        return []
    return sorted(current_secret_scope() or {})


def env_passthrough_pin(existing: dict, names: list[str]) -> dict:
    """UNION ``terminal.env_passthrough``, for the same reason ``plugins_pin`` unions
    ``plugins.enabled``: ``deep_merge`` replaces lists wholesale, so an operator- or
    agent-added name would be dropped by a plain merge."""
    current = ((existing.get("terminal") or {}).get("env_passthrough")) or []
    return {"terminal": {"env_passthrough": list(dict.fromkeys([*current, *names]))}}


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
    # Same merge direction and reason as plugins_pin: a UNION that must win over `existing`.
    # This runs INSIDE the turn's secret scope (see _construct_agent's caller), which is what
    # makes profile_child_env_names() see this profile's env rather than another's.
    config = deep_merge(config, env_passthrough_pin(existing, profile_child_env_names()))
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


def attachment_path(room_id: str, attachment: dict) -> Path:
    return HOME / "media" / "messaging" / "attachments" / room_id / (
        f"{attachment['id']}{_EXT.get(attachment.get('contentType'), '')}"
    )


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
            target = attachment_path(room_id, att)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    target.write_bytes(_get_backend_bytes(f"attachments/{bridge.OWNER}/{room_id}/{att['id']}"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("attachment fetch failed room=%s attachment=%s: %s", room_id, att.get("id"), exc)
                    continue
            out.append(str(target))
    return out
