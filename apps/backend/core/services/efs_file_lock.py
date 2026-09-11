"""Shared EFS/NFS read-modify-write locking primitives.

A correct RMW on an EFS file that is replaced via ``os.rename`` needs TWO
layers, and getting either wrong silently drops concurrent writers:

1. A per-key in-process ``threading.Lock``. POSIX ``fcntl.lockf`` is
   *process*-scoped on Linux/macOS, so two threads in the SAME backend worker
   (e.g. two ``asyncio.to_thread`` callers) both "acquire" the file lock and
   race the read-modify-write. This gate serializes them before they ever
   reach the file lock.

2. A cross-process lock on a SEPARATE sidecar — never the data file — because
   the write swaps the data file's inode via ``os.rename``. Generic backend-
   only files use a persistent ``fcntl.lockf`` sidecar. ``openclaw.json`` uses
   ``openclaw_config_sidecar_lock`` and a persistent protocol marker: before
   boot recovery it preserves the legacy ``fcntl`` protocol, and after the
   one-time transition it participates in pinned OpenClaw's ephemeral
   ``O_EXCL`` sidecar protocol.

``config_patcher`` (openclaw.json, cron/jobs.json) and ``desktop_pair``
(devices/nodes paired.json) go through these helpers so each protocol lives in
one place. Key on the absolute ``.lock`` path so each file gets its own
in-process lock (different owners and different files never contend).
"""

import fcntl
import os
import stat
import threading
import time
from contextlib import contextmanager
from typing import Iterator


OPENCLAW_CONFIG_GUARD_SUFFIX = ".isol8-guard"
OPENCLAW_CONFIG_PROTOCOL_SUFFIX = ".isol8-protocol"
OPENCLAW_CONFIG_PROTOCOL_MIGRATING = b"migrating-v1\n"
OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL = b"ephemeral-v1\n"
OPENCLAW_CONFIG_LOCK_TIMEOUT_S = 30.0

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def get_file_lock(key: str) -> threading.Lock:
    """Return (and lazily create) the in-process lock for ``key``.

    The guard mutex is contended only on first-time lookup; steady-state
    callers acquire the inner per-key lock directly. Key on the absolute
    ``.lock`` path.
    """
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def chown_uid_1000_best_effort(path: str) -> None:
    """Match OpenClaw container ownership (node uid=1000 gid=1000).

    Only chowns when running as root (the backend container runs as root;
    tests run as the local user where ``chown`` to another uid is denied).
    """
    try:
        if os.getuid() == 0:
            os.chown(path, 1000, 1000)
    except OSError:
        pass


def chown_fd_uid_1000_best_effort(fd: int) -> None:
    """Match access-point ownership without following a replaceable pathname."""
    try:
        if os.getuid() == 0:
            os.fchown(fd, 1000, 1000)
    except OSError:
        pass


def open_lock_fd(lock_dir: str, lock_path: str):
    """Open (creating if needed) a dedicated ``.lock`` file in ``r+`` mode.

    NEVER lock the data file itself — the RMW swaps its inode via
    ``os.rename``, so a lock on the old inode wouldn't fence a racing rename.
    The caller ``fcntl.lockf``-locks the returned fd and closes it when done.
    """
    os.makedirs(lock_dir, exist_ok=True)
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
    dir_fd = os.open(lock_dir, dir_flags)
    try:
        if not _path_names_open_fd(lock_dir, dir_fd):
            raise SidecarLockIdentityError(f"lock directory changed identity: {lock_dir}")
        chown_fd_uid_1000_best_effort(dir_fd)
        if os.path.dirname(os.path.abspath(lock_path)) != os.path.abspath(lock_dir):
            raise SidecarLockIdentityError(f"lock path is outside lock directory: {lock_path}")
        lock_name = os.path.basename(lock_path)
        fd = _open_regular_fd(lock_name, create=True, dir_fd=dir_fd)
        try:
            if not _path_names_open_fd(lock_name, fd, dir_fd=dir_fd):
                raise SidecarLockIdentityError(f"lock path changed identity: {lock_path}")
            chown_fd_uid_1000_best_effort(fd)
            return os.fdopen(fd, "r+")
        except Exception:
            os.close(fd)
            raise
    finally:
        os.close(dir_fd)


class SidecarLockTimeoutError(TimeoutError):
    """Raised when another OpenClaw-compatible sidecar owner does not release."""


class SidecarLockIdentityError(RuntimeError):
    """Raised when a held sidecar pathname no longer names the acquired inode."""


def _open_regular_fd(path: str, *, create: bool, dir_fd: int | None = None) -> int:
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600, dir_fd=dir_fd)
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        os.close(fd)
        raise SidecarLockIdentityError(f"lock path is not a regular file: {path}")
    return fd


def _path_names_open_fd(path: str, fd: int, *, dir_fd: int | None = None) -> bool:
    try:
        current = os.lstat(path, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    opened = os.fstat(fd)
    return (
        not stat.S_ISLNK(current.st_mode)
        and stat.S_IFMT(current.st_mode) == stat.S_IFMT(opened.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def _acquire_fcntl_until(
    fd: int,
    *,
    deadline: float,
    poll_interval_s: float,
    timeout_message: str,
) -> None:
    while True:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SidecarLockTimeoutError(timeout_message)
            time.sleep(poll_interval_s)


def _uses_ephemeral_openclaw_protocol(lock_path: str) -> bool:
    """Return whether boot recovery has started the one-time transition.

    ``migrating-v1`` deliberately selects the new protocol too. If recovery
    is interrupted after publishing that state but before removing the legacy
    sidecar, backend writers fail closed on the still-existing sidecar until
    the next boot resumes recovery; they never recreate or wait on its inode.
    """

    protocol_path = f"{lock_path}{OPENCLAW_CONFIG_PROTOCOL_SUFFIX}"
    try:
        protocol_fd = _open_regular_fd(protocol_path, create=False)
    except FileNotFoundError:
        return False
    try:
        if not _path_names_open_fd(protocol_path, protocol_fd):
            raise SidecarLockIdentityError(f"OpenClaw config protocol marker changed identity: {protocol_path}")
        value = os.read(protocol_fd, 64)
    finally:
        os.close(protocol_fd)

    if value not in (OPENCLAW_CONFIG_PROTOCOL_MIGRATING, OPENCLAW_CONFIG_PROTOCOL_EPHEMERAL):
        raise SidecarLockIdentityError(f"invalid OpenClaw config protocol marker: {protocol_path}")
    return True


@contextmanager
def _legacy_openclaw_config_lock(
    lock_dir: str,
    lock_path: str,
    *,
    deadline: float,
    poll_interval_s: float,
) -> Iterator[None]:
    """Preserve the pre-recovery backend protocol during rollout phase one."""

    sidecar_fd = _open_regular_fd(lock_path, create=True)
    try:
        if not _path_names_open_fd(lock_path, sidecar_fd):
            raise SidecarLockIdentityError(f"OpenClaw config sidecar changed identity: {lock_path}")
        chown_fd_uid_1000_best_effort(sidecar_fd)
        _acquire_fcntl_until(
            sidecar_fd,
            deadline=deadline,
            poll_interval_s=poll_interval_s,
            timeout_message=f"timed out waiting for legacy OpenClaw config sidecar: {lock_path}",
        )
        yield
    finally:
        try:
            fcntl.lockf(sidecar_fd, fcntl.LOCK_UN)
        finally:
            os.close(sidecar_fd)


@contextmanager
def _ephemeral_openclaw_config_lock(
    lock_path: str,
    *,
    deadline: float,
    poll_interval_s: float,
) -> Iterator[None]:
    """Acquire pinned OpenClaw's ephemeral ``O_EXCL`` sidecar."""

    sidecar_fd: int | None = None
    try:
        create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            create_flags |= os.O_NOFOLLOW
        while True:
            try:
                sidecar_fd = os.open(lock_path, create_flags, 0o600)
                break
            except FileExistsError:
                try:
                    observed = os.lstat(lock_path)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
                    raise SidecarLockIdentityError(f"lock path is not a regular file: {lock_path}")
                if time.monotonic() >= deadline:
                    raise SidecarLockTimeoutError(f"timed out waiting for OpenClaw config sidecar: {lock_path}")
                time.sleep(poll_interval_s)

        chown_fd_uid_1000_best_effort(sidecar_fd)
        yield
    finally:
        if sidecar_fd is not None:
            if not _path_names_open_fd(lock_path, sidecar_fd):
                os.close(sidecar_fd)
                raise SidecarLockIdentityError(f"held OpenClaw config sidecar changed identity: {lock_path}")
            os.unlink(lock_path)
            os.close(sidecar_fd)


@contextmanager
def openclaw_config_recovery_guard(
    lock_dir: str,
    lock_path: str,
    *,
    timeout_s: float = OPENCLAW_CONFIG_LOCK_TIMEOUT_S,
    poll_interval_s: float = 0.05,
) -> Iterator[None]:
    """Exclude backend config writers from boot-time sidecar recovery."""

    os.makedirs(lock_dir, exist_ok=True)
    chown_uid_1000_best_effort(lock_dir)
    guard_path = f"{lock_path}{OPENCLAW_CONFIG_GUARD_SUFFIX}"
    guard_fd = _open_regular_fd(guard_path, create=True)
    acquired = False
    try:
        if not _path_names_open_fd(guard_path, guard_fd):
            raise SidecarLockIdentityError(f"OpenClaw config guard changed identity: {guard_path}")
        chown_fd_uid_1000_best_effort(guard_fd)
        deadline = time.monotonic() + max(0.0, timeout_s)
        _acquire_fcntl_until(
            guard_fd,
            deadline=deadline,
            poll_interval_s=poll_interval_s,
            timeout_message=f"timed out waiting for OpenClaw config guard: {guard_path}",
        )
        acquired = True
        if not _path_names_open_fd(guard_path, guard_fd):
            raise SidecarLockIdentityError(f"OpenClaw config guard changed identity: {guard_path}")
        yield
        if not _path_names_open_fd(guard_path, guard_fd):
            raise SidecarLockIdentityError(f"held OpenClaw config guard changed identity: {guard_path}")
    finally:
        try:
            if acquired:
                fcntl.lockf(guard_fd, fcntl.LOCK_UN)
        finally:
            os.close(guard_fd)


@contextmanager
def _openclaw_config_sidecar_under_guard(
    lock_dir: str, lock_path: str, *, deadline: float, poll_interval_s: float
) -> Iterator[None]:
    """Acquire only the sidecar; caller holds the matching local lock and recovery guard."""
    if _uses_ephemeral_openclaw_protocol(lock_path):
        with _ephemeral_openclaw_config_lock(lock_path, deadline=deadline, poll_interval_s=poll_interval_s):
            yield
    else:
        with _legacy_openclaw_config_lock(lock_dir, lock_path, deadline=deadline, poll_interval_s=poll_interval_s):
            yield


@contextmanager
def openclaw_config_sidecar_lock(
    lock_dir: str,
    lock_path: str,
    *,
    timeout_s: float = OPENCLAW_CONFIG_LOCK_TIMEOUT_S,
    poll_interval_s: float = 0.05,
) -> Iterator[None]:
    """Acquire the rollout-safe protocol for the OpenClaw config sidecar.

    Pinned OpenClaw serializes ``openclaw.json`` mutations by atomically
    creating ``openclaw.json.lock`` and unlinking it on release.  A permanent
    backend ``fcntl`` lock at that pathname is not interoperable: OpenClaw
    sees the empty file as an ambiguous owner and correctly fails closed.

    Phase one adds the persistent recovery guard while retaining the legacy
    ``fcntl`` behavior, so it can reach steady state without making existing
    permanent sidecars unavailable. Boot recovery later publishes a protocol
    marker under that guard and removes the legacy inode. Once either the
    migrating or final marker exists, writers use the same existence protocol
    as OpenClaw. The empty payload is intentional: an OpenClaw process must
    retain it rather than treating a backend PID from another PID namespace as
    dead.
    """

    deadline = time.monotonic() + max(0.0, timeout_s)
    with openclaw_config_recovery_guard(
        lock_dir,
        lock_path,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    ):
        with _openclaw_config_sidecar_under_guard(
            lock_dir, lock_path, deadline=deadline, poll_interval_s=poll_interval_s
        ):
            yield


if __name__ == "__main__":  # pragma: no cover - self-check
    # Concurrent same-process writers must serialize on the in-process lock,
    # and the fcntl lock must survive an inode swap of the DATA file.
    import json
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    d = tempfile.mkdtemp()
    data_path = os.path.join(d, "data.json")
    lock_path = os.path.join(d, "data.json.lock")
    with open(data_path, "w") as f:
        json.dump({"n": 0}, f)

    def bump():
        with get_file_lock(lock_path):
            fd = open_lock_fd(d, lock_path)
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX)
                with open(data_path) as f:
                    cur = json.load(f)
                cur["n"] += 1
                tmp = data_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(cur, f)
                os.rename(tmp, data_path)  # inode swap
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN)
                fd.close()

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: bump(), range(200)))

    with open(data_path) as f:
        final = json.load(f)["n"]
    assert final == 200, f"lost writes: {final} != 200"
    print("ok:", final)
