"""One-time import of a workspace's existing AgentCore facts into the network log.

    uv run python -m scripts.memory_import <owner_id>

Reuses the memory sync (same lease, watermark and ``memory.import`` entries), so it is safe to
re-run: facts already imported are skipped.
"""

from __future__ import annotations

import asyncio
import sys

from core.config import settings
from core.services import memory_sync, workspace_memory


async def import_workspace(owner_id: str) -> int:
    memory_id = settings.AGENTCORE_MEMORY_ID
    if not memory_id:
        return 0
    out = await memory_sync.sync_workspace(
        owner_id, memory_id=memory_id, namespaces=workspace_memory.namespaces_for(owner_id, None)
    )
    return out["imported"]


if __name__ == "__main__":
    print(f"{sys.argv[1]}: imported {asyncio.run(import_workspace(sys.argv[1]))}")
