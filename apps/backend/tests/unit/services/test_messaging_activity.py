"""Activity feed v1 (rooms F5): mention / dm / thread_reply pointer rows written by
the REAL messaging projector, read back through ``core.services.activity``, plus
the Paperclip approval / assigned-issue items merged into the first page.

The projector module is loaded from ``apps/infra/lambda/messaging-projector`` and
pointed at the same moto table, so these tests prove the writer and the reader
agree on the row shape end to end.
"""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import boto3
import pytest

from core.auth import AuthContext
from core.services import activity, messaging
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    FRIDAY,
    OWNER,
    USER_A,
    USER_B,
    _member,
    repo,
    table,
)

USER_C = "user_C"
ANN = {"kind": "human", "id": USER_A, "displayName": "Ann"}
BOB = {"kind": "human", "id": USER_B, "displayName": "Bob"}
CAROL = {"kind": "human", "id": USER_C, "displayName": "Carol"}
AGENT = {"kind": "agent", "id": FRIDAY, "displayName": "Friday"}

_INDEX = Path(__file__).resolve().parents[4] / "infra" / "lambda" / "messaging-projector" / "index.py"
_spec = importlib.util.spec_from_file_location("messaging_projector_index", _INDEX)
projector = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("messaging_projector_index", projector)
_spec.loader.exec_module(projector)


def auth(user_id=USER_B):
    return AuthContext(user_id=user_id, org_id=OWNER)


@pytest.fixture(autouse=True)
def _no_user_lookups(monkeypatch):
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid))


@pytest.fixture
def world(table):
    """The moto table with the real projector wired to it (AppSync publish stubbed)."""
    projector.MESSAGING_TABLE = "test-messaging"
    projector.USERS_TABLE = ""
    projector._ddb_client = boto3.client("dynamodb", region_name="us-east-1")
    with patch.object(projector, "_publish"):
        yield table
    projector._ddb_client = None


async def _room(kind="channel", members=(USER_A, USER_B, USER_C), name="general", client_room_id="c-1"):
    rows = [_member("human", uid, "owner" if uid == USER_A else "member") for uid in members]
    rows.append(_member("agent", FRIDAY, handle="friday"))
    room_id = repo.deterministic_room_id(OWNER, USER_A, client_room_id)
    await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id=client_room_id,
        creator_user_id=USER_A,
        name=None if kind == "dm" else name,
        kind=kind,
        members=rows,
        req_hash=repo.request_hash(client_room_id),
    )
    await repo.ensure_workspace_id(OWNER)
    return room_id


_ids = iter(range(10**6))


async def say(room_id, author, text="hi", *, mentions=(), thread_root=None):
    """Write through the canonical writer, then run the projector on the new row."""
    root_seq = None
    if thread_root:
        root_seq = int((await repo.get_message_by_id(room_id, thread_root))["messageSeq"])
    message, _ = await repo.create_message(
        room_id=room_id,
        owner_id=OWNER,
        idemp_pk=repo.room_pk(room_id),
        # "m-", not "c-": _room() stores its own IDEMP#human#<creator>#c-<n> row in the room
        # partition, so a "c-" message key collided whenever the module counter hit a room's
        # number -- which depended on how many tests ran before it in the process (xdist).
        idemp_sk=repo.human_idemp_sk(author["id"], f"m-{next(_ids)}"),
        req_hash=repo.request_hash(text, next(_ids)),
        author=author,
        text=text,
        mentions=[{"kind": "human", "id": uid} for uid in mentions],
        reply_to_message_id=None,
        thread_root_message_id=thread_root,
        thread_root_seq=root_seq,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=None,
        client_message_id=None,
    )
    stored = await repo.get_message(room_id, message["messageSeq"])
    projector._project_message(stored)
    return message


async def feed(user_id=USER_B, cursor=None, limit=30):
    page = await activity.list_activity(auth(user_id), cursor=cursor, limit=limit)
    return page.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# the message transaction's thread-participant set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_reply_records_repliers_and_mentions_on_the_root(world):
    room_id = await _room()
    root = await say(room_id, AGENT, "root")
    await say(room_id, ANN, "reply @carol", mentions=[USER_C], thread_root=root["messageId"])
    await say(room_id, AGENT, "agent reply, no humans", thread_root=root["messageId"])
    stored_root = await repo.get_message(room_id, root["messageSeq"])
    assert set(stored_root["threadParticipants"]) == {USER_A, USER_C}
    reply = await repo.get_message(room_id, 2)
    assert reply["threadRootSeq"] == root["messageSeq"]


@pytest.mark.asyncio
async def test_an_edit_or_delete_of_the_root_clears_the_thread_touch_marker(world):
    """The projector reads threadTouchedAt's PRESENCE as "reply bookkeeping, not an
    edit"; every edit/delete removes it so a late-committing edit is never skipped."""
    room_id = await _room()
    root = await say(room_id, ANN, "root")
    await say(room_id, ANN, "reply", thread_root=root["messageId"])
    assert "threadTouchedAt" in await repo.get_message(room_id, root["messageSeq"])
    await repo.update_message(OWNER, room_id, root["messageSeq"], USER_A, "fix", 0, "root, edited")
    assert "threadTouchedAt" not in await repo.get_message(room_id, root["messageSeq"])


# ---------------------------------------------------------------------------
# message kinds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mention_is_self_explaining_and_read_follows_the_room_cursor(world):
    room_id = await _room()
    message = await say(room_id, ANN, "**Ship** it @bob", mentions=[USER_B])
    page = await feed()
    [item] = page["items"]
    assert item["id"] == f"mention:{message['messageId']}:{room_id}:1"
    assert item["kind"] == "mention" and item["roomId"] == room_id and item["messageId"] == message["messageId"]
    assert item["actor"] == {"kind": "human", "id": USER_A, "name": "Ann"}
    assert item["summary"]["title"] == "Ann mentioned you in #general"
    assert item["summary"]["requester"] == item["actor"]
    assert item["summary"]["preview"]["text"] == "Ship it @bob"
    assert item["state"] == "open" and item["read"] is False
    assert [a["id"] for a in item["actions"]] == ["reply", "mark_done", "open"]
    reply = item["actions"][0]
    assert reply["input"] == {"kind": "text", "label": "Reply to Ann", "required": True, "options": None, "hint": None}
    assert item["actions"][2]["navigate"]["kind"] in {"thread", "room"}
    assert page["unseenCount"] == 1 and page["sources"] == {"paperclip": "ok"}
    assert item["roomKind"] == "channel"
    # Viewing Activity (seen) does not clear the dot; opening the room does.
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to=message["createdAt"]))
    assert (await feed())["items"][0]["read"] is False
    await repo.set_read_cursor(room_id, USER_B, 1)
    assert (await feed())["items"][0]["read"] is True
    # The author and an unmentioned member see nothing.
    assert (await feed(USER_A))["items"] == []
    assert (await feed(USER_C))["items"] == []


@pytest.mark.asyncio
async def test_dm_to_me_is_an_item_but_never_for_its_author(world):
    room_id = await _room("dm", (USER_A, USER_B), client_room_id="dm-1")
    await say(room_id, ANN, "hey")
    [item] = (await feed())["items"]
    assert item["kind"] == "dm"
    assert item["summary"]["title"] == "Ann sent you a direct message"
    assert [a["id"] for a in item["actions"]] == ["reply", "open"]
    assert (await feed(USER_A))["items"] == []


@pytest.mark.asyncio
async def test_thread_reply_reaches_the_starter_repliers_and_the_mentioned(world):
    room_id = await _room()
    root = await say(room_id, BOB, "root by bob")
    await say(room_id, ANN, "first reply @carol", mentions=[USER_C], thread_root=root["messageId"])
    await say(room_id, AGENT, "friday replies", thread_root=root["messageId"])
    bob = [i["kind"] for i in (await feed(USER_B))["items"]]
    carol = [i["kind"] for i in (await feed(USER_C))["items"]]
    ann = [i["kind"] for i in (await feed(USER_A))["items"]]
    assert bob == ["thread_reply", "thread_reply"]  # started it
    assert carol == ["thread_reply", "mention"]  # mentioned in it
    assert ann == ["thread_reply"]  # replied in it (not her own reply)
    item = (await feed(USER_B))["items"][0]
    assert item["threadRootMessageId"] == root["messageId"]
    assert item["summary"]["title"] == "Friday replied in a thread in #general"


@pytest.mark.asyncio
async def test_thread_replies_never_badge_but_mentions_do(world):
    room_id = await _room()
    root = await say(room_id, BOB, "root by bob")
    await say(room_id, ANN, "first reply @carol", mentions=[USER_C], thread_root=root["messageId"])
    await say(room_id, AGENT, "friday replies", thread_root=root["messageId"])
    bob, carol = await feed(USER_B), await feed(USER_C)
    # Both still count as unseen; only needs-you, mentions and DMs reach the badge.
    assert bob["unseenCount"] == 2 and bob["attentionCount"] == 0
    assert carol["unseenCount"] == 2 and carol["attentionCount"] == 1


# ---------------------------------------------------------------------------
# pagination, membership, deletes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_is_newest_first_with_an_opaque_cursor(world):
    room_id = await _room()
    sent = [await say(room_id, ANN, f"@bob {n}", mentions=[USER_B]) for n in range(5)]
    seen_ids = []
    cursor = None
    pages = 0
    while True:
        page = await feed(cursor=cursor, limit=2)
        pages += 1
        seen_ids += [i["messageId"] for i in page["items"]]
        cursor = page["next"]
        if pages > 1:
            assert page["unseenCount"] is None  # the badge rides the first page only
        if cursor is None:
            break
    assert seen_ids == [m["messageId"] for m in reversed(sent)]
    with pytest.raises(messaging.ApiProblem) as bad:
        await feed(cursor="not-a-cursor!!")
    assert bad.value.status_code == 400


@pytest.mark.parametrize(
    "item_id",
    [
        "0f8fad5b-d9cb-469f-a165-70867728950e",  # a message
        "session:0f8fad5b-d9cb-469f-a165-70867728950e",  # local_session_relay
        "login:0f8fad5bd9cb469fa16570867728950e",  # website_logins
        "extension:0f8fad5bd9cb469fa16570867728950e",  # browser_extensions
    ],
)
def test_every_cursor_we_mint_is_one_we_accept(item_id):
    """A page that ended on a row that is not a message handed back a cursor the next
    request refused ("Invalid activity cursor."), so the feed could not page past it."""
    sk = f"2026-09-28T18:10:41.123456+00:00#{item_id}"
    assert activity._decode_cursor(activity._encode_cursor(sk)) == sk


@pytest.mark.parametrize(
    "cursor", ["", "not-base64!", "aGVsbG8", activity._encode_cursor("2026-09-28T18:10:41+00:00#")]
)
def test_a_cursor_we_did_not_mint_is_refused(cursor):
    with pytest.raises(activity.ApiProblem):
        activity._decode_cursor(cursor)


@pytest.mark.asyncio
async def test_leaving_a_room_hides_its_items_immediately(world):
    room_id = await _room()
    other = await _room(client_room_id="c-2", name="random")
    await say(room_id, ANN, "@bob one", mentions=[USER_B])
    await say(other, ANN, "@bob two", mentions=[USER_B])
    assert len((await feed())["items"]) == 2
    world.delete_item(Key={"pk": repo.room_pk(room_id), "sk": repo.member_sk("human", USER_B)})
    items = (await feed())["items"]
    assert [i["roomId"] for i in items] == [other]
    assert (await feed())["unseenCount"] == 1


@pytest.mark.asyncio
async def test_deleted_messages_drop_and_edits_show_the_current_text(world):
    room_id = await _room()
    first = await say(room_id, ANN, "@bob secret plan", mentions=[USER_B])
    await say(room_id, ANN, "@bob typo", mentions=[USER_B])
    await repo.update_message(OWNER, room_id, 1, USER_A, "del", 0, None)
    await repo.update_message(OWNER, room_id, 2, USER_A, "fix", 0, "@bob fixed")
    page = await feed()
    assert [i["summary"]["preview"]["text"] for i in page["items"]] == ["@bob fixed"]
    assert first["messageId"] not in json.dumps(page)
    assert "secret plan" not in json.dumps(page)


# ---------------------------------------------------------------------------
# seen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seen_cursor_is_monotonic_and_leaves_thread_replies_unread_until_opened(world):
    room_id = await _room()
    root = await say(room_id, BOB, "root")
    reply = await say(room_id, ANN, "reply", thread_root=root["messageId"])
    page = await feed()
    assert page["unseenCount"] == 1 and page["items"][0]["read"] is False
    seen = await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to=reply["createdAt"]))
    assert seen.seen_up_to is not None
    page = await feed()
    # Seen clears the badge count, not the dot: the reply is read once the room cursor passes it.
    assert page["unseenCount"] == 0 and page["items"][0]["read"] is False
    await repo.set_read_cursor(room_id, USER_B, int(reply["messageSeq"]))
    assert (await feed())["items"][0]["read"] is True
    # An older upTo never moves the cursor back.
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to="2000-01-01T00:00:00+00:00"))
    assert (await feed())["unseenCount"] == 0


@pytest.mark.asyncio
async def test_a_wrong_thread_root_seq_is_not_retried_as_a_sequence_race(world, monkeypatch):
    room_id = await _room()
    root = await say(room_id, ANN, "root")
    await say(room_id, ANN, "other")
    calls = []
    real = repo._transact

    async def counting(items):
        calls.append(1)
        await real(items)

    monkeypatch.setattr(repo, "_transact", counting)
    with pytest.raises(repo.ThreadRootMismatch):
        await repo.create_message(
            room_id=room_id,
            owner_id=OWNER,
            idemp_pk=repo.room_pk(room_id),
            idemp_sk=repo.human_idemp_sk(USER_A, "c-wrong-root"),
            req_hash=repo.request_hash("x"),
            author=ANN,
            text="x",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=root["messageId"],
            thread_root_seq=2,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
        )
    assert len(calls) == 1
    assert int((await repo.get_room(room_id))["lastSeq"]) == 2  # nothing landed


@pytest.mark.asyncio
async def test_unseen_counts_is_the_badge_by_kind(world, monkeypatch):
    """The daily digest's source: the same unseen rule as ``unseenCount``, split by kind."""
    room_id = await _room()
    await say(room_id, ANN, "@bob one", mentions=[USER_B])
    await say(room_id, ANN, "@bob two", mentions=[USER_B])
    dm = await _room("dm", members=(USER_A, USER_B), client_room_id="dm-1")
    await say(dm, ANN, "psst")
    page = await feed()
    counts = await activity.unseen_counts(OWNER, USER_B)
    assert counts == {"mention": 2, "dm": 1}
    assert sum(counts.values()) == page["unseenCount"]
    # Seen moves everything out, exactly like the badge.
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to="2100-01-01T00:00:00Z"))
    assert await activity.unseen_counts(OWNER, USER_B) == {}
