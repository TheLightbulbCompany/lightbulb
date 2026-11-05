"""Proactive rules (contracts-v2 §18, spec 12): the pure part. No I/O.

A rule is an automation row (`SCHED#`) with a `match` scope and a `mode` (off / observe / auto). In wave 2 every
mode records only: a match writes "would have done X" and nothing runs. `AUTO_AVAILABLE` is the one switch wave 3
flips. Rows are the camelCase dicts `automations_repo` returns.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.services.jev import _clip

AUTO_AVAILABLE = False

RULE_CONFIDENT_P = 0.7
RULES_PER_ROOM = 5
RULE_TEXT_MAX = 600
MIN_MATCHES_TO_PROMOTE = 5
MATCHES_KEPT = 50
DID_IT_YOURSELF_WINDOW_H = 24
SPEC_WS_DAILY_USD = 20

# Built-in row defaults (founder, 4 Nov): budget per run, time limit, daily cap per rule.
INVITE_PREP = {"budgetPerRunUsd": 0.25, "timeLimitS": 180, "dailyCapUsd": 10.0}
REPRO_FIX = {"budgetPerRunUsd": 2.00, "timeLimitS": 1200, "dailyCapUsd": 10.0}

# Exact copy, spec 12 §3.3.
RULE_INSTRUCTIONS = (
    "The team wrote these rules for work an assistant may do in this room without being asked. Does the latest "
    "message clearly meet the 'when' condition of exactly one rule? Judge the latest message in its conversation. "
    "Pick none unless the match is clear: a rule is not a topic, it is a condition."
)
RULE_NONE = (
    "None: no rule's condition is clearly met, the message only mentions the topic, is chatter, a joke, "
    "a question about the rule itself, or something already handled in the conversation"
)

_LIVE_MODES = {"observe", "auto"}


def rules_for_room(rules: list[dict], room_id: str) -> list[dict]:
    """The active, switched-on room rules scoped to this room, oldest first, at most `RULES_PER_ROOM`."""
    found = [
        r
        for r in rules
        if r.get("mode") in _LIVE_MODES
        and r.get("status") == "active"
        and "room" in ((r.get("match") or {}).get("sources") or [])
        and room_id in ((r.get("match") or {}).get("roomIds") or [])
    ]
    return sorted(found, key=lambda r: str(r.get("createdAt") or ""))[:RULES_PER_ROOM]


def jev_question(rules: list[dict]) -> dict:
    """The one extra `rule` question on Jev's per-message call (`_clip` may add an ellipsis, hence -1)."""
    criteria = {r["scheduleId"]: _clip(r.get("policyText") or "", RULE_TEXT_MAX - 1) for r in rules[:RULES_PER_ROOM]}
    return {"type": "choice", "instructions": RULE_INSTRUCTIONS, "criteria": criteria | {"none": RULE_NONE}}


def parse_match(answers: dict, rule_ids: set[str]) -> tuple[str | None, float | None]:
    """(sched id, p) when Jev confidently picks a known rule over none; any malformed answer is no match."""
    try:
        rule = answers.get("rule")
        choice = rule.get("choice")
        probabilities = rule.get("probabilities") or {}
        if choice not in rule_ids:
            return None, None
        p = probabilities[choice]
        none = probabilities.get("none", 0)
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not isinstance(none, (int, float)):
            return None, None
        if p >= RULE_CONFIDENT_P and p > none:
            return choice, float(p)
    except (AttributeError, KeyError, TypeError):
        pass
    return None, None


def code_gates(
    *, author_kind: str, addressed: bool, thread_task_id: str | None, thread_stopped_by_human: bool
) -> str | None:
    """Spec 12 §3.3 code gates after scope, in order; the first failure's reason, or None to go on.
    Dedupe is the conditional put in `record_match`."""
    if author_kind != "human":
        return "not_human"
    if addressed:
        return "addressed"
    if thread_task_id:
        return "thread_busy"
    if thread_stopped_by_human:
        return "human_stopped"
    return None


def would_do(rule: dict, ref: dict, tz: str) -> str:
    """The observe line, a code template (no model)."""
    if "calendar" in ((rule.get("match") or {}).get("sources") or []):
        start = datetime.fromisoformat(str(ref["start"]).replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
        when = f"{start:%a} {start.day} {start:%b %H:%M}"
        return f"Would prep a reply to “{ref.get('title') or 'an invite'}” ({when})"
    text = " ".join(str(ref.get("text") or "").split())[:120]
    return f"Would {rule.get('name') or 'act'} for: “{text}”"


def validate_scope(
    match: dict | None,
    *,
    member_room_ids: set[str],
    active_rules_by_room: dict[str, int],
    builtin: str | None,
) -> str | None:
    """None when the scope is valid, else the 409 code."""
    sources = (match or {}).get("sources") or []
    if not sources:
        return "scope_missing"
    if len(sources) != 1:
        return "one_source_only"
    if sources[0] == "room":
        rooms = match.get("roomIds") or []
        if not rooms:
            return "scope_missing"
        if any(r not in member_room_ids for r in rooms):
            return "room_not_member"
        if any(active_rules_by_room.get(r, 0) >= RULES_PER_ROOM for r in rooms):
            return "room_rule_cap"
    elif sources[0] == "calendar":
        if not match.get("account"):
            return "scope_missing"
    else:
        return "scope_missing"
    if builtin == "repro_fix" and not match.get("repo"):
        return "repo_required"
    return None


def matches_on_version(rule: dict) -> int:
    """Matches recorded on the rule's current version: an edit starts the promotion count again."""
    version = rule.get("version")
    return sum(1 for m in (rule.get("stats") or {}).get("matches") or [] if m.get("version") == version)


# --- Built-in: invite prep (observe, on for connected calendars) ---------------------------------------


def invite_prep_id(email: str) -> str:
    return "builtin:invite_prep:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def invite_prep_row(email: str, owner_user_id: str) -> dict:
    """The built-in row for one connected Google account whose owner is a workspace member."""
    return {
        "scheduleId": invite_prep_id(email),
        "name": "Invite prep",
        "policyText": "When someone invites me to a meeting I haven't answered, prepare a reply for me to send.",
        "tz": "UTC",  # ponytail: the owner's zone once users.timezone is read here; would_do takes tz per call
        "ownerUserId": owner_user_id,
        "match": {"sources": ["calendar"], "account": email},
        "builtin": "invite_prep",
        "mode": "observe",
        "speculative": True,
        **INVITE_PREP,
    }


def _start(event: dict) -> datetime | None:
    start = event.get("start") or {}
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def open_invite(event: dict, account: str, now: datetime) -> bool:
    """A Google Calendar event (API shape) the account was invited to and hasn't answered (spec 12 §3.3): it is an
    attendee with `needsAction`, someone else organized it, it isn't cancelled, and it starts in the future."""
    me = account.strip().lower()
    if event.get("status") == "cancelled":
        return False
    organizer = event.get("organizer") or {}
    if organizer.get("self") or str(organizer.get("email") or "").lower() == me:
        return False
    mine = [a for a in event.get("attendees") or [] if a.get("self") or str(a.get("email") or "").lower() == me]
    if not mine or mine[0].get("responseStatus") != "needsAction":
        return False
    start = _start(event)
    return start is not None and start > now
