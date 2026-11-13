"""Question rules for super-agent tasks (contracts-v2 §3, §5, §8; spec 05 §3.3, §3.5).

Every "a person must decide" is one Question row on a task. This module is the pure part: who may answer, what the
options are, whether an external effect may proceed (`gate`), the card copy, and the false-approval pattern. The only
I/O is the Clerk admin roster and the stored capability policy, both read through their existing modules.

Only answering a Question approves. Nothing here maps message text to an answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Collection
from dataclasses import dataclass
from email.utils import getaddresses

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
    "allow_always": "Always allow",
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


_log = logging.getLogger(__name__)


class NoAnswerer(Exception):
    """Nobody can answer this question: the ask fails (`no_answerer`), it never waits silently."""


def subkind_for(effect_kind: str) -> str:
    return _SUBKINDS[effect_kind]


def kind_and_options(effect_kind: str, operands: dict | None = None) -> tuple[ts.QuestionKind, list[str]]:
    """The card's buttons. `allow_always` (a standing rule, `add_always_allow`) only when the call names a target
    the rule can bind to: a button that could not make a rule is not offered."""
    always = ["allow_always"] if _target(effect_kind, operands or {}) else []
    action = REVIEW_KINDS.get(effect_kind)
    if action:
        return "review", [action, *always, "request_changes"]
    return "approval", ["allow_once", *always, "deny"]


# --- approval modes (founder ruling 6 Nov 2026) --------------------------------------------------------------
# One mode per workspace, stored in the capability policy (`billing_repo`, key `approval_mode`, default auto) and
# read by the effects route only, so a Worker's VM can never change it. Ask: every classified outside action stops
# for a person. Auto: only the fixed list below. Never ask: nothing stops; the intent row still logs every action.
# The floors hold in every mode, outside this module: Hermes deny list, purchase caps and Link's approval, credit
# caps, A2A outside_org deny, one action per command, typed chat never approves.

APPROVAL_MODES = billing_repo.APPROVAL_MODES
_RECIPIENT_KINDS = frozenset({"mail.send", "text.send", "invite.send"})
_COPY_KEYS = ("cc", "bcc")
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # agent_email.EMAIL_ADDRESS_RE, the sender's own check


def _recipients(operands: dict) -> list[str] | None:
    """Every address the call sends to, parsed the way the sender parses `to` (`email.utils.getaddresses`,
    agent_mail_send._single_address), lower-case; None when any part does not parse to a plain address."""
    raw = [str(operands.get(k) or "") for k in ("to", *_COPY_KEYS)]
    pairs = getaddresses([r for r in raw if r])
    addrs = [a.lower() for _, a in pairs]
    if not addrs or any(not _ADDRESS.match(a) for a in addrs):
        return None
    return addrs


def asks(mode: str, kind: str, operands: dict, *, members: Collection[str] = (), limits: dict | None = None) -> bool:
    """Whether this outside action stops for a person's yes (pure). Auto stops only for money over the per-purchase
    cap, a recipient who is not a workspace member (`members`: their emails, lower-case), and deletes of outside
    data; publish, merge, provision and agent messages go ahead. Unknown mode or unreadable operands: ask.
    (`delete` and `invite.send` are not classified yet: the classify slice brings them here.)"""
    if mode == "never_ask":
        return False
    if mode != "auto":
        return True
    if kind == "delete":
        return True
    if kind in _RECIPIENT_KINDS:
        to = _recipients(operands)
        return to is None or any(r not in members for r in to)  # a reply or a phone number names no member: ask
    if kind == "purchase":
        cap = int((limits or {}).get("perPurchaseCents") or 0)
        try:
            dollars_ = float(str(operands["amount"]).lstrip("$").replace(",", ""))
        except (KeyError, ValueError):
            return True
        if not math.isfinite(dollars_) or dollars_ < 0:
            return True
        return cap <= 0 or round(dollars_ * 100) > cap
    return False


async def member_emails(ws: str) -> set[str]:
    """The workspace members' primary emails, lower-case, strictly read (a Clerk error raises: the caller asks)."""
    if not ws.startswith("org_"):
        return {(await clerk_admin.get_primary_email(ws)).lower()}
    members = await clerk_admin.list_organization_members(ws, strict=True)
    return {m["email"].lower() for m in members if m.get("email")}


#: The operand an Always-allow rule binds to: the recipient, app, repo or agent.
_TARGET_KEYS = {
    "mail.send": "to",
    "text.send": "to",
    "app.publish": "appId",
    "pr.merge": "repo",
    "a2a.send": "to",
}
#: Money and provider consent are never remembered: no Always allow on the card, and no stored allow rule (an older
#: card's, or an admin's) ever lets one through. Every purchase and provision faces the mode and the purchase caps.
NEVER_REMEMBERED = frozenset({"purchase", "provision"})


def _target(kind: str, operands: dict) -> tuple[str, str] | None:
    key = _TARGET_KEYS.get(kind)
    value = operands.get(key) if key else None
    return (key, value) if isinstance(value, str) and value else None


async def rule_effect(ws: str, specialist: str | None, kind: str, operands: dict) -> tuple[str, str] | None:
    """`("allow" | "deny", rule id)` when a stored rule names this action (`a2a_rules.decide` over the workspace's
    Rules document), else None. The A2A defaults and holders never decide an effect: only a rule for this skill
    does. A mail with cc/bcc is never covered: a rule binds `to` alone. An allow for a purchase or provision is
    ignored (`NEVER_REMEMBERED`): a deny still holds."""
    from core.repositories import a2a_repo
    from core.services import a2a_rules

    if kind in _RECIPIENT_KINDS and any(operands.get(k) for k in _COPY_KEYS):
        return None
    doc = await a2a_repo.get_rules(ws)
    if not doc:
        return None
    d = a2a_rules.decide(
        a2a_rules.Rules.model_validate(doc),
        org=ws,
        requester_person="",
        requester_org=ws,
        chain_agents=[],
        resource=f"agent:{specialist or '*'}",
        skill=kind,
        session_person=None,
        exception=None,
        origin_is_person=False,
        operands=operands,
    )
    if d.effect == "hold" or d.rule in ("default", "holder"):
        return None
    if d.effect == "allow" and kind in NEVER_REMEMBERED:
        return None
    return (d.effect, d.rule)


class RulesBusy(Exception):
    """The Rules document kept changing under an Always-allow write (retryable: the Activity tries again)."""


_RULE_WRITE_TRIES = 3


async def add_always_allow(ws: str, kind: str, operands: dict, *, by: str) -> None:
    """The card's Always allow: one workspace-wide `allow` rule for this action kind and target in the workspace's
    Rules document (any answerer may make it: ruling 6 Nov 2026). Idempotent: the id comes from kind and target.
    The write is conditional on the revision it read, so an admin's rules PUT in between is never overwritten; it
    re-reads and tries again, then raises `RulesBusy`. (The admin PUT, a2a_tasks.put_rules, is still unconditional:
    it can drop an always-* rule written after its own read. Follow-up: send a base revision there too.)"""
    from core.repositories import a2a_repo
    from core.services import a2a_rules, a2a_tasks

    target = _target(kind, operands)
    if target is None:
        return
    key, value = target
    rid = "always-" + hashlib.sha256(f"{kind}\0{key}\0{value}".encode()).hexdigest()[:16]
    for _ in range(_RULE_WRITE_TRIES):
        doc = await a2a_repo.get_rules(ws)
        rules = a2a_rules.Rules.model_validate(doc) if doc else a2a_rules.default_rules(ws, await _admins(ws))
        if any(r.id == rid for r in rules.rules):
            return
        rules.rules.append(
            a2a_rules.Rule(
                id=rid,
                resource="agent:*",
                skill=kind,
                requesters=[f"org:{ws}"],
                effect="allow",
                when=a2a_rules.When(operands_equal={key: value}),
            )
        )
        new = {**rules.model_dump(exclude_none=True), "revision": a2a_tasks.revision(rules)}
        if await a2a_repo.put_rules_if(ws, new, doc):
            await audit(by, ws, "approval.always_allow", {"ruleId": rid, "kind": kind, key: value})
            return
    raise RulesBusy(ws)


async def audit(actor: str, ws: str, action: str, payload: dict) -> None:
    """One row in the admin-actions audit table (who changed how agents ask, and when). A failed write is logged
    CRITICAL and never undoes the change (the routers/users.py self-wipe posture)."""
    from core.repositories import admin_actions_repo

    try:
        await admin_actions_repo.create(
            admin_user_id=actor,
            target_user_id=ws,
            action=action,
            payload=payload,
            result="success",
            audit_status="written",
            http_status=200,
            elapsed_ms=0,
            error_message=None,
            user_agent="lightbulb",
            ip="",
        )
    except Exception:  # noqa: BLE001
        _log.critical("APPROVAL_AUDIT_PANIC action=%s ws=%s actor=%s", action, ws, actor, exc_info=True)


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
    inviter = task.get("ownerUserId")
    if kind == "approval" and task.get("confirmAll") and inviter and inviter != ws and inviter in await _members(ws):
        # Founder ruling 8 Nov 2026: a meeting's Needs-your-OK card goes to the inviter only (the meeting acts under
        # the inviter's authority, voice_meet.inviting_member), never to the workspace's other admins. An inviter
        # who left the workspace (or a workspace-owned task) falls through to the rule below: never a locked card.
        return [inviter], "inviter", [inviter]
    rule = await _current_rule(ws, subkind)
    if rule == "holders":
        base = list(holders or [])
    elif rule == "creator+admins":
        # An app the call did not name (`lightbulb app publish` run in the app's folder carries no id) has no
        # creator seat to add; its admins still answer, as they would for a named app.
        base = [app["created_by"]] if app and app.get("created_by") else []
    elif rule.startswith("requester"):
        base = [task["ownerUserId"]] if task.get("ownerUserId") and task["ownerUserId"] != ws else []
    else:
        base = []
    ids = list(base)
    # A workspace-owned request (the proactive layer's runs: requester = the workspace) is answered by its admins.
    if rule.endswith("admins") or (rule.startswith("requester") and task.get("ownerUserId") == ws):
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
    if q["answererRule"] == "inviter":
        return user_id in (q.get("baseAnswerers") or [])
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


def gate(effect: dict, prior: list[dict], closed: Collection[str] = (), recent_since: str | None = None) -> GateResult:
    """Decide one external effect from the task's earlier rows with the same `operandsHash` (pure).

    Same call already done → replay. Another identical call done at or after `recent_since` (ISO) → replay too (gate
    run 9: a Worker sent the same mail twice in 17 s from two tool calls), whatever the mode or rules; a `refused:` row
    never happened and is skipped. Any matching call with an unknown outcome → refuse (never a second send).
    Denied before → denied, no new card. An approved, unused answer → proceed (single use). An open ask → reuse it;
    one whose Question is `closed` (cancelled by a Close; dev walk 7: a Reopen then waited for good) is never reused.
    Otherwise a new approval with a deterministic qid.
    """
    idem = effect["idemKey"]
    effects = [p for p in prior if p.get("type") != "effect_use"]
    used = {p["idemKey"].removesuffix(":use") for p in prior if p.get("type") == "effect_use"}
    for p in effects:
        if p["idemKey"] == idem and p["state"] == "done":
            return GateResult("replay", result_ref=p.get("resultRef"))
    for p in effects:
        at = p.get("updatedAt") or p.get("createdAt") or ""
        refused = (p.get("resultRef") or "").startswith("refused:")  # it never happened: asking again is allowed
        if recent_since and p["state"] == "done" and not refused and at >= recent_since:
            msg = f"Already done in this task at {at}; not sent again."  # the Worker's tool result, never ""
            return GateResult("replay", result_ref=p.get("resultRef"), message=msg)
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
        if p["state"] == "proposed" and p.get("questionId") and p["questionId"] not in closed:
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
_CALL_PUSH = {
    "handoff": "Caller wanted a person",
    "didnt_know": "Caller asked something we didn't know",
    "line_problem": "A call hit a problem on the line",
}


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
        what = o.get("item") or (f"from {o['merchant']}" if o.get("merchant") else "something")
        line = f"buy {what}" + (f" for {dollars(o['amount'])}" if o.get("amount") else "")
    elif kind == "provision":
        line = f"set up {o.get('provider') or 'a provider'}"
    elif kind == "a2a.send":
        line = f"message {o.get('target') or 'another agent'}"
    else:
        line = f"run {effect.get('tool') or 'a tool'}"
    return line if len(line) <= 200 else line[:199] + "…"


def dollars(amount: object) -> str:
    """A purchase amount as people read it. `request_purchase` sends the total in US dollars as a bare number
    ("11.81", 35: dev walk 7 showed "for 11.81"); anything already written with its currency stays as written."""
    try:
        return f"${float(str(amount)):,.2f}"
    except ValueError:
        return str(amount)


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
    if q.get("subkind") in _CALL_PUSH:  # a call card: the kind and a first name, never the number or the question
        who = (q.get("preview") or {}).get("firstName")
        body = _CALL_PUSH[q["subkind"]] + (f": {who}" if who else "")
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
