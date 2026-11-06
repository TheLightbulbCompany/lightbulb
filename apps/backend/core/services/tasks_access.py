"""Who can see a task, and what they see (contracts-v2 §4, §5; C15).

`can_view` is the only visibility rule: every route, Needs you, push, recall link, memory read and the projector's
refetch path asks it. `summarize` is pure over a task tree and returns the `TaskSummary` clients render; its
`shownGroup` mirrors `packages/shared/src/tasks/view.ts` (`taskView`) so web and API agree.
"""

from __future__ import annotations

import time
from typing import Any

from core import tasks_schema as ts
from core.repositories import messaging_repo, tasks_repo
from core.services import clerk_admin, questions

_ORG_TTL_S = 60.0
_ORG_CACHE_MAX = 10_000
_org_cache: dict[str, tuple[float, set[str]]] = {}


async def _user_org_ids(user_id: str) -> set[str]:
    # ponytail: per-process 60 s cache of a user's Clerk orgs; a removed member keeps workspace-task access
    # for up to a minute. Swap for the auth context's org when routes pass it.
    hit = _org_cache.get(user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    if len(_org_cache) >= _ORG_CACHE_MAX:
        _org_cache.clear()
    orgs = {o["id"] for o in await clerk_admin.list_user_organizations(user_id, limit=100) if o.get("id")}
    _org_cache[user_id] = (time.monotonic() + _ORG_TTL_S, orgs)
    return orgs


async def _is_workspace_member(ws: str, user_id: str) -> bool:
    if ws.startswith("user_"):
        return ws == user_id
    return ws in await _user_org_ids(user_id)


async def can_view(task: dict, user_id: str) -> bool:
    """DM ⇒ requester; room ⇒ current members of the home room; workspace ⇒ every member; plus anyone in
    `audience.userIds` (Lane Q adds a Question's answerers there when it opens, contracts §5)."""
    if not user_id:
        return False
    audience = task.get("audience") or {}
    if user_id in (audience.get("userIds") or []):
        return True
    kind = audience.get("kind")
    if kind == "room" and audience.get("roomId"):
        return await messaging_repo.get_member(audience["roomId"], "human", user_id) is not None
    if kind == "workspace":
        return await _is_workspace_member(task["pk"].removeprefix("WS#"), user_id)
    return False


def _shown_group(task: dict, viewer_id: str, open_qs: list[dict], effects: list[dict]) -> str | None:
    status = task["status"]
    if status == "resolved":
        return "resolved"
    if status == "failed":
        return "waiting_on_you" if task.get("ownerUserId") == viewer_id else "idle"
    if status == "idle":
        return "idle"
    mine = [q for q in open_qs if viewer_id in (q.get("answerers") or [])]
    if any(q.get("kind") != "review" for q in mine):
        return "waiting_on_you"
    if mine:
        return "ready_for_review"
    if task.get("landingEffect") or any(e.get("state") in ("approved", "intent") for e in effects):
        return "landing"
    return "working"


def _origin_room(subscriptions: list[dict], audience: dict) -> str | None:
    for sub in subscriptions:
        key = sub.get("threadKey", "")
        if sub.get("createdBy") == "router" and key.startswith("room:"):
            return key.split(":")[1]
    return audience.get("roomId")


def summarize(tree: dict, viewer_id: str) -> dict[str, Any]:
    """The C15 `TaskSummary` for one viewer. Callers check `can_view(task, viewer_id)` first: this does not. `tree` is `tasks_repo.get_task_tree` plus optional `effects`
    (Effect rows of the task) and `userNames` ({userId: display name}) added by the caller."""
    task = tree["task"]
    if task.get("status") == "proposed":  # stored before 5 Nov 2026 (proposals removed): it never started, so Closed
        task = {**task, "status": "resolved", "statusReason": "dismissed"}
    names = tree.get("userNames") or {}
    effects = tree.get("effects") or []
    open_qs = [q for q in tree.get("questions") or [] if q.get("state") == "open"]
    attempts = sorted(tree.get("attempts") or [], key=lambda a: a.get("n", 0))
    last = attempts[-1] if attempts else None
    audience = task.get("audience") or {}

    question = None
    if open_qs:
        q = next((q for q in open_qs if viewer_id in (q.get("answerers") or [])), open_qs[0])
        answerers = list(q.get("answerers") or [])
        question = {
            "qid": q["qid"],
            "kind": q["kind"],
            "subkind": q.get("subkind"),
            "title": q.get("title", ""),
            "answerers": answerers,
            "answererNames": [names.get(u, u) for u in answerers],
            "canAnswer": viewer_id in answerers,
            "options": q.get("options") or [],
        }

    draft = next((e for e in effects if e.get("state") == "proposed" and e.get("output")), None)
    output = None
    if draft:
        output = {
            "idemKey": draft["idemKey"],
            "kind": draft["output"]["kind"],
            "title": draft["output"]["title"],
            "nextStep": (questions.REVIEW_KINDS.get(draft.get("kind")) or "approve")
            if draft.get("questionId")
            else None,
        }

    usage = task.get("usage")
    return {
        "taskId": task["taskId"],
        "identifier": task["identifier"],
        "title": task["title"],
        "status": task["status"],
        "statusReason": task.get("statusReason"),
        "shownGroup": _shown_group(task, viewer_id, open_qs, effects),
        "specialist": task.get("specialist"),
        "ownerUserId": task.get("ownerUserId"),
        "audience": audience,
        "audienceKind": audience.get("kind"),
        "originRoomId": _origin_room(tree.get("subscriptions") or [], audience),
        "taskRoomId": ts.task_room_id(task["taskId"]),
        "openQuestion": question,
        "landing": task.get("landingEffect"),
        "output": output,
        "outputs": task.get("outputs") or [],
        "watchUntil": task.get("watchUntil"),
        "watching": task["status"] == "idle" and bool(task.get("watchUntil")),
        "runsOn": None if not last else ("task" if "-t-" in (last.get("sessionId") or "") else "workspace"),
        "activity": None
        if not last
        else {
            "n": last["n"],
            "status": last["status"],
            "outcome": last.get("outcome"),
            "lastHeartbeatAt": last.get("lastHeartbeatAt"),
        },
        "recentInputs": list(last.get("inputTriggerIds") or []) if last else [],
        "attemptCount": task.get("attemptCount", 0),
        "usage": usage,
        "costUsd": float((usage or {}).get("costUsd", 0.0)),
        "budgetUsd": task.get("budgetUsd"),
        "updatedAt": task["updatedAt"],
        "version": task["version"],
    }


# --- What a task made (UX slice 5) ---------------------------------------------------------------------------------
# `TaskSummary.output` is only the draft waiting on a review; `outputs` stays on the task row for every status, written
# when an effect lands (`record_effect_output`), when a result attaches files (`activities.end_attempt`) and when a
# request hands an app to Apps (`app_conversation`).

DOC_TYPES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/pdf",
    "application/vnd.oasis.opendocument.text",
    "text/markdown",
    "text/plain",
    "text/html",
)
_EFFECT_REF_KEYS = ("to", "subject", "appId", "appName", "repo", "number")


def effect_output(effect: dict) -> dict | None:
    """The output a landed review-kind Effect leaves (mail sent, PR merged, app published). None for an approval kind
    (purchase, provision, a2a), a refusal (`refused:…` did not happen) and anything not `done`."""
    if effect.get("state") != "done" or str(effect.get("resultRef") or "").startswith("refused:"):
        return None
    drafted = effect.get("output") or questions.output_for(effect)
    if drafted is None:
        return None
    o = effect.get("operands") or {}
    ref = {"idemKey": effect["idemKey"]} | {k: str(o[k]) for k in _EFFECT_REF_KEYS if o.get(k) not in (None, "")}
    if effect.get("kind") == "pr.merge" and "repo" in ref and "number" in ref:
        ref["url"] = f"https://github.com/{ref['repo']}/pull/{ref['number']}"
    if effect.get("resultRef"):
        ref["resultRef"] = str(effect["resultRef"])
    return {"id": f"effect:{effect['idemKey']}", "kind": drafted["kind"], "title": drafted["title"], "ref": ref}


def file_output(task_id: str, file: dict) -> dict:
    """A file a result attached in the task room; opened by `GET /messaging/rooms/{roomId}/attachments/{id}/url`."""
    ctype = file.get("contentType") or ""
    return {
        "id": f"file:{file['attachmentId']}",
        "kind": "document" if ctype in DOC_TYPES else "file",
        "title": (file.get("fileName") or "file")[:200],
        "ref": {
            "roomId": ts.task_room_id(task_id),
            "attachmentId": str(file["attachmentId"]),
            "fileName": file.get("fileName") or "",
            "contentType": ctype,
        },
    }


async def record_effect_output(ws: str, effect: dict) -> None:
    if out := effect_output(effect):
        await tasks_repo.add_task_output(ws, effect["taskId"], out)
