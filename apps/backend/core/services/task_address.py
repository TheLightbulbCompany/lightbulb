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
    return f"http://{socket.gethostbyname(socket.gethostname())}:{os.environ.get('PORT', '8000')}"
