"""Spec 05 P1 (dev, super-agent switch): a provisioning consent asked from a task run is a Question on that task,
and the Question's answer approves or rejects the workflow. Switch off or no task: the board card, unchanged."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import provisioning_questions as pq
from core.services import task_asks

from .test_provisioning_workflow import CONFIG, PROVIDER, SERVICE, _row, mod  # noqa: F401 -- autouse fixtures below
from .test_provisioning_workflow import account, cards, catalog_unchanged, no_network, projects_ready  # noqa: F401

QREF = {"taskId": "t-1", "qid": "q-1", "workflowId": "pwf_1"}


async def _start(**kw):
    with (
        patch.object(mod.api, "eligibility", AsyncMock(return_value={"is_eligible": True, "requirements": []})),
        patch.object(mod.repo, "create", AsyncMock(return_value=_row())),
        patch.object(
            mod.repo,
            "transition",
            AsyncMock(side_effect=lambda wf, **k: _row(state=k["to_state"], **k.get("fields", {}))),
        ) as move,
    ):
        row = await mod.start(
            owner_id="org_a",
            provider_name="posthog",
            service_id="svc_analytics",
            configuration=CONFIG,
            issue_id=None,
            **kw,
        )
    return row, move


@pytest.mark.asyncio
async def test_task_run_ask_opens_a_question_not_a_card(monkeypatch):
    ask = AsyncMock(return_value=QREF)
    monkeypatch.setattr(pq, "ask_consent", ask)
    row, move = await _start(task_id="t-1", reason="the signup page takes payments")
    assert row["state"] == "awaiting_approval" and row["question"] == QREF and "approval_id" not in row
    mod.provisioning_approvals.request_provider_consent.assert_not_awaited()
    owner, task_id, wf, snapshot, reason, key = ask.await_args.args
    assert (owner, task_id, wf, reason, key) == ("org_a", "t-1", "pwf_1", "the signup page takes payments", "pwf_1")


@pytest.mark.asyncio
async def test_question_refused_files_the_card(monkeypatch):
    monkeypatch.setattr(pq, "ask_consent", AsyncMock(side_effect=task_asks.AskRefused("task_closed")))
    row, _ = await _start(task_id="t-1")
    assert row["approval_id"] == "apr_1" and "question" not in row
    mod.provisioning_approvals.request_provider_consent.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_task_files_the_card_as_before(monkeypatch):
    ask = AsyncMock()
    monkeypatch.setattr(pq, "ask_consent", ask)
    row, _ = await _start()
    assert row["approval_id"] == "apr_1"
    ask.assert_not_awaited()


@pytest.mark.parametrize(
    "answer,expect",
    [
        (("open", None), None),
        (("approved", "user_ada"), ("approved", "user_ada")),
        (("rejected", None), ("rejected", None)),
    ],
)
@pytest.mark.asyncio
async def test_reconcile_reads_the_question(monkeypatch, answer, expect):
    monkeypatch.setattr(pq, "answer_of", AsyncMock(return_value=answer))
    approved = AsyncMock(return_value={"state": "connecting_provider"})
    rejected = AsyncMock(return_value={"state": "rejected"})
    monkeypatch.setattr(mod, "on_approved", approved)
    monkeypatch.setattr(mod, "on_rejected", rejected)
    row = _row(state="awaiting_approval", question=QREF)
    out = await mod._reconcile_approval(row)
    if expect is None:
        assert out is row and not approved.await_count and not rejected.await_count
    elif expect[0] == "approved":
        assert approved.await_args.kwargs["approved_by"] == "user_ada"
    else:
        rejected.assert_awaited_once_with("pwf_1")


@pytest.mark.parametrize(
    "q,expect",
    [
        (None, ("rejected", None)),
        ({"state": "cancelled"}, ("rejected", None)),
        ({"state": "open"}, ("open", None)),
        ({"state": "answered", "answer": {"optionId": "allow_once"}, "answeredBy": "u"}, ("approved", "u")),
        ({"state": "answered", "answer": {"optionId": "deny"}, "answeredBy": "u"}, ("rejected", "u")),
    ],
)
@pytest.mark.asyncio
async def test_answer_of(monkeypatch, q, expect):
    monkeypatch.setattr(pq.questions_repo, "get_question", AsyncMock(return_value=q))
    assert await pq.answer_of("org_a", QREF) == expect


@pytest.mark.asyncio
async def test_task_for_run_needs_a_live_run_and_its_secret(monkeypatch):
    att = {"taskId": "t-1", "runId": "run-1", "runSecret": "s3cret"}
    monkeypatch.setattr("core.services.effect_doors.live_attempts", AsyncMock(return_value=[att]))
    monkeypatch.setattr(pq.tasks_repo, "get_task", AsyncMock(return_value={"taskId": "t-1"}))
    assert await pq.task_for_run("org_a", "run-1", "s3cret") == {"taskId": "t-1"}
    assert await pq.task_for_run("org_a", "run-1", "wrong") is None  # another run's id without its secret: card
    assert await pq.task_for_run("org_a", "run-1", None) is None
    assert await pq.task_for_run("org_a", "run-2", "s3cret") is None
    assert await pq.task_for_run("org_a", None, "s3cret") is None


@pytest.mark.asyncio
async def test_ask_consent_opens_a_blocking_admin_approval(monkeypatch):
    monkeypatch.setattr(pq.tasks_repo, "get_task", AsyncMock(return_value={"taskId": "t-1", "attemptCount": 2}))
    monkeypatch.setattr(pq.tasks_repo, "get_attempt", AsyncMock(return_value={"runId": "run-2"}))
    opened = AsyncMock(return_value={"qid": "q-x"})
    monkeypatch.setattr(pq.task_asks, "open_ask", opened)
    snap = mod.snapshot_of(SERVICE, PROVIDER, CONFIG, environment="Signup — live")
    ref = await pq.ask_consent("org_a", "t-1", "pwf_1", snap, "for the signup page", "pwf_1")
    assert ref == {"taskId": "t-1", "qid": "q-x", "workflowId": "pwf_1"}
    kw = opened.await_args.kwargs
    assert (kw["kind"], kw["subkind"], kw["blocking"], kw["attempt"], kw["run_id"]) == (
        "approval",
        "provider_consent",
        True,
        2,
        "run-2",
    )
    assert kw["title"] == "set up posthog svc_analytics for Signup — live"
    assert kw["options"] == [{"id": "allow_once", "label": "Allow"}, {"id": "deny", "label": "Deny"}]
    assert kw["qid"] == pq.questions.qid_for("provision:pwf_1")


def test_paid_consent_states_price_recurrence_and_limit():
    paid = {**SERVICE, "pricing": {"type": "paid", "paid_pricing": [{"freeform": "$20/mo", "is_default": True}]}}
    snap = mod.snapshot_of(paid, PROVIDER, CONFIG)
    assert pq._money(snap) == (
        "Price: $20/mo, billed to your card on your Stripe account. Spending limit: $50 a month."
    )
    assert pq._money(mod.snapshot_of(SERVICE, PROVIDER, CONFIG)) == ""  # free: nothing to bill
