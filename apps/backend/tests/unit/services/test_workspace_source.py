"""T12: the source a person reports and their stored touches follow them onto the workspace."""

import time
from unittest.mock import MagicMock, patch

import pytest

from core.services import webhook_dedup, workspace_source


class _Table:
    def __init__(self):
        self.items: dict = {}

    def put_item(self, Item, **_):
        self.items[Item["event_id"]] = Item

    def get_item(self, Key, **_):
        item = self.items.get(Key["event_id"])
        return {"Item": item} if item else {}


@pytest.fixture
def table():
    t = _Table()
    with patch.object(webhook_dedup, "_table", return_value=t):
        yield t


def test_answers_and_touches_are_cleaned_never_refused():
    assert workspace_source.clean_answer("reddit", "  r/startups  thread ") == ("reddit", "r/startups thread")
    assert workspace_source.clean_answer("bogus", "x") == (None, None)
    touch = {"utm_source": "x", "landing_path": "/s", "evil": "drop me", "referrer": None}
    assert workspace_source.clean_touch(touch) == {"utm_source": "x", "landing_path": "/s"}
    assert workspace_source.clean_touch("nope") == {}


@pytest.mark.asyncio
async def test_the_stash_moves_onto_the_workspace_once(table):
    await workspace_source.stash_for_user(
        "user_a", answer="hacker_news", detail="launch thread", first_touch={"utm_source": "hn", "landing_path": "/"}
    )
    props = await workspace_source.adopt_for_workspace("org_1", "user_a")
    assert props == {
        "self_reported_source": "hacker_news",
        "has_self_reported_detail": True,
        "source": "sign_up",
        "first_touch_utm_source": "hn",
        "first_touch_landing_path": "/",
    }
    assert "ttl" not in table.items["ws-source:org_1"]  # the workspace keeps it
    # A later stash by the same person does not rewrite this workspace's origin.
    await workspace_source.stash_for_user("user_a", answer="x")
    assert (await workspace_source.adopt_for_workspace("org_1", "user_a"))["self_reported_source"] == "hacker_news"


@pytest.mark.asyncio
async def test_an_expired_or_missing_stash_is_nothing(table):
    assert await workspace_source.adopt_for_workspace("org_2", "user_b") == {}
    await workspace_source.stash_for_user("user_b", answer="x")
    table.items["ws-source:user_b"]["ttl"] = int(time.time()) - 1
    assert await workspace_source.adopt_for_workspace("org_2", "user_b") == {}


@pytest.mark.asyncio
async def test_adopt_never_raises():
    with patch.object(webhook_dedup, "_table", MagicMock(side_effect=RuntimeError("no table"))):
        assert await workspace_source.adopt_for_workspace("org_3", "user_c") == {}


@pytest.mark.asyncio
async def test_the_session_page_door_names_itself(table):
    """T16: "Start your own workspace" on a session page -> workspace_created {source: session_link}."""
    await workspace_source.stash_for_user("user_d", door="session_link")
    assert (await workspace_source.adopt_for_workspace("org_4", "user_d"))["source"] == "session_link"
