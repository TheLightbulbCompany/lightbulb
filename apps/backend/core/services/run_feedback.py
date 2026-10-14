"""The 30-minute check on every ended room run, and its one event (T35).

``end_run`` leaves the ledger row on the sweep index as ``followup`` with a deadline 30
minutes out. The run-ledger sweeper calls ``label_due_runs_once``, which reads what the room
did after the run and writes one label to the row:

- ``reacted``  -- someone reacted to the run's reply;
- ``replied``  -- the run replied and a person wrote in the same conversation afterwards;
- ``re_asked`` -- the run was stopped or said nothing, and the requester wrote again;
- ``ignored``  -- none of those.

Then it emits ``room_run_feedback`` with the routing source, Jev's confidence, the Stop
fields and the label. Nothing here changes agent behaviour: it only records labels for the
grader and the Monday readout ("stop rate after Jev-routed runs").
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta

from core.observability.metrics import put_metric
from core.observability.product_analytics import capture_product_event
from core.repositories import messaging_repo

logger = logging.getLogger(__name__)

EVENT_NAME = "room_run_feedback"
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:room_run_feedback")
_LOOKAHEAD = 50  # messages after the trigger worth reading; a busy room past this is "ignored"


def _ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def classify(run: dict, trigger: dict | None, later: list[dict]) -> str:
    """The label for one ended run. ``later``: room messages after the trigger, in order."""
    ended = _ts(run.get("endedAt"))
    if ended is None:
        return "ignored"
    window_end = ended + timedelta(seconds=messaging_repo.RUN_FOLLOWUP_CHECK_S)
    # The conversation: the trigger's thread (or the main room) and the thread the run answered in.
    conversation = {(trigger or {}).get("threadRootMessageId"), run.get("threadRootId")}
    requester = ((trigger or {}).get("author") or {}).get("id")
    replies = [
        m for m in later if m.get("runId") == run.get("runId") and (m.get("author") or {}).get("kind") == "agent"
    ]
    if any(users for m in replies for users in (m.get("reactions") or {}).values()):
        return "reacted"
    humans_after = [
        m
        for m in later
        if (m.get("author") or {}).get("kind") == "human"
        and m.get("threadRootMessageId") in conversation
        and (t := _ts(m.get("createdAt"))) is not None
        and ended < t <= window_end
    ]
    if replies and run.get("outcome") == "completed" and humans_after:
        return "replied"
    if (not replies or run.get("outcome") == "aborted") and any(
        (m.get("author") or {}).get("id") == requester for m in humans_after
    ):
        return "re_asked"
    return "ignored"


async def _label_one(owner_id: str, run_id: str) -> bool:
    run = await messaging_repo.get_run(owner_id, run_id)
    if run is None or run.get("sweep") != "followup":
        return False
    trigger = (
        await messaging_repo.get_message_by_id(run["roomId"], run["triggerMessageId"])
        if run.get("roomId") and run.get("triggerMessageId")
        else None
    )
    later: list[dict] = []
    if trigger and trigger.get("messageSeq") is not None:
        later, _ = await messaging_repo.list_messages(run["roomId"], int(trigger["messageSeq"]), _LOOKAHEAD)
    label = classify(run, trigger, later)
    labelled = await messaging_repo.label_run_followup(owner_id, run_id, label)
    if labelled is None:
        return False  # another pass got it
    requester = ((trigger or {}).get("author") or {}).get("id") or ""
    stopped_by = run.get("stoppedBy")
    capture_product_event(
        distinct_id=requester if requester.startswith("user_") else (stopped_by or owner_id),
        event=EVENT_NAME,
        properties={
            "workspace_id": run.get("workspaceId") or owner_id,
            "run_id": run_id,
            "room_id": run.get("roomId"),
            "agent_id": run.get("agentId"),
            "event_kind": run.get("eventKind"),
            "routed_by": run.get("routedBy") or "unknown",
            "routing_p": float(run["routingP"]) if run.get("routingP") is not None else None,
            "outcome": run.get("outcome"),
            "stopped": bool(run.get("abortRequestedAt")),
            "stopped_by": stopped_by,
            "stop_after_s": run.get("stopAfterS"),
            "follow_up": label,
        },
        event_uuid=str(uuid.uuid5(_NS, f"{owner_id}:{run_id}")),
    )
    put_metric("messaging.run.followup", dimensions={"label": label, "routed_by": run.get("routedBy") or "unknown"})
    return True


async def label_due_runs_once(now_epoch: int | None = None) -> int:
    """Label every ended run whose 30-minute check is due. Never raises per row."""
    labelled = 0
    now = int(now_epoch if now_epoch is not None else time.time())
    for ref in await messaging_repo.list_overdue_runs(now, marker="followup"):
        try:
            labelled += await _label_one(ref["ownerId"], ref["runId"])
        except Exception:
            put_metric("messaging.run.followup", dimensions={"label": "error", "routed_by": "unknown"})
            logger.warning("run follow-up label failed", exc_info=True)
    return labelled
