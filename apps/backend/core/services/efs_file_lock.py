"""Shared EFS/NFS read-modify-write locking primitives.

A correct RMW on an EFS file that is replaced via ``os.rename`` needs TWO
layers, and getting either wrong silently drops concurrent writers:

1. A per-key in-process ``threading.Lock``. POSIX ``fcntl.lockf`` is
   *process*-scoped on Linux/macOS, so two threads in the SAME backend worker
   (e.g. two ``asyncio.to_thread`` callers) both "acquire" the file lock and
   race the read-modify-write. This gate serializes them before they ever
   reach the file lock.

2. A cross-process ``fcntl.lockf`` on a SEPARATE ``.lock`` file — never the
   data file — because the write swaps the data file's inode via
   ``os.rename``. A lock held on the old inode does not fence a new writer
   racing the rename.

Both ``config_patcher`` (openclaw.json, cron/jobs.json) and ``desktop_pair``
(devices/nodes paired.json) go through these helpers so the pattern lives in
exactly one place. Key on the absolute ``.lock`` path so each file gets its
own in-process lock (different owners and different files never contend).
"""

import fcntl
import os
import threading

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


def open_lock_fd(lock_dir: str, lock_path: str):
    """Open (creating if needed) a dedicated ``.lock`` file in ``r+`` mode.

    NEVER lock the data file itself — the RMW swaps its inode via
    ``os.rename``, so a lock on the old inode wouldn't fence a racing rename.
    The caller ``fcntl.lockf``-locks the returned fd and closes it when done.
    """
    if not os.path.exists(lock_path):
        os.makedirs(lock_dir, exist_ok=True)
        chown_uid_1000_best_effort(lock_dir)
        # ``open(..., "a")`` creates the file if missing without truncating a
        # concurrent lock holder's fd.
        with open(lock_path, "a"):
            pass
        chown_uid_1000_best_effort(lock_path)
    return open(lock_path, "r+")


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
