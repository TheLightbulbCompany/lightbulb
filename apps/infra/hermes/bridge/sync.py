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
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bridge import HOME, S3_BUCKET, S3_PREFIX, task_mode

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
#: _ETAGS: S3's ETag for each file as of the last restore/pull/save -- the version a delete is
#: conditioned on, so an object someone else rewrote since is never deleted (_propagate_deletes).
_ETAGS: dict[str, dict[str, str]] = {}

# Fix round (minor 12): two turns for the SAME agent in different rooms run concurrently in
# one process (the FIFO group is room+agent), and both call save_profile on the one profile
# dir at the end. Without a lock, two concurrent uploads interleave and the last writer wins
# on S3 -- one turn's session rows can be lost on the next cold start.
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def _save_lock(agent_id: str) -> threading.Lock:
    with _save_locks_guard:
        return _save_locks.setdefault(agent_id, threading.Lock())


_profile_locks: dict[str, threading.RLock] = {}


def profile_lock(agent_id: str) -> threading.RLock:
    """Serializes the per-agent pulls (restore_profile, ensure_profile) now that two
    conversations of one agent run at once: both would otherwise see "not restored yet" and
    download the same tree over each other."""
    with _save_locks_guard:
        return _profile_locks.setdefault(agent_id, threading.RLock())

#: ``$HOME`` for a turn's shell. ``hermes_constants.get_subprocess_home`` returns
#: ``{HERMES_HOME}/home`` under ``TERMINAL_HOME_MODE: profile`` (turn.py sets it), and
#: ``_hermes_home_scope`` makes ``HERMES_HOME`` the profile dir for the turn -- so this is where
#: every CLI's own default lookup goes, and where the backend projects their credentials.
#: It must EXIST: ``auto`` mode falls back to ``/`` when it does not.
PROFILE_HOME = "home"

#: Backend-owned VALUES a projected CLI needs in the shell, written by
#: ``core/services/credentials/hermes_cli.py`` and read by ``cli_credential_env`` below.
CLI_ENV_FILE = "cli-env.json"

#: The profile paths the backend's CLI-connector projection owns -- mirrors
#: ``core/services/credentials/hermes_cli.py``'s ``profile_paths()`` + its ``cli-env.json``.
#: They are BACKEND-OWNED: pulled by name on every revision bump (_pull_cli_credentials) and
#: never uploaded by the bulk save (see EXCLUDE_GLOBS below), which is what keeps an agent from
#: writing a credential every later turn inherits. The one exception is ROTATING_CLI_CREDENTIALS.
CLI_CREDENTIAL_PREFIXES = (
    f"{PROFILE_HOME}/.aws/",
    f"{PROFILE_HOME}/.config/gh/",
    f"{PROFILE_HOME}/.config/gogcli/",
    f"{PROFILE_HOME}/.local/share/gogcli/",
    f"{PROFILE_HOME}/.xurl",
    f"{PROFILE_HOME}/quickbooks/",
    "skills/instagram/",
    # ponytail: the pull unlinks local files S3 lacks, so token.sh's local token-cache.json is
    # dropped on each revision bump and the next call re-mints (one extra grant). Harmless; move
    # the cache outside skills/shopify/ if the grant rate ever matters.
    "skills/shopify/",
    CLI_ENV_FILE,
)

#: CLI credential FILES whose refresh token is SINGLE-USE and rotates on every refresh
#: (``refresh_owner: actor``): the CLI rewrites the file in place, the old refresh token is dead
#: the moment it does, so letting the next pull restore the S3 copy hands the CLI a spent token
#: (xurl then fails as NoAuthMethod ~2 h after every connect). save_rotated_cli_credentials
#: writes these -- and only these -- back. Providers: ``x`` (xurl's ``~/.xurl``) and
#: ``quickbooks`` (the Intuit MCP server's QUICKBOOKS_TOKEN_STORE_PATH), plus ``instagram``, whose
#: 60-day token is not single-use but IS extended in place by its skill's ``token.sh`` -- without
#: the write-back every pull restores the original, which then expires on its first schedule.
#: An explicit list because providers.yaml is not in the image; gog's refresh token never changes.
#: Add a path here when an actor-refresh CLI rewrites its credential file in place.
ROTATING_CLI_CREDENTIALS = (
    f"{PROFILE_HOME}/.xurl",
    f"{PROFILE_HOME}/quickbooks/.env",
    "skills/instagram/credentials.json",
)

#: agent -> rel -> (S3 ETag, sha256) of the ROTATING_CLI_CREDENTIALS copy last pulled or written
#: back. The ETag is what lets a re-pull tell "our own copy" from a backend connect/disconnect.
_CLI_PULLED: dict[str, dict[str, tuple[str, str]]] = {}
_rotate_lock = threading.Lock()

#: agent -> rels of ROTATING_CLI_CREDENTIALS files the bridge filled a BROKERED access token into
#: (bridge/credentials.apply_cli_tokens, spec 13): the backend custodies the grant, the local
#: patch is never a rotation to write back. A re-pull (the backend rewrote the file) clears it;
#: the next vend patches again.
_BROKERED_CLI: dict[str, set[str]] = {}


def mark_brokered(agent_id: str, rel: str) -> None:
    _BROKERED_CLI.setdefault(agent_id, set()).add(rel)

#: Hazard of putting ``$HOME`` inside the profile: every tool's dotfiles and caches now sit in
#: the dir ``save_profile`` walks, so an `npm install` or a `git clone` would start uploading
#: package caches and agent-written credentials to S3 on every turn. These never go up.
#: ``.cache`` is also in #2756's profile-wide ``_REPRODUCIBLE_DIRS`` below, and ``.ssh``-shaped
#: files in its ``_CREDENTIAL_FILES``. That overlap is deliberate and left standing, for the
#: reason that block gives about the mirror case: ``is_excluded`` is an ``any()``, and each list
#: stays readable on its own terms -- theirs is profile-wide and push-only, this one is anchored
#: at the projected ``home/`` and pairs with a pull-by-name that UNLINKS. The two must not be
#: folded together: ``_pull_cli_credentials`` deletes local files under ITS prefixes that S3
#: lacks, which must never be pointed at an agent-owned path.
#: NB ``fnmatch``'s ``*`` spans ``/``, so one pattern per name covers the whole subtree.
# ``.local/share/uv/python`` = uv-managed CPython installs (hundreds of MB, reinstalled on demand).
# NOT all of ``.local/share/uv``: ``uv tool install`` envs live in ``tools/`` and their
# ``~/.local/bin`` entry points break if the env does not survive a cold start.
_HOME_UNSYNCED = (".cache", ".npm", ".ssh", ".gnupg", ".local/state", ".local/share/uv/python")
# pnpm puts its reproducible package store under XDG_DATA_HOME, which points into this
# profile. Old S3 copies must also be skipped on cold restore, not just future uploads.
PNPM_STORE_PREFIX = f"{PROFILE_HOME}/.local/share/pnpm/store/"
HOME_EXCLUDE_GLOBS = (
    f"{PROFILE_HOME}/.gitconfig",
    *(f"{PROFILE_HOME}/{name}/*" for name in _HOME_UNSYNCED),
    f"{PNPM_STORE_PREFIX}*",
)

#: Hazard 4 (local-exec persistence report): ``terminal.backend: local`` runs the agent's shell
#: IN its profile dir, so a build drops whole dependency/output trees into what save_profile
#: rglobs. PUSH-side only (see is_reproducible) -- ``.git/`` is deliberately absent, a fresh
#: clone's objects being packfiles rather than thousands of loose ones.
# ponytail: two ceilings left standing. (1) The per-file S3 fanout itself: a big SOURCE tree is
# still one PUT/GET per file in front of the first reply -- now on a 32-worker pool (_fan_out),
# which divides the wait but not the object count. (2) Gone: save_profile's walk prunes these
# dirs (_save_walk). Upgrade path for (1): archive the workspace subtree as ONE object and special-case it in
# save_profile/restore_profile, the way the SQLite backup API already is below. Not implemented.
# "*venv" is every venv-shaped name: .venv, venv, and the per-job ones a skill makes (.docx-venv:
# 65 MB each, 130 MB of the founder's profiles by 10-07).
_REPRODUCIBLE_DIRS = (
    "node_modules", "*venv", "__pycache__", ".next", "dist", "build", "target",
    "coverage", ".turbo", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
)
_REPRODUCIBLE_GLOBS = tuple(
    pattern for name in _REPRODUCIBLE_DIRS for pattern in (f"{name}/*", f"*/{name}/*")
)

#: Hazard 5: credential shapes the AGENT itself can write while doing git/gh work. Before local
#: exec, terminal commands never ran inside the profile dir at all, so EXCLUDE_GLOBS only ever
#: had to cover Hermes's own leak surface.
#: ``.aws/credentials`` and ``.config/gh/hosts.yml`` are covered at the profile ROOT by the
#: CLI-credential rail's own broader ``.aws/*`` / ``.config/gh/*`` too. That duplicate is
#: deliberate and harmless -- ``is_excluded`` is an ``any()``, and keeping them here means this
#: file does not depend on which of the two lands first. The ``*/``-prefixed forms are NOT
#: duplicates: that rail's prefixes are root-anchored and never match the copy an agent writes
#: inside a clone. The two lists stay separate -- its ``_pull_cli_credentials`` UNLINKS local
#: files under its prefixes that S3 lacks, which must never be pointed at an agent-owned path.
_CREDENTIAL_FILES = (
    ".git-credentials", ".netrc", ".npmrc", ".pypirc", ".docker/config.json",
    ".aws/credentials", ".config/gh/hosts.yml",
)
_CREDENTIAL_GLOBS = tuple(
    pattern for name in _CREDENTIAL_FILES for pattern in (name, f"*/{name}")
)

# Hermes writes sessions/request_dump_*.json on EVERY API error with no config switch
# (report 01 §1). The body is redacted but the file must never reach S3; logs/ likewise;
# .lightbulb-turn/ holds the per-turn local-session JWT (Task 11). WAL/SHM are skipped
# because save_profile snapshots SQLite through the backup API instead.
#: Hermes's own zero-byte lock sidecars beside state.db (observed in prod S3:
#: ``state.db.fts_rebuild.lock``, ``state.db.quarantine.lock``). A lock file means "a process
#: HERE holds this", so neither pushing one to S3 nor restoring one onto a fresh microVM is ever
#: right -- which is why this is checked on the restore side too (``restore_profile`` otherwise
#: downloads the whole prefix unfiltered, because EXCLUDE_GLOBS also names backend-owned files
#: that a cold start MUST pull). Deliberately not a bare ``*.lock``: an agent's own working tree
#: legitimately carries ``Cargo.lock`` / ``uv.lock`` / ``yarn.lock``.
LOCK_SIDECAR_GLOBS = ("state.db.*.lock", "*/state.db.*.lock")


def is_lock_sidecar(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in LOCK_SIDECAR_GLOBS)


#: S3-only terminal records of native Paperclip runs, under the agent's own profile prefix so
#: the backend's profile delete takes them with the agent. See put_paperclip_run_record.
PAPERCLIP_RUNS_DIR = ".paperclip-runs"
#: The same records for v1 task runs (/internal/v1/runs, bridge/runs.py).
TASK_RUNS_DIR = ".runs"
PAPERCLIP_RUN_RECORD_TTL_S = 24 * 3600
_RUN_RECORD_ID = re.compile(r"^[A-Za-z0-9_-]+$")  # the poll route's own run-id charset

#: The agent's scratch space: worktrees and other throwaway checkouts. Inside the profile (so Hermes's own
#: write_file/patch, fenced to HERMES_WRITE_SAFE_ROOT=/opt/data, can edit there -- /tmp they cannot, which is
#: why the "/tmp/wt" worktree rule never worked and Forge cloned into its saved tree, 10-07) and never synced.
SCRATCH_DIR = ".scratch"
EXCLUDE_GLOBS = (
    *_CREDENTIAL_GLOBS,
    f"{SCRATCH_DIR}/*",
    "sessions/request_dump_*.json",
    "logs/*",
    "logs/**/*",
    ".lightbulb-turn/*",
    "*.db-wal",
    "*.db-shm",
    "*.s3snap",  # save_profile's own SQLite snapshot, mid-upload
    # A live git lock uploaded mid-operation came back on every cold restore and git then
    # refused to touch the repo. Skipped on restore too (_restore_profile).
    ".git/*.lock",
    "*/.git/*.lock",
    "*.sqlite-wal",
    "*.sqlite-shm",
    *LOCK_SIDECAR_GLOBS,
    # Task 25: attachments a turn fetches (media/messaging/turn-*/...) live only for that
    # turn and are never synced to S3.
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
    # Voice notes: hermes_profiles.upload_voice_notes is the only writer; the turn reads it
    # into the system prompt (turn.with_profile_agents_md). Same clobber risk as the above.
    "VOICE_NOTES.md",
    # AgentCore Browser task: Hermes's own browser_vision tool writes
    # browser_screenshot_*.png here on every browse turn. Nothing stops save_profile pushing
    # them to S3 otherwise, and a screenshot reaches the room as an attachment (Task 9), not
    # a profile artifact.
    "cache/screenshots/*",
    # Backend-projected CLI connector credentials (backend credentials/hermes_cli.py). Same
    # class as the five above: the backend is the only writer, so an upload from here could
    # only ever revert a connect/disconnect the owner just made -- and would let a turn push
    # whatever it wrote into these paths back into the profile every later turn inherits.
    # They are pulled by NAME instead (_pull_cli_credentials), which bypasses these globs.
    *(f"{_p}*" if _p.endswith("/") else _p for _p in CLI_CREDENTIAL_PREFIXES),
    *HOME_EXCLUDE_GLOBS,
    # Native Paperclip terminal-run records (put_paperclip_run_record): written straight to S3
    # by the bridge, never through the profile dir, so no save may push a local copy over one
    # and no recency pull brings them down.
    f"{PAPERCLIP_RUNS_DIR}/*",
    f"{TASK_RUNS_DIR}/*",
    # Hermes's local credential vault keeps its Fernet key BESIDE the ciphertext. The bridge
    # routes the vault to the workspace store (bridge/website_logins.py), so nothing should land
    # here -- but a `hermes vault add` run from the agent's own shell still would, and a key
    # next to its ciphertext must never reach S3.
    "vault/*",
)


def _is_sqlite(path: Path) -> bool:
    """SQLite's own 16-byte file header. Only a real SQLite file may take the backup-API path;
    uploading a LIVE one raw can capture a half-written page."""
    try:
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _is_git_lock(rel: str) -> bool:
    return fnmatch.fnmatch(rel, ".git/*.lock") or fnmatch.fnmatch(rel, "*/.git/*.lock")


#: Hermes's own rule for its backups (hermes_cli/backup.py, _KEPT_CACHE_SUBDIRS): everything
#: under cache/ is regenerable -- catalogs, stamps, tool-output spill, the web/terminal/exec/
#: delegation caches -- EXCEPT media the gateway delivered or received, generated media and the
#: citations ledger. Those were 1,300 of Pulse's 3,582 cold-restore objects (57 MB), so the
#: regenerable ones neither go up at the end of a turn nor come down on a cold start; what older
#: images already pushed stays in S3 as dead weight.
KEPT_CACHE_SUBDIRS = frozenset({"images", "audio", "videos", "documents", "screenshots", "citations", "generated"})


def is_regenerable_cache(rel: str) -> bool:
    parts = rel.split("/", 2)
    return len(parts) >= 2 and parts[0] == "cache" and parts[1] not in KEPT_CACHE_SUBDIRS


def is_excluded(rel: str) -> bool:
    return is_regenerable_cache(rel) or any(fnmatch.fnmatch(rel, pattern) for pattern in EXCLUDE_GLOBS)


def is_reproducible(rel: str) -> bool:
    """Reproducible dependency/build/cache output. **PUSH side only**, deliberately not part of
    ``EXCLUDE_GLOBS``: an object under one of these names reaching S3 was put there by a BACKEND
    writer, never by ``save_profile``, and those must restore. Two do it -- the migration carries
    ``skills/**`` verbatim (``migrate_workspace_to_hermes._collect_carry_files``, which refuses
    the same directory names at the source), and the Files panel accepts any non-dot path
    (``hermes_profiles.is_hidden_profile_path``), so ``dist/``/``build/`` are uploadable by hand.
    Skipping those on the way down half-materialises a skill with no error anywhere."""
    return any(fnmatch.fnmatch(rel, pattern) for pattern in _REPRODUCIBLE_GLOBS)


WORK_DIR_PREFIX = "work/"
#: Durable trees a cold start does not wait for: they come down in the background right after the
#: profile itself (_restore_deferred). cache/ reaches here only for Hermes's kept media subdirs
#: (the rest is regenerable, never restored). work/ is deliberately NOT here: a conversation's own
#: tree is pulled by its turn (restore_subtree) and every other conversation's stays in S3. A
#: session's disk is ~4.7 GB inner on ~4.1 GB of outer free space (disk_free_mb); the founder's agents held 940 MB of old
#: work/ trees (raw demo clips, clones, a venv per docx job); sweeping them all down filled the
#: disk four hours into a session and every turn after that died in SQLite ("disk I/O error",
#: 10-07 prod). Pulse's 266 curator rollback blobs and the kept media are small: they still come.
_DEFERRED_PREFIXES = (".curator_backups/", "cache/")
_DEFERRED_RESTORE_DELAY_S = 2.0  # the turn's own restore_subtree (its work/ tree) goes first
_RESTORED_SUBTREES: set[tuple[str, str]] = set()

def _restored_work_tree(agent_id: str, rel: str) -> bool:
    """A work/ object in a tree this process restored for its turn: a backend write into it (a
    Files-panel save) still reaches the warm microVM on the next revision bump, as before."""
    return rel.startswith(WORK_DIR_PREFIX) and (agent_id, "/".join(rel.split("/")[:2]) + "/") in _RESTORED_SUBTREES


#: Restore-side skip: reinstallable home caches an older image already pushed. Deliberately NOT
#: ``is_excluded``/``is_reproducible`` -- those name backend-owned files and migrated skills a cold
#: start MUST pull -- and not all of ``home/.local``: the backend projects CLI credentials under
#: ``home/.local/share/gogcli/``.
_RESTORE_SKIP_PREFIXES = (
    *(f"{PROFILE_HOME}/{name}/" for name in _HOME_UNSYNCED),
    PNPM_STORE_PREFIX,
    f"{PAPERCLIP_RUNS_DIR}/",  # read by key on a poll miss, never needed in the profile dir
    f"{TASK_RUNS_DIR}/",
    WORK_DIR_PREFIX,  # a conversation's tree comes down on its own turn (see _DEFERRED_PREFIXES)
)

#: Per-file S3 GET/PUT ran at ~10-16 objects/s sequentially (Forge: 92,139 objects in 5,749 s).
#: boto3 clients are thread-safe, so the one shared ``_s3()`` client serves every worker.
_SYNC_WORKERS = 32
#: Below this much free disk the next turn's SQLite write is the one that fails -- with "disk I/O
#: error" and nothing to say why (10-07 prod). The warning is the clue.
LOW_DISK_MB = 200


def _outer_statvfs() -> os.statvfs_result:
    """``/`` is an ext4 inside a sparse loop image that sits on an OUTER overlay; blocks freed
    inside never go back to the outer, so the outer fills first and writes fail with EIO while
    ``/`` still reads free (10-07/10-08 founder stalls). ``/etc/hosts`` is bind-mounted FROM the
    outer, so its statvfs is the outer's free space."""
    try:
        return os.statvfs("/etc/hosts")
    except OSError:
        return os.statvfs("/")


def disk_free_mb() -> tuple[int, int]:
    """(outer, inner) free MB. Callers that need one number take ``min``."""
    st = _outer_statvfs()
    return st.f_bavail * st.f_frsize // 1_000_000, shutil.disk_usage("/").free // 1_000_000


#: Subtrees save_profile never uploads (every file under them is is_excluded or is_reproducible),
#: pruned at the directory so the walk never enters them: the pnpm store alone is ~195k files,
#: 15-18 s of walking on every save (10-08).
_UNSAVED_DIR_PREFIXES = (
    *(f"{PROFILE_HOME}/{name}/" for name in _HOME_UNSYNCED), PNPM_STORE_PREFIX, f"{SCRATCH_DIR}/",
)


def _unsaved_dir(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return f"{rel}/".startswith(_UNSAVED_DIR_PREFIXES) or any(fnmatch.fnmatch(name, d) for d in _REPRODUCIBLE_DIRS)


def _save_walk(src: Path) -> list[Path]:
    """Every file path under ``src`` except the pruned subtrees, sorted. Materialized, not lazy:
    the .s3snap temp save_profile creates inside src must not come back as a file of its own."""
    paths: list[Path] = []
    for root, dirs, files in os.walk(src):
        base = Path(root)
        prefix = "" if base == src else f"{base.relative_to(src).as_posix()}/"
        dirs[:] = [d for d in dirs if not _unsaved_dir(prefix + d)]
        paths.extend(base / name for name in files)
    return sorted(paths)


def _fan_out(fn, items) -> None:
    """Run ``fn`` over ``items`` on a bounded pool. The first worker exception cancels every
    transfer not yet started and re-raises here -- a throttle on a 90k-object restore must fail
    now, not after every other object has downloaded."""
    pool = ThreadPoolExecutor(max_workers=_SYNC_WORKERS)
    try:
        for future in as_completed([pool.submit(fn, item) for item in items]):
            future.result()
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


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
        from botocore.config import Config

        # botocore pools 10 connections by default; _fan_out runs _SYNC_WORKERS transfers at once.
        _S3_CLIENT = boto3.client("s3", config=Config(max_pool_connections=_SYNC_WORKERS * 2))
    return _S3_CLIENT


def _profile_dir(agent_id: str) -> Path:
    return HOME / "profiles" / agent_id


def read_prefixes(agent_id: str) -> list[str]:
    """Where a restore reads from, in order (a later prefix wins): the specialist profile, then --
    in task mode (spec 02 §3.3) -- the task's own overlay. One entry on a workspace VM: today's."""
    prefixes = [f"{S3_PREFIX}/{agent_id}/"]
    if task_mode.active():
        task_mode.check_agent(agent_id)
        prefixes.append(task_mode.overlay_prefix() + "/")
    return prefixes


def write_prefix(agent_id: str) -> str:
    """The ONE prefix every write of this process goes to: the task overlay in task mode, else the
    specialist profile. The 16 Sep invariant (one writer per S3 profile prefix) with a second
    microVM in the picture: the overlay has this VM as its one writer, and the specialist profile
    keeps the workspace VM (plus the backend's learning merge) as its one."""
    if task_mode.active():
        task_mode.check_agent(agent_id)
        return task_mode.overlay_prefix() + "/"
    return f"{S3_PREFIX}/{agent_id}/"


def _specialist_per_vm_state(prefixes: list[str], prefix: str, rel: str) -> bool:
    """Under task mode the specialist's ``state.db*`` and ``work/`` never restore: they are the
    workspace VM's per-VM state; a task VM starts its own (its overlay's copy, if any, restores)."""
    return len(prefixes) > 1 and prefix == prefixes[0] and rel.startswith(("state.db", WORK_DIR_PREFIX))


def ensure_profile_home(agent_id: str) -> Path:
    """Create the profile's ``home/`` -- the shell's ``$HOME`` for every turn under
    ``TERMINAL_HOME_MODE: profile``. Called from ``restore_profile`` (both paths), so it exists
    before the first turn of a process whether or not S3 had anything to restore: an absent dir
    silently degrades ``hermes_constants.get_subprocess_home`` back to ``/``, where every
    projected credential is invisible."""
    home = _profile_dir(agent_id) / PROFILE_HOME
    home.mkdir(parents=True, exist_ok=True)
    return home


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


def emit_metrics(metrics: dict[str, tuple[float, str]]) -> None:
    """One CloudWatch EMF line ({name: (value, unit)}). Raw JSON on stdout: CloudWatch extracts EMF
    only from a log event that IS the JSON, so the logger's "[bridge] " prefix would hide it. Runtime
    log groups are per runtime, so a CDK metric filter cannot target them; EMF works in any group.
    The env comes from the bucket name (lightbulb-<env>-openclaw-replicas) -- the bridge has no
    env variable of its own."""
    parts = (S3_BUCKET or "").split("-")
    env = parts[1] if len(parts) > 2 and parts[0] == "lightbulb" else "unknown"
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": "LightbulbAI",
                            "Dimensions": [["env", "service"]],
                            "Metrics": [{"Name": n, "Unit": u} for n, (_v, u) in metrics.items()],
                        }
                    ],
                },
                "env": env,
                "service": "hermes-bridge",
                **{n: v for n, (v, _u) in metrics.items()},
            }
        ),
        flush=True,
    )


def _emit_restore_metric(seconds: float, objects: int, free_mb: float) -> None:
    """One EMF line per cold restore."""
    emit_metrics({
        "bridge.restore.seconds": (round(seconds, 3), "Seconds"),
        "bridge.restore.objects": (objects, "Count"),
        "bridge.restore.disk_free_mb": (round(free_mb), "Megabytes"),
    })


#: Hermes's durable lease tables in state.db (hermes_state_common.py:506-520).
_LEASE_TABLES = ("session_turn_leases", "compression_locks")


def clear_turn_leases(db: Path) -> None:
    """Drop every turn lease and compression lock a restored state.db carries.

    save_profile snapshots state.db while another conversation of the agent may be mid-turn, so
    the S3 copy can hold that turn's lease (holder ``pid=<n>:turn=...``, TTL 300 s,
    agent/turn_facade_lease.py:20-21). Hermes reclaims a lease only once it expires or its pid is
    provably dead -- and never when the pid is its OWN (hermes_state.py:121-145) -- and a fresh
    microVM's bridge often has the very pid the old one had. A resurrected lease would make the
    next turn on that key wait up to 300 s, then drop our room history for a DB reload
    (turn_facade_lease.py:284-296). A cold restore runs once per process, before anything here
    opens the file, so every lease in it belongs to a dead process. Hermes has no startup sweep
    of its own for these (it relies on pid liveness, which a microVM restart defeats)."""
    if not db.is_file() or not _is_sqlite(db):
        return
    try:
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            for table in _LEASE_TABLES:
                with contextlib.suppress(sqlite3.OperationalError):  # a state.db older than the table
                    conn.execute(f"DELETE FROM {table}")
    except sqlite3.Error:
        log.warning("could not clear restored leases in %s", db, exc_info=True)


def _download_one(agent_id: str, dest: Path, item: tuple[str, dict]) -> None:
    rel, obj = item
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    s3 = _s3()
    s3.download_file(S3_BUCKET, obj["Key"], str(target))
    modified = _modified_at(obj)
    if modified is not None:
        _SYNCED.setdefault(agent_id, {})[rel] = modified
    _MTIMES.setdefault(agent_id, {})[rel] = _change_stamp(target)
    if obj.get("ETag"):
        _ETAGS.setdefault(agent_id, {})[rel] = obj["ETag"]
    _remember_cli_credential(agent_id, rel, obj, target)


def restore_subtree(agent_id: str, path: Path) -> int:
    """Pull one directory of the profile that the cold restore left in S3 (a conversation's
    ``work/`` tree, a deferred durable tree), once per process. Returns the object count. The
    files it brings down join the save baseline exactly like a cold-restored one, so an unchanged
    file is never re-uploaded and a deleted one is deleted in S3. A file that already exists
    locally is left alone: this process started empty, so a local copy is a turn's own write,
    newer than anything in S3, and the end-of-turn save carries it up."""
    if not S3_BUCKET:
        return 0
    dest = _profile_dir(agent_id)
    rel_dir = path.relative_to(dest).as_posix().rstrip("/") + "/"
    if (agent_id, rel_dir) in _RESTORED_SUBTREES:
        return 0
    with profile_lock(agent_id):
        if (agent_id, rel_dir) in _RESTORED_SUBTREES:
            return 0
        prefixes = read_prefixes(agent_id)
        wanted: dict[str, tuple[str, dict]] = {}  # a later prefix (the task overlay) wins, as in the cold restore
        for prefix in prefixes:
            if _specialist_per_vm_state(prefixes, prefix, rel_dir):
                continue  # a cold attempt 2+ gets the task's own work/ tree back, never the specialist's copy
            for page in _s3().get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix + rel_dir):
                for obj in page.get("Contents", []):
                    rel = obj["Key"][len(prefix):]
                    if rel.endswith("/") or is_lock_sidecar(rel) or _is_git_lock(rel) or (dest / rel).exists():
                        continue
                    wanted[rel] = (rel, obj)
        _fan_out(lambda item: _download_one(agent_id, dest, item), list(wanted.values()))
        _RESTORED_SUBTREES.add((agent_id, rel_dir))
    if wanted:
        log.info("restored %s %s: %d objects", agent_id, rel_dir, len(wanted))
    return len(wanted)


def _start_deferred_restore(agent_id: str) -> None:
    threading.Thread(target=_restore_deferred, args=(agent_id,), name=f"restore-deferred-{agent_id[:8]}", daemon=True).start()


def _restore_deferred(agent_id: str) -> int:
    """Bring down the trees the cold restore deferred (_DEFERRED_PREFIXES), one subtree at a time
    so a turn's own restore_subtree interleaves instead of waiting: the curator's rollback blobs
    and Hermes's kept media caches. Returns the object count."""
    time.sleep(_DEFERRED_RESTORE_DELAY_S)
    dest = _profile_dir(agent_id)
    subtrees: list[str] = []
    try:
        for prefix in read_prefixes(agent_id):  # the task overlay's deferred trees too
            for top in _DEFERRED_PREFIXES:
                for page in _s3().get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix + top):
                    for obj in page.get("Contents", []):
                        rel = obj["Key"][len(prefix):]
                        if is_regenerable_cache(rel):
                            continue
                        sub = "/".join(rel.split("/")[:2]) if top == "cache/" else top.rstrip("/")
                        if "/" in rel[len(top):] or top != "cache/":
                            if sub not in subtrees:
                                subtrees.append(sub)
        total = sum(restore_subtree(agent_id, dest / sub) for sub in subtrees)
    except Exception:  # noqa: BLE001 -- the next turn of that conversation restores its tree itself
        log.warning("deferred restore failed for %s", agent_id, exc_info=True)
        return 0
    log.info("restored deferred trees for %s: %d objects in %d subtrees", agent_id, total, len(subtrees))
    return total


def restore_profile(agent_id: str) -> float:
    """Download the agent's profile dir from S3 once per process. Returns seconds taken."""
    with profile_lock(agent_id):
        return _restore_profile(agent_id)


def _restore_profile(agent_id: str) -> float:
    if agent_id in _restored or not S3_BUCKET:
        ensure_profile_home(agent_id)  # also mkdirs the profile dir itself
        _restored.add(agent_id)
        return 0.0
    t0 = time.time()
    dest = _profile_dir(agent_id)
    ensure_profile_home(agent_id)
    s3 = _s3()
    # M6-R51: _download_one records the save baseline (_MTIMES/_SYNCED) per object as it writes
    # it -- without that the first save of the process would upload the entire tree it just
    # downloaded -- and not by a second rglob afterwards: cold-restore latency sits in front of
    # the first reply.
    prefixes = read_prefixes(agent_id)
    wanted: dict[str, tuple[str, dict]] = {}  # rel -> (rel, obj); a later prefix (the task overlay) wins
    deferred = 0
    for prefix in prefixes:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(prefix):]
                if not rel or rel.endswith("/"):
                    continue  # directory marker: downloading it onto the directory raises
                if is_lock_sidecar(rel) or rel.startswith(_RESTORE_SKIP_PREFIXES) or _is_git_lock(rel):
                    continue  # process-local lock or reinstallable home cache
                if is_regenerable_cache(rel):
                    continue  # Hermes rebuilds it; never worth a GET
                if _specialist_per_vm_state(prefixes, prefix, rel):
                    continue  # per-VM state: a task VM starts its own (its overlay's copy, if any, still restores)
                if rel.startswith(_DEFERRED_PREFIXES):
                    deferred += 1
                    continue
                wanted[rel] = (rel, obj)

    _fan_out(lambda item: _download_one(agent_id, dest, item), list(wanted.values()))
    clear_turn_leases(dest / "state.db")
    n = len(wanted)
    _restored.add(agent_id)
    took = time.time() - t0
    free_mb = shutil.disk_usage(dest).free / 1e6
    log.info("restored profile %s: %d objects in %.1fs (%d deferred), %.0f MB free", agent_id, n, took, deferred, free_mb)
    _emit_restore_metric(took, n, free_mb)
    if deferred:
        _start_deferred_restore(agent_id)
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
    save, migration-seeded ``memories/``. A file the agent DELETED locally is deleted in S3 too
    (``_propagate_deletes``); before that, every cold restore brought it back and a workspace
    could only grow.

    Serialized per agent (see _save_lock) -- two turns for the same agent in different rooms
    can finish concurrently, and an interleaved pair of uploads would let the later-finishing
    one silently clobber files the earlier one already wrote for a different room's session
    rows."""
    if not S3_BUCKET:
        return 0.0
    with _save_lock(agent_id):
        t0 = time.time()
        src = _profile_dir(agent_id)
        prefix = write_prefix(agent_id)  # the task overlay in task mode: never the specialist profile
        s3 = _s3()
        mtimes = _MTIMES.setdefault(agent_id, {})
        synced = _SYNCED.setdefault(agent_id, {})
        changed: list[tuple[Path, str, int]] = []
        for path in _save_walk(src):
            rel = path.relative_to(src).as_posix()
            if not path.is_file() or is_excluded(rel) or is_reproducible(rel):
                continue
            stamp = _change_stamp(path)
            if mtimes.get(rel) == stamp:
                continue  # untouched by this turn: S3's copy is the source of truth
            changed.append((path, rel, stamp))

        def _upload(item: tuple[Path, str, int]) -> None:
            # Never raises: one bad file must not cancel the rest of the save through _fan_out,
            # and since it is never marked saved it would fail every later save the same way --
            # everything the agent wrote afterwards lost at the next park. It retries next save.
            path, rel, stamp = item
            try:
                _upload_one(path, rel, stamp)
            except FileNotFoundError:
                pass  # gone mid-save (another conversation's git lock, a temp file)
            except Exception:  # noqa: BLE001
                log.warning("save %s: could not upload %s; retrying next save", agent_id, rel, exc_info=True)

        def _upload_one(path: Path, rel: str, stamp: int) -> None:
            upload = path
            if path.suffix in (".db", ".sqlite") and _is_sqlite(path):
                snap = path.with_name(path.name + ".s3snap")
                snap.unlink(missing_ok=True)  # a leftover from an interrupted save poisons backup
                with contextlib.closing(sqlite3.connect(path)) as conn:
                    with contextlib.closing(sqlite3.connect(snap)) as out:
                        try:
                            conn.backup(out)  # a real failure raises: retried next save, never raw bytes
                        except sqlite3.Error as exc:  # EIO (outer disk full) vs ENOSPC: say which
                            outer_mb, inner_mb = disk_free_mb()
                            log.warning("save %s: %s backup failed (%s); outer %d MB / %d inodes free, inner "
                                        "%d MB / %d inodes free", agent_id, rel, getattr(exc, "sqlite_errorname", "?"),
                                        outer_mb, _outer_statvfs().f_favail, inner_mb, os.statvfs("/").f_favail)
                            raise
                upload = snap
                log.info("save %s: %s snapshot %d bytes", agent_id, rel, snap.stat().st_size)
            # anything else -- including a .db that is not SQLite (DuckDB, Thumbs.db) -- goes up raw
            # I1: the instant is read BEFORE the PUT, never after. Reading it after would put it
            # past the object's real LastModified by the PUT's own latency plus any clock skew,
            # and a backend write landing inside that gap would then be <= synced[rel] FOREVER --
            # skipped on every later bump, C1 re-opened for that one file. Under-recording is the
            # safe rounding: it costs at most a redundant re-download of our own identical bytes.
            now = time.time()
            try:
                etag = s3_etag(upload)
                s3.upload_file(str(upload), S3_BUCKET, prefix + rel)
            finally:
                if upload is not path:
                    upload.unlink(missing_ok=True)
            mtimes[rel] = stamp
            synced[rel] = now
            _ETAGS.setdefault(agent_id, {})[rel] = etag

        _fan_out(_upload, changed)
        n = len(changed)
        try:
            _propagate_deletes(agent_id, src, s3, prefix, mtimes, synced)
        except Exception:  # noqa: BLE001 -- the uploads landed; unremoved entries retry next save
            log.warning("delete propagation failed for %s", agent_id, exc_info=True)
        took = time.time() - t0
        log.info("saved profile %s: %d changed objects in %.1fs", agent_id, n, took)
        outer_mb, inner_mb = disk_free_mb()
        if min(outer_mb, inner_mb) < LOW_DISK_MB:
            log.warning("profile disk low: outer %d MB, inner %d MB free after saving %s", outer_mb, inner_mb, agent_id)
    try:
        save_rotated_cli_credentials()
    except Exception:  # a listing blip retries on the next save
        log.warning("rotated CLI credential write-back failed", exc_info=True)
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
    # Unique per write: a shared "config.yaml.tmp" let one concurrent writer rename away the
    # other's temp file mid-write (FileNotFoundError, a crashed turn).
    tmp = directory / f"{CONFIG_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    tmp.replace(directory / CONFIG_NAME)  # atomic: a torn config.yaml bricks the agent's next turn


# ---- Task 13b: hot-patch re-pull -----------------------------------------------------------
# The backend is the ONLY writer of these backend-owned files (config.yaml, .env, SOUL.md,
# .revision -- see hermes_profiles.upload_profile/patch_profile_config/set_profile_config on the
# backend, and save_profile's EXCLUDE_GLOBS entries above -- plus lightbulb-facts.json, written by
# hermes_profiles.write_facts_to_profiles, Row #15 / Ruling M6-R30). A patch bumps .revision; this
# is how the bridge notices without a watcher or a restart.
BACKEND_OWNED_FILES = (
    "config.yaml", ".env", "SOUL.md", ".revision", "lightbulb-facts.json", "VOICE_NOTES.md",
)

_REVISIONS: dict[str, str] = {}
#: F5 (spec 02 §2.7): set when a re-pull wrote a key under ``skills/``; ensure_profile clears
#: Hermes's in-process skills prompt cache once, so the NEXT session's system prompt lists it.
_SKILLS_TOUCHED: dict[str, bool] = {}


def _read_revision(agent_id: str) -> str | None:
    """The profile's ``.revision`` marker in S3, or None when it cannot be read."""
    try:
        key = f"{S3_PREFIX}/{agent_id}/.revision"
        return _s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode().strip()
    except Exception:  # noqa: BLE001 -- a transient S3 blip must not tear down a warm profile
        return None


#: boto3's default TransferConfig: upload_file goes multipart at 8 MiB, in 8 MiB parts.
_MULTIPART_CHUNK = 8 * 1024 * 1024


def s3_etag(path: Path) -> str:
    """The ETag S3 gives ``upload_file``'s object for this file: the MD5 for a single PUT, the
    MD5 of the part MD5s plus ``-<parts>`` for a multipart one (SSE-S3 bucket; verified against
    the dev bucket for 3 MB and 20 MB files). If it is ever wrong -- a KMS bucket, a changed
    part size -- a conditional delete just refuses, so the file is kept, never lost."""
    parts = []
    with path.open("rb") as stream:
        while chunk := stream.read(_MULTIPART_CHUNK):
            parts.append(hashlib.md5(chunk).digest())
    if path.stat().st_size < _MULTIPART_CHUNK:  # s3transfer: multipart at >= the threshold
        return '"%s"' % (parts[0].hex() if parts else hashlib.md5(b"").hexdigest())
    return '"%s-%d"' % (hashlib.md5(b"".join(parts)).hexdigest(), len(parts))


def _propagate_deletes(agent_id: str, src: Path, s3, prefix: str, mtimes: dict, synced: dict) -> int:
    """Delete from S3 what the agent deleted locally, so a cold restore stops resurrecting it.

    Only files this process itself restored, pulled or saved are candidates, and each delete is
    conditioned on the ETag we last saw (S3 ``If-Match``): an object the backend rewrote since
    -- a Files-panel save landing mid-turn, even between our check and the delete -- fails the
    precondition and stays, and the next re-pull brings it back down. A profile without its
    config.yaml deletes nothing: the backend writes that file into every real profile and the
    agent cannot remove it from S3 (it is excluded), so its absence means a broken or wiped
    microVM, not an agent's ``rm`` -- ``home/`` and other empty dirs prove nothing."""
    if not (src / CONFIG_NAME).is_file():
        return 0
    etags = _ETAGS.setdefault(agent_id, {})
    gone = [rel for rel in mtimes if rel in etags and not is_excluded(rel)
            and not is_reproducible(rel) and not (src / rel).is_file()]
    removed = 0
    for i in range(0, len(gone), 1000):
        batch = gone[i:i + 1000]
        result = s3.delete_objects(Bucket=S3_BUCKET, Delete={
            "Objects": [{"Key": prefix + rel, "ETag": etags[rel]} for rel in batch], "Quiet": False})
        forget = [d["Key"][len(prefix):] for d in result.get("Deleted", [])]
        # NoSuchKey: already gone. PreconditionFailed: someone rewrote it -- keep the entry so
        # the re-pull restores it. Anything else (throttle, access) retries on the next save.
        forget += [e["Key"][len(prefix):] for e in result.get("Errors", []) if e.get("Code") == "NoSuchKey"]
        for rel in forget:
            mtimes.pop(rel, None)
            synced.pop(rel, None)
            etags.pop(rel, None)
        removed += len(result.get("Deleted", []))
    if removed:
        log.info("save %s: deleted %d objects the agent removed", agent_id, removed)
    return removed


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
    # Same: a CLI connector the owner just connected/disconnected is a dead (or stale) binary,
    # not stale instructions. Unlike mcp-tokens this OVERWRITES -- the backend is the only
    # writer, so its object is always the newer truth.
    _pull_cli_credentials(agent_id)
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
    if task_mode.active():
        # The walk lists the SPECIALIST prefix: a merge (another task's) moving a learning file
        # there would come down over the overlay-layered copy this task is working on. The task
        # VM keeps the tree its cold restore layered; the merge reconciles the two at the end.
        return True
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
                if rel.startswith(_RESTORE_SKIP_PREFIXES) and not _restored_work_tree(agent_id, rel):
                    # never on this disk (Codex on #3794: with work/ absent from _SYNCED, every old
                    # work object looked new to this walk and the first bump brought the 940 MB back)
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
                if obj.get("ETag"):
                    _ETAGS.setdefault(agent_id, {})[rel] = obj["ETag"]
                if rel.startswith("skills/"):
                    _SKILLS_TOUCHED[agent_id] = True
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


def _pull_cli_credentials(agent_id: str) -> None:
    """Re-pull the backend's projected CLI connector credentials by name.

    They sit in EXCLUDE_GLOBS so ``save_profile`` never pushes them, which also takes them out
    of ``_pull_changed_objects``'s recency walk -- so without this an owner connecting GitHub
    (or disconnecting AWS) mid-session would never reach an already-warm microVM, exactly the
    hole ``_pull_new_mcp_token_files`` exists to close for MCP tokens.

    Overwrites rather than seeding once: the backend is the writer of these paths (an agent's
    own edits are never uploaded), so its object is by construction the newer one -- except a
    ROTATING_CLI_CREDENTIALS file whose S3 ETag is still the one we pulled or wrote back, which
    is skipped so a re-pull cannot resurrect a spent refresh token over the live rotated copy.
    A local file the backend has since DELETED is removed too.

    Scope of that last claim: the prune is PER AGENT, on that agent's next turn. A disconnect
    therefore lands immediately for whichever agents take a turn after it and LAGS for the rest
    until they do — so "a disconnected connector stops working" is eventually-consistent across
    the roster, not atomic. Bounded by the life of the warm process (a cold restore re-pulls the
    whole prefix, where the object is already gone), and the server side is the real fence
    anyway: the AWS bearer token is revoked at the backend the moment the row is deleted, and a
    pasted PAT is revoked at the vendor, not here."""
    dest = _profile_dir(agent_id)
    base = f"{S3_PREFIX}/{agent_id}/"
    try:
        s3 = _s3()
        for prefix in CLI_CREDENTIAL_PREFIXES:
            remote: set[str] = set()
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=f"{base}{prefix}"):
                for obj in page.get("Contents", []):
                    rel = obj["Key"][len(base):]
                    if not rel or rel.endswith("/"):
                        continue
                    # A prefix with no trailing "/" names one FILE: "home/.xurl" must not pull
                    # a hypothetical "home/.xurlfoo" (matches the backend's own fetch rule).
                    if not prefix.endswith("/") and rel != prefix:
                        continue
                    remote.add(rel)
                    target = dest / rel
                    held = _CLI_PULLED.get(agent_id, {}).get(rel)
                    if held and held[0] == obj.get("ETag") and target.is_file():
                        continue  # our own copy, or a rotation not yet written back
                    target.parent.mkdir(parents=True, exist_ok=True)
                    s3.download_file(S3_BUCKET, obj["Key"], str(target))
                    _remember_cli_credential(agent_id, rel, obj, target)
                    _BROKERED_CLI.get(agent_id, set()).discard(rel)  # the backend's token-free form again
            local_root = dest / prefix.rstrip("/")
            locals_ = (
                [p for p in local_root.rglob("*") if p.is_file()]
                if local_root.is_dir()
                else ([local_root] if local_root.is_file() else [])
            )
            for path in locals_:
                if (rel := path.relative_to(dest).as_posix()) not in remote:
                    path.unlink(missing_ok=True)
                    _CLI_PULLED.get(agent_id, {}).pop(rel, None)
    except Exception:  # noqa: BLE001 -- a listing/download blip must not abort ensure_profile
        log.warning("ensure_profile: CLI credential re-pull failed for %s", agent_id)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _remember_cli_credential(agent_id: str, rel: str, obj: dict, target: Path) -> None:
    if rel in ROTATING_CLI_CREDENTIALS and obj.get("ETag"):
        _CLI_PULLED.setdefault(agent_id, {})[rel] = (obj["ETag"], _sha(target.read_bytes()))


def save_rotated_cli_credentials() -> None:
    """Write a CLI's in-place token rotation back to S3, for EVERY roster profile.

    Every profile holds an identical copy of the owner-level grant (backend ``hermes_cli``), but
    WHICH local copy rotates differs: xurl rides ``$HOME``, frozen at the first profile to run a
    shell, while the QuickBooks MCP server writes the running agent's own profile. So every
    local copy is scanned, and the rotation goes to every profile's key. Each profile is
    replaced only while S3 still holds the exact bytes we rotated FROM, and the PUT is
    ``IfMatch`` on the ETag that check read: a backend connect/disconnect that landed first (or
    lands in between, 412) wins and is skipped, never overwritten. Best-effort per profile."""
    if not S3_BUCKET or task_mode.active():
        return  # a task VM is never a second writer of the roster's credential files
    with _rotate_lock:
        for rel in ROTATING_CLI_CREDENTIALS:
            rotated = []
            for agent_id, held in list(_CLI_PULLED.items()):
                if rel in _BROKERED_CLI.get(agent_id, ()):
                    continue  # a brokered access token the bridge filled in: local only, never written back
                path = _profile_dir(agent_id) / rel
                if rel in held and path.is_file() and _sha(new := path.read_bytes()) != held[rel][1]:
                    rotated.append((path.stat().st_mtime_ns, agent_id, new, held[rel][1]))
            if rotated:
                # ponytail: two agents rotating in one process -> newest mtime wins; the loser's
                # token is dropped (Intuit tolerates its reuse briefly, nothing relies on that).
                _, agent_id, new, old_sha = max(rotated)
                _write_back_rotation(agent_id, rel, new, old_sha)


def _write_back_rotation(source: str, rel: str, new: bytes, old_sha: str) -> None:
    s3 = _s3()
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=S3_BUCKET, Prefix=f"{S3_PREFIX}/", Delimiter="/")
    roster = [p["Prefix"][len(S3_PREFIX) + 1:].strip("/") for page in pages for p in page.get("CommonPrefixes", [])]
    for agent_id in roster:
        key = f"{S3_PREFIX}/{agent_id}/{rel}"
        try:
            current = s3.get_object(Bucket=S3_BUCKET, Key=key)
            if _sha(current["Body"].read()) != old_sha:
                continue  # the backend wrote a newer grant (or we already did): it wins
            etag = s3.put_object(Bucket=S3_BUCKET, Key=key, Body=new, IfMatch=current["ETag"])["ETag"]
        except Exception as exc:  # noqa: BLE001 -- 412/absent/blip: skip this profile, keep the rest
            log.info("rotated %s not written back for %s: %s", rel, agent_id, exc)
            continue
        held = _CLI_PULLED.get(agent_id, {}).get(rel)
        local = _profile_dir(agent_id) / rel
        if agent_id == source or (held and local.is_file() and _sha(local.read_bytes()) == held[1]):
            # an untouched warm copy gets the new token now: its own MCP server reads that file
            if agent_id != source:
                local.write_bytes(new)
            _CLI_PULLED[agent_id][rel] = (etag, _sha(new))


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
    if _SKILLS_TOUCHED.pop(agent_id, False):  # even if incomplete: what landed must reach new sessions
        _clear_skills_cache()


def _clear_skills_cache() -> None:
    """F5: Hermes keys its skills prompt cache by dirs/tools/platform, not the manifest, so a
    skill pulled into a warm process is invisible to new sessions until this runs. Sessions
    already built keep their stored prompt. Process-global clear is fine (spec 08 R13)."""
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
    except ImportError:
        return
    clear_skills_system_prompt_cache()


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

#: Spec 13: the root auth.json S3 holds is the backend's custody marker (``providers.openai-codex.
#: custody == "backend"``, tokens empty); the bridge fills the vended token into the LOCAL copy
#: (credentials.write_codex_auth) and never writes it back. Set by restore_root_auth from what it
#: pulled, and by write_codex_auth.
_CODEX_BROKERED = False


def _is_codex_marker(path: Path) -> bool:
    try:
        providers = json.loads(path.read_text(encoding="utf-8")).get("providers") or {}
        state = providers.get("openai-codex") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(state, dict) and state.get("custody") == "backend"


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
    global _ROOT_AUTH_REVISION, _ROOT_AUTH_ETAG, _ROOT_AUTH_HASH, _CODEX_BROKERED
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
    # The backend's marker (spec 13): the vend fills the token in locally; this process never
    # writes the grant back. A real grant pulled later (custody rolled back) ends that.
    _CODEX_BROKERED = _is_codex_marker(HOME / "auth.json")


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
    if not S3_BUCKET or task_mode.active() or _CODEX_BROKERED:
        return  # a task VM, or a brokered grant: never written back (spec 02 §3.3; the broker owns it, spec 13)
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


def _run_records_prefix(agent_id: str, runs_dir: str = PAPERCLIP_RUNS_DIR) -> str:
    return f"{write_prefix(agent_id)}{runs_dir}/"


# ---- task mode: base manifest and seal (spec 07 §3.5; lane V2) --------------------------------
#: The learning files the backend's merge (core/services/learning_merge.py) reads; the three the
#: entry-level merge needs the starting TEXT of ride in the base manifest.
LEARNING_TEXT_FILES = ("memories/MEMORY.md", "memories/USER.md", "skills/.usage.json")


def is_learning_file(rel: str) -> bool:
    """Mirrors ``learning_merge.is_learning_file``: the two memory files, ``skills/.usage.json``
    and skill dirs -- never ``skills/workspace/`` (review G16), the curator's dot files, or a
    credential skill dir the backend projects (CLI_CREDENTIAL_PREFIXES)."""
    if rel in LEARNING_TEXT_FILES:
        return True
    if not rel.startswith("skills/") or rel.startswith(CLI_CREDENTIAL_PREFIXES):
        return False
    parts = rel.split("/")[1:]
    return not (parts[0] == "workspace" or any(p.startswith(".") for p in parts))


def _learning_files(agent_id: str) -> dict[str, Path]:
    src = _profile_dir(agent_id)
    out = {}
    for path in _save_walk(src):
        rel = path.relative_to(src).as_posix()
        if path.is_file() and is_learning_file(rel):
            out[rel] = path
    return out


def write_base_manifest(agent_id: str, attempt: int) -> bool:
    """B for this attempt's merge: every learning file as the attempt STARTS (sha256, the S3 ETag we
    restored it at, and the text of the three entry-level files), as ``base-{attempt:04d}.json`` in
    the task overlay. Once per attempt: False when it is already there (a retried start). The
    local tree is the right source on a cold start (specialist layered with the overlay) and on a
    warm one (the end of the previous attempt) alike."""
    if not S3_BUCKET or not task_mode.active():
        return False
    s3, key = _s3(), f"{write_prefix(agent_id)}base-{attempt:04d}.json"
    try:
        s3.get_object(Bucket=S3_BUCKET, Key=key)
        return False
    except Exception as exc:  # noqa: BLE001 -- only "absent" means "write it"
        if not _is_absent(exc):
            raise
    etags = _ETAGS.get(agent_id, {})
    files: dict[str, dict] = {}
    for rel, path in _learning_files(agent_id).items():
        data = path.read_bytes()
        entry: dict = {"sha256": _sha(data), "etag": etags.get(rel, "")}
        if rel in LEARNING_TEXT_FILES:
            entry["text"] = data.decode("utf-8", errors="replace")
        files[rel] = entry
    body = json.dumps({"attempt": attempt, "files": files}, separators=(",", ":")).encode("utf-8")
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body)
    return True


def seal_attempt(agent_id: str, task_id: str, attempt: int) -> None:
    """After the attempt's FINAL save: ``sealed.json`` = the sha256 of every learning file as the
    task left them. The merge takes an attempt only with its seal (a VM that died mid-attempt
    leaves none: nothing of it is merged); a later attempt's seal supersedes this one."""
    global _SEALED_ATTEMPT
    if not S3_BUCKET or not task_mode.active():
        return
    files = {rel: _sha(path.read_bytes()) for rel, path in _learning_files(agent_id).items()}
    body = json.dumps({"attempt": attempt, "taskId": task_id, "files": files}, separators=(",", ":"))
    _s3().put_object(Bucket=S3_BUCKET, Key=f"{write_prefix(agent_id)}sealed.json", Body=body.encode("utf-8"))
    _SEALED_ATTEMPT = attempt


#: The attempt this process sealed last; None until `seal_attempt` ran.
_SEALED_ATTEMPT: int | None = None


def reseal_attempt(agent_id: str) -> None:
    """After the SIGTERM save of a task VM stopped BETWEEN turns (the backend's stop after the seal):
    that save may have carried a learning file Hermes touched after the final save, and a sealed
    sha that no longer matches reads to the merge as "a later attempt is writing" (skipped). Seal
    the SAME attempt again. Never when a later attempt has latched since: its unfinished files
    must not be sealed under the earlier attempt's number (it seals itself when it ends, or stays
    unsealed and the next attempt's merge carries the overlay)."""
    task = task_mode.active()
    if task is not None and _SEALED_ATTEMPT is not None and _SEALED_ATTEMPT == task["attempt"]:
        seal_attempt(agent_id, task["taskId"], _SEALED_ATTEMPT)


def put_paperclip_run_record(agent_id: str, run_id: str, record: dict, *, prune: bool = True,
                             runs_dir: str = PAPERCLIP_RUNS_DIR) -> None:
    """Persist a native Paperclip run's terminal result so a poll that lands on a FRESH process
    (a roll's StopRuntimeSession, an idle park, an OOM right after the turn) still gets the real
    outcome instead of a 404 the backend facade reads as ``runtime_lost``. Raises on a failed
    PUT; the caller logs it. Also prunes this agent's records older than a day -- one listing of
    a prefix that only ever holds a day of runs -- unless ``prune`` is off (the accept-time and
    heartbeat writes, which would otherwise list the prefix every 30 s per live run)."""
    if not S3_BUCKET or not _RUN_RECORD_ID.fullmatch(run_id):
        return  # an id the poll route can never address has nothing to answer
    s3, prefix = _s3(), _run_records_prefix(agent_id, runs_dir)
    s3.put_object(Bucket=S3_BUCKET, Key=f"{prefix}{run_id}.json",
                  Body=json.dumps(record, separators=(",", ":")).encode("utf-8"))
    if not prune:
        return
    try:
        cutoff = time.time() - PAPERCLIP_RUN_RECORD_TTL_S
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            stale = [{"Key": obj["Key"]} for obj in page.get("Contents", [])
                     if (_modified_at(obj) or cutoff) < cutoff]
            if stale:
                s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": stale})
    except Exception:  # noqa: BLE001 -- retention is housekeeping; the next write retries it
        log.warning("paperclip run record prune failed for %s", agent_id, exc_info=True)


def get_paperclip_run_record(agent_id: str, run_id: str, *, runs_dir: str = PAPERCLIP_RUNS_DIR) -> dict | None:
    """The record put_paperclip_run_record wrote, or None when there is none. Any other S3
    failure raises: a throttle is "unknown", and answering "not found" would fail a live run."""
    if not S3_BUCKET or not _RUN_RECORD_ID.fullmatch(run_id):
        return None
    try:
        body = _s3().get_object(Bucket=S3_BUCKET, Key=f"{_run_records_prefix(agent_id, runs_dir)}{run_id}.json")["Body"].read()
    except Exception as exc:  # noqa: BLE001
        if _is_absent(exc):
            return None
        raise
    return json.loads(body)


def _aux_defaults() -> dict:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    return DEFAULT_CONFIG["auxiliary"]


#: The only names the backend's CLI-connector lane writes into a profile's ``cli-env.json``
#: (``credentials/hermes_cli._shell_env``). It is an allowlist on a file that lives inside the
#: profile dir, which ``terminal.backend: local`` also lets the AGENT write -- so an unfiltered
#: read would let a turn inject any name it liked into its own secret scope and passthrough.
#: gog is the only member: its file keyring is a password-derived JWE blob and the container's
#: gog must decrypt it with the SAME shared password the backend encrypted it with (on ECS the
#: same two arrive as task-def secrets).
#: ``GOG_ACCOUNT`` is the odd one out -- not a credential but gog's default account, written PER
#: AGENT by the backend's ``google_identity`` seeder (the profile twin of the ECS IDENTITY.md
#: ``gog -a <email>`` instruction).
CLI_ENV_FILE_NAMES: tuple[str, ...] = (
    "GOG_KEYRING_PASSWORD",
    "GOG_KEYRING_BACKEND",
    "GOG_ACCOUNT",
    "VERCEL_TOKEN",
)


def cli_credential_env(agent_id: str) -> dict[str, str]:
    """The CLI-connector VALUES this profile's children need, for the turn's secret scope.

    **Why these are ABSOLUTE paths and not just ``$HOME``.** (History: the freeze below was the
    whole process sharing Hermes's ``"default"`` terminal environment and re-sourcing its first
    snapshot; ``turn._terminal_scope`` now binds a per-session key, so ``$HOME`` is per-profile.
    The explicit paths stay as the channel that never depended on it.) ``TERMINAL_HOME_MODE: profile``
    does put ``$HOME`` at ``<profile>/home`` — for the FIRST profile to run a shell in the
    process, and then it stops moving. Measured in the pinned image (two profiles, one process,
    fresh child PIDs each time, so this is not shell reuse)::

        ceo          -> PID=24 HOME=.../profiles/ceo/home  PAPERCLIP_API_KEY=key-ceo
        engineering  -> PID=33 HOME=.../profiles/ceo/home  PAPERCLIP_API_KEY=key-engineering

    ``HOME`` is in ``agent.secret_scope._GLOBAL_ENV_EXACT``, so every reader resolves it from
    ``os.environ`` rather than the per-profile scope, and the child inherits whatever the first
    turn left there. A passthrough name is not global, so it resolves through the scope and is
    correct for every profile — which the same run proves in the right-hand column.

    An AgentCore runtime is per-OWNER and serves the whole roster in one process, so relying on
    ``$HOME`` would have given every agent after the first the FIRST agent's ``~/.aws``,
    ``~/.config/gh`` and ``~/.xurl``. Hence: one absolute path per CLI, carried on the channel
    that demonstrably re-resolves per turn.

    ``TERMINAL_HOME_MODE`` stays set — an unset ``$HOME`` is ``/``, which is root-owned, so
    ``git config --global`` and friends fail outright — but nothing here DEPENDS on it.

    ``HOME`` itself is never returned here and must never be added: it is ``_is_global_env``
    (so ``resolve_passthrough_value`` hands back the process value) AND owned by
    ``hermes_constants.apply_subprocess_home_env``.

    ``AWS_PROFILE`` is absent because the projection writes ``[default]`` on this rail
    (materializers/aws_cli.py) — one name that genuinely did become unnecessary.
    """
    home = _profile_dir(agent_id) / PROFILE_HOME
    env = {
        "AWS_CONFIG_FILE": str(home / ".aws" / "config"),
        # Pinned at a file the projection never writes, so `aws` can only resolve through the
        # projected config's credential_process, never a stray shared-credentials file.
        "AWS_SHARED_CREDENTIALS_FILE": str(home / ".aws" / "credentials"),
        "GH_CONFIG_DIR": str(home / ".config" / "gh"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    }
    try:
        raw = json.loads((_profile_dir(agent_id) / CLI_ENV_FILE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- absent (no gog connected) or unreadable: not a turn failure
        return env
    env.update({str(k): str(v) for k, v in raw.items() if k in CLI_ENV_FILE_NAMES})
    return env


LIGHTBULB_PLUGINS = ("lightbulb",)


def plugins_pin(existing: dict) -> dict:
    """Opt the Lightbulb directory plugins in. Hermes loads a non-backend plugin only when
    plugins.enabled names it (hermes_cli/plugins_discovery.py gate_manifest); bundled
    backends/platforms auto-load, so this allow-list touches nothing else. deep_merge
    replaces lists wholesale, so the union is computed from the existing config here."""
    current = (existing.get("plugins") or {}).get("enabled") or []
    enabled = list(dict.fromkeys([*current, *LIGHTBULB_PLUGINS]))
    return {"plugins": {"enabled": enabled}}


def normalize_profile_backends(agent_id: str) -> dict:
    """Normalize legacy terminal config and enforce the managed browser backend on disk.

    Hermes's Browser Use selector reads ``config.yaml`` through ``read_raw_config`` instead of
    the managed-overlay-aware effective loader. Persisting ``browser.backend: off`` here keeps
    that raw reader aligned with the platform policy before Hermes builds its tool catalog.
    Other browser keys and all unrelated profile config remain unchanged.

    Pulled out of ``pin_profile_defaults`` (fix round, first-turn-after-an-image-roll bug) so a
    caller can run JUST this normalization -- no ``hermes_cli`` import, unlike the rest of
    ``pin_profile_defaults`` -- before anything installs a scope that snapshots config.yaml at
    INSTALL time. ``bridge.turn._terminal_scope`` is exactly that: its
    ``tools.terminal_scope.install_profile_terminal_scope`` call reads config.yaml once, when
    installed, and used to run BEFORE ``_construct_agent`` (and its ``pin_profile_defaults``
    call) had a chance to normalize a legacy value -- so the FIRST turn after an image roll
    installed a stale ``lightbulb_node`` scope and ``_native_image_part`` raised
    ``_ImagePrepError`` on that turn only (the second turn worked because the file was already
    rewritten by then). Calling this here, ahead of the scope, closes that window; the
    ``pin_profile_defaults`` call inside ``_construct_agent`` still runs afterwards and repeats
    the same (by-then-already-true) check, which is why this is safe to call twice.

    #2749 fix-forward: freshly written profiles now default to ``terminal.backend: local`` and
    the bridge honours whatever backend a profile's config.yaml names. A profile written before
    that PR can still carry the legacy ``lightbulb_node`` value -- Node/Mac execution is
    deferred and no product surface can set it any more, so left alone it would route every
    shell command at a Mac node that isn't attached.
    """
    existing = read_profile_config(agent_id)
    changed = False
    terminal = existing.get("terminal")
    if isinstance(terminal, dict) and terminal.get("backend") == "lightbulb_node":
        existing = {**existing, "terminal": {**terminal, "backend": "local"}}
        changed = True
    # allow_private_urls: the browser is the workspace's own AgentCore session behind the bridge's
    # loopback CDP override, which Hermes treats as off-host -- it would refuse snapshots and raw
    # CDP on about:, chrome://, chrome-extension:// and file:// pages. Agents control everything
    # in this browser (owner ruling 2026-09-27); Hermes's cloud-metadata floor stays regardless.
    browser = existing.get("browser")
    managed = {"backend": "off", "allow_private_urls": True}
    if not isinstance(browser, dict) or any(browser.get(k) != v for k, v in managed.items()):
        existing = {**existing, "browser": {**(browser if isinstance(browser, dict) else {}), **managed}}
        changed = True
    # AgentCore workspace memory (plugins/memory/agentcore) follows the runtime env: on while
    # AGENTCORE_MEMORY_ID is set and the profile is on the built-in provider, back to built-in
    # when it is gone. Any other provider is someone's explicit choice and is never touched.
    memory = existing.get("memory") if isinstance(existing.get("memory"), dict) else {}
    provider = memory.get("provider") or ""
    wanted = "agentcore" if os.environ.get("AGENTCORE_MEMORY_ID") else ""
    if provider in ("", "agentcore") and provider != wanted:
        existing = {**existing, "memory": {**memory, "provider": wanted}}
        changed = True
    if changed:
        write_profile_config(agent_id, existing)
    return existing


def is_managed_route(config: dict) -> bool:
    """Is this profile on the managed OpenRouter gate? Decided from the profile the bridge just
    pulled, never a boot-time provider choice: a warm process outlives a provider switch, and the
    backend's profile rewrite is what reaches it on the next turn. The gate-bearer reference
    counts too, so an edited ``base_url`` (or a runtime spec with no gate URL) still validates --
    and fails closed -- instead of passing as an ordinary custom route."""
    block = config.get("model") or {}
    gate_url = os.environ.get("LIGHTBULB_MANAGED_GATE_URL", "")
    return block.get("provider") == "custom" and (
        bool(gate_url) and block.get("base_url") == gate_url
        or block.get("api_key") == "${LIGHTBULB_GATE_BEARER}")


def _vision_unset(route) -> bool:
    """``auxiliary.vision`` absent, or Hermes's unset default (``hermes_profiles.AUX_VISION_UNSET``).

    Owner ruling: managed profiles leave vision unset so Hermes routes it natively. Hermes 0.21.3
    ``auxiliary_client._vision_auto_route`` then tries the main provider (the gate; the gate sends
    image requests to the policy's ``vision_model``), and only if that is refused falls through to
    OpenRouter / Nous / DeepInfra -- each needs a credential a managed profile does not hold
    (runtime env is identifiers only, the owner's OpenRouter key lives in the gate, and the managed
    overlay pins ``nous.guest: false``), so the fallthrough ends in "no vision client", not off-gate.
    Any explicit provider, model, base_url, api_key or fallback chain is NOT unset and must be the
    gate route like every other task."""
    if route is None:
        return True
    return (isinstance(route, dict) and (route.get("provider") or "auto") == "auto"
            and not any(route.get(k) for k in ("model", "base_url", "api_key", "fallback_chain")))


def pin_profile_defaults(agent_id: str) -> dict:
    """Validate a managed profile's gate routes, or leave a non-managed profile's aux to Hermes.

    M4's config writer (``hermes_profiles.build_profile_config``) is the source of truth for a
    profile's aux pins; the bridge no longer fills any in.

    ``plugins.enabled`` stays a UNION regardless of merge direction: ``plugins_pin`` already
    computes it from whatever ``existing`` holds.

    Legacy terminal and managed browser backends are normalized before ``existing``
    is read by anything else -- see ``normalize_profile_backends``'s own docstring for
    why that normalization is ALSO its own standalone function, called separately (and earlier)
    by ``bridge.turn._run_hermes``/``_run_native_paperclip``.
    """
    # Under the profile lock (reentrant): an unlocked read-modify-write here could write a stale
    # config.yaml back over a hot patch another conversation of this agent had just pulled.
    with profile_lock(agent_id):
        existing = normalize_profile_backends(agent_id)
        if is_managed_route(existing):
            tasks = (existing.get("auxiliary") or {})
            gate_url = os.environ.get("LIGHTBULB_MANAGED_GATE_URL", "")
            approved_model = os.environ.get("LIGHTBULB_MANAGED_MODEL_ID", "")
            model_block = existing["model"]
            if not gate_url or not approved_model or model_block.get("default") != approved_model or model_block.get("base_url") != gate_url or model_block.get("api_key") != "${LIGHTBULB_GATE_BEARER}":
                raise RuntimeError("managed gate route is missing or changed")
            for name, block in _aux_defaults().items():
                if isinstance(block, dict) and "provider" in block:
                    route = tasks.get(name)
                    if name == "vision" and _vision_unset(route):
                        continue  # Hermes's own auto route: the main provider, i.e. this gate
                    if not isinstance(route, dict) or not route.get("provider") or not route.get("model") or route.get("fallback_chain") != []:
                        raise RuntimeError(f"missing auxiliary route for {name}")
                    if (route.get("provider") != "custom" or route.get("base_url") != gate_url
                            or route.get("model") != approved_model
                            or route.get("api_key") != "${LIGHTBULB_GATE_BEARER}"):
                        raise RuntimeError(f"auxiliary route for {name} bypasses managed gate")
        # A non-managed profile's aux follows the owner's own provider -- Hermes's default.
        config = deep_merge(existing, plugins_pin(existing))
        if config != existing:  # every turn used to rewrite it, racing a hot patch's re-pull
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
        "application/pdf": ".pdf", "application/zip": ".zip", "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
        # turn.extract_media types a returned path by its suffix, so every type it accepts
        # needs one here, or the agent's own received file "couldn't attach".
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/csv": ".csv", "text/plain": ".txt", "text/markdown": ".md", "text/html": ".html"}


def _get_backend_bytes(path: str) -> bytes:
    import bridge
    import urllib.request

    req = urllib.request.Request(f"{bridge.API}/api/v1/internal/messaging/{path}",
                                 headers={"Authorization": f"Bearer {bridge.HOOK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


# Every turn fetches into its OWN folder under here, removed when the turn ends
# (turn._run_hermes). A shared per-room cache outlived its turn and sat readable by every
# agent's next turn in ANY room -- a DM's attachment one `ls` away from a channel turn, which
# is exactly what the room's deliveryKey rotation promises cannot happen.
# ponytail: a turn re-fetches its history's attachments every time (the shared cache that
# avoided that was the leak); add a per-room, per-turn-lifetime memo only if fetch latency shows up.
def turn_media_root() -> Path:
    return HOME / "media" / "messaging"


def attachment_path(base: Path, attachment: dict) -> Path:
    return base / f"{attachment['id']}{_EXT.get(attachment.get('contentType'), '')}"


def materialize_attachments(body: dict, base: Path) -> list[str]:
    """Download the attachments a turn can see into that turn's own folder. One
    attachment's fetch failing (network blip, deleted object, ...) must not fail the whole
    turn -- it is skipped and logged, not raised."""
    import bridge

    room_id = (body.get("room") or {}).get("id")
    out: list[str] = []
    for m in [body.get("message") or {}] + list(body.get("recentHistory", [])):
        for att in m.get("attachments") or []:
            target = attachment_path(base, att)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if str(att.get("contentType", "")).startswith("video/"):
                        # Up to 100 MB: past API Gateway's 10 MB response, so straight from S3
                        # (the attachment key's suffix is this same _EXT; the role grants <owner>/*).
                        _s3().download_file(S3_BUCKET, f"{S3_PREFIX}/attachments/{room_id}/{target.name}", str(target))
                    else:
                        target.write_bytes(_get_backend_bytes(f"attachments/{bridge.OWNER}/{room_id}/{att['id']}"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("attachment fetch failed room=%s attachment=%s: %s", room_id, att.get("id"), exc)
                    continue
            out.append(str(target))
    return out
