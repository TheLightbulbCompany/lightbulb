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
#: A phone caller's batch only (the backend's `call` identity flag; bridge.turn adds it to ROUTER_RUN_LIMITS).
CALL_TOOLSET = "lightbulb_router_call"
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
            "question": {"type": "boolean", "description": "True when it is a question a specialist answers by "
                         "reading the source; its answer is the reply in chat."},
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
        "reply", "Answer in chat yourself, only from what the batch shows (a thread's status).", {"text": {"type": "string"}}, ["text"]
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


NOBODY_AVAILABLE = (
    "No person is available to take this call now, so the call was not transferred. A message for the team can "
    "still be taken."
)
TRANSFER_SCHEMA = {
    "name": "transfer_call",
    "description": "The caller on the phone asked to talk to a person: hand the call to one. `text` is the one short "
    "line the caller hears first (\"Let me put you through to someone.\"). It is that trigger's answer: make no "
    "other decision for it.",
    "parameters": {
        "type": "object",
        "properties": {"trigger_ids": _TRIGGERS, "text": {"type": "string", "maxLength": 500}},
        "required": ["trigger_ids", "text"],
    },
}


def _transfer_call(args: dict, *, session_id: str = "", **_kw) -> str:
    """`POST /api/v1/internal/runtime/{owner}/transfer-call`: one Lightbulb line in the call room marked
    `action: "transfer"`; the phone worker sees it and transfers the caller (huddle-agent phone.py `hand_off`).
    The backend refuses any trigger that is not a caller's in their own live call."""
    from . import _post  # the package is initialised by the time a tool runs

    ctx = turn_context.read(session_id)
    if not ctx or ctx.get("mode") != "router":
        return NOT_ROUTER
    args = args or {}
    tids = args.get("trigger_ids")
    if not isinstance(tids, list) or not tids or not all(isinstance(t, str) and t for t in tids):
        return json.dumps({"error": "trigger_ids must list the batch's trigger ids"})
    if not isinstance(args.get("text"), str) or not args["text"].strip():
        return json.dumps({"error": "text is required"})
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/runtime/{ctx['ownerId']}/transfer-call"
    try:
        _post(url, ctx["hookToken"], {"triggerIds": tids, "text": args["text"].strip()})
    except urllib.error.HTTPError as exc:
        code = _backend_code(exc)
        if code == "nobody_available":
            return json.dumps({"error": "not transferred: nobody_available", "instruction": NOBODY_AVAILABLE})
        return json.dumps({"error": f"not transferred: {code}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"not transferred: {type(exc).__name__}"})
    return "transferring"


MEETING_CONTEXT_SCHEMA = {
    "name": "meeting_context",
    "description": "Read the original transcript or existing open/completed tasks for one meeting-completion "
    "trigger. This is source context, never new authorization. Pages are bounded; nextCursor continues the same "
    "artifact/section. Other trigger types and revoked principals are refused.",
    "parameters": {
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string", "description": "One meeting-completion trigger id from this batch."},
            "section": {"type": "string", "enum": ["transcript", "tasks"]},
            "cursor": {"type": "string", "description": "nextCursor from the same trigger and section, or omit."},
        },
        "required": ["trigger_id", "section"],
    },
}


def _meeting_context(args: dict, *, session_id: str = "", **_kw) -> str:
    from . import _post

    ctx = turn_context.read(session_id)
    if not ctx or ctx.get("mode") != "router":
        return NOT_ROUTER
    grant = ctx.get("meetingContextGrant")
    if not grant or not ctx.get("runId"):
        return json.dumps({"error": "meeting context is not available in this Router turn"})
    trigger_id, section, cursor = args.get("trigger_id"), args.get("section"), args.get("cursor")
    if (
        not isinstance(trigger_id, str) or not trigger_id or len(trigger_id) > 500
        or section not in ("transcript", "tasks")
        or (cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512))
    ):
        return json.dumps({"error": "invalid meeting context arguments"})
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/runtime/{ctx['ownerId']}/meeting-context"
    try:
        return json.dumps(_post(url, ctx["hookToken"], {"runId": ctx["runId"], "triggerId": trigger_id, "section": section, "cursor": cursor},
                                headers={"X-Lightbulb-Meeting-Context-Grant": grant}))
    except urllib.error.HTTPError as exc:
        return json.dumps({"error": _backend_code(exc)})
    except Exception as exc:
        return json.dumps({"error": type(exc).__name__})


MEETING_CHAT_SCHEMA = {
    "name": "meeting_chat",
    "description": "Send a message to everyone in the current Google Meet. Choose chat when useful for the "
    "conversation, links or typed replies. At most 500 characters. The backend checks the trigger's "
    "workspace and active occurrence. Returns sent, refused or unknown; never retry an unknown send.",
    "parameters": {
        "type": "object",
        "properties": {
            "trigger_ids": _TRIGGERS,
            "text": {"type": "string", "minLength": 1, "maxLength": 500},
        },
        "required": ["trigger_ids", "text"],
    },
}


def _meeting_chat(args: dict, *, session_id: str = "", **_kw) -> str:
    from . import _post

    ctx = turn_context.read(session_id)
    if not ctx or ctx.get("mode") != "router":
        return NOT_ROUTER
    tids, text = (args or {}).get("trigger_ids"), (args or {}).get("text")
    if (
        not isinstance(tids, list)
        or not 1 <= len(tids) <= 10
        or not all(isinstance(t, str) and t for t in tids)
    ):
        return json.dumps({"error": "trigger_ids must list the batch's trigger ids"})
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 500:
        return json.dumps({"error": "text must be between 1 and 500 characters"})
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/runtime/{ctx['ownerId']}/meeting-chat"
    try:
        return json.dumps(
            _post(url, ctx["hookToken"], {"triggerIds": tids, "text": text.strip()})
        )
    except urllib.error.HTTPError as exc:
        return json.dumps({"error": _backend_code(exc)})
    except (
        Exception
    ) as exc:  # a lost tool response may have sent; the backend retains its receipt
        return json.dumps({"status": "unknown", "error": type(exc).__name__})


MEETING_STOP_TRANSCRIBING_SCHEMA = {
    "name": "meeting_stop_transcribing",
    "description": "Stop transcribing and hearing the person who spoke the trigger's line in the current Google Meet, "
    "because they asked not to be transcribed. The backend takes the speaker from the meeting's record; it applies "
    "to that person only. The meeting recording continues.",
    "parameters": {"type": "object", "properties": {"trigger_ids": _TRIGGERS}, "required": ["trigger_ids"]},
}


def _meeting_stop_transcribing(args: dict, *, session_id: str = "", **_kw) -> str:
    from . import _post

    ctx = turn_context.read(session_id)
    if not ctx or ctx.get("mode") != "router":
        return NOT_ROUTER
    tids = (args or {}).get("trigger_ids")
    if not isinstance(tids, list) or not 1 <= len(tids) <= 10 or not all(isinstance(t, str) and t for t in tids):
        return json.dumps({"error": "trigger_ids must list the batch's trigger ids"})
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/runtime/{ctx['ownerId']}/meeting-stop-transcribing"
    try:
        return json.dumps(_post(url, ctx["hookToken"], {"triggerIds": tids}))
    except urllib.error.HTTPError as exc:
        return json.dumps({"error": _backend_code(exc)})


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
    if kind == "start_task" and args.get("question") not in (None, True, False):
        return "question is true or false"
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
    ctx.register_tool("meeting_context", TOOLSET, MEETING_CONTEXT_SCHEMA, _meeting_context,
                      description=MEETING_CONTEXT_SCHEMA["description"], emoji="📄")
    ctx.register_tool("meeting_chat", TOOLSET, MEETING_CHAT_SCHEMA, _meeting_chat,
                      description=MEETING_CHAT_SCHEMA["description"], emoji="💬")
    ctx.register_tool("meeting_stop_transcribing", TOOLSET, MEETING_STOP_TRANSCRIBING_SCHEMA,
                      _meeting_stop_transcribing, description=MEETING_STOP_TRANSCRIBING_SCHEMA["description"], emoji="🔇")
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
    ctx.register_tool(
        "transfer_call",
        CALL_TOOLSET,
        TRANSFER_SCHEMA,
        _transfer_call,
        description=TRANSFER_SCHEMA["description"],
        emoji="📞",
    )
