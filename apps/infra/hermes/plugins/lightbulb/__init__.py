"""The `lightbulb` Hermes tool plugin: the room tools OpenClaw gave us for free."""
from __future__ import annotations

import itertools
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
# A voice run's answer is spoken; nothing it does may land in the Live room's timeline.
VOICE_ROOM_REFUSAL = ("This is a Live voice request: your answer is spoken, and nothing is "
                      "posted to this room.")

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
    if not ctx or ctx.get("family") == "paperclip":  # a Paperclip run's answer is its result
        return NO_TURN
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
ASK_REFUSALS = {
    "LOCAL_SESSION_ASK_PENDING": "your ask on this session still waits for its owner; do not ask again.",
    "LOCAL_SESSION_ASK_DECLINED": "the session's owner declined your message; do not ask again in this run.",
}
_OPAQUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
# Strict per-action allowlist: an unexpected param is a bug, not something to ignore.
_ACTION_PARAMS = {
    "resources": {"action"},
    "read": {"action", "sessionId", "afterSeq", "limit"},
    "send": {"action", "sessionId", "requestId", "expectedGeneration", "expectedTurnId", "text", "kind"},
    "receipt": {"action", "sessionId", "requestId"},
}

SESSIONS_SCHEMA = {
    "name": "lightbulb_local_sessions",
    "description": "Read and message people's shared Claude Code/Codex sessions. Check a "
                   "session's agentSend first: \"direct\" goes straight in (the session of the "
                   "person who woke you, or its owner allows it); \"ask\" returns "
                   "awaiting_approval and its owner chooses Send or Decline -- ask once, say so "
                   "in your reply, never re-ask under a new requestId; declined is final for this "
                   "run. kind \"interrupt\" stops the session's turn: no text, and expectedTurnId = "
                   "the session's activeTurnId (also needed to steer a busy session).",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["resources", "read", "send", "receipt"]},
            "sessionId": {"type": "string"}, "requestId": {"type": "string"},
            "expectedGeneration": {"type": "string"}, "text": {"type": "string", "maxLength": 8192},
            "expectedTurnId": {"type": "string", "description": "The session's activeTurnId."},
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
        if not _OPAQUE_RE.match(generation):
            return "expectedGeneration is required (take it from the resources listing)."
        kind = args.get("kind") or "send"
        if kind not in ("send", "interrupt"):
            return "kind must be send or interrupt."
        body = {"requestId": request_id, "expectedGeneration": generation, "kind": kind}
        turn_id = str(args.get("expectedTurnId") or "")
        if turn_id:
            if not _OPAQUE_RE.match(turn_id):
                return "expectedTurnId must be the session's activeTurnId."
            body["expectedTurnId"] = turn_id
        elif kind == "interrupt":
            return "An interrupt needs expectedTurnId (the session's activeTurnId from resources or read)."
        if kind == "send":  # the backend refuses text on an interrupt
            text = str(args.get("text") or "").strip()
            if not text:
                return "text is required."
            body["text"] = text
        return json.dumps(_call_sessions("POST", f"{base}/sessions/{sid}/commands", token, body))
    except urllib.error.HTTPError as exc:
        # The two ask bounds steer the agent's next step, so their code (only) reaches it.
        try:
            code = json.loads(exc.read(4096) or b"{}").get("code")
        except Exception:  # noqa: BLE001
            code = None
        if code in ASK_REFUSALS:
            return f"{code}: {ASK_REFUSALS[code]}"
        return SESSIONS_UNAVAILABLE
    except Exception:  # noqa: BLE001 — no status, no URL, no detail reaches the agent
        return SESSIONS_UNAVAILABLE


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
_ISSUE_FILE_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")  # paperclip-api.sh ISSUE_ID_RE
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
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


def _bind_issue_to_room(session_id: str, issue_id: str) -> tuple[Path, bytes | None] | None:
    """paperclip-api.sh's write_binding, same file and format: the issue belongs to this room
    conversation, so turn._route_paperclip_run lands the approval's wake here. Returns the path
    and its exact prior bytes (None: there was no file), so a failed create can put it back."""
    from bridge import turn

    room = _scope_secret("LIGHTBULB_ROOM_SESSION_KEY") or session_id
    if not turn.ROOM_SESSION_RE.match(room or "") or not _ISSUE_FILE_RE.match(issue_id):
        return None
    path = turn_context._home() / turn.ISSUE_BINDINGS_DIR / f"{issue_id}.json"
    try:
        prior = path.read_bytes()
    except FileNotFoundError:
        prior = None
    except OSError:
        return None
    binding = {"sessionKey": room, "boundAt": int(time.time())}
    if session_id != room:
        binding["origin"] = session_id
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(binding), encoding="utf-8")
    except OSError:
        return None
    return path, prior


def _attached_run_id(issue_id: str) -> str | None:
    """paperclip-api.sh's attached_run_id: Paperclip's run the bridge attached to this room
    conversation for the issue (turn.ISSUE_RUN_FILE under .lightbulb-turn/)."""
    from bridge import turn

    if not _ISSUE_FILE_RE.match(issue_id):
        return None
    try:
        run = (turn_context._home() / turn_context.DIRNAME
               / turn.ISSUE_RUN_FILE.format(issue=issue_id)).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return run if _RUN_ID_RE.match(run) else None


def _request_approval(args: dict, *, session_id: str = "", **_kw) -> str:
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
        # Which project the plan is for: the backend matches an idea's plan to its start by this.
        # Only when every linked task agrees: a plan spanning projects names none of them.
        project_ids = {i.get("projectId") for i in issues}
        if len(project_ids) == 1 and None not in project_ids:
            payload["projectIds"] = sorted(project_ids)
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
    # Bind BEFORE the create: an approval can be answered at once, and its wake must find the room.
    bound = [b for b in (_bind_issue_to_room(session_id, i["id"]) for i in issues) if b]
    try:
        approval = _paperclip_call("POST", f"{base}/api/companies/{company_id}/approvals", key, body, run_id)
    except Exception as exc:  # noqa: BLE001
        for path, prior in bound:  # put back another room's binding byte for byte
            try:
                if prior is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(prior)
            except OSError:
                pass
        if isinstance(exc, urllib.error.HTTPError):
            return f"Approval not created: {_http_error_text(exc)}."
        return f"Approval not created: {type(exc).__name__}."
    result = {"approvalId": approval["id"], "type": body["type"],
              "link": f"{_app_url()}/{company.get('issuePrefix')}/approvals/{approval['id']}"}
    # The linked approval is the review path an agent-authored in_review needs, so it goes first.
    not_moved = []
    for issue in issues:
        try:
            _paperclip_call("PATCH", f"{base}/api/issues/{issue['id']}", key, {"status": "in_review"},
                            run_id or _attached_run_id(issue["id"]))
        except urllib.error.HTTPError as exc:
            not_moved.append(f"{issue.get('identifier') or issue['id']} ({_http_error_text(exc)})")
        except Exception as exc:  # noqa: BLE001
            not_moved.append(f"{issue.get('identifier') or issue['id']} ({type(exc).__name__})")
    if not_moved:
        result["notMovedToInReview"] = not_moved
    return json.dumps(result)


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
    ctx.register_tool("hire_suggestion", "lightbulb", HIRE_SCHEMA, _hire_suggestion,
                      description=HIRE_SCHEMA["description"], emoji="🧑‍💼")
    ctx.register_tool("request_approval", "lightbulb", APPROVAL_SCHEMA, _request_approval,
                      description=APPROVAL_SCHEMA["description"], emoji="✅")
    ctx.register_tool("lightbulb_local_sessions", "lightbulb", SESSIONS_SCHEMA, _local_sessions,
                      description=SESSIONS_SCHEMA["description"], emoji="🖥️")
    ctx.register_tool("lightbulb_read_rooms", "lightbulb", READ_ROOMS_SCHEMA, _read_rooms,
                      description=READ_ROOMS_SCHEMA["description"], emoji="🗂️")
    ctx.register_tool("lightbulb_voice_profile", "lightbulb", VOICE_PROFILE_SCHEMA, _voice_profile,
                      description=VOICE_PROFILE_SCHEMA["description"], emoji="🎙️")
    from . import desktop

    ctx.register_tool("lightbulb_desktop", "lightbulb", desktop.SCHEMA, desktop.handle,
                      description=desktop.SCHEMA["description"], emoji="🖥️")
