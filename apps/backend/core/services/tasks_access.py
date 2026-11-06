"""Who can see a task, and what they see (contracts-v2 §4, §5; C15).

`can_view` is the only visibility rule: every route, Needs you, push, recall link, memory read and the projector's
refetch path asks it. `summarize` is pure over a task tree and returns the `TaskSummary` clients render; its
`shownGroup` mirrors `packages/shared/src/tasks/view.ts` (`taskView`) so web and API agree.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from core import tasks_schema as ts
from core.repositories import messaging_repo, tasks_repo
from core.services import clerk_admin, questions

_log = logging.getLogger(__name__)
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


# --- The task's one-line result (UX slice 3, dev walk 10) ------------------------------------------------------------
# `activities._announce` posts it as a new line in the main chat the task came from, with the task as a chip.

_FAILED_WHY = {
    "refused": "the computer it runs on refused it",
    "unavailable": "the computer it runs on wasn't available",
    "runtime_lost": "the run was lost",
    "lost_run": "the run was lost",
    "checkpoint_failed": "its work couldn't be saved",
}
LINE_MAX = 120
_FENCE = re.compile(r"^\s*(```|~~~)")
_REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s")  # [1]: https://…
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_RULE = re.compile(r"^\s*([-*_=]\s*){3,}$")  # --- / *** / ===
_TABLE_EDGE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")  # a table's |---|---|
_INLINE = [
    (re.compile(r"!\[[^\]]*\]\((?:[^()]|\([^()]*\))*\)"), ""),  # an image says nothing in one line
    (re.compile(r"\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)"), r"\1"),  # [text](url (nested))
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),  # [text][ref]
    (re.compile(r"<(https?://[^>\s]+)>"), r"\1"),  # <https://…> autolink
    (re.compile(r"</?[A-Za-z][^>]*>"), ""),  # raw HTML tags
    (re.compile(r"(`+)(.+?)\1"), r"\2"),  # `code`
    (re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"), r"\2"),  # **bold**, __bold__
    (re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"), r"\1"),  # *italic*, never "5 * 3"
    (re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"), r"\1"),  # _italic_, never snake_case
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"\1"),  # ~~strike~~
]
_LEAD = re.compile(r"^(?:>\s*|[-*+]\s+|\d+[.)]\s+)+")  # quote, list marker
_CJK_END = "。！？"
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*(?=\s|$)|[。！？][」』”’）)]*")


def _inline(line: str) -> str:
    if line.lstrip().startswith("|"):  # a table row: its cells, never its pipes
        line = " · ".join(c.strip() for c in line.strip().strip("|").split("|") if c.strip())
    for pattern, repl in _INLINE:
        line = pattern.sub(repl, line)
    return " ".join(_LEAD.sub("", line.strip()).split())


def _blocks(text: str) -> list[tuple[str, bool]]:
    """Paragraphs of `text` as (plain text, is a heading). Code blocks, rules and reference definitions say nothing."""
    out: list[tuple[str, bool]] = []
    para: list[str] = []
    fenced = False

    def end() -> None:
        if para:
            out.append((" ".join(para), False))
            para.clear()

    for raw in (text or "").splitlines():
        if _FENCE.match(raw):
            fenced = not fenced
            end()
            continue
        if fenced or _REF_DEF.match(raw) or ("|" in raw and _TABLE_EDGE.match(raw)):
            continue
        if not raw.strip() or _RULE.match(raw):
            end()
            continue
        if _HEADING.match(raw):
            end()
            if line := _inline(_HEADING.sub("", raw)).rstrip("#").strip():
                out.append((line, True))
            continue
        if line := _inline(raw):
            para.append(line)
    end()
    return out


def plain_line(text: str, limit: int = LINE_MAX) -> str:
    """`text`'s first paragraph (a leading heading only when nothing follows it) as one plain line of at most `limit`
    chars: markdown, HTML and code blocks stripped, whole sentences while they fit, else cut with "…"."""
    blocks = _blocks(text)
    flat = next((t for t, heading in blocks if not heading), blocks[0][0] if blocks else "")
    if len(flat) <= limit:
        return flat
    ends = [m.end() for m in _SENTENCE_END.finditer(flat) if m.end() <= limit]
    if ends:
        return flat[: ends[-1]].rstrip()
    cut = flat[: limit - 1]
    if " " in cut and cut[-1] not in _CJK_END:  # a word boundary when the text has words; CJK cuts anywhere
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-、，") + "…"


def seen_by_whole_room(task: dict, room: str | None) -> bool:
    """Everyone in `room` can open the task (`can_view`'s audience rules): a workspace task, or this room's own. Only
    then may the line in that room carry what the task said."""
    audience = task.get("audience") or {}
    return audience.get("kind") == "workspace" or (audience.get("kind") == "room" and audience.get("roomId") == room)


def result_line(tree: dict, room: str | None) -> str | None:
    """What came of a finished task, for the line in `room`: the result's first sentence, else the newest output's
    title, else the task's title; why a failed one didn't finish. None while it works (or watches), and when someone
    in the room can't open the task (its words never leak there; a person-only task never shows any)."""
    task = tree["task"]
    status = task.get("status")
    if status not in ("idle", "failed") or not seen_by_whole_room(task, room):
        return None
    if status == "failed":  # a watch armed before it failed changes nothing: it failed
        return f"Didn't finish: {_FAILED_WHY.get(task.get('statusReason') or '', 'something went wrong')}."
    if task.get("watchUntil"):
        return None
    att = max(tree.get("attempts") or [], key=lambda a: a.get("n", 0), default=None)
    said = plain_line(att.get("output") or "") if att and att.get("outcome") == "completed" else ""
    # The first sentence only: it says what was made; what follows (a suggestion, a figure) stays in the task.
    # ponytail: "e.g. " ends a sentence here; an abbreviation list if real lines get cut short.
    if end := _SENTENCE_END.search(said):
        said = said[: end.end()]
    outputs = task.get("outputs") or []
    made = plain_line(outputs[-1].get("title") or "") if outputs else ""
    return said or made or plain_line(task.get("title") or "") or None


#: A result this short with no file is the deliverable itself (a post, a reply): it shows whole in the chat's finish
#: message and is kept as the task's output (dev walk 15: an Instagram post lived only inside the task).
SHORT_RESULT_MAX = 1500


def short_result(att: dict | None) -> str | None:
    """A completed attempt's answer when it is short text and attached no file; else None."""
    if not att or att.get("outcome") != "completed" or att.get("attachments"):
        return None
    text = (att.get("output") or "").strip()
    return text if 0 < len(text) <= SHORT_RESULT_MAX else None


def result_piece(text: str) -> str:
    """The deliverable in a short result: what follows its first paragraph ("Drafted the post." + the post), else all
    of it. The chat's Copy (MessageTasks `resultPiece`) cuts the same way."""
    body = text.strip()
    cut = body.find("\n\n")
    return (body[cut:].strip() if cut >= 0 else "") or body


_SUBJECT = re.compile(r"^\s*\**subject\**\s*:", re.IGNORECASE | re.MULTILINE)


def text_output(task: dict, n: int, message_id: str, text: str = "") -> dict:
    """A short result kept as what the task made (Made here, Files, Creations). `ref.text` is the deliverable itself,
    so a library opens, names and previews it without its task (walk 16); an email draft (a "Subject:" line) is Mail."""
    piece = result_piece(text)
    return {
        "id": f"text:{n}",
        "kind": "mail" if _SUBJECT.search(piece) else "document",
        "title": plain_line(task.get("title") or "Result", 200),
        "ref": {
            "roomId": ts.task_room_id(task["taskId"]),
            "messageId": message_id,
            **({"text": piece} if piece else {}),
        },
    }


def result_text(tree: dict, room: str | None) -> str | None:
    """The finish message for `room`: a short result whole (walk 15), else `result_line`. Same privacy as
    `result_line`: None wherever someone in the room can't open the task, so its words never reach them."""
    line = result_line(tree, room)
    if line is None or tree["task"].get("status") != "idle":
        return line
    att = max(tree.get("attempts") or [], key=lambda a: a.get("n", 0), default=None)
    return short_result(att) or line


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
            "startedAt": last.get("startedAt"),
            "endedAt": last.get("endedAt"),
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
    ref = {"idemKey": effect["idemKey"]} | {k: str(o[k])[:200] for k in _EFFECT_REF_KEYS if o.get(k) not in (None, "")}
    if effect.get("kind") == "pr.merge" and "repo" in ref and "number" in ref:
        ref["url"] = f"https://github.com/{ref['repo']}/pull/{ref['number']}"
    if effect.get("resultRef"):
        ref["resultRef"] = str(effect["resultRef"])[:200]
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
    """Best effort: the effect being done matters more than the output row, so a failed write logs and goes on."""
    try:
        out = effect_output(effect)
    except Exception:  # noqa: BLE001
        _log.warning("effect %s: no output", effect.get("idemKey"), exc_info=True)
        return
    if out:
        await record_output(ws, effect["taskId"], out)


async def record_output(ws: str, task_id: str, output: dict) -> None:
    """`tasks_repo.add_task_output` that never fails its caller (an effect's done, a run's end)."""
    try:
        await tasks_repo.add_task_output(ws, task_id, output)
    except Exception:  # noqa: BLE001
        _log.warning("task %s: output %s not recorded", task_id, output.get("id"), exc_info=True)
