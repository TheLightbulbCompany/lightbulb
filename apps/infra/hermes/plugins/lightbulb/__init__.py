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


#: tasks-schema.json `namespaces.task` (tests/test_router_tools.py checks it): the task room id's namespace.
TASK_NS = uuid.UUID("c10f836a-c762-50eb-b28b-a8a6af634983")
TASK_ROOM_ONLY = ("In a task run you post only in the task's own room; Lightbulb posts your result "
                  "to the people who asked.")


def task_room_id(session_key: str) -> str | None:
    """The task's hidden room (contracts §6) from a Worker session key `agent:<id>:lightbulb:task:<taskId>`."""
    task_id = session_key.rsplit(":task:", 1)[-1] if ":lightbulb:task:" in session_key else None
    return str(uuid.uuid5(TASK_NS, f"room:{task_id}")) if task_id else None


def _send_room_message(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx:
        return NO_TURN
    # A Paperclip run's answer is its issue result, never a room post (a task run's likewise), so any
    # room or thread is the agent's own call; its post is a routine post (hop 1), with no room-run id.
    paperclip = ctx.get("family") in ("paperclip", "task")
    room = _normalize_room(str(args.get("roomId", "")))
    if not room:
        return "Invalid roomId: expected a room UUID, optionally prefixed 'lightbulb:'."
    if ctx.get("family") == "task" and room != task_room_id(str(ctx.get("sessionKey") or "")):
        return TASK_ROOM_ONLY
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

# start_work: work asked aloud becomes a task (lane A, contracts-v2 §14.1). Offered only on a Live voice turn;
# the ids come from the turn, never from the model.
START_WORK_SCHEMA = {
    "name": "start_work",
    "description": "Hand off work asked for on this call so it goes on after the call: it becomes a task and "
                   "its result lands in the room. Say one short line that it has started.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "brief": {"type": "string", "description": "What to do, self-contained: the person's words and what they asked for."},
            "specialist": {"type": "string", "description": "A teammate id from the roster, or omit."},
        },
        "required": ["title", "brief"],
    },
}
START_WORK_ONLY = "start_work works only inside a Live voice request."
VOICE_WORK_TOOLSET = "lightbulb_voice_work"  # == bridge.turn.VOICE_WORK_TOOLSET; only a call enables it


def _start_work(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not ctx or not ctx.get("voiceRoomId"):
        return START_WORK_ONLY
    args = args or {}
    for field in ("title", "brief"):
        if not isinstance(args.get(field), str) or not args[field].strip():
            return json.dumps({"error": f"{field} is required"})
    if len(args["title"]) > 200:
        return json.dumps({"error": "title is at most 200 characters"})
    body = {"runId": ctx["runId"], "callId": ctx.get("callId"), "roomId": ctx["voiceRoomId"],
            "title": args["title"].strip(), "brief": args["brief"].strip()}
    if isinstance(args.get("specialist"), str) and args["specialist"]:
        body["specialist"] = args["specialist"]
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/automations/{ctx['ownerId']}/voice-start"
    try:
        _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        code = detail.get("code") if isinstance(detail, dict) else detail
        return f"Not started: {code or exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"Not started: {type(exc).__name__}"
    return "started"


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
    if ctx.get("taskId"):  # a Worker run: the backend lets one request file at most 3 ideas
        body["taskId"] = ctx["taskId"]
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
    if status == "run_cap":
        return "This request already filed 3 ideas: nothing added. File no more."
    return json.dumps(result)


# request_purchase tool (Link Agent Wallet, dark): the agent can only ASK. The backend checks the
# owner's purchase limits after the task's Needs-you question, then Link's own approval. The agent
# and workspace come from the turn; the reply is {requestId, status} and nothing else -- no URL,
# token or card ever reaches the model.
PURCHASE_SCHEMA = {
    "name": "request_purchase",
    "description": "Ask the owner to approve buying something for this company. You do not pay: the "
                   "owner approves it in Lightbulb and in Link first, and you are never given a card. "
                   "Use it only for a real purchase your task needs.",
    "parameters": {
        "type": "object",
        "properties": {
            "merchant": {"type": "string", "description": "Who is paid, e.g. 'Acme Hosting'."},
            "url": {"type": "string", "description": "The merchant's web address (the page you would buy on)."},
            "amount": {"type": "number", "description": "Total in US dollars, e.g. 35.00."},
            "why": {"type": "string", "description": "What it is and why the company needs it, one or two sentences. The owner reads this."},
            "issue_id": {"type": "string", "description": "Optional, ignored: the purchase is tied to your current task."},
        },
        "required": ["merchant", "url", "amount", "why"],
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


# ---- request_approval: a person's decision on a task run, as a Question ------------------------
# One named tool is what the model reaches for (dev 2026-10-14). Off a task run it refuses and the agent asks in
# its reply (approval modes ruling, 6 Nov).
APPROVAL_SCHEMA = {
    "name": "request_approval",
    "description": "On a Lightbulb task, ask a person to decide something: a question in their Needs you "
                   "with Allow and Deny; their answer wakes you. Use it for every sign-off (a plan, prices, "
                   "spend, a launch, a public post, a vendor), never a question in chat. These stop for "
                   "approval by themselves, so don't ask before them: lightbulb-mail.sh / lightbulb-text.sh "
                   "send, lightbulb app publish, gh pr merge, request_purchase and lightbulb_network send. "
                   "Any OTHER send, spend or publish still needs request_approval first, once: gog gmail send "
                   "or calendar invites, a QuickBooks send_invoice, a browser or computer checkout, form "
                   "submit or post, git push, vercel deploy, npm publish, gh release, gh pr create, or a raw "
                   "curl with credentials. In a room conversation, ask in your reply instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The decision, e.g. 'Approve the two-week launch plan'."},
            "summary": {"type": "string",
                        "description": "What the person needs to decide it, in two or three sentences, "
                                       "with the document link if there is one."},
            "subkind": {"type": "string", "enum": ["tool", "spend", "purchase", "provider_consent", "send"],
                        "description": "What kind of decision this is (who may answer follows from it)."},
        },
        "required": ["title", "summary"],
    },
}
APPROVAL_TASK_ONLY = "request_approval works only on a Lightbulb task. Ask the person in your reply instead."
TASK_SUBKINDS = ("tool", "spend", "purchase", "provider_consent", "send")  # context-free ones (spec 05 §3.3)
TASK_STALE_RUN = "This run is no longer the task's current attempt: stop and end your turn."
TASK_CLOSED = "This task was stopped: stop and end your turn."


def _task_run(ctx: dict | None) -> bool:
    """A super-agent Worker run: the turn speaks for an attempt (taskId + runSecret), never a Router or room turn."""
    return bool(ctx and ctx.get("taskId") and ctx.get("runSecret") and ctx.get("mode") != "router")


def _detail(exc: urllib.error.HTTPError) -> str:
    """FastAPI's `detail` (a string or a dict with `code`), or ''."""
    try:
        err = json.loads(exc.read(4096) or b"{}")
    except Exception:  # noqa: BLE001
        return ""
    detail = err.get("detail") if isinstance(err, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("code")
    return detail if isinstance(detail, str) else ""  # a 422's list of field errors is not a code


def _task_question(ctx: dict, *, kind: str, subkind: str, title: str, text: str, options: list[dict],
                   blocking: bool, key: str) -> str:
    """A question or approval on a super-agent task run (Lane Q): one Question row on the task, through the
    backend's internal ask route under the run's identity. Never Paperclip. A blocking ask marks the turn to end
    `waiting_approval` (turn_context.note_waiting); the answer comes back as input to the next attempt."""
    tool_call_id = "ask-" + hashlib.sha256(key.encode()).hexdigest()[:16]  # the same ask again replays
    body = {"taskId": ctx.get("taskId"), "runId": ctx.get("runId"), "attempt": ctx.get("attempt"),
            "runSecret": ctx.get("runSecret"), "toolCallId": tool_call_id, "kind": kind, "subkind": subkind,
            "title": title[:200], "text": text[:500], "options": options, "blocking": blocking}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/runtime/{ctx['ownerId']}/questions"
    try:
        made = _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        detail = _detail(exc)
        if detail == "stale_run":
            return TASK_STALE_RUN
        if detail == "task_closed":
            return TASK_CLOSED
        return f"Question not created: HTTP {exc.code}" + (f" ({detail})." if detail else ".")
    except Exception as exc:  # noqa: BLE001
        return f"Question not created: {type(exc).__name__}."
    qid = str(made.get("questionId") or "")
    label = str(made.get("waitingLabel") or "Waiting on a person")
    if made.get("state") not in (None, "open"):  # a replayed ask whose question is already closed
        return f"Question {qid} was already {made.get('state')}; its answer is in your input, if any. Carry on."
    if blocking:
        turn_context.note_waiting(turn_context._home(), str(ctx.get("runId")), qid)
        return f"{label}. Question {qid} is open. End your turn now; the answer arrives as input."
    return f"{label}. Question {qid} is open; the answer arrives as input when they answer. Carry on meanwhile."


def _request_approval(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if _task_run(ctx):  # a super-agent task run: one Question row, never Paperclip
        title, summary = str(args.get("title") or "").strip(), str(args.get("summary") or "").strip()
        if not title or not summary:
            return "title and summary are required."
        subkind = str(args.get("subkind") or "")
        return _task_question(
            ctx, kind="approval", subkind=subkind if subkind in TASK_SUBKINDS else "tool", title=title, text=summary,
            options=[{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}], blocking=True,
            key=f"approval:{title}:{summary}",
        )
    return APPROVAL_TASK_ONLY


# ---- ask_person: a question, or a blocked report, as a Question on the task run ------------------
# A blocked agent used to leave a comment nobody read (G1, 30 Oct). One named tool, like request_approval above.
ASK_PERSON_SCHEMA = {
    "name": "ask_person",
    "description": "Ask a person a question on your task: it shows in their Needs you, and their answer "
                   "wakes you with it. Use it instead of guessing between options, and instead of a "
                   "comment when you are blocked (blocked: true ends your turn until the answer). Give 2 to 4 "
                   "likely answers, recommended first, or none for a typed answer. Ask everything in one. In a "
                   "room conversation, ask in your reply instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "One sentence."},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "2 to 4 likely answers, recommended first."},
            "details": {"type": "string", "description": "What the person needs to know to answer."},
            "blocked": {"type": "boolean", "description": "True when the work cannot continue until the answer."},
        },
        "required": ["question"],
    },
}
ASK_IN_REPLY = "No task to ask on: in a room conversation, ask in your reply instead."

# watch / unwatch (lane A, Task 12): the task sleeps on a timer and wakes you when something finishes.
WATCH_SCHEMA = {
    "name": "watch",
    "description": "Ask your task to watch something that finishes later (a PR's checks, a URL that should start "
                   "answering) and end your turn: the task wakes you with 'Watch met: …' when it does. Past "
                   "give_up_hours it asks the person whether to keep watching.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["pr_checks", "url"]},
            "repo": {"type": "string", "description": "pr_checks: owner/name"},
            "number": {"type": "integer", "description": "pr_checks: the PR number"},
            "url": {"type": "string", "description": "url: the address that should answer 2xx"},
            "every_min": {"type": "integer", "description": "Minutes between checks (5 to 1440). Default 30."},
            "give_up_hours": {"type": "number", "description": "Hours until the task asks instead. Default 24, at most 336."},
        },
        "required": ["kind"],
    },
}
UNWATCH_SCHEMA = {
    "name": "unwatch",
    "description": "Stop a watch you started (its id came back from watch).",
    "parameters": {"type": "object", "properties": {"watch_id": {"type": "string"}}, "required": ["watch_id"]},
}
WATCH_ONLY = "watch works only on a Lightbulb task run."


def _watch(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not _task_run(ctx):
        return WATCH_ONLY
    args = args or {}
    kind = args.get("kind")
    if kind == "pr_checks":
        target = {"repo": str(args.get("repo") or ""), "number": args.get("number")}
        if not target["repo"] or not isinstance(target["number"], int):
            return json.dumps({"error": "pr_checks needs repo (owner/name) and number"})
    elif kind == "url":
        target = {"url": str(args.get("url") or "")}
        if not target["url"].startswith(("https://", "http://")):
            return json.dumps({"error": "url must start with https:// or http://"})
    else:
        return json.dumps({"error": "kind is pr_checks or url"})
    body = {"runId": ctx.get("runId"), "attempt": ctx.get("attempt"), "runSecret": ctx.get("runSecret"),
            "kind": kind, "target": target}
    if isinstance(args.get("every_min"), int):
        body["everyMin"] = args["every_min"]
    hours = args.get("give_up_hours")
    if isinstance(hours, (int, float)) and hours > 0:
        import datetime as _dt

        hours = min(float(hours), 14 * 24)  # the backend's cap: a watch is for something that finishes soon
        body["giveUpAt"] = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)).isoformat()
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/tasks/{ctx['ownerId']}/{ctx['taskId']}/watches"
    try:
        made = _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        detail = _detail(exc)
        return f"Watch not started: HTTP {exc.code}" + (f" ({detail})." if detail else ".")
    except Exception as exc:  # noqa: BLE001
        return f"Watch not started: {type(exc).__name__}."
    return json.dumps({"watchId": made.get("watchId"), "giveUpAt": made.get("giveUpAt"),
                       "note": "Watching. End your turn; you will be woken with 'Watch met: …'."})


def _unwatch(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not _task_run(ctx):
        return WATCH_ONLY
    watch_id = str((args or {}).get("watch_id") or "").strip()
    if not watch_id:
        return json.dumps({"error": "watch_id is required"})
    body = {"runId": ctx.get("runId"), "attempt": ctx.get("attempt"), "runSecret": ctx.get("runSecret"),
            "watchId": watch_id}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/tasks/{ctx['ownerId']}/{ctx['taskId']}/watches/remove"
    try:
        _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        return f"Watch not removed: HTTP {exc.code}."
    except Exception as exc:  # noqa: BLE001
        return f"Watch not removed: {type(exc).__name__}."
    return "stopped"


# task_note (contracts-v2 §6): a Worker posts only in its own task's room. Its toolset is stripped from every turn
# and re-added only for a super-agent Worker run (bridge.turn TASK_TOOLSET); the handler checks the run again.
TASK_TOOLSET = "lightbulb_task"  # == bridge.turn.TASK_TOOLSET
TASK_NOTE_SCHEMA = {
    "name": "task_note",
    "description": "Post a short progress note in this task's own conversation (what you found, what you are doing "
                   "next). People following the task read it; your final answer is posted for you, so never "
                   "repeat it here. This is the only place you post while working on a task.",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The note, one or two sentences."}},
        "required": ["text"],
    },
}
TASK_NOTE_ONLY = "task_note works only on a Lightbulb task run."


def _task_note(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not _task_run(ctx):
        return TASK_NOTE_ONLY
    text = str((args or {}).get("text") or "").strip()
    if not text:
        return "text is required."
    body = {"taskId": ctx.get("taskId"), "runId": ctx.get("runId"), "attempt": ctx.get("attempt"),
            "runSecret": ctx.get("runSecret"), "text": text[:2000]}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/runtime/{ctx['ownerId']}/notes"
    try:
        _post(url, ctx["hookToken"], body)
    except urllib.error.HTTPError as exc:
        detail = _detail(exc)
        if detail == "stale_run":
            return TASK_STALE_RUN
        return f"Note not posted: HTTP {exc.code}" + (f" ({detail})." if detail else ".")
    except Exception as exc:  # noqa: BLE001
        return f"Note not posted: {type(exc).__name__}."
    return "Posted in the task's conversation."


# front_desk: a phone caller's thread acts only for that caller (governance P2, the caller case). Its toolset is
# enabled only on a run whose request principal is "caller" (bridge.turn CALLER_TOOLSETS); the backend resolves the
# call from the attempt and reuses the phone's own routes, which already keep a caller to their own bookings.
FRONT_DESK_TOOLSET = "lightbulb_front_desk"  # == bridge.turn.FRONT_DESK_TOOLSET
FRONT_DESK_SCHEMA = {
    "name": "front_desk",
    "description": "Act for the caller this thread is for, at this business only. availability: open start times on "
                   "a date (date, optional service). book: book a start time (start, service, name). my_bookings: "
                   "this caller's own bookings. move: move one of them (start, new_start). cancel: cancel one "
                   "(start). message: take a message for the owner (text, optional name, callback_number). Times "
                   "are the business's local time, YYYY-MM-DDTHH:MM.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["availability", "book", "my_bookings", "move", "cancel", "message"]},
            "date": {"type": "string", "description": "YYYY-MM-DD (availability)"},
            "service": {"type": "string"},
            "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
            "new_start": {"type": "string", "description": "YYYY-MM-DDTHH:MM (move)"},
            "name": {"type": "string", "description": "the caller's name"},
            "text": {"type": "string", "description": "the message (message)"},
            "callback_number": {"type": "string"},
        },
        "required": ["action"],
    },
}


def _front_desk(args: dict, *, session_id: str = "", **_kw) -> str:
    ctx = turn_context.read(session_id)
    if not _task_run(ctx):
        return "front_desk works only on a caller's thread."
    body = {**{k: v for k, v in (args or {}).items() if v not in (None, "")},  # identity last: never the model's
            "taskId": ctx.get("taskId"), "runId": ctx.get("runId"), "attempt": ctx.get("attempt"),
            "runSecret": ctx.get("runSecret")}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/runtime/{ctx['ownerId']}/front-desk"
    try:
        return json.dumps(_post(url, ctx["hookToken"], body))
    except urllib.error.HTTPError as exc:
        detail = _detail(exc)
        return f"Not done: HTTP {exc.code}" + (f" ({detail})." if detail else ".")
    except Exception as exc:  # noqa: BLE001
        return f"Not done: {type(exc).__name__}."


def _ask_person(args: dict, *, session_id: str = "", **_kw) -> str:
    question = str(args.get("question") or "").strip()
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
    if not question:
        return "question is required."
    if options and not 2 <= len(options) <= 4:
        return "Give 2 to 4 options, or none for a typed answer."
    ctx = turn_context.read(session_id)
    if _task_run(ctx):  # a super-agent task run: one Question row, never Paperclip
        details = str(args.get("details") or "").strip()
        return _task_question(
            ctx, kind="question", subkind="question", title=question, text=details,
            options=[{"id": f"o{i}", "label": label[:200]} for i, label in enumerate(options, 1)],
            blocking=bool(args.get("blocked")), key=f"question:{question}:{details}",
        )
    return ASK_IN_REPLY


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
    ctx.register_tool("watch", "lightbulb", WATCH_SCHEMA, _watch,
                      description=WATCH_SCHEMA["description"], emoji="👀")
    ctx.register_tool("unwatch", "lightbulb", UNWATCH_SCHEMA, _unwatch,
                      description=UNWATCH_SCHEMA["description"], emoji="👀")
    ctx.register_tool("task_note", TASK_TOOLSET, TASK_NOTE_SCHEMA, _task_note,
                      description=TASK_NOTE_SCHEMA["description"], emoji="📝")
    ctx.register_tool("front_desk", FRONT_DESK_TOOLSET, FRONT_DESK_SCHEMA, _front_desk,
                      description=FRONT_DESK_SCHEMA["description"], emoji="📅")
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
    ctx.register_tool("start_work", VOICE_WORK_TOOLSET, START_WORK_SCHEMA, _start_work,
                      description=START_WORK_SCHEMA["description"], emoji="🚀")
    ctx.register_tool("lightbulb_voice_profile", "lightbulb", VOICE_PROFILE_SCHEMA, _voice_profile,
                      description=VOICE_PROFILE_SCHEMA["description"], emoji="🎙️")
    from . import desktop, router

    router.register(ctx)
    ctx.register_tool("lightbulb_desktop", "lightbulb", desktop.SCHEMA, desktop.handle,
                      description=desktop.SCHEMA["description"], emoji="🖥️")
