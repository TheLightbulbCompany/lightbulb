"""Activity feed v1 (rooms F5): mention / dm / thread_reply pointer rows written by
the REAL messaging projector, read back through ``core.services.activity``, plus
the Paperclip approval / assigned-issue items merged into the first page.

The projector module is loaded from ``apps/infra/lambda/messaging-projector`` and
pointed at the same moto table, so these tests prove the writer and the reader
agree on the row shape end to end.
"""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import importlib.util
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
    return AuthContext(user_id=user_id)


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


# ---------------------------------------------------------------------------
# pagination, membership, deletes
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# seen
# ---------------------------------------------------------------------------


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
