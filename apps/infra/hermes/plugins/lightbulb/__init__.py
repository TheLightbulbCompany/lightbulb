"""The `lightbulb` Hermes tool plugin: the room tools OpenClaw gave us for free."""
from __future__ import annotations

import itertools
import json
import logging
import re
import urllib.request

from . import turn_context
from bridge import browser_control, website_logins

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_counter = itertools.count(1)
NO_TURN = "Lightbulb tools are unavailable: no active Lightbulb turn for this session."
# Ruling M6-R45: the turn's own reply is posted for the agent when the turn ends, so a send
# into the room+thread it is answering posts the answer TWICE (observed on the fresh dev org:
# the CEO posted its introduction with this tool, then "Message sent! I've introduced myself..."
# landed as a second message).
OWN_THREAD_REFUSAL = ("Your reply to this thread is delivered automatically when the turn ends "
                      "-- write it as your answer. Use send_room_message only for other rooms "
                      "or threads.")

# Task 11: send_room_message tool
SEND_SCHEMA = {
    "name": "send_room_message",
    "description": "Post a message to ANOTHER Lightbulb room, or another thread in this one — "
                   "your reply to the thread you are answering is posted for you when the turn "
                   "ends, so never send that here.",
    "parameters": {
        "type": "object",
        "properties": {
            "roomId": {"type": "string", "description": "Room id, optionally prefixed 'lightbulb:'."},
            "text": {"type": "string"},
            "threadRootMessageId": {"type": "string",
                                    "description": "Thread to post under. Omitted = this turn's own thread."},
        },
        "required": ["roomId", "text"],
    },
}


def _post(url: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as res:
        return json.loads(res.read() or b"{}")


def _normalize_room(raw: str) -> str | None:
    room = (raw or "").removeprefix("lightbulb:").strip().lower()
    return room if _UUID_RE.match(room) else None


def _send_room_message(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx:
        return NO_TURN
    room = _normalize_room(str(args.get("roomId", "")))
    if not room:
        return "Invalid roomId: expected a room UUID, optionally prefixed 'lightbulb:'."
    text = str(args.get("text") or "").strip()
    if not text:
        return "text is required."
    thread = str(args.get("threadRootMessageId") or "").strip() or None
    own_thread = str(ctx.get("threadRootMessageId") or "").strip() or None  # same normalization both sides
    if room == _normalize_room(str(ctx.get("roomId") or "")) and thread in (None, own_thread):
        return OWN_THREAD_REFUSAL
    body = {
        "eventId": f"{ctx['runId']}:{ctx['agentId']}:tool:{next(_counter)}",
        "roomId": room, "agentId": ctx["agentId"], "text": text, "replyToMessageId": None,
        "threadRootMessageId": args.get("threadRootMessageId") or ctx.get("threadRootMessageId"),
        "runId": ctx["runId"],
    }
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/messaging/agent-messages/{ctx['ownerId']}"
    try:
        return json.dumps(_post(url, ctx["hookToken"], body))
    except Exception as exc:  # noqa: BLE001
        return f"Lightbulb room send failed: {type(exc).__name__}"


# Task 12: hire_suggestion tool
# parseHireSuggestion (HireCard.tsx:38-78) matches this string EXACTLY and returns null on
# any mismatch, which degrades the card to a plain tool pill with no error anywhere. It is
# deliberate rebrand residue — never "lightbulb.hire_suggestion".
HIRE_KIND = "isol8.hire_suggestion"
_HIRE_OPTIONAL = ("name", "summary", "tags", "price_cents")

HIRE_SCHEMA = {
    "name": "hire_suggestion",
    "description": "Offer the owner one or more marketplace agents to hire. Renders as a hire card.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "One sentence: why these, now."},
            "listings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "listing_id": {"type": "string"}, "slug": {"type": "string"},
                        "name": {"type": "string"}, "summary": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "price_cents": {"type": "integer"},
                    },
                    "required": ["listing_id", "slug"],
                },
            },
        },
        "required": ["reason", "listings"],
    },
}


def _hire_suggestion(args: dict, **_kw) -> str:
    listings = []
    for entry in args.get("listings") or []:
        if not isinstance(entry, dict) or not entry.get("listing_id") or not entry.get("slug"):
            continue  # the card drops the WHOLE payload on one bad item; drop the item instead
        listings.append({"listing_id": entry["listing_id"], "slug": entry["slug"],
                         **{k: entry[k] for k in _HIRE_OPTIONAL if entry.get(k) is not None}})
    if not listings:
        return "hire_suggestion needs at least one listing with a listing_id and a slug."
    return json.dumps({"kind": HIRE_KIND, "reason": str(args.get("reason") or ""), "listings": listings})


# Task 13: Redaction patterns
# Hermes's built-in prefix list has no pcp_ entry, and it writes sessions/request_dump_*.json
# on EVERY API error with no config switch. The registry is additive-only: a plugin can
# over-redact, never weaken a built-in (redact.py:1230-1234).
REDACTION_PATTERNS = (r"pcp_[A-Za-z0-9_-]{16,}",)


def _register_redaction(register_fn) -> int:
    try:
        return int(register_fn(list(REDACTION_PATTERNS), source="lightbulb"))
    except Exception:  # noqa: BLE001 — a redaction hiccup must never take the plugin down
        return 0


def _register_redaction_default() -> int:
    from agent.redact import register_redaction_patterns

    return _register_redaction(register_redaction_patterns)


# Task 14: lightbulb_local_sessions tool
SESSIONS_UNAVAILABLE = "Lightbulb local sessions unavailable"
SESSIONS_MAX_BYTES = 2 * 1024 * 1024
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
# Strict per-action allowlist: an unexpected param is a bug, not something to ignore.
_ACTION_PARAMS = {
    "resources": {"action"},
    "read": {"action", "sessionId", "afterSeq", "limit"},
    "send": {"action", "sessionId", "requestId", "expectedGeneration", "text", "kind"},
    "receipt": {"action", "sessionId", "requestId"},
}

SESSIONS_SCHEMA = {
    "name": "lightbulb_local_sessions",
    "description": "Read and steer the shared terminal sessions humans in this room have "
                   "attached. Available only inside the turn the request arrived on.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["resources", "read", "send", "receipt"]},
            "sessionId": {"type": "string"}, "requestId": {"type": "string"},
            "expectedGeneration": {"type": "string"}, "text": {"type": "string", "maxLength": 8192},
            "afterSeq": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "kind": {"type": "string", "enum": ["send", "interrupt"]},
        },
        "required": ["action"],
    },
}


def _call_sessions(method: str, url: str, token: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method=method)
    with urllib.request.urlopen(request, timeout=10) as res:
        if res.status // 100 != 2 or res.status in (301, 302, 303, 307, 308):
            raise RuntimeError(f"status {res.status}")
        return json.loads(res.read(SESSIONS_MAX_BYTES + 1)[:SESSIONS_MAX_BYTES] or b"{}")


def _local_sessions(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx or not ctx.get("localSessionToken"):
        return NO_TURN
    action = args.get("action")
    allowed = _ACTION_PARAMS.get(action)
    if allowed is None:
        return "action must be one of resources|read|send|receipt."
    extra = set(args) - allowed
    if extra:
        return f"Params not allowed for action {action}: {', '.join(sorted(extra))}."
    base = f"{ctx['apiUrl'].rstrip('/')}/api/v1/messaging/local-session-agent"
    token, sid = ctx["localSessionToken"], str(args.get("sessionId") or "")
    if action != "resources" and not _UUID_RE.match(sid):
        return "Invalid sessionId: expected a session UUID."
    try:
        if action == "resources":
            return json.dumps(_call_sessions("GET", f"{base}/resources", token))
        if action == "read":
            after = max(0, int(args.get("afterSeq") or 0))
            limit = min(100, max(1, int(args.get("limit") or 50)))
            return json.dumps(_call_sessions(
                "GET", f"{base}/sessions/{sid}/events?afterSeq={after}&limit={limit}", token))
        request_id = str(args.get("requestId") or "")
        if not _OPAQUE_RE.match(request_id):
            return "requestId is required and must be an opaque token."
        if action == "receipt":
            return json.dumps(_call_sessions("GET", f"{base}/sessions/{sid}/commands/{request_id}", token))
        generation = str(args.get("expectedGeneration") or "")
        text = str(args.get("text") or "").strip()
        if not _OPAQUE_RE.match(generation):
            return "expectedGeneration is required (take it from the resources listing)."
        if not text:
            return "text is required."
        # kind passthrough: the backend has accepted "interrupt" all along; only the baked
        # plugin's hard-coded "send" kept it from the agent (report 03 §6).
        body = {"requestId": request_id, "expectedGeneration": generation,
                "kind": args.get("kind") or "send", "text": text}
        return json.dumps(_call_sessions("POST", f"{base}/sessions/{sid}/commands", token, body))
    except Exception:  # noqa: BLE001 — no status, no URL, no detail reaches the agent
        return SESSIONS_UNAVAILABLE


def _log_memory_full(tool_name: str = "", result=None, **_kw) -> None:
    """Hermes refuses a built-in memory write over its char budget and tells only the model."""
    if tool_name == "memory" and isinstance(result, str) and "would exceed the limit" in result:
        try:
            usage = json.loads(result).get("usage", "")
        except ValueError:
            usage = ""
        logger.warning("hermes file memory full, write refused (%s)", usage)


def register(ctx) -> None:
    """Register all Lightbulb tools and redaction patterns with the Hermes context."""
    _register_redaction_default()
    ctx.register_hook("pre_tool_call", browser_control.pre_tool_call)
    ctx.register_hook("post_tool_call", browser_control.post_tool_call)
    ctx.register_hook("post_tool_call", _log_memory_full)
    ctx.register_hook("transform_tool_result", website_logins.transform_tool_result)
    ctx.register_tool("send_room_message", "lightbulb", SEND_SCHEMA, _send_room_message,
                      description=SEND_SCHEMA["description"], emoji="💬")
    ctx.register_tool("hire_suggestion", "lightbulb", HIRE_SCHEMA, _hire_suggestion,
                      description=HIRE_SCHEMA["description"], emoji="🧑‍💼")
    ctx.register_tool("lightbulb_local_sessions", "lightbulb", SESSIONS_SCHEMA, _local_sessions,
                      description=SESSIONS_SCHEMA["description"], emoji="🖥️")
    from . import desktop

    ctx.register_tool("lightbulb_desktop", "lightbulb", desktop.SCHEMA, desktop.handle,
                      description=desktop.SCHEMA["description"], emoji="🖥️")
