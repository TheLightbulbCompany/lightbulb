"""Cross-process serialization for one owner's container lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, ContextManager, ParamSpec, TypeVar, cast

from core.config import settings
from core.services.efs_file_lock import get_file_lock, openclaw_config_recovery_guard

_LOCK_DIR = ".lightbulb-owner-lifecycle-locks"
_held_owner: ContextVar[tuple[asyncio.Task[object], str] | None] = ContextVar(
    "held_owner_lifecycle",
    default=None,
)

P = ParamSpec("P")
R = TypeVar("R")


def _paths(owner_id: str) -> tuple[str, str]:
    if type(owner_id) is not str or not owner_id:
        raise ValueError("owner_id is required")
    lock_dir = str(Path(settings.EFS_MOUNT_PATH) / _LOCK_DIR)
    token = hashlib.sha256(owner_id.encode()).hexdigest()[:32]
    return lock_dir, str(Path(lock_dir) / f"{token}.lock")


async def _enter_guard(guard: ContextManager[None]) -> None:
    """Enter a blocking file guard without leaking it on task cancellation."""
    entering = asyncio.create_task(asyncio.to_thread(guard.__enter__))
    try:
        await asyncio.shield(entering)
    except asyncio.CancelledError:
        try:
            await entering
        except Exception:
            pass
        else:
            await asyncio.to_thread(guard.__exit__, None, None, None)
        raise


async def _exit_guard(guard: ContextManager[None]) -> None:
    """Finish releasing the file guard even if cancellation arrives again."""
    leaving = asyncio.create_task(asyncio.to_thread(guard.__exit__, None, None, None))
    try:
        await asyncio.shield(leaving)
    except asyncio.CancelledError:
        await leaving
        raise


@asynccontextmanager
async def owner_lifecycle_guard(owner_id: str) -> AsyncIterator[None]:
    """Serialize owner-scoped ECS and credential mutations across workers.

    Calls nested by the same asyncio task may re-enter for the same owner.
    A child task does not inherit that ownership, even though it inherits the
    surrounding ContextVar value, so background provision work still waits for
    its parent's lifecycle operation to finish.
    """

    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("owner lifecycle guard requires an asyncio task")
    held = _held_owner.get()
    if held is not None and held[0] is task:
        if held[1] != owner_id:
            raise RuntimeError("cannot nest lifecycle locks for different owners")
        yield
        return

    lock_dir, lock_path = _paths(owner_id)
    local_lock = get_file_lock(lock_path)
    acquiring = asyncio.create_task(asyncio.to_thread(local_lock.acquire))
    try:
        await asyncio.shield(acquiring)
    except asyncio.CancelledError:
        await acquiring
        local_lock.release()
        raise

    guard = openclaw_config_recovery_guard(lock_dir, lock_path)
    entered = False
    token = None
    try:
        await _enter_guard(guard)
        entered = True
        token = _held_owner.set((task, owner_id))
        yield
    finally:
        if token is not None:
            _held_owner.reset(token)
        try:
            if entered:
                await _exit_guard(guard)
        finally:
            local_lock.release()


def serialize_owner_lifecycle(method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Decorate an async method whose first argument after ``self`` is an owner id."""

    owner_parameter = tuple(inspect.signature(method).parameters)[1]

    @wraps(method)
    async def serialized(*args: P.args, **kwargs: P.kwargs) -> R:
        owner_id = cast(str, args[1] if len(args) > 1 else kwargs.get(owner_parameter))
        async with owner_lifecycle_guard(owner_id):
            return await method(*args, **kwargs)

    return serialized
