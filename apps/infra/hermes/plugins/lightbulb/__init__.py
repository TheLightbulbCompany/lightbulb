"""The `lightbulb` Hermes tool plugin: the room tools OpenClaw gave us for free."""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import turn_context
from bridge import browser_control, network, website_logins

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
# A voice run's answer is spoken; nothing it does may land in the Live room's timeline.
VOICE_ROOM_REFUSAL = ("This is a Live voice request: your answer is spoken, and nothing is "
                      "posted to this room.")

# Task 11: send_room_message tool
SEND_SCHEMA = {
    "name": "send_room_message",
    "description": "Post a message to a Lightbulb room you sit in. In a room turn, your reply "
                   "to the thread you are answering is posted for you when the turn ends, so "
                   "never send that here; use this for other rooms or threads. In a task run, "
                   "any room or thread is yours to post in.",
    "parameters": {
        "type": "object",
        "properties": {
            "roomId": {"type": "string", "description": "Room id, optionally prefixed 'lightbulb:'."},
            "text": {"type": "string"},
            "threadRootMessageId": {"type": "string",
                                    "description": "Thread to post under. Omitted = this room turn's own thread, or the top level in a task run."},
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


ASK_SESSION_SCHEMA = {
    "name": "ask_session",
    "description": "Ask one of the workspace's laptop sessions to do something, under the person who asked you. Held for that session's person when no rule allows it.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "text": {"type": "string"},
            "intent": {"type": "string"},
        },
        "required": ["session_id", "text", "intent"],
    },
}


def _ask_session(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx:
        return NO_TURN
    body = {
        "sessionId": args.get("session_id"), "text": args.get("text"), "intent": args.get("intent"),
        # The origin is the turn's own trigger, never the agent's pick; the backend checks its author.
        "originMessageId": (ctx.get("messageId") or ctx.get("threadRootMessageId")) if ctx.get("roomId") else None,
        "roomId": ctx.get("roomId"), "agentId": ctx["agentId"], "runId": ctx.get("runId"),
    }
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/a2a/{ctx['ownerId']}/ask-session"
    try:
        return json.dumps(_post(url, ctx["hookToken"], body))
    except Exception as exc:  # noqa: BLE001
        return f"Lightbulb ask_session failed: {type(exc).__name__}"

def _normalize_room(raw: str) -> str | None:
    room = (raw or "").removeprefix("lightbulb:").strip().lower()
    return room if _UUID_RE.match(room) else None


def _send_room_message(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx:
        return NO_TURN
    # A Paperclip run's answer is its issue result, never a room post, so any room or thread is
    # the agent's own call; its post is a routine post (hop 1), with no room-run id to bind to.
    paperclip = ctx.get("family") == "paperclip"
    room = _normalize_room(str(args.get("roomId", "")))
    if not room:
        return "Invalid roomId: expected a room UUID, optionally prefixed 'lightbulb:'."
    if room == _normalize_room(str(ctx.get("voiceRoomId") or "")):
        return VOICE_ROOM_REFUSAL
    text = str(args.get("text") or "").strip()
    if not text:
        return "text is required."
    thread = str(args.get("threadRootMessageId") or "").strip() or None
    own_thread = str(ctx.get("threadRootMessageId") or "").strip() or None  # same normalization both sides
    if not paperclip and room == _normalize_room(str(ctx.get("roomId") or "")) and thread in (None, own_thread):
        return OWN_THREAD_REFUSAL
    body = {
        # A run-less turn (a network message) has no runId: its eventId is the message id.
        "eventId": f"{ctx.get('runId') or ctx.get('eventId') or uuid.uuid4()}:{ctx['agentId']}:tool:{next(_counter)}",
        "roomId": room, "agentId": ctx["agentId"], "text": text, "replyToMessageId": None,
        "threadRootMessageId": thread if paperclip else thread or ctx.get("threadRootMessageId"),
        **({"runId": ctx["runId"]} if not paperclip and ctx.get("runId") else {}),
    }
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/messaging/agent-messages/{ctx['ownerId']}"
    try:
        return json.dumps(_post(url, ctx["hookToken"], body))
    except Exception as exc:  # noqa: BLE001
        return f"Lightbulb room send failed: {type(exc).__name__}"


# lightbulb_voice_profile tool: rename / voice notes, only from inside a voice run.
VOICE_PROFILE_SCHEMA = {
    "name": "lightbulb_voice_profile",
    "description": "Only when the person speaking asked you to change your name or how you talk; "
                   "confirm a rename, replacement or clear before calling.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["rename", "add_note", "replace_notes", "clear_notes"]},
            "name": {"type": "string", "description": "New name (rename only)."},
            "text": {"type": "string", "description": "Note text (add_note / replace_notes)."},
        },
        "required": ["action"],
    },
}
VOICE_ONLY = "lightbulb_voice_profile works only inside a Live voice request."


def _voice_profile(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx or not ctx.get("voiceRoomId"):
        return VOICE_ONLY
    # agentId/runId come from the turn, never the model: the backend resolves the speaker
    # from the run's VOICERUN row.
    body = {"agentId": ctx["agentId"], "runId": ctx["runId"], "action": args.get("action"),
            **{k: args[k] for k in ("name", "text") if args.get(k) is not None}}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/agents/voice-profile/{ctx['ownerId']}"
    try:
        return json.dumps(_post(url, ctx["hookToken"], body))
    except urllib.error.HTTPError as exc:
        # C8 codes (name_taken, rate_limited, ...) steer the skill's reply, so surface them.
        try:
            err = json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            err = {}
        detail = err.get("detail") if isinstance(err, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else detail
        return f"Voice profile update failed: {code or (err.get('error') if isinstance(err, dict) else None) or exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"Voice profile update failed: {type(exc).__name__}"


# suggest_idea tool (spec 2026-10-14 §4.4): an idea for THIS company joins the workspace's Ideas
# list (never a chat card). The suggesting agent and workspace come from the turn, never the model.
SUGGEST_IDEA_SCHEMA = {
    "name": "suggest_idea",
    "description": "Add an idea to this company's Ideas list (the person sees it there, not in chat). "
                   "Use it only when you notice a concrete, company-specific job during real work "
                   "(\"14 reviews have no reply\"), never a generic or routine one. You will own it.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The outcome, short: 'Answer the 14 open Shopify reviews'."},
            "description": {"type": "string",
                            "description": "Why it matters for this company and what you will do, in one or "
                                           "two sentences. Promise the action, never the result."},
            "deliverables": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                             "description": "Up to three short lines the person will have."},
            "size": {"type": "string", "enum": ["project", "task", "quick"],
                     "description": "project: many steps over days; task: one errand; quick: answered in one go."},
            "emoji": {"type": "string", "description": "One emoji that pictures the outcome, shown beside the title."},
            "kind": {"type": "string", "enum": ["app"], "description": "Set to app when the idea is a small app."},
            "app_kind": {"type": "string", "description": "With kind app: what sort of app.",
                         "enum": ["landing_page", "content_site", "form", "internal_tool", "dashboard",
                                  "customer_portal", "product_with_payments", "automation"]},
            "origin": {"type": "string", "maxLength": 120,
                       "description": "With kind app: one line on what keeps coming back."},
        },
        "required": ["title", "description", "deliverables", "size"],
    },
}
SUGGEST_NO_TURN = "suggest_idea works only inside a Lightbulb turn."


def _suggest_idea(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx or not ctx.get("agentId") or not ctx.get("hookToken"):
        return SUGGEST_NO_TURN
    body = {
        "agentId": ctx["agentId"],
        "title": str(args.get("title") or "").strip(),
        "why": str(args.get("description") or "").strip(),
        "deliverables": [str(d).strip() for d in (args.get("deliverables") or []) if str(d).strip()][:3],
        "size": args.get("size"),
    }
    if str(args.get("emoji") or "").strip():
        body["emoji"] = str(args["emoji"]).strip()
    for key, field in (("kind", "kind"), ("app_kind", "appKind"), ("origin", "origin")):
        if str(args.get(key) or "").strip():
            body[field] = str(args[key]).strip()
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/ideas/suggestions/{ctx['ownerId']}"
    try:
        result = _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        return f"Idea not added: HTTP {exc.code}."
    except Exception as exc:  # noqa: BLE001
        return f"Idea not added: {type(exc).__name__}."
    status = result.get("status")
    if status == "added":
        return "Added to the Ideas list. Do not post it in chat; the person sees it under the 💡."
    if status == "duplicate":
        return "Already in the Ideas list: nothing added."
    if status == "cap":
        return "The Ideas list already has 10 suggestions: nothing added."
    return json.dumps(result)


# request_purchase tool (Link Agent Wallet, dark): the agent can only ASK. The backend checks the
# owner's purchase limits, files the Needs-you card and, after the owner's click, Link's own
# approval. The agent and workspace come from the turn; the reply is {requestId, status} and
# nothing else -- no URL, token or card ever reaches the model.
PURCHASE_SCHEMA = {
    "name": "request_purchase",
    "description": "Ask the owner to approve buying something for this company. You do not pay: the "
                   "owner approves it in Lightbulb and in Link first, and you are never given a card. "
                   "Use it only for a real purchase your task needs, tied to the issue you are working on.",
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {"type": "string", "description": "Who is paid, e.g. 'Acme Hosting'."},
            "url": {"type": "string", "description": "The merchant's web address (the page you would buy on)."},
            "amount": {"type": "number", "description": "Total in US dollars, e.g. 35.00."},
            "why": {"type": "string", "description": "What it is and why the company needs it, one or two sentences. The owner reads this."},
            "issue_id": {"type": "string", "description": "The issue this purchase is for."},
        },
        "required": ["merchant", "url", "amount", "why", "issue_id"],
    },
}
PURCHASE_NO_TURN = "request_purchase works only inside a Lightbulb turn."


def _request_purchase(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx or not ctx.get("agentId") or not ctx.get("hookToken"):
        return PURCHASE_NO_TURN
    body = {"agentId": ctx["agentId"], "merchant": args.get("merchant"), "url": args.get("url"),
            "amount": args.get("amount"), "why": args.get("why"), "issueId": args.get("issue_id")}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/link-wallet/{ctx['ownerId']}/requests"
    try:
        result = _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "Purchases are not switched on for this workspace."
        try:
            code = (json.loads(exc.read() or b"{}").get("detail") or {}).get("code")
        except Exception:  # noqa: BLE001
            code = None
        return f"Purchase not requested: {code or f'HTTP {exc.code}'}."
    except Exception as exc:  # noqa: BLE001
        return f"Purchase not requested: {type(exc).__name__}."
    return json.dumps({"requestId": result.get("requestId"), "status": result.get("status")})


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


# Task 14: lightbulb_local_sessions tool -- read-only: an agent messages a session with lightbulb_network
SESSIONS_UNAVAILABLE = "Lightbulb local sessions unavailable"
SESSIONS_MAX_BYTES = 2 * 1024 * 1024
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
# Strict per-action allowlist: an unexpected param is a bug, not something to ignore.
_ACTION_PARAMS = {
    "resources": {"action"},
    "read": {"action", "sessionId", "afterSeq", "limit"},
}

SESSIONS_SCHEMA = {
    "name": "lightbulb_local_sessions",
    "description": "Read people's shared Claude Code/Codex sessions: \"resources\" lists them, \"read\" "
                   "pages one's events. To message a session, use lightbulb_network.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["resources", "read"]},
            "sessionId": {"type": "string"},
            "afterSeq": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["action"],
    },
}


def _call_sessions(method: str, url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method=method)
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
        return "action must be one of resources|read."
    extra = set(args) - allowed
    if extra:
        return f"Params not allowed for action {action}: {', '.join(sorted(extra))}."
    base = f"{ctx['apiUrl'].rstrip('/')}/api/v1/messaging/local-session-agent"
    token, sid = ctx["localSessionToken"], str(args.get("sessionId") or "")
    try:
        if action == "resources":
            return json.dumps(_call_sessions("GET", f"{base}/resources", token))
        if not _UUID_RE.match(sid):
            return "Invalid sessionId: expected a session UUID."
        after = max(0, int(args.get("afterSeq") or 0))
        limit = min(100, max(1, int(args.get("limit") or 50)))
        return json.dumps(_call_sessions("GET", f"{base}/sessions/{sid}/events?afterSeq={after}&limit={limit}", token))
    except Exception:  # noqa: BLE001 — no status, no URL, no detail reaches the agent
        return SESSIONS_UNAVAILABLE


# ---- lightbulb_network: messages to people's computers and their agents ----------------------
NETWORK_SCHEMA = {
    "name": "lightbulb_network",
    "description": "Messages to people's computers and their agents on the Lightbulb network (not "
                   "rooms). Also how you message a shared coding session (lightbulb_local_sessions only reads them). "
                   "action \"directory\" finds sessions (optional query); \"send\" sends text to a "
                   "session name and returns a receipt; \"receipt\" reads a sent message's state by its "
                   "request_id.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["send", "receipt", "directory"]},
            "to": {"type": "string", "description": "The session name to send to (from directory). "
                                                  "Leave it out to answer a message: reply_to alone "
                                                  "sends to its sender."},
            "text": {"type": "string", "maxLength": 8192},
            "reply_to": {"type": "string", "description": "The message id this answers."},
            "request_id": {"type": "string", "description": "Omit on send (one is made for you); "
                                                            "required for receipt."},
            "query": {"type": "string", "maxLength": 200},
            "status": {"type": "string", "enum": ["done", "failed", "cancelled"],
                       "description": "With reply_to: how an assignment ended."},
            "reason": {"type": "string", "maxLength": 1000},
            "include_ended": {"type": "boolean", "description": "directory: also list sessions "
                                                                "that ended in the past week."},
        },
        "required": ["action"],
    },
}
NETWORK_OFF = "The Lightbulb network is off for this workspace right now. Say so; do not retry in this turn."
NETWORK_NO_SESSION = ("You have no session on the Lightbulb network, so you cannot send network "
                      "messages. Say so.")
_net_lock = threading.Lock()


def _next_request_id(session_id: str) -> str:
    """``<runId or eventId>.net.<n>`` (the engine takes only ``[A-Za-z0-9._-]``), n counted in the turn's own context file: the bridge
    writes a fresh file for each turn, so a re-run turn re-sends with the same ids. The file is
    re-read under the lock, so parallel sends in one turn never share an id."""
    with _net_lock:
        ctx = turn_context.read(session_id) or {}
        base = ctx.get("runId") or ctx.get("eventId") or ctx.get("netBase") or str(uuid.uuid4())
        n = int(ctx.get("netCount") or 0) + 1
        turn_context.write(turn_context._home(), session_id, {**ctx, "netCount": n, "netBase": base})
    return f"{base}.net.{n}"


def _network(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx:
        return NO_TURN
    action = args.get("action")
    try:
        if action == "directory":
            answer = network.ask("directory", {"query": str(args.get("query") or "")})
            # Ended sessions stay listed a week; a turn gets them only when it asks.
            if not args.get("include_ended") and isinstance(answer.get("sessions"), list):
                answer["sessions"] = [s for s in answer["sessions"] if s.get("status") != "ended"]
            return json.dumps(answer)
        if action == "receipt":
            request_id = str(args.get("request_id") or "")
            if not _OPAQUE_RE.match(request_id):
                return "request_id is required (the one send returned)."
            return json.dumps(network.ask("receipt", {"request_id": request_id}))
        if action != "send":
            return "action must be one of send|receipt|directory."
        if not network.running():
            return NETWORK_OFF
        sender = network.session_for_agent(ctx["agentId"])  # never a model argument
        if not sender:
            return NETWORK_NO_SESSION
        to, text = str(args.get("to") or "").strip(), str(args.get("text") or "").strip()
        if not text or not (to or args.get("reply_to")):
            return "text, and to or reply_to, are required."
        request_id = str(args.get("request_id") or "") or _next_request_id(session_id)
        if not _OPAQUE_RE.match(request_id):
            return "request_id must be an opaque token."
        status = str(args.get("status") or "")
        if status and status not in ("done", "failed", "cancelled"):
            return "status must be one of done|failed|cancelled."
        if status and not args.get("reply_to"):
            return "status needs reply_to (the assignment's requestId)."
        body = {"from": sender, "text": text, "request_id": request_id}
        if status:
            body["status"] = status
        if args.get("reason"):
            body["reason"] = str(args["reason"])
        if to:  # without it the engine sends the reply to the sender of reply_to
            body["to"] = to
        if args.get("reply_to"):
            body["reply_to"] = str(args["reply_to"])
        return json.dumps(network.ask("send", body))
    except network.NetworkOff:
        return NETWORK_OFF
    except network.OpError as exc:
        return f"Lightbulb network {action} refused: {exc.code}" + (f" ({exc.reason})" if exc.reason else "")
    except Exception as exc:  # noqa: BLE001 -- the class only, never a traceback
        return f"Lightbulb network {action} failed: {type(exc).__name__}"


# ---- read_rooms: other rooms, with the access of the person who asked --------------------------
READ_ROOMS_SCHEMA = {
    "name": "lightbulb_read_rooms",
    "description": "Find and read the workspace's other channels, DMs and group chats, with exactly the "
                   "access of the person who asked you (what they could open themselves). action "
                   "\"find\" lists rooms (optional query matches a room or member name); \"read\" returns "
                   "a room's latest messages, or those after afterSeq. In a Google Meet you read only "
                   "channels the whole workspace is in (never a DM, group chat or narrower channel), and "
                   "everyone on the call hears what you say. With no signed-in person behind this turn (a routine) "
                   "nothing is readable: say so.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["find", "read"]},
            "query": {"type": "string", "maxLength": 200},
            "roomId": {"type": "string"},
            "afterSeq": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["action"],
    },
}
READ_ROOMS_REFUSED = ("No rooms can be read for this turn: no signed-in person asked for it, or they "
                      "cannot open that room. Say so; do not guess what was said there.")


def _read_rooms(args: dict, *, session_id: str = "", **_kw) -> str:
    """The hosted MCP's find_rooms/read_messages for an agent turn, authorized by the turn's grant
    (its requester is the backend's, never the agent's): routers/messaging.py agent_reader."""
    ctx = turn_context.read(session_id)
    if not ctx or not ctx.get("localSessionToken"):
        return NO_TURN
    base = f"{ctx['apiUrl'].rstrip('/')}/api/v1/messaging/agent-read"
    limit = args.get("limit")
    try:
        if args.get("action") == "find":
            query = urllib.parse.urlencode({"query": str(args.get("query") or ""), "limit": int(limit or 30)})
            return json.dumps(_call_sessions("GET", f"{base}/rooms?{query}", ctx["localSessionToken"]))
        if args.get("action") == "read":
            room_id = str(args.get("roomId") or "")
            if not _UUID_RE.match(room_id):
                return "Invalid roomId: take it from action find."
            params = {"room_id": room_id, "limit": int(limit or 25)}
            if args.get("afterSeq") is not None:
                params["after_seq"] = max(0, int(args["afterSeq"]))
            url = f"{base}/messages?{urllib.parse.urlencode(params)}"
            return json.dumps(_call_sessions("GET", url, ctx["localSessionToken"]))
        return "action must be find or read."
    except urllib.error.HTTPError as exc:
        return READ_ROOMS_REFUSED if exc.code in (403, 404) else "Lightbulb rooms are unavailable right now."
    except Exception:  # noqa: BLE001 -- no status, no URL, no detail reaches the agent
        return "Lightbulb rooms are unavailable right now."


# ---- workspace: act in the workspace with the asker's rights ------------------------------------
WORKSPACE_SCHEMA = {
    "name": "lightbulb_workspace",
    "description": "Act in the Lightbulb workspace with exactly the access of the person who asked you: room "
                   "members (add_member, remove_member, set_listen_mode), create_room, update_room "
                   "(rename or archive), pin and unpin a message, search_messages and search_sessions. Never "
                   "change Lightbulb data any other way.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "add_member", "set_listen_mode", "remove_member", "create_room",
                "update_room", "pin", "unpin", "search_messages", "search_sessions"]},
            "roomId": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["action"],
    },
}


def _workspace(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx or not ctx.get("localSessionToken"):
        return NO_TURN
    if not isinstance(args.get("args") or {}, dict):
        return "args must be an object"
    act_args = dict(args.get("args") or {})
    if args.get("action") == "create_room" and not (act_args.get("clientRoomId") or act_args.get("client_room_id")):
        act_args["clientRoomId"] = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"lightbulb-agent-act:{ctx.get('runId') or ctx.get('eventId')}:create_room:"
            f"{json.dumps(act_args, sort_keys=True)}"))
    body = {"action": args.get("action"), "roomId": args.get("roomId"), "args": act_args}
    try:
        return json.dumps(_post(f"{ctx['apiUrl'].rstrip('/')}/api/v1/messaging/agent-act",
                                ctx["localSessionToken"], body))
    except urllib.error.HTTPError as exc:
        try:
            problem = json.loads(exc.read())
            return f"Refused ({problem['code']}): {problem['message']}"
        except Exception:  # noqa: BLE001 -- an unreadable body is just the status
            return f"Lightbulb workspace action refused: {exc.code}"
    except Exception:  # noqa: BLE001 -- no status, no URL, no detail reaches the agent
        return "Lightbulb workspace is unavailable right now."


# ---- request_approval: a person's decision as a real Paperclip approval ------------------------
# Observed dev 2026-10-14 (Walker Studio): told to raise an approval through
# `paperclip-api.sh raw POST .../approvals` with a jq body, the managed model asked in chat or
# assigned the person a task instead, and /teams/approvals stayed empty. One named tool is what
# the model reaches for. Auth is the agent's own Paperclip key from the profile's secret scope,
# the same credential paperclip-api.sh uses (turn._secret_scope; Hermes copies the ContextVar
# into tool workers).
APPROVAL_SCHEMA = {
    "name": "request_approval",
    "description": "Ask a person to decide something: creates an approval in their Needs you with "
                   "Approve and Reject buttons; approving it wakes you with the answer. Use it for "
                   "every sign-off (a plan, prices, spend, a launch, a public post, a vendor), never a "
                   "task assigned to the person or a question in chat. Linked tasks move to in_review. "
                   "Returns the approval's link: give it to the person in your reply.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The decision, e.g. 'Approve the two-week launch plan'."},
            "summary": {"type": "string",
                        "description": "What the person needs to decide it, in two or three sentences, "
                                       "with the document link if there is one."},
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "Tasks this decision is about, as identifiers like WAL-6 (or ids)."},
            "recommendedAction": {"type": "string", "description": "What you will do if approved."},
            "companyPlan": {"type": "boolean",
                            "description": "True only when this is your plan or strategy for the whole company."},
        },
        "required": ["title", "summary"],
    },
}
APPROVAL_NO_KEY = "request_approval is unavailable: this turn has no Paperclip credentials."


def _paperclip_call(method: str, url: str, key: str, body: dict | None = None, run_id: str | None = None):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    if run_id:  # a live heartbeat run's checkout lock needs its id (fork issues.ts)
        headers["X-Paperclip-Run-Id"] = run_id
    request = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as res:
        return json.loads(res.read() or b"{}")


def _scope_secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(name) or "").strip()
    except Exception:  # noqa: BLE001 -- unimportable, or no scope installed (fails closed)
        return ""


def _app_url() -> str:
    try:
        facts = json.loads((turn_context._home() / "lightbulb-facts.json").read_text(encoding="utf-8"))
        return str(facts.get("app_url") or "").rstrip("/")
    except Exception:  # noqa: BLE001 -- a relative link still opens in the app
        return ""


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    try:
        err = json.loads(exc.read(4096) or b"{}")
        detail = err.get("error") if isinstance(err, dict) else None
    except Exception:  # noqa: BLE001
        detail = None
    return f"HTTP {exc.code}" + (f": {str(detail)[:200]}" if detail else "")


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _with_from_section(description: str) -> str:
    """paperclip-api.sh's ``with_from_section``: a task moved from a room turn records the room
    (and the message and person that asked), so its own run knows where to report back."""
    from bridge import turn

    key = _scope_secret("LIGHTBULB_ROOM_SESSION_KEY")  # set for room turns only (turn.py room_env)
    if not turn.ROOM_SESSION_RE.match(key) or "## From" in description.splitlines():
        return description
    room = key.rsplit(":", 1)[-1]
    text = f"## From\nFiled from room `{room}`."
    if _SAFE_ID.match(message := _scope_secret("LIGHTBULB_ROOM_MESSAGE_ID")):
        text += f" The message that asked: /chat?panel=chat&room={room}&message={message}"
    if _SAFE_ID.match(asker := _scope_secret("LIGHTBULB_ROOM_REQUESTER_ID")):
        text += f" Asked by user `{asker}`."
    return f"{description}\n\n{text}" if description else text


def _request_approval(args: dict, **_kw) -> str:
    base, key = _scope_secret("PAPERCLIP_API_URL").rstrip("/"), _scope_secret("PAPERCLIP_API_KEY")
    if not base or not key:
        return APPROVAL_NO_KEY
    run_id = _scope_secret("PAPERCLIP_RUN_ID") or None
    title, summary = str(args.get("title") or "").strip(), str(args.get("summary") or "").strip()
    if not title or not summary:
        return "title and summary are required."
    refs = [str(t).strip() for t in (args.get("tasks") or []) if str(t).strip()]
    try:
        me = _paperclip_call("GET", f"{base}/api/agents/me", key)
        issues = []
        for ref in refs:  # GET /api/issues/:id resolves WAL-6 identifiers too (fork router.param)
            try:
                issues.append(_paperclip_call("GET", f"{base}/api/issues/{urllib.parse.quote(ref)}", key))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return f"Approval not created: task {ref} was not found."
                raise
        company_id = me["companyId"]
        # Read before the create: a failure after it would read "not created" and invite a duplicate.
        company = _paperclip_call("GET", f"{base}/api/companies/{company_id}", key)
        # approve_ceo_strategy is the ROOT CEO's company plan only: by role, never by name.
        root_ceo = me.get("role") == "ceo" and not me.get("reportsTo")
        ceo_plan = bool(args.get("companyPlan")) and root_ceo
        payload = {"title": title, "summary": summary}
        if ceo_plan:
            payload["plan"] = summary
        if str(args.get("recommendedAction") or "").strip():
            payload["recommendedAction"] = str(args["recommendedAction"]).strip()
        body = {"type": "approve_ceo_strategy" if ceo_plan else "request_board_approval",
                "requestedByAgentId": me["id"], "payload": payload}
        if issues:
            body["issueIds"] = [issue["id"] for issue in issues]
    except urllib.error.HTTPError as exc:
        return f"Approval not created: {_http_error_text(exc)}."
    except Exception as exc:  # noqa: BLE001
        return f"Approval not created: {type(exc).__name__}."
    try:
        approval = _paperclip_call("POST", f"{base}/api/companies/{company_id}/approvals", key, body, run_id)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, urllib.error.HTTPError):
            return f"Approval not created: {_http_error_text(exc)}."
        return f"Approval not created: {type(exc).__name__}."
    result = {"approvalId": approval["id"], "type": body["type"],
              "link": f"{_app_url()}/{company.get('issuePrefix')}/approvals/{approval['id']}"}
    # The linked approval is the review path an agent-authored in_review needs, so it goes first.
    not_moved = []
    for issue in issues:
        try:
            change = {"status": "in_review"}
            described = _with_from_section(issue.get("description") or "")
            if described != (issue.get("description") or ""):
                change["description"] = described
            _paperclip_call("PATCH", f"{base}/api/issues/{issue['id']}", key, change, run_id)
        except urllib.error.HTTPError as exc:
            not_moved.append(f"{issue.get('identifier') or issue['id']} ({_http_error_text(exc)})")
        except Exception as exc:  # noqa: BLE001
            not_moved.append(f"{issue.get('identifier') or issue['id']} ({type(exc).__name__})")
    if not_moved:
        result["notMovedToInReview"] = not_moved
    return json.dumps(result)


# ---- ask_person: a question, or a blocked report, as a Paperclip interaction ---------------------
# Paperclip's issue-thread interactions already reach Activity's Needs you ("has a question for
# you", core/services/activity.py render_interaction) and wake the assignee when answered. Agents
# only had a hand-built raw API call for them, so a blocked agent left a comment nobody read
# (G1, 30 Oct: ISOA-867 sat 8 days). One named tool, like request_approval above.
ASK_PERSON_SCHEMA = {
    "name": "ask_person",
    "description": "Ask a person a question on your task: it shows in their Needs you, and their answer "
                   "wakes you with it. Use it instead of guessing between options, and instead of a "
                   "comment when you are blocked (blocked: true also marks the task blocked). Give 2 to 4 "
                   "likely answers, recommended first, or none for a typed answer. One open question per "
                   "task: a new one replaces your earlier unanswered one, so ask everything in one. In a "
                   "room conversation, ask in your reply instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "One sentence."},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "2 to 4 likely answers, recommended first."},
            "details": {"type": "string", "description": "What the person needs to know to answer."},
            "task": {"type": "string",
                     "description": "The task this is about, like WAL-6. Defaults to the task you are running."},
            "blocked": {"type": "boolean", "description": "True when the work cannot continue until the answer."},
        },
        "required": ["question"],
    },
}
ASK_IN_REPLY = "No task to ask on: in a room conversation, ask in your reply instead."


def _ask_person(args: dict, **_kw) -> str:
    question = str(args.get("question") or "").strip()
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
    if not question:
        return "question is required."
    if options and not 2 <= len(options) <= 4:
        return "Give 2 to 4 options, or none for a typed answer."
    ref = str(args.get("task") or "").strip() or _scope_secret("PAPERCLIP_TASK_ID")
    run_id = _scope_secret("PAPERCLIP_RUN_ID")
    if not ref or not run_id:  # the fork's create requires the agent's run (routes requireAgentRunId)
        return ASK_IN_REPLY
    base, key = _scope_secret("PAPERCLIP_API_URL").rstrip("/"), _scope_secret("PAPERCLIP_API_KEY")
    if not base or not key:
        return "ask_person is unavailable: this turn has no Paperclip credentials."
    choices = ([{"id": f"o{i}", "label": label[:1000]} for i, label in enumerate(options, 1)]
               or [{"id": "answer", "label": "Type your answer", "freeText": True}])
    prompt = {"id": "q1", "prompt": question[:4000], "selectionMode": "single", "required": True,
              "options": choices}
    if str(args.get("details") or "").strip():
        prompt["helpText"] = str(args["details"]).strip()[:4000]
    body = {"kind": "ask_user_questions", "resolverPolicy": "board_only",
            "continuationPolicy": "wake_assignee", "title": question[:240],
            "payload": {"version": 1, "supersedeOnUserComment": True, "questions": [prompt]}}
    try:
        try:
            issue = _paperclip_call("GET", f"{base}/api/issues/{urllib.parse.quote(ref)}", key)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return f"Question not created: task {ref} was not found."
            raise
        me = _paperclip_call("GET", f"{base}/api/agents/me", key)
        company = _paperclip_call("GET", f"{base}/api/companies/{me['companyId']}", key)
        # The fork replays a key only for the same run and an identical body, and 409s anything else
        # under it (issue-thread-interactions.ts isEquivalentCreateRequest), so both are in the key.
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]
        body["idempotencyKey"] = f"ask:{issue['id']}:{run_id}:{digest}"
        made = _paperclip_call("POST", f"{base}/api/issues/{issue['id']}/interactions", key, body, run_id)
        if made.get("status", "pending") != "pending":  # a replay of a card already answered or expired
            return f"Question not created: this exact question is already {made.get('status')} on this task."
        blocked = False
        if args.get("blocked"):  # after the card exists: a failed ask never leaves the task parked
            change = {"status": "blocked", "unblockDescriptor": {
                "owner": {"agentId": me["id"]},  # an agent may name only itself (heartbeat.md)
                "action": f"Resume when the person answers: {question}"[:500]}}
            try:
                _paperclip_call("PATCH", f"{base}/api/issues/{issue['id']}", key, change, run_id)
                blocked = True
            except urllib.error.HTTPError:
                pass  # the question is out; the result says the task is not blocked
    except urllib.error.HTTPError as exc:
        return f"Question not created: {_http_error_text(exc)}."
    except Exception as exc:  # noqa: BLE001
        return f"Question not created: {type(exc).__name__}."
    ident = issue.get("identifier") or issue["id"]
    return json.dumps({"interactionId": made.get("id"), "blocked": blocked,
                       "link": f"{_app_url()}/{company.get('issuePrefix')}/issues/{ident}"})


def _log_memory_full(tool_name: str = "", result=None, **_kw) -> None:
    """Hermes refuses a built-in memory write over its char budget and tells only the model."""
    if tool_name == "memory" and isinstance(result, str) and "would exceed the limit" in result:
        try:
            usage = json.loads(result).get("usage", "")
        except ValueError:
            usage = ""
        logger.warning("hermes file memory full, write refused (%s)", usage)


# ---- session_search: this conversation's own history only ------------------------------------
# One agent profile's state.db records every room, DM, group DM, mail thread, call and Paperclip
# run that agent is in, and Hermes's session_search reads all of it -- plus any other agent's
# state.db through `profile=` (tools/session_search_tool.py:329-339, 472-505). Anyone who can talk
# to the agent anywhere could have it recite a DM they are not in, or a room they were removed
# from (around the deliveryKey rotation that cuts their live access). Hermes has no option for
# this, so the plugin narrows the tool to the lineage of the session it is called from: the
# room's own compacted or compression-split history (Hermes already hides the live part, which is
# in context). A spoken turn runs on its call's own transcript but belongs to its room, so it also
# reaches that room's lineage: recall on a call is recall in the room, as in chat (one code path,
# 10-09). Cross-conversation recall needs a product ruling first.
RECALL_SCOPE = ("In Lightbulb, session_search reaches only this conversation's own earlier "
                "history. Other rooms, DMs, calls, mail threads and other agents' sessions are "
                "private to the people in them.")


class _LineageDB:
    """A SessionDB whose FTS and title lookups answer only from the given lineages."""

    def __init__(self, db, roots: set, lineage) -> None:
        self._db, self._roots, self._lineage = db, roots, lineage

    def __getattr__(self, name):
        return getattr(self._db, name)

    def search_messages(self, *args, **kwargs):
        return [r for r in self._db.search_messages(*args, **kwargs)
                if self._lineage(self._db, r["session_id"]) in self._roots]

    def resolve_session_by_title(self, title):
        sid = self._db.resolve_session_by_title(title)
        return sid if sid and self._lineage(self._db, sid) in self._roots else None


def _recall_roots(tool, db, current_session_id: str) -> set:
    """The lineages a turn may recall: its own, and on a spoken turn a signed-in member asked, its
    room's (the room's Hermes session is keyed like any room turn's). Both the room and the member
    come from the bridge's run registry, never from the turn context file, which the agent's own
    shell can rewrite. A Meet guest's turn keeps to the call: guests are not the room's members."""
    roots = {tool._resolve_lineage(db, current_session_id)}
    from bridge import turn

    room_key = turn.verified_call_room_key(current_session_id)
    if room_key:
        roots.add(tool._resolve_lineage(db, room_key))
    return roots


def _scope_session_search() -> None:
    """Replace Hermes's session_search function in place. Both of its callers look it up at
    call time: the inline executor (agent/inline_tool_executors.py `_call_tool`, getattr on the
    module) and the registry handler (a lambda over the module global)."""
    try:
        from tools import session_search_tool as tool
    except ImportError:  # outside the Hermes image
        return
    hermes_search = tool.session_search
    if getattr(hermes_search, "lightbulb_scoped", False):
        return

    def session_search(query="", role_filter=None, limit=3, db=None, current_session_id=None,
                       session_id=None, around_message_id=None, window=5, sort=None, profile=None,
                       detail="adaptive", **hermes_args):
        # **hermes_args: arguments a newer Hermes adds (`after`, prod 10-09: a TypeError cost a spoken turn
        # two rounds) pass straight through; scope is the db, the lineage checks and profile, all fixed here.
        session_id = str(session_id).strip() if session_id else None
        if (db is None or not current_session_id or (profile and str(profile).strip())
                or (session_id and "/" in session_id) or not (str(query or "").strip() or session_id)):
            return tool.tool_error(RECALL_SCOPE, success=False)  # browse lists every conversation
        roots = _recall_roots(tool, db, current_session_id)
        if session_id and tool._resolve_lineage(db, session_id) not in roots:
            return tool.tool_error(RECALL_SCOPE, success=False)
        return hermes_search(query=query, role_filter=role_filter, limit=limit,
                             db=_LineageDB(db, roots, tool._resolve_lineage),
                             current_session_id=current_session_id, session_id=session_id,
                             around_message_id=around_message_id, window=window, sort=sort,
                             profile=None, detail=detail, **hermes_args)

    session_search.lightbulb_scoped = True
    tool.session_search = session_search


def register(ctx) -> None:
    """Register all Lightbulb tools and redaction patterns with the Hermes context."""
    _register_redaction_default()
    _scope_session_search()
    from bridge import turn

    ctx.register_hook("pre_tool_call", browser_control.pre_tool_call)
    ctx.register_hook("pre_tool_call", turn.memory_pre_tool_call)  # no shared notes from a private DM
    ctx.register_hook("post_tool_call", browser_control.post_tool_call)
    ctx.register_hook("post_tool_call", _log_memory_full)
    ctx.register_hook("transform_tool_result", website_logins.transform_tool_result)
    ctx.register_tool("send_room_message", "lightbulb", SEND_SCHEMA, _send_room_message,
                      description=SEND_SCHEMA["description"], emoji="💬")
    ctx.register_tool("ask_session", "lightbulb", ASK_SESSION_SCHEMA, _ask_session,
                      description=ASK_SESSION_SCHEMA["description"], emoji="🔐")
    ctx.register_tool("suggest_idea", "lightbulb", SUGGEST_IDEA_SCHEMA, _suggest_idea,
                      description=SUGGEST_IDEA_SCHEMA["description"], emoji="💡")
    ctx.register_tool("hire_suggestion", "lightbulb", HIRE_SCHEMA, _hire_suggestion,
                      description=HIRE_SCHEMA["description"], emoji="🧑‍💼")
    ctx.register_tool("request_purchase", "lightbulb", PURCHASE_SCHEMA, _request_purchase,
                      description=PURCHASE_SCHEMA["description"], emoji="💳")
    ctx.register_tool("request_approval", "lightbulb", APPROVAL_SCHEMA, _request_approval,
                      description=APPROVAL_SCHEMA["description"], emoji="✅")
    ctx.register_tool("ask_person", "lightbulb", ASK_PERSON_SCHEMA, _ask_person,
                      description=ASK_PERSON_SCHEMA["description"], emoji="❓")
    ctx.register_tool("lightbulb_local_sessions", "lightbulb", SESSIONS_SCHEMA, _local_sessions,
                      description=SESSIONS_SCHEMA["description"], emoji="🖥️")
    ctx.register_tool("lightbulb_network", "lightbulb", NETWORK_SCHEMA, _network,
                      description=NETWORK_SCHEMA["description"], emoji="🌐")
    ctx.register_tool("lightbulb_read_rooms", "lightbulb", READ_ROOMS_SCHEMA, _read_rooms,
                      description=READ_ROOMS_SCHEMA["description"], emoji="🗂️")
    ctx.register_tool("lightbulb_workspace", "lightbulb", WORKSPACE_SCHEMA, _workspace,
                      description=WORKSPACE_SCHEMA["description"], emoji="🏢")
    ctx.register_tool("lightbulb_voice_profile", "lightbulb", VOICE_PROFILE_SCHEMA, _voice_profile,
                      description=VOICE_PROFILE_SCHEMA["description"], emoji="🎙️")
    from . import desktop

    ctx.register_tool("lightbulb_desktop", "lightbulb", desktop.SCHEMA, desktop.handle,
                      description=desktop.SCHEMA["description"], emoji="🖥️")
