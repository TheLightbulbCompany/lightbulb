"""Cross-process serialization for one owner's credential lifecycle.

The critical section is the OAuth connect / disconnect pair in ``routers/connections.py``: a
Secrets-Manager write, the integrations row, and the Hermes profile fan-out. Two of those
interleaving for one owner is how a disconnect's teardown gets undone by a callback still
finishing its writes.

This used to be an EFS file lock on the backend's shared mount. That mount is gone with the ECS
runtime, and a filesystem lock without it succeeds independently in each of the 2-4 backend
tasks -- i.e. fences nothing, silently. The lease is a conditional DynamoDB write instead
(``container_repo.try_claim_owner_lifecycle``), which is the only thing left that all the tasks
can see. The in-process lock below is kept in front of it: same-process callers wait on a mutex
instead of both polling DynamoDB.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import AsyncIterator, Awaitable, Callable, ParamSpec, TypeVar, cast

from core.repositories import container_repo

#: How long a caller waits for the lease before giving up. Same budget the EFS sidecar lock
#: used, and the callers map it to a retryable 409 rather than a 500.
OWNER_LIFECYCLE_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.25

_held_owner: ContextVar[tuple[asyncio.Task[object], str] | None] = ContextVar(
    "held_owner_lifecycle",
    default=None,
)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

P = ParamSpec("P")
R = TypeVar("R")


class OwnerLifecycleBusyError(TimeoutError):
    """Raised when another caller holds this owner's lifecycle lease."""


def _local_lock(owner_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(owner_id, threading.Lock())


async def _acquire_lease(owner_id: str) -> str:
    """Poll for the owner's lease until ``OWNER_LIFECYCLE_TIMEOUT_S``, then give up.

    Polling rather than waiting on a signal because there is nothing to signal on: the holder
    may be in another task. The interval is coarse on purpose -- the section it fences takes
    seconds, so a tighter poll only buys DynamoDB writes.
    """
    deadline = asyncio.get_running_loop().time() + OWNER_LIFECYCLE_TIMEOUT_S
    while True:
        token = await container_repo.try_claim_owner_lifecycle(owner_id)
        if token:
            return token
        if asyncio.get_running_loop().time() >= deadline:
            raise OwnerLifecycleBusyError(f"timed out waiting for the lifecycle lease: {owner_id}")
        await asyncio.sleep(_POLL_INTERVAL_S)


@asynccontextmanager
async def owner_lifecycle_guard(owner_id: str) -> AsyncIterator[None]:
    """Serialize owner-scoped credential mutations across backend tasks.

    Calls nested by the same asyncio task may re-enter for the same owner. A child task does
    not inherit that ownership, even though it inherits the surrounding ContextVar value, so
    background work still waits for its parent's operation to finish.

    Raises ``OwnerLifecycleBusyError`` when the lease does not free up in
    ``OWNER_LIFECYCLE_TIMEOUT_S`` -- nothing has been written at that point, so the caller can
    answer "busy" and let the owner retry.
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

    local = _local_lock(owner_id)
    # Shielded so a cancellation between "acquired" and "we hold it" cannot leak the mutex.
    acquiring = asyncio.create_task(asyncio.to_thread(local.acquire))
    try:
        await asyncio.shield(acquiring)
    except asyncio.CancelledError:
        await acquiring
        local.release()
        raise

    lease: str | None = None
    token = None
    try:
        lease = await _acquire_lease(owner_id)
        token = _held_owner.set((task, owner_id))
        yield
    finally:
        if token is not None:
            _held_owner.reset(token)
        try:
            if lease is not None:
                # Shielded: the release is the only thing standing between a cancelled caller
                # and a lease nobody drops for LIFECYCLE_CLAIM_STALE_S.
                await asyncio.shield(
                    asyncio.create_task(container_repo.release_owner_lifecycle_claim(owner_id, token=lease))
                )
        finally:
            local.release()


def serialize_owner_lifecycle(method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Decorate an async method whose first argument after ``self`` is an owner id."""

    owner_parameter = tuple(inspect.signature(method).parameters)[1]

    @wraps(method)
    async def serialized(*args: P.args, **kwargs: P.kwargs) -> R:
        owner_id = cast(str, args[1] if len(args) > 1 else kwargs.get(owner_parameter))
        async with owner_lifecycle_guard(owner_id):
            return await method(*args, **kwargs)

    return serialized
