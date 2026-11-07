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
