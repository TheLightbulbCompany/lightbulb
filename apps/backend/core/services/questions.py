"""Question rules for super-agent tasks (contracts-v2 §3, §5, §8; spec 05 §3.3, §3.5).

Every "a person must decide" is one Question row on a task. This module is the pure part: who may answer, what the
options are, whether an external effect may proceed (`gate`), the card copy, and the false-approval pattern. The only
I/O is the Clerk admin roster and the stored capability policy, both read through their existing modules.

Only answering a Question approves. Nothing here maps message text to an answer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from core import tasks_schema as ts
from core.repositories import billing_repo
from core.services import clerk_admin

#: External, irreversible effects that go through the effects route (contracts-v2 §8). Nothing else does.
EFFECT_KINDS = ("mail.send", "text.send", "app.publish", "pr.merge", "purchase", "provision", "a2a.send")

#: A draft output a person reviews; the first option lands it (Landing), the second sends it back to the Worker.
REVIEW_KINDS = {"mail.send": "send", "text.send": "send", "app.publish": "publish", "pr.merge": "merge"}
_OUTPUT_KINDS = {"mail.send": "mail", "text.send": "mail", "app.publish": "app", "pr.merge": "pr"}

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
MONEY_SUBKINDS = frozenset(_MONEY)  # public: the acceptance harness refuses to answer these


class NoAnswerer(Exception):
    """Nobody can answer this question: the ask fails (`no_answerer`), it never waits silently."""


def subkind_for(effect_kind: str) -> str:
    return _SUBKINDS[effect_kind]


def kind_and_options(effect_kind: str) -> tuple[ts.QuestionKind, list[str]]:
    action = REVIEW_KINDS.get(effect_kind)
    if action:
        return "review", [action, "request_changes"]
    return "approval", ["allow_once", "deny"]


def operands_hash(kind: str, operands: dict) -> str:
    """The canonical effect hash shared with the bridge (bridge/effects.py `operands_hash`): sha256 of
    `{kind, **operands}` as sorted, compact JSON. Every backend door and the effects route compute it themselves."""
    canonical = json.dumps({"kind": kind, **operands}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def qid_for(idem_key: str) -> str:
    return "q-" + hashlib.sha256(idem_key.encode()).hexdigest()[:16]


async def _members(ws: str) -> dict[str, str]:
    """{user_id: role} of the workspace, strictly read (a Clerk error raises, never reads as "nobody")."""
    if not ws.startswith("org_"):
        return {ws: "org:admin"}  # a personal workspace's owner is its only member and admin
    members = await clerk_admin.list_organization_members(ws, strict=True)
    return {m["user_id"]: m.get("role") or "" for m in members if m.get("user_id")}


async def _admins(ws: str) -> list[str]:
    return [u for u, role in (await _members(ws)).items() if role == "org:admin"]


async def _current_rule(ws: str, subkind: str | None) -> str:
    needs_policy = subkind == "send" or subkind in _MONEY
    return _rule_for(subkind, await billing_repo.get_capability_policy(ws) if needs_policy else {})


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
    rule = await _current_rule(ws, subkind)
    if rule == "holders":
        base = list(holders or [])
    elif rule == "creator+admins":
        # An app the call did not name (`lightbulb app publish` run in the app's folder carries no id) has no
        # creator seat to add; its admins still answer, as they would for a named app.
        base = [app["created_by"]] if app and app.get("created_by") else []
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
    """Spec 05 §3.3 "re-checks authority": in the stored list AND still a workspace member AND today's rule (from
    today's policy) still admits them. An admin demoted, a member removed, or a policy tightened since the ask
    (outreach/money moves → admins only) is refused. Fails closed on a row with no stored rule."""
    if user_id not in (q.get("answerers") or []) or not q.get("answererRule"):
        return False
    members = await _members(ws)
    if user_id not in members:
        return False
    rule = await _current_rule(ws, q.get("subkind"))
    if rule != "admins" and user_id in (q.get("baseAnswerers") or []):
        return True  # requester, app creator or governance holder, and today's rule still has that seat
    return rule.endswith("admins") and members[user_id] == "org:admin"


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
#: Narrowed to first-person / present claims (lead, 5 Nov): a sentence-initial "Approved", "approval was granted",
#: "you've approved", "going ahead now", "I'm proceeding". "Sam approved the copy yesterday" is a report, not a claim.
FALSE_APPROVAL_RE = re.compile(
    r"(?:(?:^|(?<=[.!?:;,—–])\s+|(?<=\n)\s*|\b(?:ok|okay|great|thanks|right|done|sure|yes)\W+)approved\b(?!\s+by\b))"
    r"|\b(?:approval (?:was |has been |is )?(?:granted|given)|you(?:'ve| have) approved|go(?:ing)? ahead now"
    r"|(?:i(?:'m| am)|we(?:'re| are)) (?:now )?(?:proceeding|going ahead))\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def waiting_label(names: list[str]) -> str:
    """ "Waiting on Sam" / "… Sam or Ada" / "… Sam, Ada and 2 others": never a roster."""
    if not names:
        return "Waiting on a decision"
    if len(names) <= 2:
        return "Waiting on " + " or ".join(names)
    return f"Waiting on {names[0]}, {names[1]} and {len(names) - 2} other{'s' if len(names) > 3 else ''}"


_PUSH_MAX = 180


def title_for(effect: dict) -> str:
    """The question's one-liner for an Effect, naming the action and its target ("send 'Q3' to a@b.c")."""
    o = effect.get("operands") or {}
    kind = effect.get("kind")
    if kind == "mail.send":
        line = f"send '{o.get('subject') or 'a mail'}' to {o.get('to') or 'someone'}"
    elif kind == "text.send":
        line = f"send a text to {o.get('to') or 'someone'}"
    elif kind == "app.publish":
        line = f"publish {o.get('appName') or 'the app'}"
    elif kind == "pr.merge":
        line = f"merge PR #{o.get('number', '?')} in {o.get('repo') or 'the repo'}"
    elif kind == "purchase":
        line = f"buy {o.get('item') or 'something'}" + (f" for {o['amount']}" if o.get("amount") else "")
    elif kind == "provision":
        line = f"set up {o.get('provider') or 'a provider'}"
    elif kind == "a2a.send":
        line = f"message {o.get('target') or 'another agent'}"
    else:
        line = f"run {effect.get('tool') or 'a tool'}"
    return line if len(line) <= 200 else line[:199] + "…"


def output_for(effect: dict) -> dict | None:
    """The Output (draft) a proposed review-kind Effect carries, so the task shows Ready for review with its next step
    (Send / Publish / Merge). Approval kinds (purchase, provision, a2a) have no draft to review: None."""
    kind = _OUTPUT_KINDS.get(effect.get("kind"))
    if kind is None:
        return None
    line = title_for(effect)
    return {"kind": kind, "title": line[:1].upper() + line[1:]}


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


async def guard_reply(
    ws: str, text: str, task_refs: list[dict] | None, specialist: str | None
) -> tuple[str, str, str] | None:
    """Spec 05 §3.5 layer 3: a Lightbulb reply that names a task with an open Question and claims an approval
    ("Approved — proceeding") is not posted as written. Only the claiming sentences are dropped; the rest of the reply
    stays, and the card line is appended. Returns `(text, task_id, qid)`, or None when nothing is claimed."""
    if not task_refs or not FALSE_APPROVAL_RE.search(text):
        return None
    from core.observability.metrics import put_metric
    from core.repositories import questions_repo
    from core.services.messaging import human_display_name

    for ref in task_refs:
        open_qs = await questions_repo.list_open(ws, ref["taskId"])
        if not open_qs:
            continue
        q = open_qs[0]
        names = [await human_display_name(u) for u in q["answerers"]]
        kept = [s for s in _SENTENCE.split(text) if s.strip() and not FALSE_APPROVAL_RE.search(s)]
        who = waiting_label(names).removeprefix("Waiting on ")
        sp = specialist or "Lightbulb"
        card = (
            f"Queued for {sp} after {who}'s decision."
            if len(names) <= 2
            else f"Queued for {sp} after a decision from {who}."
        )
        put_metric("router.false_approval_blocked")
        return " ".join([*kept, card]), ref["taskId"], q["qid"]
    return None
