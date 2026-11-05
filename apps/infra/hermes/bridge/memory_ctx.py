"""The turn's backend-computed ``memory`` block (spec 07 §5.2), for the AgentCore memory plugin.

Set on every agent construction (``turn._construct_agent``) from the run or turn body, ``None`` when
the body has none: a turn never sees the block of the turn before it. Hermes copies ContextVars into
its tool workers, so the plugin reads the same value in ``sync_turn`` and in tool calls.

Shape: ``{"lane": "shared"|"private", "member": "user_…"|None, "audience": {...}, "tags": {...},
"sync": "events"|"none"}``. No block = today's behaviour, byte for byte.
"""

from __future__ import annotations

import contextvars
import re

MEMORY_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar("lightbulb_memory", default=None)
_PERSON = re.compile(r"user_[A-Za-z0-9]+")


def valid(block) -> bool:
    """The block as the backend builds it (memory_tags.block_for_worker/_router). A private lane
    always names its person; that person is never taken from anywhere else."""
    if not isinstance(block, dict):
        return False
    lane, member = block.get("lane"), block.get("member")
    return (
        lane in ("shared", "private")
        and block.get("sync") in ("events", "none")
        and isinstance(block.get("tags") or {}, dict)
        and isinstance(block.get("audience") or {}, dict)
        and (bool(isinstance(member, str) and _PERSON.fullmatch(member)) if lane == "private" else member is None)
    )
