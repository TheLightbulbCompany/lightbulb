"""Approval modes, slice 1 (founder ruling 6 Nov 2026): Ask / Auto (default) / Never ask per workspace, and the
card's Always allow writing a rule into the A2A Rules store that the effects route reads before asking."""

# ruff: noqa: F811 -- fixtures imported by name from the orchestration suites.

from unittest.mock import AsyncMock

import pytest

from core.repositories import a2a_repo, billing_repo, effects_repo
from core.services import questions
from core.services.a2a_rules import Rules
from routers import runtime_internal as ri

from ..orchestration.test_activities_task import WS, _task, world  # noqa: F401
from ..orchestration.test_runtime_internal_routes import H, _running, _secret, api  # noqa: F401

MEMBERS = {"sam@acme.dev", "ada@acme.dev"}
LIMITS = {"perPurchaseCents": 5000, "projectMonthlyCents": 20000, "merchants": []}


# --- the Auto rule list (pure) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "operands", "asks"),
    [
        ("mail.send", {"to": "sam@acme.dev"}, False),  # a member: goes ahead
        ("mail.send", {"to": "Sam <SAM@acme.dev>, ada@acme.dev"}, False),
        ("mail.send", {"to": "client@example.com"}, True),  # an outsider: asks
        ("mail.send", {"to": "sam@acme.dev, client@example.com"}, True),  # one outsider is enough
        ("mail.send", {"replyKey": "r-1"}, True),  # a reply names nobody: fail closed
        ("text.send", {"to": "+15551234567"}, True),  # a number is never a known member
        ("delete", {"target": "drive:file-1"}, True),  # deletes of outside data
        ("app.publish", {"appId": "app-1"}, False),  # publish does not stop in Auto (ruling 6 Nov)
        ("pr.merge", {"repo": "acme/web", "number": 3}, False),
        ("provision", {"provider": "neon"}, False),
        ("a2a.send", {"to": "agent:x"}, False),
        ("purchase", {"merchant": "Acme", "amount": "49.99"}, False),  # under the cap
        ("purchase", {"merchant": "Acme", "amount": 50.01}, True),  # over the cap
        ("purchase", {"merchant": "Acme"}, True),  # no amount: fail closed
        ("purchase", {"amount": "inf"}, True),  # non-finite: asks, never a 500
        ("purchase", {"amount": "nan"}, True),
        ("purchase", {"amount": "-5"}, True),  # negative: asks
        ("mail.send", {"to": "x'sam@acme.dev"}, True),  # parsed as the sender parses it: another mailbox
        ("mail.send", {"to": "sam@acme.dev <client@example.com>"}, True),  # the display name is not the address
        ("mail.send", {"to": "sam@acme.dev", "cc": "client@example.com"}, True),
        ("mail.send", {"to": "sam@acme.dev", "bcc": "client@example.com"}, True),
        ("mail.send", {"to": "sam@acme.dev", "cc": "Ada <ada@acme.dev>"}, False),
        ("mail.send", {"to": "<<<>>>"}, True),  # unparseable: asks
    ],
)
def test_auto_rule_table(kind, operands, asks):
    assert questions.asks("auto", kind, operands, members=MEMBERS, limits=LIMITS) is asks


def test_purchase_with_no_limit_set_asks():
    assert questions.asks("auto", "purchase", {"amount": "1"}, members=(), limits={"perPurchaseCents": 0}) is True


@pytest.mark.parametrize("kind", ["mail.send", "app.publish", "purchase", "a2a.send"])
def test_ask_asks_for_everything_and_never_ask_for_nothing(kind):
    ops = {"to": "sam@acme.dev", "amount": "1"}
    assert questions.asks("ask", kind, ops, members=MEMBERS, limits=LIMITS) is True
    assert questions.asks("never_ask", kind, ops, members=MEMBERS, limits=LIMITS) is False
    assert questions.asks("bogus", kind, ops, members=MEMBERS, limits=LIMITS) is True  # unknown mode: fail closed


def test_allow_always_is_offered_only_with_a_target_to_bind():
    # Money and provider consent are never remembered: Deny / Allow once only, even with a merchant or provider.
    assert questions.kind_and_options("purchase", {"merchant": "Acme"}) == ("approval", ["allow_once", "deny"])
    assert questions.kind_and_options("provision", {"provider": "neon"}) == ("approval", ["allow_once", "deny"])
    assert questions.kind_and_options("mail.send", {"to": "a@b.c"}) == (
        "review",
        ["send", "allow_always", "request_changes"],
    )
    assert questions.kind_and_options("app.publish", {}) == ("review", ["publish", "request_changes"])


# --- the effects route reads the mode -------------------------------------------------------------------


def _url():
    return f"/api/v1/internal/runtime/{WS}/effects"


async def _body(tid, call, kind, operands):
    from core.repositories import tasks_repo

    return {
        "taskId": tid,
        "attempt": 1,
        "runId": (await tasks_repo.get_attempt(WS, tid, 1))["runId"],
        "runSecret": await _secret(tid, 1),
        "toolCallId": call,
        "kind": kind,
        "tool": "terminal",
        "operands": operands,
        "operandsHash": ri.operands_hash(kind, operands),
        "executorOk": False,
        "idemKey": f"{tid}:1:{call}",
    }


@pytest.fixture(autouse=True)
def rules_doc(monkeypatch):
    """The workspace's Rules document (a2a_repo stores it in the messaging table; here a dict)."""
    store: dict = {}

    async def get_rules(org):
        return store.get(org)

    async def put_rules_if(org, doc, read):
        if store.get(org) != read:
            return False
        store[org] = doc
        return True

    monkeypatch.setattr(a2a_repo, "get_rules", get_rules)
    monkeypatch.setattr(a2a_repo, "put_rules_if", put_rules_if)
    monkeypatch.setattr(questions, "audit", AsyncMock())
    return store


@pytest.fixture
def mode(monkeypatch):
    """The workspace's stored mode (default auto) and its members' emails."""
    state = {"approval_mode": "auto"}

    async def policy(_ws):
        return {"money_moves": True, "outreach": True, "publishing": True, "provisioning": True, **state}

    monkeypatch.setattr(billing_repo, "get_capability_policy", policy)
    monkeypatch.setattr(questions, "member_emails", AsyncMock(return_value={"sam@acme.dev"}))
    return state


async def _started():
    tid = await _task()
    await _running(tid)
    return tid


async def test_auto_member_mail_proceeds_and_is_logged(api, mode):
    tid = await _started()
    resp = await api.post(_url(), json=await _body(tid, "c1", "mail.send", {"to": "sam@acme.dev"}), headers=H)
    assert resp.json() == {"decision": "proceed"}
    row = await effects_repo.get_effect(WS, f"{tid}:1:c1")
    assert row["state"] == "intent" and row["questionId"] is None  # logged; the door and done route find it
    assert row["allowedBy"] == "mode:auto"  # and why it went ahead


async def _land(tid, call, ref="ses:1"):
    await effects_repo.mark_effect_done(WS, f"{tid}:1:{call}", disposition="done", result_ref=ref)


async def test_auto_duplicate_identical_send_replays_instead_of_sending_twice(api, mode):
    """Gate run 9 (task d440eab3): under Auto the Worker called send twice, 17 s apart, with the same mail. Two tool
    calls, two idemKeys, both went ahead: two emails. The second identical call now replays the first's result."""
    tid = await _started()
    ops = {"to": "sam@acme.dev", "subject": "Workshop", "body": "Moved to Friday."}
    first = await api.post(_url(), json=await _body(tid, "c1", "mail.send", ops), headers=H)
    assert first.json() == {"decision": "proceed"}
    await _land(tid, "c1")
    second = await api.post(_url(), json=await _body(tid, "c2", "mail.send", ops), headers=H)
    assert second.json()["decision"] == "replay" and second.json()["result"].endswith("; not sent again.")
    assert await effects_repo.get_effect(WS, f"{tid}:1:c2") is None  # no second row: one email


async def test_auto_send_with_a_different_body_still_goes(api, mode):
    tid = await _started()
    ops = {"to": "sam@acme.dev", "subject": "Workshop", "body": "Moved to Friday."}
    await api.post(_url(), json=await _body(tid, "c1", "mail.send", ops), headers=H)
    await _land(tid, "c1")
    other = await api.post(_url(), json=await _body(tid, "c2", "mail.send", {**ops, "body": "Room 4."}), headers=H)
    assert other.json() == {"decision": "proceed"}


async def test_auto_outsider_mail_asks(api, mode):
    tid = await _started()
    resp = await api.post(_url(), json=await _body(tid, "c1", "mail.send", {"to": "x@example.com"}), headers=H)
    assert resp.json()["decision"] == "needs_approval"


async def test_ask_mode_asks_for_a_member_mail_and_never_ask_does_not(api, mode):
    tid = await _started()
    mode["approval_mode"] = "ask"
    resp = await api.post(_url(), json=await _body(tid, "c1", "app.publish", {"appId": "a-1"}), headers=H)
    assert resp.json()["decision"] == "needs_approval"
    mode["approval_mode"] = "never_ask"
    resp = await api.post(_url(), json=await _body(tid, "c2", "mail.send", {"to": "x@example.com"}), headers=H)
    assert resp.json() == {"decision": "proceed"}
    assert (await effects_repo.get_effect(WS, f"{tid}:1:c2"))["state"] == "intent"


async def test_a_phone_callers_task_asks_even_where_the_mode_would_go_ahead(api, mode, monkeypatch):
    """A task a caller started from the phone line (trigger ref origin phone_contact) never spends, mails or publishes
    on Auto or Never ask: the owner says yes."""
    from core.repositories import tasks_repo

    async def trigger(_ws, tid):
        return {"ref": {"origin": "phone_contact"}} if tid == "voice:call" else None

    monkeypatch.setattr(tasks_repo, "get_trigger", trigger)
    plain, contact = await _started(), await _task(triggerId="voice:call")
    await _running(contact)
    member = {"to": "sam@acme.dev"}
    assert (await api.post(_url(), json=await _body(plain, "c1", "mail.send", member), headers=H)).json() == {
        "decision": "proceed"
    }
    mode["approval_mode"] = "never_ask"
    for n, (kind, ops) in enumerate(
        [
            ("mail.send", member),
            ("purchase", {"amount": "$1"}),
            ("app.publish", {"appId": "a"}),
            ("a2a.send", {"to": "agent:x"}),
        ]
    ):
        resp = await api.post(_url(), json=await _body(contact, f"c{n}", kind, ops), headers=H)
        assert resp.json()["decision"] == "needs_approval", kind


async def test_contact_task_cannot_purchase(api, mode, monkeypatch):
    """End-state Task 4: a sub-task of a caller's task carries `origin` on its own row (its trigger is a plain chat
    line), and even on Never ask its purchase waits for the owner's card."""
    mode["approval_mode"] = "never_ask"
    sub = await _task(origin="phone_contact")
    await _running(sub)
    resp = await api.post(
        _url(), json=await _body(sub, "c1", "purchase", {"merchant": "Acme", "amount": "1"}), headers=H
    )
    assert resp.json()["decision"] == "needs_approval" and resp.json()["questionId"]


async def test_meeting_spend_always_carded_even_in_auto_mode(api, mode):
    """A task started from a Meet (confirmAll) asks for a spend, an outside send or a publish whatever the mode."""
    plain, meeting = await _started(), await _task(confirmAll=True)
    await _running(meeting)
    mode["approval_mode"] = "never_ask"
    ops = {"to": "sam@acme.dev"}
    assert (await api.post(_url(), json=await _body(plain, "c1", "mail.send", ops), headers=H)).json() == {
        "decision": "proceed"
    }
    for n, (kind, o) in enumerate(
        [("mail.send", ops), ("purchase", {"amount": "$1"}), ("app.publish", {"appId": "a"})]
    ):
        resp = await api.post(_url(), json=await _body(meeting, f"m{n}", kind, o), headers=H)
        assert resp.json()["decision"] == "needs_approval", kind


async def test_always_allow_rule_lets_the_second_identical_action_through(api, mode, monkeypatch):
    monkeypatch.setattr(questions, "_admins", AsyncMock(return_value=["user_ada"]))
    tid = await _started()
    ops = {"to": "x@example.com"}
    first = await api.post(_url(), json=await _body(tid, "c1", "mail.send", ops), headers=H)
    assert first.json()["decision"] == "needs_approval"
    await questions.add_always_allow(WS, "mail.send", ops, by="user_sam")
    second = await api.post(_url(), json=await _body(tid, "c2", "mail.send", ops), headers=H)
    assert second.json() == {"decision": "proceed"}
    assert (await effects_repo.get_effect(WS, f"{tid}:1:c2"))["allowedBy"].startswith("rule:always-")
    # Another recipient is not covered by the rule.
    other = await api.post(_url(), json=await _body(tid, "c3", "mail.send", {"to": "y@example.com"}), headers=H)
    assert other.json()["decision"] == "needs_approval"


@pytest.mark.parametrize(
    ("kind", "operands"), [("purchase", {"merchant": "Acme", "amount": "999"}), ("provision", {"provider": "neon"})]
)
async def test_an_existing_always_allow_rule_never_lets_money_or_a_provider_skip_its_card(
    api, mode, monkeypatch, rules_doc, kind, operands
):
    """A rule an older card wrote (before money was never remembered) binds the merchant or provider; it is ignored,
    so the purchase still faces the mode and its cap, and the provision still asks. A new one is never written."""
    from core.services import a2a_rules

    key = "merchant" if kind == "purchase" else "provider"
    rules = a2a_rules.default_rules(WS, ["user_ada"])
    rules.rules.append(
        a2a_rules.Rule(
            id="always-old",
            resource="agent:*",
            skill=kind,
            requesters=[f"org:{WS}"],
            effect="allow",
            when=a2a_rules.When(operands_equal={key: operands[key]}),
        )
    )
    rules_doc[WS] = rules.model_dump(exclude_none=True)
    assert await questions.rule_effect(WS, None, kind, operands) is None
    mode["approval_mode"] = "ask"
    tid = await _started()
    resp = await api.post(_url(), json=await _body(tid, "c1", kind, operands), headers=H)
    assert resp.json()["decision"] == "needs_approval"
    monkeypatch.setattr(questions, "_admins", AsyncMock(return_value=["user_ada"]))
    before = dict(rules_doc)
    await questions.add_always_allow(WS, kind, operands, by="user_sam")
    assert rules_doc == before


async def test_add_always_allow_keeps_the_a2a_defaults_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(questions, "_admins", AsyncMock(return_value=["user_ada"]))
    await questions.add_always_allow(WS, "mail.send", {"to": "x@example.com"}, by="user_sam")
    await questions.add_always_allow(WS, "mail.send", {"to": "x@example.com"}, by="user_sam")
    rules = Rules.model_validate(await a2a_repo.get_rules(WS))
    ids = [r.id for r in rules.rules]
    assert "r1" in ids and rules.holders["agent:*"] == ["user_ada"]  # the A2A defaults survive
    [mine] = [r for r in rules.rules if r.skill == "mail.send"]
    assert (mine.resource, mine.requesters, mine.effect) == ("agent:*", [f"org:{WS}"], "allow")
    assert mine.when.operands_equal == {"to": "x@example.com"}
    assert rules.revision not in ("", "default")


async def test_add_always_allow_retries_over_a_concurrent_rules_change(monkeypatch, rules_doc):
    """An admin's rules PUT landing between the read and the write is never overwritten: the write is conditional on
    the revision read, and the retry re-reads the admin's document and appends to it."""
    monkeypatch.setattr(questions, "_admins", AsyncMock(return_value=["user_ada"]))
    admin_doc = {"revision": "admin-1", "holders": {}, "rules": [], "default": "hold"}
    calls = {"n": 0}

    async def racing_get(org):
        calls["n"] += 1
        if calls["n"] == 1:
            rules_doc[org] = admin_doc  # the admin's PUT lands after this read
            return None
        return rules_doc.get(org)

    monkeypatch.setattr(a2a_repo, "get_rules", racing_get)
    await questions.add_always_allow(WS, "mail.send", {"to": "x@example.com"}, by="user_sam")
    stored = Rules.model_validate(rules_doc[WS])
    assert [r.skill for r in stored.rules] == ["mail.send"] and stored.holders == {}  # built on the admin's doc
    questions.audit.assert_awaited_once()


async def test_add_always_allow_gives_up_after_three_lost_races(monkeypatch, rules_doc):
    monkeypatch.setattr(a2a_repo, "put_rules_if", AsyncMock(return_value=False))
    monkeypatch.setattr(questions, "_admins", AsyncMock(return_value=[]))
    with pytest.raises(questions.RulesBusy):
        await questions.add_always_allow(WS, "mail.send", {"to": "x@example.com"}, by="user_sam")
    assert a2a_repo.put_rules_if.await_count == 3


async def test_settings_lists_and_removes_always_allow_rules(monkeypatch):
    """Settings › Approvals: the rules a card wrote, listed by kind and target; Remove drops one (that action asks
    again) and keeps the A2A defaults and the other rule; an A2A rule of an admin's is never removed from here."""
    monkeypatch.setattr(questions, "_admins", AsyncMock(return_value=["user_ada"]))
    await questions.add_always_allow(WS, "mail.send", {"to": "x@example.com"}, by="user_sam")
    await questions.add_always_allow(WS, "app.publish", {"appId": "app-1"}, by="user_sam")
    listed = await questions.always_allow_rules(WS)
    assert [(r["kind"], r["key"], r["value"]) for r in listed] == [
        ("mail.send", "to", "x@example.com"),
        ("app.publish", "appId", "app-1"),
    ]
    assert await questions.remove_always_allow(WS, listed[0]["id"], by="user_ada") is True
    assert [r["kind"] for r in await questions.always_allow_rules(WS)] == ["app.publish"]
    assert "r1" in [r.id for r in Rules.model_validate(await a2a_repo.get_rules(WS)).rules]
    assert await questions.remove_always_allow(WS, listed[0]["id"], by="user_ada") is False  # already gone
    assert await questions.remove_always_allow(WS, "r1", by="user_ada") is False  # not an Always-allow rule
