import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from core.services import apps_idle

NOW = datetime(2026, 11, 30, tzinfo=timezone.utc)
AGENT = str(uuid.uuid4())
CEO = str(uuid.uuid4())


def _app(name="Tracker", **kw):
    return {
        "app_id": "app_" + name,
        "name": name,
        "state": "live",
        "room_id": str(uuid.uuid4()),
        "actor_id": AGENT,
        "actor_kind": "agent",
        "origin": {"kind": "asked", "ref": "m1"},
        "last_opened_at": (NOW - timedelta(days=31)).isoformat(),
        "idle_posted_at": None,
        "versions": [],
        **kw,
    }


@pytest.fixture
def env(monkeypatch):
    env = type("E", (), {})()
    env.apps = []
    env.post = AsyncMock()
    env.patch = AsyncMock()
    monkeypatch.setattr(apps_idle.apps_repo, "list_for_owner", AsyncMock(side_effect=lambda o: env.apps))
    monkeypatch.setattr(apps_idle.apps_repo, "patch", env.patch)
    monkeypatch.setattr(apps_idle.messaging, "post_agent_message", env.post)
    monkeypatch.setattr(
        apps_idle.owner_facts,
        "_lookup_teammates",
        AsyncMock(
            return_value=[
                {"id": AGENT, "name": "a", "role": "x", "is_ceo": False},
                {"id": CEO, "name": "c", "role": "ceo", "is_ceo": True},
            ]
        ),
    )
    return env


@pytest.mark.asyncio
async def test_posts_once_per_30_days_for_idle_live_apps(env):
    env.apps = [_app()]
    assert await apps_idle.post_idle_lines("o1", now=NOW) == 1
    req = env.post.await_args.args[1]
    assert str(req.agent_id) == AGENT
    assert req.text == (
        "Tracker (asked for in this room) has not been opened in 30 days. "
        "Say 'remove Tracker' if it's no longer needed."
    )
    assert env.patch.await_args.args[1] == {"idle_posted_at": NOW.isoformat()}
    # recorded -> not again inside 30 days
    env.post.reset_mock()
    env.apps = [_app(idle_posted_at=(NOW - timedelta(days=5)).isoformat())]
    assert await apps_idle.post_idle_lines("o1", now=NOW) == 0
    env.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_recently_opened_and_destroyed(env):
    env.apps = [
        _app("A", last_opened_at=(NOW - timedelta(days=3)).isoformat()),
        _app("B", state="destroyed"),
        _app("C", state="building"),
    ]
    assert await apps_idle.post_idle_lines("o1", now=NOW) == 0
    env.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_never_opened_uses_first_live_version_and_ceo_fallback(env):
    old = (NOW - timedelta(days=40)).isoformat()
    env.apps = [_app(last_opened_at=None, actor_kind="human", versions=[{"n": 1, "live": True, "at": old}])]
    assert await apps_idle.post_idle_lines("o1", now=NOW) == 1
    assert str(env.post.await_args.args[1].agent_id) == CEO
    env.apps = [_app("Y", last_opened_at=None, versions=[{"n": 1, "live": True, "at": NOW.isoformat()}])]
    assert await apps_idle.post_idle_lines("o1", now=NOW) == 0


@pytest.mark.asyncio
async def test_post_failure_does_not_patch(env):
    env.apps = [_app()]
    env.post.side_effect = RuntimeError("nope")
    assert await apps_idle.post_idle_lines("o1", now=NOW) == 0
    env.patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_app_with_a_conversation_hears_there_and_one_with_no_room_is_skipped(env):
    conversation = str(uuid.uuid4())
    env.apps = [_app("Solo", room_id=None), _app("Talk", conversation_id=conversation)]
    assert await apps_idle.post_idle_lines("o1", now=NOW) == 1
    req = env.post.await_args.args[1]
    assert str(req.room_id) == conversation
    assert req.text == "Talk has not been opened in 30 days. Say 'remove Talk' if it's no longer needed."
