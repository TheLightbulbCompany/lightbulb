"""Materializers write a CLI's own credential file (0600, owned by uid/gid 1000,
atomic) so the CLI reads it at runtime.

Nothing writes to a live filesystem the runtime shares any more: the ONE dispatch is
``credentials.hermes_cli.project``, which points ``materializer_root`` at a throwaway
directory, runs the materializer verbatim, and uploads whatever landed into every
Hermes profile in S3. A materializer reached outside that context has nowhere to write
and says so — ``materializer_root`` raises rather than inventing a path.

The shape every materializer module implements is
``write(owner_id, provider_key, credential)`` plus a mirroring ``remove(...)``,
registered by name in ``materializers/__init__.py``. That contract is enforced
by the registry's tests, not by a type: this repo runs ruff and pytest in CI
and no type checker, so an annotation here cannot fail a build.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import tempfile
from collections.abc import Coroutine, Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

NODE_UID = 1000
NODE_GID = 1000

#: Where the materializers in this context write. A ContextVar rather than a settings/env swap
#: because the backend serves many owners concurrently: the root must be scoped to the one call
#: that set it.
_PROJECTION_ROOT: ContextVar[Path | None] = ContextVar("materializer_projection_root", default=None)


@contextlib.contextmanager
def materialize_into(root: Path) -> Iterator[None]:
    """Point every materializer in this context at ``root``."""
    token = _PROJECTION_ROOT.set(root)
    try:
        yield
    finally:
        _PROJECTION_ROOT.reset(token)


def materializer_root() -> Path:
    """The root the current materializer writes into — the scratch tree ``materialize_into``
    set, which stands for exactly one owner's home.

    Raises outside a projection: there is no fallback path. A materializer dispatched without
    one would write a credential nothing ever reads, which is worse than a 500 on the connect.
    """
    root = _PROJECTION_ROOT.get()
    if root is None:
        raise RuntimeError("materializers run only inside materialize_into() — see credentials/hermes_cli.py")
    return root


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        if os.geteuid() == 0:  # only in-container (root); tests run non-root and skip chown
            os.chown(tmp, NODE_UID, NODE_GID)
        os.replace(tmp, path)
        if os.geteuid() == 0:
            os.chown(path.parent, NODE_UID, NODE_GID)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive an async coroutine to completion from a synchronous materializer.

    The apply/disconnect paths offload materializers via run_in_thread, so the
    common case is a worker thread with no running loop and a bare asyncio.run
    suffices. But asyncio.run raises if a loop is already running, so guard
    against a caller that forgot the offload: when a loop IS running, run the
    coroutine on a fresh loop in a throwaway worker thread instead. This keeps
    materializers correct whether or not the offload is present."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
