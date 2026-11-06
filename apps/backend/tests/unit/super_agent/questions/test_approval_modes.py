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
    assert questions.kind_and_options("purchase", {"merchant": "Acme"}) == (
        "approval",
        ["allow_once", "allow_always", "deny"],
    )
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
    assert second.json() == {"decision": "replay", "result": "ses:1"}
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
