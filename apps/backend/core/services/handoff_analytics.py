"""Handoff events (T03): a person took the agents' work and decided on it.

Three kinds, each with its own event, and ``handoff_completed {kind}`` once per workspace
on the first of them (D12: a rejected approval counts; a person decided):

- ``approval_answered {decision}`` -- approve or reject in the Teams approvals rail.
- ``task_moved {status}`` -- a person changed the status of an agent-assigned task.
- ``session_picked_up`` -- already emitted by ``session_pickup``; it calls ``handoff`` too.

Once-ever is a conditional write in the webhook-dedup table (TTL a year), not the event
uuid: PostHog drops a repeated uuid only within one UTC day. The readout keeps workspaces
whose ``days_since_created`` is under 7. Best-effort: never raises into the route.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from core.observability.product_analytics import capture_product_event
from core.services import clerk_admin, webhook_dedup

logger = logging.getLogger(__name__)

KINDS = frozenset({"approval_answered", "task_moved", "session_picked_up"})
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:handoff_completed")
_ONCE_TTL_S = 366 * 86400

# Fire-and-forget tasks, held so the loop does not collect them mid-flight.
_tasks: set[asyncio.Task] = set()


def handoff(*, workspace_id: str, user_id: str, kind: str) -> None:
    """Schedule the once-per-workspace ``handoff_completed``. Synchronous, I/O-free."""
    if kind not in KINDS or not workspace_id or not workspace_id.startswith("org_"):
        return
    try:
        task = asyncio.get_running_loop().create_task(_first_handoff(workspace_id, user_id, kind))
    except RuntimeError:
        return  # no loop (sync caller): skip rather than block
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _first_handoff(workspace_id: str, user_id: str, kind: str) -> None:
    try:
        first = await webhook_dedup.record_event_or_skip(
            f"handoff-first:{workspace_id}", source="analytics", ttl_seconds=_ONCE_TTL_S
        )
        if first is not webhook_dedup.WebhookDedupResult.RECORDED:
            return
        org = await clerk_admin.get_organization(workspace_id) or {}
        created = org.get("created_at")
        days = (
            round((time.time() - created / 1000) / 86400, 2)
            if isinstance(created, (int, float)) and created > 0
            else None
        )
        capture_product_event(
            distinct_id=user_id,
            event="handoff_completed",
            properties={"workspace_id": workspace_id, "kind": kind, "days_since_created": days},
            event_uuid=str(uuid.uuid5(_NS, workspace_id)),
        )
    except Exception:
        logger.warning("handoff_analytics: handoff_completed failed for %s", workspace_id, exc_info=True)


def approval_answered(*, workspace_id: str, user_id: str, approval_id: str, decision: str) -> None:
    try:
        capture_product_event(
            distinct_id=user_id,
            event="approval_answered",
            properties={"workspace_id": workspace_id, "approval_id": approval_id, "decision": decision},
            event_uuid=str(uuid.uuid5(_NS, f"approval_answered:{approval_id}:{decision}")),
        )
    except Exception:
        logger.warning("handoff_analytics: approval_answered failed", exc_info=True)
    handoff(workspace_id=workspace_id, user_id=user_id, kind="approval_answered")


def task_moved(*, workspace_id: str, user_id: str, issue: dict, status: str) -> None:
    """Only for a task assigned to an agent: that is the person taking the agent's work."""
    if not isinstance(issue, dict) or not issue.get("assigneeAgentId"):
        return
    try:
        capture_product_event(
            distinct_id=user_id,
            event="task_moved",
            properties={
                "workspace_id": workspace_id,
                "issue_id": issue.get("id"),
                "agent_id": issue.get("assigneeAgentId"),
                "status": status,
            },
        )
    except Exception:
        logger.warning("handoff_analytics: task_moved failed", exc_info=True)
    handoff(workspace_id=workspace_id, user_id=user_id, kind="task_moved")
