"""Owner mode (owner.py): the Router session's tools name the member speaking and hit the owner routes."""

import pytest
from livekit.plugins import google, openai

from huddle_agent import owner
from huddle_agent.backend import Backend, BackendError


def _agent():
    return owner.OwnerAgent(Backend("r", "o", "t"), "user_maya", {"name": "Nova", "who": "Maya", "today": "Sunday"})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("LIGHTBULB_API_URL", "http://backend.test")
    monkeypatch.setenv("LIGHTBULB_HUDDLE_SECRET", "s")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")


async def test_every_tool_names_the_member_and_its_route():
    a, sent = _agent(), []

    async def owner_op(op, body):
        sent.append((op, body))
        return {"already": True} if op == "start-work" else {}

    a._b.owner = owner_op
    await a.recall_memory("holiday hours?")
    await a.call_stats(1)
    await a.calendar("2026-11-09")
    await a.list_integrations()
    await a.read_integration("calendar", "tomorrow")
    out = await a.start_work("Draft a post about holiday hours", "A short post saying we close Dec 25.")
    assert [op for op, _ in sent] == ["read", "read", "read", "integrations", "integration-read", "start-work"]
    assert {b["speakerUserId"] for _, b in sent} == {"user_maya"}
    assert out["say"] == "already on it"


async def test_a_backend_error_is_a_sentence_not_a_crash():
    a = _agent()

    async def fail(op, body):
        raise BackendError(403, "not_a_member", None)

    a._b.owner = fail
    assert await a.start_work("x", "y") == {"error": "not_a_member"}


def test_the_engine_is_openai_unless_gemini_is_asked_for(monkeypatch):
    monkeypatch.delenv("OWNER_ENGINE", raising=False)
    assert isinstance(owner.realtime_model(), openai.realtime.RealtimeModel)
    monkeypatch.setenv("OWNER_ENGINE", "gemini")
    assert isinstance(owner.realtime_model(), google.realtime.RealtimeModel)


async def test_the_talk_is_kept_with_its_transcript_and_started_tasks():
    a = _agent()

    async def owner_op(op, body):
        return {"started": True, "taskId": "k-1"} if body["title"] == "Post" else {"error": "x"}

    a._b.owner = owner_op
    await a.start_work("Post", "Holiday hours.")
    await a.start_work("Post", "Holiday hours.")  # the same task once
    await a.start_work("Broken", "fails")
    items = [{"role": "assistant", "text": "Drafting it.", "t": 2.0, "interrupted": False},
             {"role": "user", "text": "Draft the post", "t": 0.5, "interrupted": False},
             {"role": "assistant", "text": "cut off", "t": 3.0, "interrupted": True}]  # fmt: skip
    body = owner.talk_record(a, items, "job_1", 42.7)
    assert body == {
        "speakerUserId": "user_maya", "sessionId": "job_1", "durationSec": 42,
        "transcript": [{"who": "caller", "text": "Draft the post", "t": 0.5},
                       {"who": "assistant", "text": "Drafting it.", "t": 2.0}],
        "work": [{"title": "Post", "taskId": "k-1"}],
    }  # fmt: skip


def test_owner_has_approve_tool():
    assert "approve" in {t.info.name for t in _agent().tools}


async def test_approve_posts_route():
    a, sent = _agent(), []

    async def owner_op(op, body):
        sent.append((op, body))
        return {"confirm_sent": True, "title": "x"}

    a._b.owner = owner_op
    assert await a.approve("yes", "the supply order") == {"confirm_sent": True, "title": "x"}
    assert sent == [("approve", {"phrase": "yes", "hint": "the supply order", "speakerUserId": "user_maya"})]


def test_the_routers_state_is_in_the_instructions():
    a = owner.OwnerAgent(
        Backend("r", "o", "t"), "u", {"name": "Nova", "who": "Maya", "today": "Sunday", "context": "- [k-1] Post: done"}
    )
    assert a.instructions.endswith("- [k-1] Post: done")
    assert "What you know" not in _agent().instructions  # no context, no empty section


async def test_spoken_lines_post_to_the_dm_in_order():
    import asyncio

    sent = []

    class B:
        async def owner(self, op, body):
            await asyncio.sleep(0.01 if body["n"] == 0 else 0)  # the first post is the slow one
            sent.append((op, body))
            if body["n"] == 1:
                raise BackendError(500, "x", None)  # one failure does not stop the next
            return {}

    t = owner.Turns(B(), "user_maya", "job_1")
    t.add("user", "Draft the post")
    t.add("assistant", "On it.")
    t.add("user", "Thanks")
    await t.flush()
    assert [(b["n"], b["role"], b["text"]) for _, b in sent] == [
        (0, "user", "Draft the post"), (1, "assistant", "On it."), (2, "user", "Thanks")
    ]
    assert {(op, b["sessionId"], b["speakerUserId"]) for op, b in sent} == {("turn", "job_1", "user_maya")}
