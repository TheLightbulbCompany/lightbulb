"""The one-off end of unheld local sessions (delete with the script)."""

from unittest.mock import AsyncMock

import pytest

from core.repositories import local_sessions_repo as repo
from scripts import end_unheld_local_sessions as script
from tests.unit.services import test_local_sessions as registry_tests
from tests.unit.services.test_local_sessions import OWNER, registered

db = registry_tests.db


@pytest.mark.asyncio
async def test_ends_live_unheld_sessions_only(db, monkeypatch):
    monkeypatch.setattr(script, "publish_resources_changed", AsyncMock())
    unheld = await registered()
    held = await registered(conversation="thread-2")
    db[0].put_item(Item={**(await repo.get_session(OWNER, held)), "holderId": "ab12cd34"})

    assert await script.main([]) == 0
    assert "endedAt" not in await repo.get_session(OWNER, unheld), "a dry run writes nothing"

    assert await script.main(["--apply"]) == 0
    ended = await repo.get_session(OWNER, unheld)
    assert (ended["status"], ended["endedReason"]) == ("ended", "exited") and ended["endedAt"]
    assert "endedAt" not in await repo.get_session(OWNER, held)
    assert await script.unheld() == []
    script.publish_resources_changed.assert_awaited_once()
