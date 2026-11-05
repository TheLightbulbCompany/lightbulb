"""One malformed stored message never fails its room's read; valid rows read exactly as before (5 Nov hardening)."""

import uuid
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging

ROOM = str(uuid.UUID(int=99))
ATT = {"id": str(uuid.UUID(int=7)), "fileName": "a.png", "contentType": "image/png", "size": 10}


def _row(seq=1, **over):
    return {
        "roomId": ROOM,
        "messageId": str(uuid.UUID(int=seq)),
        "messageSeq": seq,
        "roomRevision": seq,
        "author": {"kind": "human", "id": "user_sam", "displayName": "Sam"},
        "text": "hi",
        "mentions": [{"kind": "agent", "id": "a1"}],
        "createdAt": "2026-11-05T10:00:00Z",
        "attachments": [ATT],
        "reactions": {"👍": ["user_sam"]},
    } | over


@pytest.mark.parametrize(
    "over,check",
    [
        (
            {"author": {"kind": "human", "id": "u", "displayName": "x" * 300, "avatar": "y"}},
            lambda m: m["author"] == {"kind": "human", "id": "u", "displayName": "x" * 256},
        ),
        (
            {"mentions": [{"kind": "agent", "id": "a1", "handle": "z"}]},
            lambda m: m["mentions"] == [{"kind": "agent", "id": "a1"}],
        ),
        ({"via": "rolled_back_value"}, lambda m: "via" not in m),
        (
            {"attachments": [ATT, {**ATT, "contentType": "image/heic"}, {**ATT, "fileName": "a/b"}]},
            lambda m: m["attachments"] == [ATT],
        ),
        (
            {"routedTo": {"taskId": "t", "n": 3}, "forwardedFrom": "x"},
            lambda m: m["routedTo"] == {"taskId": "t"} and "forwardedFrom" not in m,
        ),
        (
            {"sessionTargetLabels": {"s1": 4, "s2": "ok"}, "sessionMentions": ["s2"]},
            lambda m: m["sessionTargetLabels"] == {"s2": "ok"},
        ),
        ({"reactions": {"👍": "user_sam", "🎉": ["user_sam"]}}, lambda m: m["reactions"] == {"🎉": ["user_sam"]}),
        (
            {"specialist": 5, "kind": ["x"], "postReason": {}, "inReplyTo": 1, "runId": 2},
            lambda m: not ({"specialist", "kind", "postReason", "inReplyTo"} & m.keys()) and m["runId"] is None,
        ),
    ],
)
def test_bad_stored_field_is_repaired_on_read(over, check):
    msg = messaging.canonical_message(_row(**over)).model_dump(mode="json")
    assert msg["text"] == "hi" and check(msg)


async def test_room_lists_around_a_malformed_message(monkeypatch):
    rows = [_row(1), _row(2, via="rolled_back_value"), _row(3, roomRevision="nope"), _row(4)]
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=({"roomId": ROOM}, {})))
    monkeypatch.setattr(messaging.messaging_repo, "list_messages", AsyncMock(return_value=(rows, None)))
    page = await messaging.list_messages(AuthContext(user_id="user_sam", org_id="org_1"), ROOM, 0, 50)
    assert [m.message_seq for m in page.messages] == [1, 2, 4]  # 3's core field is broken: skipped, logged


def test_writes_stay_strict():
    with pytest.raises(ValueError):
        messaging.Author(kind="human", id="u", display_name="x", avatar="y")


def test_unknown_author_kind_reads_as_agent_never_system():
    msg = messaging.canonical_message(_row(author={"kind": "bot", "id": "b", "displayName": "B"}))
    assert msg.author.kind == "agent"


def test_garbage_deleted_at_still_reads_deleted():
    msg = messaging.canonical_message(_row(deletedAt="not-a-time")).model_dump(mode="json")
    assert msg["text"] == "" and msg["attachments"] == [] and msg["deletedAt"] == msg["createdAt"]


def test_repair_warning_once_per_message(caplog):
    row = _row(77, via="rolled_back_value")
    messaging._REPAIR_LOGGED.clear()
    with caplog.at_level("WARNING"):
        messaging.canonical_message(row)
        messaging.canonical_message(row)
    assert sum("repaired fields" in r.message for r in caplog.records) == 1


async def test_list_and_feed_survive_malformed_dispatch_targets(monkeypatch):
    rows = [
        _row(1, dispatchTargets=["junk", {"agentId": "a1", "eventKind": "user_request"}]),
        _row(2, dispatchTargets=5),
    ]
    assert [m.addressed_agent_ids for m in messaging._listable(rows)] == [["a1"]]  # 2 is skipped, not a failed room
    monkeypatch.setattr(messaging, "ensure_feed_room", AsyncMock(return_value=ROOM))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value={"lastSeq": 2}))
    monkeypatch.setattr(messaging.messaging_repo, "list_messages_before", AsyncMock(return_value=rows))
    feed = await messaging.agent_feed("org_1")
    assert [p["text"] for p in feed["posts"]] == ["hi"]
