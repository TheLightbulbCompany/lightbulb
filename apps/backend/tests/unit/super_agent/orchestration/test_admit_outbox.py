"""`create_message(admit=True)` writes the one admission row and no WORK#MESSAGE rows (O Task 6)."""

import pytest

from core.repositories import messaging_repo as repo
from tests.unit.repositories.test_messaging_repo import FRIDAY, _post, _room, table  # noqa: F401


async def _work_rows(t, room_id):
    items = t.scan()["Items"]
    return sorted(i["sk"] for i in items if i["pk"] == repo.room_pk(room_id) and i["sk"].startswith("WORK#"))


@pytest.mark.asyncio
async def test_router_room_writes_only_admit_row(table, monkeypatch):  # noqa: F811
    room_id, _ = await _room()
    real = repo.create_message

    async def admit_create(**kw):
        return await real(**kw, admit=True)

    monkeypatch.setattr(repo, "create_message", admit_create)
    msg = await _post(room_id, targets=[{"agentId": FRIDAY, "eventKind": "user_request"}])
    rows = await _work_rows(table, room_id)
    assert rows == [repo.work_admit_sk(int(msg["messageSeq"]))]
    assert msg["dispatchTargets"] == [{"agentId": FRIDAY, "eventKind": "user_request"}]  # kept on the MSG row


@pytest.mark.asyncio
async def test_legacy_room_writes_work_message_rows(table):  # noqa: F811
    room_id, _ = await _room()
    msg = await _post(room_id)
    assert await _work_rows(table, room_id) == [repo.work_message_sk(int(msg["messageSeq"]), FRIDAY)]
