"""The Router's decision tools (toolset `lightbulb_router`; contracts-v2 §9, §10; wave-2 O Task 4).

A Router run has no execution tools: each tool here appends one decision to the run
(`bridge.turn.record_decision`) and returns "recorded". The backend's `router_turn` reads the list from the
run's status and applies it (shapes: `routerDecisions` in tasks-schema.json; it validates again).
`ask` is a plain clarifying reply: it never approves anything (§8).
"""

from __future__ import annotations

import json
import re
import urllib.error

from . import turn_context

TOOLSET = "lightbulb_router"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NOT_ROUTER = "Router tools are only available in a Lightbulb Router run."

_TRIGGERS = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "description": "The trigger ids (from the batch) this decision answers.",
}
_REASON = {"type": "string", "description": "One short line on why."}
_TASK = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 200},
        "brief": {"type": "string", "description": "What the specialist should do, self-contained."},
        "specialist": {"type": "string", "description": "A specialist id from the roster, or omit."},
    },
    "required": ["title", "brief"],
}


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {"trigger_ids": _TRIGGERS, "reason": _REASON, **props},
            "required": ["trigger_ids", *required],
        },
    }


SCHEMAS = {
    "start_task": _schema(
        "start_task",
        "Start one task now for a request in the batch. `ack` is the one line people see. "
        "Write the brief as the work itself: the specialist handles approvals for sending, spending "
        "and publishing, so never add approval or confirmation steps to the brief.",
        {
            **_TASK["properties"],
            "ack": {"type": "string"},
            "visibility": {"type": "string", "enum": ["person", "room", "workspace"]},
        },
        ["title", "brief", "ack"],
    ),
    "pass_through": _schema(
        "pass_through",
        "Send a message to the task already working on it.",
        {"task_id": {"type": "string"}, "ack": {"type": "string"}},
        ["task_id"],
    ),
    "reply": _schema(
        "reply", "Answer in chat yourself (quick answers, status).", {"text": {"type": "string"}}, ["text"]
    ),
    "ask": _schema(
        "ask",
        "Ask a clarifying question in chat. It approves nothing.",
        {"text": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
        ["text"],
    ),
    "note": _schema(
        "note",
        "Someone told you a fact to keep for this project (a date, a preference, a detail), with nothing to do "
        "now. Lightbulb answers with one short line; the message is already in the project's memory.",
        {"fact": {"type": "string", "description": "The fact, self-contained, in one sentence."}},
        ["fact"],
    ),
    "ignore": _schema("ignore", "Nothing to do for these triggers.", {}, []),
}

_CAMEL = {"trigger_ids": "triggerIds", "task_id": "taskId"}

FOLLOW_LINK_SCHEMA = {
    "name": "follow_link",
    "description": "Open a task that a recall result linked: its summary, threads and last result, as the "
    "people in this lane may see it. Read-only.",
    "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
}
NO_LINK = "follow_link needs this run's memory block (the backend sets it on every Router run)."


def _follow_link(args: dict, *, session_id: str = "", **_kw) -> str:
    """The Router's second hop (lane M6): `POST /api/v1/internal/memory/link/{owner}` with the run's audience."""
    from bridge.memory_ctx import MEMORY_CTX

    from . import _post  # the package is initialised by the time a tool runs

    ctx = turn_context.read(session_id)
    if not ctx or ctx.get("mode") != "router":
        return NOT_ROUTER
    task_id = str((args or {}).get("task_id") or "")
    if not _UUID.fullmatch(task_id):
        return json.dumps({"error": "task_id must be a task id from a Linked: line"})
    block = MEMORY_CTX.get()
    if not block or not block.get("audience"):
        return NO_LINK
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/memory/link/{ctx['ownerId']}"
    try:
        return json.dumps(_post(url, ctx["hookToken"], {"id": task_id, "audience": block["audience"]}))
    except urllib.error.HTTPError as exc:
        return json.dumps({"error": "not visible to this lane" if exc.code == 404 else f"link failed: {exc.code}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"link failed: {type(exc).__name__}"})


PROPOSE_SCHEMA = {
    "name": "propose_automation",
    "description": "Draft a recurring automation for a request in the batch and post its confirm card in the room. "
    "Nothing runs until a person turns it on with the card's Turn on button; the card already says "
    "'Turn it on with the button on the card.', so don't ask anyone to confirm in chat.",
    "parameters": {
        "type": "object",
        "properties": {
            "trigger_ids": _TRIGGERS,
            "name": {"type": "string", "maxLength": 200},
            "instructions": {"type": "string", "description": "What each run should do, self-contained."},
            "cron": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
                "description": "5-field cron lines in the person's zone (runs at least 10 minutes apart). "
                'A cron that pins one date ("0 9 5 11 *") runs once.',
            },
            "repeat": {
                "type": "string",
                "enum": ["yearly"],
                "description": "Only for a one-date cron the person wants every year on that date.",
            },
            "tz": {"type": "string", "description": "IANA zone; omit for the requester's own zone."},
            "specialist": {"type": "string", "description": "A specialist id from the roster, or omit."},
            "deliver_to": {
                "type": "string",
                "description": 'Omit to post in the room the request came from ("here", '
                '"this room"). Another room: room:{roomId} or room:#{name}. '
                "dm:{requester's userId} for their DM with you. none for no post.",
            },
            "budget_per_run_usd": {"type": "number"},
        },
        "required": ["trigger_ids", "name", "instructions", "cron"],
    },
}
_PROPOSE_KEYS = {
    "trigger_ids": "triggerIds",
    "name": "name",
    "instructions": "instructions",
    "cron": "cron",
    "tz": "tz",
    "specialist": "specialistId",
    "deliver_to": "deliverTo",
    "budget_per_run_usd": "budgetPerRunUsd",
    "repeat": "repeat",
}


def _backend_code(exc: urllib.error.HTTPError) -> str:
    """The backend's error code (`room_not_found`) so the model can fix its call; the status when there is none."""
    try:
        detail = json.loads(exc.read() or b"{}").get("detail")
    except (ValueError, OSError, AttributeError):
        detail = None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and code else f"the backend answered {exc.code}"


def _propose_automation(args: dict, *, session_id: str = "", **_kw) -> str:
    """Lane A: `POST /api/v1/internal/automations/{owner}/propose` drafts the row, pauses its Schedule and posts
    the confirm card; the Router says one line and moves on."""
    from . import _post  # the package is initialised by the time a tool runs

    ctx = turn_context.read(session_id)
    if not ctx or ctx.get("mode") != "router":
        return NOT_ROUTER
    args = args or {}
    if not isinstance(args.get("trigger_ids"), list) or not args["trigger_ids"]:
        return json.dumps({"error": "trigger_ids must list the batch's trigger ids"})
    for field in ("name", "instructions"):
        if not isinstance(args.get(field), str) or not args[field].strip():
            return json.dumps({"error": f"{field} is required"})
    if len(args["name"]) > 200:
        return json.dumps({"error": "name is at most 200 characters"})
    cron = args.get("cron")
    if not isinstance(cron, list) or not cron or not all(isinstance(c, str) and c.strip() for c in cron):
        return json.dumps({"error": "cron must be a list of 5-field cron lines"})
    body = {_PROPOSE_KEYS[k]: v for k, v in args.items() if k in _PROPOSE_KEYS and v is not None}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/automations/{ctx['ownerId']}/propose"
    try:
        _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        return json.dumps({"error": f"not proposed: {_backend_code(exc)}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"not proposed: {type(exc).__name__}"})
    return "proposed"


def _problem(kind: str, args: dict) -> str | None:
    if (
        not isinstance(args.get("trigger_ids"), list)
        or not args["trigger_ids"]
        or not all(isinstance(t, str) and t for t in args["trigger_ids"])
    ):
        return "trigger_ids must list the batch's trigger ids"
    for field in SCHEMAS[kind]["parameters"]["required"]:
        if args.get(field) in (None, "", []):
            return f"{field} is required"
    if kind == "start_task" and len(str(args.get("title"))) > 200:
        return "title is at most 200 characters"
    if kind == "pass_through" and not _UUID.fullmatch(str(args.get("task_id"))):
        return "task_id must be a task id from the batch"
    if kind == "start_task" and args.get("visibility") not in (None, "person", "room", "workspace"):
        return "visibility is person, room or workspace"
    for field in ("ack", "text", "brief", "title", "reason", "specialist", "fact"):
        if field in args and args[field] is not None and not isinstance(args[field], str):
            return f"{field} must be text"
    return None


def _decision(kind: str, args: dict) -> dict:
    allowed = set(SCHEMAS[kind]["parameters"]["properties"])
    return {"kind": kind, **{_CAMEL.get(k, k): v for k, v in args.items() if k in allowed and v is not None}}


def _handler(kind: str):
    def handle(args: dict, *, session_id: str = "", **_kw) -> str:
        ctx = turn_context.read(session_id)
        if not ctx or ctx.get("mode") != "router":
            return NOT_ROUTER
        problem = _problem(kind, args or {})
        if problem:
            return json.dumps({"error": problem})
        from bridge import turn

        if not turn.record_decision(ctx["runId"], _decision(kind, args)):
            return json.dumps({"error": "decision not recorded (too many, or the run ended)"})
        return "recorded"

    return handle


def register(ctx) -> None:
    for kind, schema in SCHEMAS.items():
        ctx.register_tool(kind, TOOLSET, schema, _handler(kind), description=schema["description"], emoji="🧭")
    ctx.register_tool(
        "follow_link",
        TOOLSET,
        FOLLOW_LINK_SCHEMA,
        _follow_link,
        description=FOLLOW_LINK_SCHEMA["description"],
        emoji="🔗",
    )
    ctx.register_tool(
        "propose_automation",
        TOOLSET,
        PROPOSE_SCHEMA,
        _propose_automation,
        description=PROPOSE_SCHEMA["description"],
        emoji="🗓️",
    )
