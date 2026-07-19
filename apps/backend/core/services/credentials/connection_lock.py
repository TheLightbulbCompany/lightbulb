"""Per-(owner, provider) async lock serializing connection-mutating operations.

The one background writer in the credential layer is the Instagram token refresh
(``meta_token_refresh``), fired fire-and-forget from ``GET /connections``. Without
serialization, a ``DELETE /connections/instagram`` that lands while a refresh is
mid-write can be undone by the refresh's ``apply_connection`` + ``put`` — the
materializer's own Graph ``/me`` call and EFS write give a wide interleave window
— resurrecting a live credential + re-enabled skill behind a disconnected UI.

The backend runs single-instance (ECS ``desiredCount=1``; multi-instance is the
still-open #1196), so an in-process ``asyncio.Lock`` fully serializes the two on
the actual deployment. The refresh holds the lock around its recheck→write
section (NOT the slow Meta network call), and disconnect holds it around its
teardown; whichever runs second sees the other's completed state. When
multi-instance lands, this becomes a distributed lock.
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
