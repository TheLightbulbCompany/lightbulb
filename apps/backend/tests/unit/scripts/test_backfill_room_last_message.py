"""The one-off META.lastMessage backfill (delete with the script)."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import pytest

from scripts import backfill_room_last_message as backfill
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    _post,
    _room,
    repo,
    table,
)


async def _legacy(table, room_id):
    table.update_item(Key={"pk": repo.room_pk(room_id), "sk": "META"}, UpdateExpression="REMOVE lastMessage")


@pytest.mark.asyncio
async def test_backfill_stamps_the_newest_live_top_level_message_or_null(table):
    busy, _ = await _room(client_room_id="c-busy")
    empty, _ = await _room(client_room_id="c-empty")
    root = await _post(busy, text="**root**", client_id="m-1")
    await _post(busy, text="reply", client_id="m-2", thread_root=root["messageId"])
    for room_id in (busy, empty):
        await _legacy(table, room_id)

    assert await backfill.main([]) == 0
    assert (await repo.get_room(busy))["lastMessage"] == repo.last_message_preview(root) | {"textPreview": "root"}
    assert (await repo.get_room(empty))["lastMessage"] is None
    # Idempotent: nothing is legacy any more.
    assert await backfill._legacy_rooms() == []


@pytest.mark.asyncio
async def test_backfill_never_overwrites_a_preview_written_by_a_newer_message(table, monkeypatch):
    room_id, _ = await _room()
    await _post(room_id, text="old", client_id="m-1")
    await _legacy(table, room_id)
    real = repo.latest_preview_row

    async def racing(*args, **kwargs):
        row = await real(*args, **kwargs)
        await _post(room_id, text="new", client_id="m-2")  # lands between the read and the write
        return row

    monkeypatch.setattr(repo, "latest_preview_row", racing)
    assert await backfill.backfill_room(room_id) == "skipped"
    assert (await repo.get_room(room_id))["lastMessage"]["textPreview"] == "new"
