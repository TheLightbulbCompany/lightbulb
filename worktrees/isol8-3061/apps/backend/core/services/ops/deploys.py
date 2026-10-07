"""CloudFormation deploy/stack status for the operator console.

Reports per-stack status and, for failed/rolled-back stacks, the root-cause
failure WITHIN the current operation window (not a stale prior failure).
"""

from __future__ import annotations

import asyncio

import boto3

from core.config import settings

_MAX_EVENT_PAGES = 5
_client = None


def _cfn():
    global _client
    if _client is None:
        _client = boto3.client("cloudformation", region_name=settings.AWS_REGION)
    return _client


def _is_failed(status: str) -> bool:
    return status.endswith("_FAILED") or status == "ROLLBACK_COMPLETE" or status.endswith("ROLLBACK_COMPLETE")


async def _root_cause(stack_name: str) -> str | None:
    """Earliest *_FAILED ResourceStatusReason within the current operation."""
    earliest_reason: str | None = None
    next_token: str | None = None
    pages = 0
    while pages < _MAX_EVENT_PAGES:
        kwargs = {"StackName": stack_name}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = await asyncio.to_thread(_cfn().describe_stack_events, **kwargs)
        for ev in resp.get("StackEvents", []):  # newest-first
            rtype = ev.get("ResourceType")
            rstatus = ev.get("ResourceStatus", "")
            # Boundary of the current operation: the stack's own operation-START
            # *_IN_PROGRESS (CREATE/UPDATE/REVIEW). A rollback IN_PROGRESS is NOT
            # the start — for an auto-rolled-back stack the newest-first stream
            # hits UPDATE_ROLLBACK_IN_PROGRESS before the failing resource's
            # *_FAILED, so stopping there would drop the real cause. Walk past
            # rollback stack events to collect the underlying *_FAILED reason.
            if rtype == "AWS::CloudFormation::Stack" and rstatus.endswith("_IN_PROGRESS") and "ROLLBACK" not in rstatus:
                return earliest_reason
            if rstatus.endswith("_FAILED"):
                # newest-first walk → keep overwriting so we end with the earliest
                earliest_reason = ev.get("ResourceStatusReason") or earliest_reason
        next_token = resp.get("NextToken")
        pages += 1
        if not next_token:
            break
    return earliest_reason


async def list_stacks() -> list[dict]:
    prefix = f"lightbulb-{settings.ENVIRONMENT or 'dev'}-"
    # describe_stacks() with no StackName enumerates ALL stacks; CloudFormation
    # authorizes that form against the cloudformation:ListStacks IAM action
    # (NOT DescribeStacks). Both grants live on the backend task role.
    resp = await asyncio.to_thread(_cfn().describe_stacks)
    rows: list[dict] = []
    for st in resp.get("Stacks", []):
        name = st.get("StackName", "")
        if not name.startswith(prefix):
            continue
        status = st.get("StackStatus", "")
        root_cause = await _root_cause(name) if _is_failed(status) else None
        last_updated = st.get("LastUpdatedTime")
        rows.append(
            {
                "stack_name": name,
                "status": status,
                "status_reason": st.get("StackStatusReason"),
                "last_updated": last_updated.isoformat() if hasattr(last_updated, "isoformat") else None,
                "root_cause": root_cause,
            }
        )
    return rows
