"""META.lastMessage: the room-list preview and its sort (rooms F9, PR 1).

Written inside the message transaction for top-level messages only, refreshed
or replaced inside the edit/delete transaction, so a deleted message's text is
never readable from META.
"""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    OWNER,
    USER_A,
    _post,
    _room,
    repo,
    table,
)

_INDEX = Path(__file__).resolve().parents[4] / "infra" / "lambda" / "messaging-projector" / "index.py"


@pytest.fixture(autouse=True)
def _no_user_lookups(monkeypatch):
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))


async def _last(room_id):
    return (await repo.get_room(room_id))["lastMessage"]


@pytest.mark.asyncio
async def test_new_room_has_an_explicit_null_preview(table):
    room_id, meta = await _room()
    assert meta["lastMessage"] is None
    assert await _last(room_id) is None


@pytest.mark.asyncio
async def test_top_level_message_sets_the_preview_and_thread_replies_do_not(table):
    room_id, _ = await _room()
    root = await _post(room_id, text="## Ship **it** [now](https://x.y)\n- today", client_id="m-1")
    assert await _last(room_id) == {
        "messageId": root["messageId"],
        "seq": 1,
        "authorId": USER_A,
        "authorKind": "human",
        "authorName": "Ann",
        "textPreview": "Ship it now today",
        "hasAttachments": False,
        "createdAt": root["createdAt"],
        "threadRootMessageId": None,
    }
    await _post(room_id, text="a reply", client_id="m-2", thread_root=root["messageId"])
    assert (await _last(room_id))["messageId"] == root["messageId"]


@pytest.mark.asyncio
async def test_preview_is_capped_at_140_chars_and_keeps_mention_tokens(table):
    room_id, _ = await _room()
    await _post(room_id, text="@friday " + "x" * 300)
    preview = (await _last(room_id))["textPreview"]
    assert len(preview) == 140
    assert preview.startswith("@friday xxx")


@pytest.mark.asyncio
async def test_editing_the_preview_message_refreshes_it_and_older_edits_do_not(table):
    room_id, _ = await _room()
    await _post(room_id, text="first", client_id="m-1")
    await _post(room_id, text="second", client_id="m-2")
    await repo.update_message(OWNER, room_id, 2, USER_A, "e-2", 0, "second, fixed")
    assert (await _last(room_id))["textPreview"] == "second, fixed"
    await repo.update_message(OWNER, room_id, 1, USER_A, "e-1", 0, "first, fixed")
    assert (await _last(room_id))["textPreview"] == "second, fixed"


@pytest.mark.asyncio
async def test_deleting_the_preview_falls_back_to_the_newest_live_top_level_message(table):
    room_id, _ = await _room()
    first = await _post(room_id, text="first", client_id="m-1")
    await _post(room_id, text="gone too", client_id="m-2")
    await _post(room_id, text="latest", client_id="m-3")
    await _post(room_id, text="reply", client_id="m-4", thread_root=first["messageId"])
    await repo.update_message(OWNER, room_id, 2, USER_A, "d-2", 0, None)
    await repo.update_message(OWNER, room_id, 3, USER_A, "d-3", 0, None)
    last = await _last(room_id)
    assert (last["messageId"], last["textPreview"]) == (first["messageId"], "first")
    await repo.update_message(OWNER, room_id, 1, USER_A, "d-1", 0, None)
    assert await _last(room_id) is None


@pytest.mark.asyncio
async def test_delete_racing_a_newer_message_retries_and_keeps_the_newer_preview(table, monkeypatch):
    room_id, _ = await _room()
    await _post(room_id, text="old", client_id="m-1")
    stale = await repo.get_room(room_id)  # lastMessage = seq 1
    newer = await _post(room_id, text="newer", client_id="m-2")
    real_get_room = repo.get_room
    reads = iter([stale])

    async def get_room(rid):
        return next(reads, None) or await real_get_room(rid)

    monkeypatch.setattr(repo, "get_room", get_room)
    await repo.update_message(OWNER, room_id, 1, USER_A, "d-1", 0, None)
    assert (await real_get_room(room_id))["lastMessage"]["messageId"] == newer["messageId"]
    assert (await repo.get_message(room_id, 1))["deletedAt"]


@pytest.mark.asyncio
async def test_room_list_carries_the_preview_and_sorts_by_latest_top_level_message(table):
    busy, _ = await _room(client_room_id="c-busy", name="busy")
    quiet, _ = await _room(client_room_id="c-quiet", name="quiet")
    empty, _ = await _room(client_room_id="c-empty", name="empty")
    root = await _post(busy, text="early", client_id="b-1")
    await _post(quiet, text="later", client_id="q-1")
    # A thread reply and a rename bump neither the order nor the preview.
    await _post(busy, text="reply", client_id="b-2", thread_root=root["messageId"])
    await repo.patch_room_meta(busy, {"name": "busy!"})

    rooms = (await messaging.list_rooms(AuthContext(user_id=USER_A, org_id=OWNER))).rooms
    assert [str(r.room_id) for r in rooms] == [quiet, busy, empty]
    by_id = {str(r.room_id): r for r in rooms}
    assert by_id[busy].last_message.text_preview == "early"
    assert by_id[busy].last_message_at == by_id[busy].last_message.created_at
    assert by_id[empty].last_message is None
    dumped = by_id[quiet].model_dump(mode="json")
    assert dumped["lastMessage"]["textPreview"] == "later"


def test_backend_preview_matches_the_projector_copy():
    spec = importlib.util.spec_from_file_location("messaging_projector_index", _INDEX)
    projector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(projector)
    message = {
        "messageId": "m",
        "messageSeq": 7,
        "author": {"kind": "agent", "id": "a", "displayName": "Friday"},
        "text": "```py\nx\n```\n> **quoted** `code` ~~gone~~ \\_\\_init\\_\\_ 3\\*4 \\`x\\` " + "y" * 200,
        "attachments": [{"id": "f"}],
        "createdAt": "2026-09-24T00:00:00Z",
        "threadRootMessageId": None,
    }
    assert projector._last_message(message) == repo.last_message_preview(message)


def test_preview_shows_the_composers_escaped_characters_as_typed(table):
    assert repo._plain_preview("\\_\\_init\\_\\_ and 3\\*4 then 5\\*6, \\*\\*not bold\\*\\*") == (
        "__init__ and 3*4 then 5*6, **not bold**"
    )
    # A typed `x` keeps its backticks; real code marks still go.
    assert repo._plain_preview("\\# 1: use \\`x\\` here, `code`") == "# 1: use `x` here, code"
