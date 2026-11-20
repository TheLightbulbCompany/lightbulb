"""`use_browser`: where a shared session's next browser connection goes."""

from unittest.mock import AsyncMock

import pytest

from core.services import session_browser

OWNER = "org_1"
SID = "11111111-1111-4111-8111-111111111111"


class PlaceTable:
    """A one-row META table whose update_item honours `revision = :rev`; `bump` simulates a racing registration."""

    def __init__(self, row, bumps=0):
        self.row, self.bumps, self.calls = row, bumps, 0

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues):
        from botocore.exceptions import ClientError

        self.calls += 1
        if self.bumps:
            self.bumps -= 1
            self.row["revision"] = f"raced-{self.calls}"
        if ExpressionAttributeValues[":rev"] != self.row["revision"]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.row["browserPlace"] = ExpressionAttributeValues[":w"]
        self.row["revision"] = ExpressionAttributeValues[":new"]


@pytest.fixture
def place_env(monkeypatch):
    row = {"revision": "r0"}
    table = PlaceTable(row)

    async def run_in_thread(fn, **kwargs):
        return fn(**kwargs)

    monkeypatch.setattr(session_browser.store, "run_in_thread", run_in_thread)
    monkeypatch.setattr(session_browser.store, "_table", lambda: table)
    monkeypatch.setattr(session_browser.repo, "get_session", AsyncMock(side_effect=lambda o, s: dict(row)))
    return table


async def test_use_browser_sets_where_the_next_connection_goes(place_env):
    from core.services.messaging import ApiProblem

    assert await session_browser.set_place(OWNER, SID, "cloud") == {
        "sessionId": SID,
        "browser": "cloud",
        "note": session_browser.PLACE_NOTE,
    }
    assert "restart the lightbulb-browser MCP server" in session_browser.PLACE_NOTE
    assert place_env.row["browserPlace"] == "cloud"
    assert place_env.row["revision"] != "r0"
    with pytest.raises(ApiProblem):
        await session_browser.set_place(OWNER, SID, "elsewhere")


async def test_use_browser_retries_when_a_registration_bumps_the_revision(place_env):
    place_env.bumps = 1
    await session_browser.set_place(OWNER, SID, "cloud")
    assert place_env.calls == 2
    assert place_env.row["browserPlace"] == "cloud"
    assert place_env.row["revision"] not in ("r0", "raced-1")


async def test_use_browser_gives_a_retryable_error_when_the_row_keeps_changing(place_env):
    from core.services.messaging import ApiProblem

    place_env.bumps = 99
    with pytest.raises(ApiProblem) as caught:
        await session_browser.set_place(OWNER, SID, "cloud")
    assert caught.value.error.retryable
    assert "browserPlace" not in place_env.row
