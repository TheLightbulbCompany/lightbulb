"""The Router's decision tools (toolset `lightbulb_router`; contracts-v2 §9, §10; wave-2 O Task 4).

A Router run has no execution tools: each tool here appends one decision to the run
(`bridge.turn.record_decision`) and returns "recorded". The backend's `router_turn` reads the list from the
run's status and applies it (shapes: `routerDecisions` in tasks-schema.json; it validates again).
`ask` is a plain clarifying reply: it never approves anything (§8).
"""
from __future__ import annotations

import json

from . import turn_context

TOOLSET = "lightbulb_router"
MAX_PROPOSED = 5
NOT_ROUTER = "Router tools are only available in a Lightbulb Router run."

_TRIGGERS = {"type": "array", "items": {"type": "string"}, "minItems": 1,
             "description": "The trigger ids (from the batch) this decision answers."}
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
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": {"trigger_ids": _TRIGGERS, "reason": _REASON, **props},
        "required": ["trigger_ids", *required]}}


SCHEMAS = {
    "start_task": _schema(
        "start_task", "Start one task now for a request in the batch. `ack` is the one line people see.",
        {**_TASK["properties"], "ack": {"type": "string"},
         "visibility": {"type": "string", "enum": ["person", "room", "workspace"]}},
        ["title", "brief", "ack"]),
    "propose_tasks": _schema(
        "propose_tasks", "Suggest up to 5 tasks a person starts with one tap (use for paid tools or unclear asks).",
        {"tasks": {"type": "array", "items": _TASK, "minItems": 1, "maxItems": MAX_PROPOSED},
         "ack": {"type": "string"}},
        ["tasks", "ack"]),
    "pass_through": _schema(
        "pass_through", "Send a message to the task already working on it.",
        {"task_id": {"type": "string"}, "ack": {"type": "string"}}, ["task_id"]),
    "reply": _schema("reply", "Answer in chat yourself (quick answers, status).",
                     {"text": {"type": "string"}}, ["text"]),
    "ask": _schema("ask", "Ask a clarifying question in chat. It approves nothing.",
                   {"text": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, ["text"]),
    "ignore": _schema("ignore", "Nothing to do for these triggers.", {}, []),
}

_CAMEL = {"trigger_ids": "triggerIds", "task_id": "taskId"}


def _problem(kind: str, args: dict) -> str | None:
    if not isinstance(args.get("trigger_ids"), list) or not args["trigger_ids"] or not all(
            isinstance(t, str) and t for t in args["trigger_ids"]):
        return "trigger_ids must list the batch's trigger ids"
    for field in SCHEMAS[kind]["parameters"]["required"]:
        if args.get(field) in (None, "", []):
            return f"{field} is required"
    if kind == "start_task" and len(str(args.get("title"))) > 200:
        return "title is at most 200 characters"
    if kind == "propose_tasks":
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_PROPOSED:
            return f"propose 1 to {MAX_PROPOSED} tasks"
        if not all(isinstance(t, dict) and t.get("title") and t.get("brief") for t in tasks):
            return "every proposed task needs a title and a brief"
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
