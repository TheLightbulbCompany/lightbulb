"""This task's own reachable address.

The desktop's WebSocket is pinned to whichever backend task accepted the upgrade, while the
bridge's node-invoke call can land on any of the 2-4. The presence row therefore records the
accepting task's address and the internal route forwards to it (spec §4.4).

Fargate awsvpc gives each task exactly one ENI, so the container's own hostname resolves to
the address peers reach it on — no metadata call, no extra IAM.
"""

import functools
import os
import socket


@functools.lru_cache(maxsize=1)
def own_address() -> str:
    """This task's own address, memoized for the process lifetime.

    Deliberately does not catch a resolution failure: a task that cannot
    resolve its own hostname has no address worth writing to the presence
    row, and a swallowed exception there would instead publish a bogus
    address that Task 28's bridge would forward to and fail against with no
    clue why. Loud and unresolved beats quiet and wrong. `functools.lru_cache`
    never caches a call that raised, so a transient failure does not wedge
    this task into permanent addresslessness -- the next caller (the 60s
    relay-presence refresh loop, or the next connect) simply retries the
    resolution.
    """
    return f"http://{socket.gethostbyname(socket.gethostname())}:{os.environ.get('PORT', '8000')}"
