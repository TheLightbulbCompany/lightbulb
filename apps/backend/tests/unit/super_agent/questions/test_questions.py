"""Question rules (wave2-Q Task 1; spec 05 §3.3, §3.5; contracts-v2 §3, §8). Pure; Clerk and policy are faked."""

import pytest

from core import tasks_schema as ts
from core.services import questions

ORG = "org_acme"
TASK = {"taskId": "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f", "ownerUserId": "user_sam"}
MEMBERS = [
    {"user_id": "user_sam", "role": "org:member"},
    {"user_id": "user_ada", "role": "org:admin"},
    {"user_id": "user_bo", "role": "org:admin"},
    {"user_id": "user_priya", "role": "org:member"},
]


@pytest.fixture
def clerk(monkeypatch):
    calls = []
    state = {"members": MEMBERS}

    async def fake(org_id, *, strict=False):
        calls.append((org_id, strict))
        return state["members"]

    monkeypatch.setattr(questions.clerk_admin, "list_organization_members", fake)
    return {"calls": calls, "state": state}


@pytest.fixture
def policy(monkeypatch):
    stored = {"money_moves": True, "outreach": False, "publishing": True, "provisioning": True}

    async def fake(owner_id):
        return dict(stored)

    monkeypatch.setattr(questions.billing_repo, "get_capability_policy", fake)
    return stored


@pytest.mark.parametrize(
    ("subkind", "policy_over", "extra", "expected", "rule"),
    [
        ("question", {}, {}, ["user_sam"], "requester"),
        ("tool", {}, {}, ["user_sam"], "requester"),
        ("send", {}, {}, ["user_sam"], "requester"),
        ("send", {"outreach": True}, {}, ["user_ada", "user_bo"], "admins"),
        ("purchase", {}, {}, ["user_ada", "user_bo"], "admins"),
        ("spend", {"money_moves": False}, {}, ["user_sam", "user_ada", "user_bo"], "requester+admins"),
        ("provider_consent", {}, {}, ["user_ada", "user_bo"], "admins"),
        ("publish", {}, {"app": {"created_by": "user_priya"}}, ["user_priya", "user_ada", "user_bo"], "creator+admins"),
        ("review", {}, {}, ["user_sam", "user_ada", "user_bo"], "requester+admins"),
        ("a2a_hold", {}, {"holders": ["user_bo", "user_zed"]}, ["user_bo", "user_zed"], "holders"),
    ],
)
async def test_answerers_by_subkind(clerk, policy, subkind, policy_over, extra, expected, rule):
    policy.update(policy_over)
    ids, got_rule, base = await questions.answerers(ORG, TASK, "approval", subkind, **extra)
    assert ids == expected
    assert got_rule == rule
    assert set(base) <= set(ids)


async def test_admin_answerer_never_listed_twice(clerk, policy):
    ids, _, _ = await questions.answerers(ORG, {**TASK, "ownerUserId": "user_ada"}, "review", "review")
    assert ids == ["user_ada", "user_bo"]


async def test_personal_workspace_owner_is_the_admin(clerk, policy):
    ids, _, _ = await questions.answerers("user_solo", {**TASK, "ownerUserId": "user_solo"}, "approval", "purchase")
    assert ids == ["user_solo"]
    assert clerk["calls"] == []


async def test_no_answerer_raises(clerk, policy):
    clerk["state"]["members"] = []
    with pytest.raises(questions.NoAnswerer):
        await questions.answerers(ORG, TASK, "approval", "purchase")


async def test_no_requester_raises(clerk, policy):
    with pytest.raises(questions.NoAnswerer):
        await questions.answerers(ORG, {**TASK, "ownerUserId": None}, "question", "question")


async def test_publish_without_app_raises(clerk, policy):
    with pytest.raises(questions.NoAnswerer):
        await questions.answerers(ORG, TASK, "review", "publish")


async def test_strict_admin_lookup(clerk, policy):
    await questions.answerers(ORG, TASK, "approval", "purchase")
    assert clerk["calls"] == [(ORG, True)]


async def test_can_answer_now(clerk, policy):
    ids, rule, base = await questions.answerers(ORG, TASK, "review", "review")
    q = {"subkind": "review", "answerers": ids, "answererRule": rule, "baseAnswerers": base}
    assert await questions.can_answer_now(ORG, q, "user_sam")
    assert await questions.can_answer_now(ORG, q, "user_ada")
    assert not await questions.can_answer_now(ORG, q, "user_priya")  # never stored

    # Ada demoted between ask and answer: still stored, no longer admitted.
    clerk["state"]["members"] = [{**m, "role": "org:member"} if m["user_id"] == "user_ada" else m for m in MEMBERS]
    assert not await questions.can_answer_now(ORG, q, "user_ada")
    assert await questions.can_answer_now(ORG, q, "user_sam")  # the requester is not an admin seat


async def test_can_answer_now_promoted_member_not_stored(clerk, policy):
    """Authority gained after the ask doesn't add a person: stored list AND rule (spec 05 §3.3)."""
    ids, rule, base = await questions.answerers(ORG, TASK, "approval", "purchase")
    q = {"subkind": "purchase", "answerers": ids, "answererRule": rule, "baseAnswerers": base}
    clerk["state"]["members"] = [*MEMBERS, {"user_id": "user_new", "role": "org:admin"}]
    assert not await questions.can_answer_now(ORG, q, "user_new")


async def test_tightened_policy_blocks_the_requester_of_an_old_question(clerk, policy):
    """Outreach was open when Sam's send was asked; an admin turned it to admins-only before Sam pressed."""
    ids, rule, base = await questions.answerers(ORG, TASK, "review", "send")
    q = {"subkind": "send", "answerers": ids, "answererRule": rule, "baseAnswerers": base}
    assert ids == ["user_sam"] and await questions.can_answer_now(ORG, q, "user_sam")
    policy["outreach"] = True
    assert not await questions.can_answer_now(ORG, q, "user_sam")


async def test_tightened_money_policy_keeps_admins_only(clerk, policy):
    policy["money_moves"] = False
    ids, rule, base = await questions.answerers(ORG, TASK, "approval", "spend")
    q = {"subkind": "spend", "answerers": ids, "answererRule": rule, "baseAnswerers": base}
    policy["money_moves"] = True
    assert not await questions.can_answer_now(ORG, q, "user_sam")
    assert await questions.can_answer_now(ORG, q, "user_ada")


async def test_removed_member_refused(clerk, policy):
    ids, rule, base = await questions.answerers(ORG, TASK, "question", "question")
    q = {"subkind": "question", "answerers": ids, "answererRule": rule, "baseAnswerers": base}
    clerk["state"]["members"] = [m for m in MEMBERS if m["user_id"] != "user_sam"]
    assert not await questions.can_answer_now(ORG, q, "user_sam")


async def test_missing_rule_fails_closed(clerk, policy):
    q = {"subkind": "question", "answerers": ["user_sam"], "answererRule": None, "baseAnswerers": ["user_sam"]}
    assert await questions.can_answer_now(ORG, q, "user_sam") is False
    assert await questions.can_answer_now(ORG, {"answerers": ["user_sam"]}, "user_sam") is False


def test_review_kind_for_drafts():
    assert questions.kind_and_options("mail.send") == ("review", ["send", "request_changes"])
    assert questions.kind_and_options("app.publish") == ("review", ["publish", "request_changes"])
    assert questions.kind_and_options("pr.merge") == ("review", ["merge", "request_changes"])
    assert questions.kind_and_options("provision") == ("approval", ["allow_once", "deny"])
    assert questions.kind_and_options("purchase") == ("approval", ["allow_once", "deny"])


def test_subkind_for():
    assert [questions.subkind_for(k) for k in questions.EFFECT_KINDS] == [
        "send",
        "send",
        "publish",
        "review",
        "purchase",
        "provider_consent",
        "a2a",
    ]


def test_qid_is_deterministic():
    assert questions.qid_for("t:1:c") == questions.qid_for("t:1:c") != questions.qid_for("t:1:d")
    assert questions.qid_for("t:1:c").startswith("q-") and len(questions.qid_for("t:1:c")) == 18


def _eff(idem, state, *, attempt=1, qid=None, ohash="h1"):
    return {"idemKey": idem, "state": state, "attempt": attempt, "questionId": qid, "operandsHash": ohash}


def test_gate_decisions():
    effect = _eff("T:3:c9", "proposed", attempt=3)
    nothing = questions.gate(effect, [])
    assert nothing.decision == "needs_approval"
    assert nothing.qid == questions.qid_for("T:3:c9")

    allowed = questions.gate(effect, [_eff("T:1:c1", "approved", qid="q-a")])
    assert allowed == questions.GateResult("proceed", qid="q-a", approved_idem_key="T:1:c1")

    denied = questions.gate(effect, [_eff("T:1:c1", "denied", qid="q-d")])
    assert (denied.decision, denied.qid) == ("denied", "q-d")

    unknown = questions.gate(effect, [_eff("T:2:c4", "intent", attempt=2)])
    assert unknown.decision == "refuse"
    assert unknown.message == "A matching call (attempt 2) has an unknown outcome; check before retrying."
    assert questions.gate(effect, [_eff("T:2:c4", "unknown", attempt=2)]).decision == "refuse"


def test_gate_approval_is_single_use():
    effect = _eff("T:3:c9", "proposed", attempt=3)
    used = [
        _eff("T:1:c1", "approved", qid="q-a"),
        {"idemKey": "T:1:c1:use", "type": "effect_use", "operandsHash": "h1"},
    ]
    assert questions.gate(effect, used).decision == "needs_approval"


def test_gate_pending_question_is_reused_not_reasked():
    effect = _eff("T:3:c9", "proposed", attempt=3)
    res = questions.gate(effect, [_eff("T:2:c4", "proposed", attempt=2, qid="q-open")])
    assert (res.decision, res.qid) == ("needs_approval", "q-open")


def test_gate_replays_same_idem_key_done():
    effect = _eff("T:2:c4", "proposed", attempt=2)
    done = {**_eff("T:2:c4", "done", attempt=2), "resultRef": "ses:abc"}
    assert questions.gate(effect, [done]) == questions.GateResult("replay", result_ref="ses:abc")


def test_gate_earlier_done_send_asks_again():
    """Sending the same mail twice is a second send: a fresh approval, not a replay."""
    effect = _eff("T:3:c9", "proposed", attempt=3)
    assert questions.gate(effect, [_eff("T:1:c1", "done")]).decision == "needs_approval"


def test_gate_deny_wins_over_later_retry_approval():
    effect = _eff("T:4:c1", "proposed", attempt=4)
    prior = [_eff("T:1:c1", "approved", qid="q-a"), _eff("T:2:c1", "denied", qid="q-d")]
    assert questions.gate(effect, prior).decision == "denied"


@pytest.mark.parametrize(
    "text",
    [
        "Approved — proceeding",
        "you've approved the send",
        "OK, going ahead now",
        "Approval was granted by Sam",
        "Thanks! I'm proceeding with the send.",
        "Done with the draft. Approved, sending it now.",
    ],
)
def test_false_approval_regex_matches(text):
    assert questions.FALSE_APPROVAL_RE.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Waiting on Sam to approve",
        "Needs approval from an admin",
        "go ahead and ask",
        "Sam approved the copy yesterday, so I sent it.",  # a report of the past, not a claim about this question
        "Once approved, I'll send it.",
        "The approved vendor list is in Drive.",
        "Ada will go ahead once Sam decides.",
    ],
)
def test_false_approval_regex_ignores(text):
    assert not questions.FALSE_APPROVAL_RE.search(text)


def test_card_copy():
    q = {"kind": "approval", "title": "send 'Q3 invoice' to acme@example.com", "text": "x" * 400}
    title, body = questions.card_copy(q, "Finance")
    assert title == "**Finance** needs your OK to send 'Q3 invoice' to acme@example.com"
    assert len(body) <= 180 and body.startswith("Finance needs your OK")
    rtitle, _ = questions.card_copy({**q, "kind": "review", "title": "the Q3 invoice mail"}, "Finance")
    assert rtitle == "**Finance** has a draft ready for review: the Q3 invoice mail"
    qtitle, _ = questions.card_copy({**q, "kind": "question", "title": "Which client?"}, "Finance")
    assert qtitle == "**Finance** asks: Which client?"


def test_schema_fields():
    assert "answer" in ts.TRIGGER_ID_FORMATS
    assert ts.trigger_id("answer", "q-1", "c-1") == "answer:q-1:c-1"
    fields = ts.Question.model_fields
    for f in ("client_answer_id", "relay", "gov_ref", "preview", "answerer_rule", "base_answerers"):
        assert f in fields
    for f in ("kind", "operands", "executor_ok"):
        assert f in ts.Effect.model_fields


def test_waiting_label_caps_the_names():
    assert questions.waiting_label(["Sam"]) == "Waiting on Sam"
    assert questions.waiting_label(["Sam", "Ada"]) == "Waiting on Sam or Ada"
    assert questions.waiting_label(["Sam", "Ada", "Bo"]) == "Waiting on Sam, Ada and 1 other"
    assert questions.waiting_label(["Sam", "Ada", "Bo", "Cy", "Di"]) == "Waiting on Sam, Ada and 3 others"
    assert questions.waiting_label([]) == "Waiting on a decision"
