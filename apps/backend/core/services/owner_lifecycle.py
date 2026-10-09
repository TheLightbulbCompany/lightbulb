"""Cross-process serialization for one owner's credential lifecycle.

The critical section is the OAuth connect / disconnect pair in ``routers/connections.py``: a
Secrets-Manager write, the integrations row, and the Hermes profile fan-out. Two of those
interleaving for one owner is how a disconnect's teardown gets undone by a callback still
finishing its writes.

This used to be an EFS file lock on the backend's shared mount. That mount is gone with the ECS
runtime, and a filesystem lock without it succeeds independently in each of the 2-4 backend
tasks -- i.e. fences nothing, silently. The lease is a conditional DynamoDB write instead
(``container_repo.try_claim_owner_lifecycle``), which is the only thing left that all the tasks
can see -- and which serializes same-process callers too, so there is no in-process mutex in
front of it. A second caller on this task pays one ``_POLL_INTERVAL_S`` handoff rather than
waking the instant the first releases; the alternative was a per-owner ``threading.Lock`` dict
that nothing ever evicted.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import AsyncIterator, Awaitable, Callable, ParamSpec, TypeVar, cast

from core.repositories import container_repo, oauth_state_repo

#: How long a caller waits for the lease before giving up. Same budget the EFS sidecar lock
#: used, and the callers map it to a retryable 409 rather than a 500.
OWNER_LIFECYCLE_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.25

_held_owner: ContextVar[tuple[asyncio.Task[object], str] | None] = ContextVar(
    "held_owner_lifecycle",
    default=None,
)

P = ParamSpec("P")
R = TypeVar("R")


class OwnerLifecycleBusyError(TimeoutError):
    """Raised when another caller holds this owner's lifecycle lease."""


async def _acquire_lease(owner_id: str, deadline: float) -> str:
    """Poll for the owner's lease until ``deadline``, then give up.

    Polling rather than waiting on a signal because there is nothing to signal on: the holder
    is in another backend task. The interval is coarse on purpose -- the section it fences takes
    seconds, so a tighter poll only buys DynamoDB writes.
    """
    while True:
        token = await container_repo.try_claim_owner_lifecycle(owner_id)
        if token:
            return token
        if asyncio.get_running_loop().time() >= deadline:
            raise OwnerLifecycleBusyError(f"timed out waiting for the lifecycle lease: {owner_id}")
        await asyncio.sleep(_POLL_INTERVAL_S)


async def revoked_after(owner_id: str, started_ms: int) -> bool:
    """Revoke everything ran at or after ``started_ms`` (a request's entry time). Asked under
    ``owner_lifecycle_guard`` by every bind that is not an OAuth callback."""
    mark = await container_repo.get_oauth_revoked_at(owner_id)
    return mark is not None and started_ms <= mark


@asynccontextmanager
async def revoke_fence(owner_id: str, started_ms: int) -> AsyncIterator[None]:
    """A connect's final writes, under the lease revoke-all reads under: refused (409
    ``revoked``) when revoke everything ran at or after ``started_ms``, the request's entry."""
    from fastapi import HTTPException

    try:
        async with owner_lifecycle_guard(owner_id):
            if await revoked_after(owner_id, started_ms):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "revoked",
                        "message": "Revoke everything ran while this was connecting. Try again.",
                    },
                )
            yield
    except OwnerLifecycleBusyError:
        raise HTTPException(status_code=409, detail="busy") from None


async def revoked_since(owner_id: str, state: dict) -> bool:
    """Revoke everything ran after this OAuth attempt's stored state was minted. Every OAuth
    callback asks this under ``owner_lifecycle_guard`` and lands nothing when it is True."""
    mark = await container_repo.get_oauth_revoked_at(owner_id)
    return mark is not None and oauth_state_repo.started_before(state, mark)


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

    deadline = asyncio.get_running_loop().time() + OWNER_LIFECYCLE_TIMEOUT_S
    lease = await _acquire_lease(owner_id, deadline)
    token = _held_owner.set((task, owner_id))
    try:
        yield
    finally:
        _held_owner.reset(token)
        # Shielded: the release is the only thing standing between a cancelled caller and a
        # lease nobody drops for LIFECYCLE_CLAIM_STALE_S.
        await asyncio.shield(asyncio.create_task(container_repo.release_owner_lifecycle_claim(owner_id, token=lease)))


def serialize_owner_lifecycle(method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Decorate an async method whose first argument after ``self`` is an owner id."""

    owner_parameter = tuple(inspect.signature(method).parameters)[1]

    @wraps(method)
    async def serialized(*args: P.args, **kwargs: P.kwargs) -> R:
        owner_id = cast(str, args[1] if len(args) > 1 else kwargs.get(owner_parameter))
        async with owner_lifecycle_guard(owner_id):
            return await method(*args, **kwargs)

    return serialized
