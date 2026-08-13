"""Per-(owner, provider) async lock serializing connection-mutating operations.

Connect (the OAuth callback) and disconnect both mutate the same three places —
Secrets Manager, the EFS materialization, and the status row — and a materializer
can spend seconds in a provider API call and an EFS write between them. Without
serialization, a ``DELETE /connections/<provider>`` landing mid-connect can be
undone by the callback finishing its writes afterwards, resurrecting a live
credential and re-enabled skill behind a disconnected UI.

The backend runs single-instance (ECS ``desiredCount=1``; multi-instance is the
still-open #1196), so an in-process ``asyncio.Lock`` fully serializes them on the
actual deployment. Each holds the lock around its write section, never around a
slow provider network call; whichever runs second sees the other's completed
state. When multi-instance lands, this becomes a distributed lock.

Note there is no longer a background writer here: Instagram's token renewal moved
into the container (``skills/instagram/token.sh``), which touches only the EFS
credentials file the agent itself reads.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def connection_lock(owner_id: str, provider: str) -> asyncio.Lock:
    """The lock guarding connection mutations for this owner+provider. asyncio.Lock
    binds to the running loop lazily (3.10+), so constructing it here off-loop is
    safe. Uncontended for providers with no background writer."""
    return _locks[(owner_id, provider)]
