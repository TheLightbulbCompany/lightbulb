"""Admission rules for non-chat sources (wave-2 lane A, Task 2; review C6)."""

import pytest

from core import tasks_schema as ts
from core.services.admission_rules import SourceTrigger, Verdict, decide, enforce


def _t(source, *, addressed=False, actor="system", thread=None, status=None):
    return SourceTrigger(source, addressed, actor, thread, status)


@pytest.mark.parametrize("source", ["schedule", "manual", "webhook", "voice", "a2a"])
@pytest.mark.parametrize("actor", ["human", "bot", "ci", "system"])
@pytest.mark.parametrize("addressed", [True, False])
def test_matrix_deterministic_sources(source, actor, addressed):
    v = decide(_t(source, addressed=addressed, actor=actor))
    assert (v.admission, v.deterministic) == ("respond", True)


@pytest.mark.parametrize("source", ["mail", "text"])
@pytest.mark.parametrize("addressed", [True, False])
def test_matrix_mail_text_from_people(source, addressed):
    v = decide(_t(source, addressed=addressed, actor="human"))
    assert (v.admission, v.deterministic) == ("respond", False)


@pytest.mark.parametrize("source", ["mail", "text"])
@pytest.mark.parametrize("actor", ["bot", "ci"])
def test_matrix_bot_without_subscription_ignored(source, actor):
    assert decide(_t(source, actor=actor)) == Verdict("ignore", False, "bot_unsubscribed")


@pytest.mark.parametrize("status", ["queued", "working", "waiting", "idle"])
@pytest.mark.parametrize("actor", ["human", "bot", "ci"])
def test_open_subscribed_thread_passes_through(status, actor):
    v = decide(_t("mail", actor=actor, thread="mail:t1", status=status))
    assert v.admission == "pass_through"


def test_ci_event_never_wakes_ended_task():
    for status in ("resolved", "failed"):
        v = decide(_t("mail", actor="ci", thread="mail:t1", status=status))
        assert v == Verdict("ignore", False, "ended_task_bot")


def test_human_on_ended_task_thread_is_new_work():
    v = decide(_t("mail", actor="human", thread="mail:t1", status="resolved"))
    assert v.admission == "respond"


def test_bot_mail_on_subscribed_thread_passes_through_while_open():
    assert decide(_t("mail", actor="bot", thread="mail:t1", status="working")).admission == "pass_through"
    assert decide(_t("mail", actor="bot", thread="mail:t1", status="resolved")).admission == "ignore"


def _start(i):
    return ts.StartTask(kind="start_task", trigger_ids=["sched:s1:1"], title=f"t{i}", brief="b", ack="a")


def test_enforce_keeps_only_deterministic_start():
    reply = ts.Reply(kind="reply", trigger_ids=["sched:s1:1"], text="hi")
    out = enforce([reply, _start(1), _start(2)], decide(_t("schedule")))
    assert out == [_start(1)]
    # dict-shaped decisions (as loaded from a Trigger ref) work too
    as_dict = _start(1).model_dump(by_alias=True)
    assert enforce([{"kind": "reply"}, as_dict], decide(_t("webhook"))) == [as_dict]


def test_enforce_ignore_drops_all():
    v = decide(_t("text", actor="bot"))
    assert enforce([_start(1)], v) == []


def test_enforce_leaves_model_decisions_for_mail():
    reply = ts.Reply(kind="reply", trigger_ids=["mail:m1"], text="hi")
    assert enforce([reply, _start(1)], decide(_t("mail", actor="human"))) == [reply, _start(1)]
