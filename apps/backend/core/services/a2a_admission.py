"""Inbound A2A → a pinned task (wave2-Q Task 10; spec 05 §3.8 inbound HTTP only; contracts-v2 §10).

Governance (`a2a_tasks`) decides who may ask what; once a request is allowed it is dispatched. With the switch on,
`admit_inbound_a2a` is that dispatch: one `TRIG#a2a:{a2aTaskId}` with a deterministic start pinned to the asked
agent, in the requester's private lane, answered in the requester's DM with that agent. Idempotent per A2A task;
a task a person stopped is never restarted by a late allow. The governance call site (`a2a_tasks._dispatch`) is the
A2A session's: one line, `if await super_agent_flags.flags_for(org): return await a2a_admission.admit_inbound_a2a(org, row) is not None`.

Typed text never answers anything here (contracts §8): an A2A hold is governance's own card.
"""

from __future__ import annotations

import logging

from core import tasks_schema as ts
from core.auth import AuthContext
from core.dynamodb import utc_now_iso
from core.observability.metrics import put_metric
from core.repositories import tasks_repo
from core.services import admission, hosted_mcp, super_agent_flags

logger = logging.getLogger(__name__)

_TITLE_MAX = 200


def _title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _TITLE_MAX else line[: _TITLE_MAX - 1] + "…"


async def admit_inbound_a2a(org: str, a2a_row: dict) -> str | None:
    """The trigger id the request runs under, or None (switch off, or its task was stopped). Idempotent."""
    if not await super_agent_flags.flags_for(org):
        return None
    a2a_task_id, agent_id, requester = a2a_row["id"], a2a_row["agentId"], a2a_row["requester"]
    trigger_id = ts.trigger_id("a2a", a2a_task_id)
    existing = await tasks_repo.get_trigger(org, trigger_id)
    if existing is not None:
        routed = (existing.get("routedTo") or {}).get("taskId")
        task = await tasks_repo.get_task(org, routed) if routed else None
        if task is not None and task["status"] == "resolved":
            put_metric("a2a.admission.stopped_task_ignored")
            return None  # a late allow never restarts what a person stopped (or finished)
        # Otherwise fall through: enqueue_trigger re-puts the same rows, so a crash between its two puts can't
        # strand the request (the outbox ignores an identical re-put).
    text = str(a2a_row.get("text") or "")
    start = ts.StartTask(
        kind="start_task",
        trigger_ids=[trigger_id],
        title=_title(text) or "A2A request",
        brief=text,
        specialist=agent_id,  # pinned: the agent that was asked, not the Router's pick
        ack="",
        visibility="person",
        a2a_task_id=a2a_task_id,  # lands on Task.a2aTaskId, so report_a2a can tell governance how it ended
    )
    room = await hosted_mcp.person_dm(AuthContext(user_id=requester, org_id=org), agent_id, create_key=a2a_task_id)
    trigger = ts.Trigger(
        ws=org,
        trigger_id=trigger_id,
        source="a2a",
        ref={
            "a2aTaskId": a2a_task_id,
            "agentId": agent_id,
            "requester": requester,
            "text": text,
            "start": start.model_dump(by_alias=True, exclude_none=True),  # C31: applied with no model turn
        },
        admission="respond",
        lane=ts.router_lane_key(requester),  # the requester's private lane (A12)
        actor_user_id=requester,
        received_at=utc_now_iso(),
        created_at=utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    created = await admission.enqueue_trigger(org, trigger, room_id=room)
    put_metric("a2a.admission.enqueued" if created else "a2a.admission.replayed")
    return trigger_id


async def report_a2a(ws: str, task_id: str, outcome: str) -> None:
    """Tell governance how the task ended (`complete_from_task` / `fail_from_task` / `cancel_from_task` are the A2A
    session's, spec 05 T9). Until they land: a metric and a log line, never a failure."""
    task = await tasks_repo.get_task(ws, task_id)
    a2a_task_id = (task or {}).get("a2aTaskId")
    if not a2a_task_id:
        return
    put_metric("a2a.report_pending", dimensions={"outcome": outcome})
    logger.info("a2a report pending task=%s a2a=%s outcome=%s", task_id, a2a_task_id, outcome)
