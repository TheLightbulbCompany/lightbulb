"""The one-off top-level ordinal backfill (delete with the script)."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging
from scripts import backfill_room_ordinals as backfill
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    FRIDAY,
    OWNER,
    USER_A,
    USER_B,
    _member,
    _room,
    repo,
    table,
)
from tests.unit.services.test_messaging_unread_counts import AGENT, BOB, _say, _strip_ordinals


@pytest.fixture(autouse=True)
def _no_user_lookups(monkeypatch):
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))


async def _unread(room_id, user_id):
    rooms = (await messaging.list_rooms(AuthContext(user_id=user_id, org_id=OWNER))).rooms
    return next(r for r in rooms if str(r.room_id) == room_id).unread_count


async def _legacy_room(table):
    room_id, _ = await _room(
        members=[_member("human", USER_A, "owner"), _member("human", USER_B), _member("agent", FRIDAY, handle="friday")]
    )
    root = await _say(room_id, BOB, "1")  # ordinal 1
    await _say(room_id, AGENT, "2", thread_root=root["messageId"])  # reply: ordinal 1
    await _say(room_id, AGENT, "3")  # ordinal 2
    await repo.set_read_cursor(room_id, USER_A, 2)
    await repo.upsert_member(room_id=room_id, owner_id=OWNER, member=_member("human", "user_C"))  # joins at seq 3
    await _say(room_id, AGENT, "4")  # ordinal 3
    _strip_ordinals(table, room_id)
    return room_id


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(table):
    room_id = await _legacy_room(table)
    before = table.scan()["Items"]
    assert await backfill.main([]) == 0
    assert table.scan()["Items"] == before
    assert await backfill.backfill_room(room_id, apply=False) == "would-fix"


@pytest.mark.asyncio
async def test_apply_stamps_ordinals_so_counts_become_exact_and_a_rerun_is_a_no_op(table):
    room_id = await _legacy_room(table)
    assert await _unread(room_id, USER_A) == 0  # unbackfilled reads as 0
    assert await backfill.main(["--apply"]) == 0

    meta = await repo.get_room(room_id)
    assert meta["topCount"] == 3
    assert [(await repo.get_message(room_id, seq))["topOrdinal"] for seq in (1, 2, 3, 4)] == [1, 1, 2, 3]
    assert (await repo.get_read_row(room_id, USER_A))["topOrdinal"] == 1
    assert (await repo.get_member(room_id, "human", "user_C"))["joinedTopOrdinal"] == 2
    assert await _unread(room_id, USER_A) == 2  # messages 3 and 4
    assert await _unread(room_id, USER_B) == 2  # Bob posted 1: messages 3 and 4
    assert await _unread(room_id, "user_C") == 1  # joined after 3

    before = table.scan()["Items"]
    assert await backfill.backfill_room(room_id, apply=True) == "ok"
    assert table.scan()["Items"] == before


@pytest.mark.asyncio
async def test_a_message_landing_mid_backfill_retries_the_room(table, monkeypatch):
    room_id = await _legacy_room(table)
    real_query = repo._query
    raced = []

    async def racing(pk, **kwargs):
        rows = await real_query(pk, **kwargs)
        if kwargs.get("begins_with") == "MSG#" and not raced:
            raced.append(True)
            await _say(room_id, AGENT, "5")  # lands between the walk and the META write
        return rows

    monkeypatch.setattr(repo, "_query", racing)
    assert await backfill.backfill_room(room_id, apply=True) == "fixed"
    meta = await repo.get_room(room_id)
    assert meta["lastSeq"] == 5 and meta["topCount"] == 4
    assert (await repo.get_message(room_id, 5))["topOrdinal"] == 4
