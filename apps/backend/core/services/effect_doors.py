"""Backend door gating for task-run agents (wave2-Q Task 12; contracts-v2 §8).

The effect checkpoint (bridge/effects.py, F1) runs in the VM, so a Worker that was refused could still reach a door
with `lightbulb-api.sh`, `curl` and the hook token, or a command shape the classifier does not parse. Each backend
door an external effect goes through (mail send, text send, app publish, purchase request) asks here first: for an
agent whose current live task attempt this is, the call must match an Effect row the checkpoint left in `intent`
for that attempt (same `operandsHash`, or the door's own looser match where the classifier cannot know an id).

Legacy and workspace agents (no live task attempt) and workspaces with the switch off are unchanged. Doors with no
backend side stay the checkpoint's alone: the network send (the VM's own node, `/opt/data/network.sock`) and
`gh pr merge` (the GitHub token is in the VM) — the accepted residual of spec 05 R3.
"""

from __future__ import annotations

from collections.abc import Callable

from core.observability.metrics import put_metric
from core.repositories import effects_repo, tasks_repo
from core.services import questions, super_agent_flags

_LIVE = frozenset({"dispatched", "running"})
MESSAGE = (
    "This action needs a person's approval first: ask through the task (request_approval / the effect checkpoint) "
    "and run it again once it is allowed."
)


class DoorRefused(Exception):
    code = "effect_not_approved"

    def __init__(self, kind: str):
        super().__init__(MESSAGE)
        self.kind = kind


async def live_attempt(ws: str, agent_id: str) -> dict | None:
    """The agent's current live task attempt, or None (a room or legacy turn: today's path).

    ponytail: one `by-status` query plus one read per working task (≤ the per-workspace cap); a per-agent
    pointer row if sends ever get hot."""
    tasks, _ = await tasks_repo.list_tasks_by_status(ws, "working", limit=50)
    for task in tasks:
        n = task.get("attemptCount") or 0
        if not n:
            continue
        att = await tasks_repo.get_attempt(ws, task["taskId"], n)
        if att and att.get("agentId") == agent_id and att.get("status") in _LIVE:
            return att
    return None


async def require_intent(
    ws: str,
    agent_id: str,
    kind: str,
    operands: dict,
    *,
    match: Callable[[dict], bool] | None = None,
) -> None:
    """Pass for a non-task caller or a switched-off workspace; else raise `DoorRefused` unless the attempt has an
    `intent` row of this kind whose operands hash equals the door's (or `match(row.operands)` says so)."""
    if not await super_agent_flags.flags_for(ws):
        return
    att = await live_attempt(ws, agent_id)
    if att is None:
        return
    wanted = questions.operands_hash(kind, operands)
    for row in await effects_repo.effects_of_task(ws, att["taskId"]):
        if row.get("attempt") != att["n"] or row.get("kind") != kind or row.get("state") != "intent":
            continue
        if row.get("operandsHash") == wanted or (match is not None and match(row.get("operands") or {})):
            return
    put_metric("effect_door.refused", dimensions={"kind": kind})
    raise DoorRefused(kind)


def mail_operands(*, agent_id: str, subject: str, body_text: str, to: str | None, reply_key: str | None) -> dict:
    """The classifier's `mail.send` operands for a `lightbulb-mail.sh send` call (bridge/effects.py)."""
    ops: dict = {"agentId": agent_id}
    if to:
        ops["to"] = to
    if subject:
        ops["subject"] = subject
    if reply_key:
        ops["replyKey"] = reply_key
    ops["body"] = body_text.rstrip("\n")  # `$(cat)` in the script drops trailing newlines; the heredoc keeps none
    return ops
