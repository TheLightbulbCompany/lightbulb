"""Question rules for super-agent tasks (contracts-v2 §3, §5, §8; spec 05 §3.3, §3.5).

Every "a person must decide" is one Question row on a task. This module is the pure part: who may answer, what the
options are, whether an external effect may proceed (`gate`), the card copy, and the false-approval pattern. The only
I/O is the Clerk admin roster and the stored capability policy, both read through their existing modules.

Only answering a Question approves. Nothing here maps message text to an answer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from core import tasks_schema as ts
from core.repositories import billing_repo
from core.services import clerk_admin

#: External, irreversible effects that go through the effects route (contracts-v2 §8). Nothing else does.
EFFECT_KINDS = ("mail.send", "text.send", "app.publish", "pr.merge", "purchase", "provision", "a2a.send")

#: A draft output a person reviews; the first option lands it (Landing), the second sends it back to the Worker.
REVIEW_KINDS = {"mail.send": "send", "text.send": "send", "app.publish": "publish", "pr.merge": "merge"}

_SUBKINDS = {
    "mail.send": "send",
    "text.send": "send",
    "app.publish": "publish",
    "pr.merge": "review",
    "purchase": "purchase",
    "provision": "provider_consent",
    "a2a.send": "a2a",
}

OPTION_LABELS = {
    "allow_once": "Allow once",
    "deny": "Deny",
    "send": "Send",
    "publish": "Publish",
    "merge": "Merge",
    "request_changes": "Request changes",
}

_ADMIN_ONLY = {
    "purchase",
    "spend",
    "budget_override",
    "provider_consent",
    "provider_connect",
    "provider_information",
    "payment_setup",
    "company_stripe",
}
_MONEY = {"purchase", "spend", "budget_override"}


class NoAnswerer(Exception):
    """Nobody can answer this question: the ask fails (`no_answerer`), it never waits silently."""


def subkind_for(effect_kind: str) -> str:
    return _SUBKINDS[effect_kind]


def kind_and_options(effect_kind: str) -> tuple[ts.QuestionKind, list[str]]:
    action = REVIEW_KINDS.get(effect_kind)
    if action:
        return "review", [action, "request_changes"]
    return "approval", ["allow_once", "deny"]


def qid_for(idem_key: str) -> str:
    return "q-" + hashlib.sha256(idem_key.encode()).hexdigest()[:16]


async def _admins(ws: str) -> list[str]:
    if not ws.startswith("org_"):
        return [ws]  # a personal workspace's owner is its only admin
    members = await clerk_admin.list_organization_members(ws, strict=True)
    return [m["user_id"] for m in members if m.get("role") == "org:admin" and m.get("user_id")]


def _rule_for(subkind: str | None, policy: dict[str, bool]) -> str:
    if subkind == "send":
        return "admins" if policy["outreach"] else "requester"
    if subkind in _MONEY:
        return "admins" if policy["money_moves"] else "requester+admins"
    if subkind in _ADMIN_ONLY:
        return "admins"
    if subkind in ("publish", "destructive_migration"):
        return "creator+admins"
    if subkind == "review":
        return "requester+admins"
    if subkind == "a2a_hold":
        return "holders"
    return "requester"  # `question`, `tool`, `a2a` and any gated effect not listed above


async def answerers(
    ws: str,
    task: dict,
    kind: str,
    subkind: str | None,
    *,
    app: dict | None = None,
    holders: list[str] | None = None,
) -> tuple[list[str], str, list[str]]:
    """Spec 05 §3.3, computed at ask time and stored on the row.

    Returns `(answerers, rule, base)`: `base` is the non-admin part (requester, app creator, governance holders), kept
    so `can_answer_now` can re-check the admin part at answer time. Raises `NoAnswerer` on an empty list.
    """
    needs_policy = subkind == "send" or subkind in _MONEY
    rule = _rule_for(subkind, await billing_repo.get_capability_policy(ws) if needs_policy else {})
    if rule == "holders":
        base = list(holders or [])
    elif rule == "creator+admins":
        if not app:
            raise NoAnswerer("publish needs the app")
        base = [app["created_by"]] if app.get("created_by") else []
    elif rule.startswith("requester"):
        base = [task["ownerUserId"]] if task.get("ownerUserId") else []
    else:
        base = []
    ids = list(base)
    if rule.endswith("admins"):
        ids += await _admins(ws)
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise NoAnswerer(rule)
    return ids, rule, base


async def can_answer_now(ws: str, q: dict, user_id: str) -> bool:
    """In the stored list AND the rule still admits them: an admin demoted since the ask is refused."""
    if user_id not in q.get("answerers", []):
        return False
    if user_id in q.get("baseAnswerers", []):
        return True
    return q.get("answererRule", "").endswith("admins") and user_id in await _admins(ws)


@dataclass(frozen=True)
class GateResult:
    decision: ts.EffectDecision
    qid: str | None = None
    message: str | None = None
    approved_idem_key: str | None = None  # the approval a `proceed` consumes
    result_ref: str | None = None  # a `replay`'s stored result


def gate(effect: dict, prior: list[dict]) -> GateResult:
    """Decide one external effect from the task's earlier rows with the same `operandsHash` (pure).

    Same call already done → replay. Any matching call with an unknown outcome → refuse (never a second send).
    Denied before → denied, no new card. An approved, unused answer → proceed (single use). An open ask → reuse it.
    Otherwise a new approval with a deterministic qid.
    """
    idem = effect["idemKey"]
    effects = [p for p in prior if p.get("type") != "effect_use"]
    used = {p["idemKey"].removesuffix(":use") for p in prior if p.get("type") == "effect_use"}
    for p in effects:
        if p["idemKey"] == idem and p["state"] == "done":
            return GateResult("replay", result_ref=p.get("resultRef"))
    for p in effects:
        if p["state"] in ("intent", "unknown"):
            return GateResult(
                "refuse",
                message=f"A matching call (attempt {p['attempt']}) has an unknown outcome; check before retrying.",
            )
    for p in effects:
        if p["state"] == "denied":
            return GateResult("denied", qid=p.get("questionId"))
    for p in effects:
        if p["state"] == "approved" and p["idemKey"] not in used:
            return GateResult("proceed", qid=p.get("questionId"), approved_idem_key=p["idemKey"])
    for p in effects:
        if p["state"] == "proposed" and p.get("questionId"):
            return GateResult("needs_approval", qid=p["questionId"])
    return GateResult("needs_approval", qid=qid_for(idem))


#: Spec 05 §3.5 layer 3: a Router reply claiming an approval that a Question row does not show.
FALSE_APPROVAL_RE = re.compile(
    r"\b(approved|approval (was )?(granted|given)|you('ve| have) approved|go(ing)? ahead now)\b", re.IGNORECASE
)

_PUSH_MAX = 180


def card_copy(q: dict, specialist_label: str) -> tuple[str, str]:
    """(card title in markdown, push body ≤180 chars) for an open Question."""
    if q["kind"] == "review":
        title = f"**{specialist_label}** has a draft ready for review: {q['title']}"
    elif q["kind"] == "question":
        title = f"**{specialist_label}** asks: {q['title']}"
    else:
        title = f"**{specialist_label}** needs your OK to {q['title']}"
    body = title.replace("**", "")
    if q.get("text"):
        body = f"{body}. {q['text']}"
    if len(body) > _PUSH_MAX:
        body = body[: _PUSH_MAX - 1].rstrip() + "…"
    return title, body
