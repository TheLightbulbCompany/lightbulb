from unittest.mock import AsyncMock, patch

import pytest

from core.services import local_session_relay as relay

CMD = {"actor": {"kind": "session", "id": "s1"}, "sourceSessionId": "s1", "sourceHumanUserId": "aden"}


async def label(command, source_row, owner_human="bob"):
    with (
        patch("core.services.messaging.human_display_name", AsyncMock(return_value="Aden Smith")),
        patch.object(relay.repo, "get_session", AsyncMock(return_value=source_row)),
    ):
        return await relay._sender_label(command, "o", owner_human)


@pytest.mark.asyncio
async def test_codex_session_named_for_owner():
    got = await label(CMD, {"provider": "codex", "humanUserId": "aden"})
    assert got == {"kind": "session", "displayName": "Aden's Codex"}


@pytest.mark.asyncio
async def test_own_session():
    got = await label(CMD, {"provider": "claude", "humanUserId": "bob"}, owner_human="bob")
    assert got["displayName"] == "Your Claude Code"


@pytest.mark.asyncio
async def test_removed_source_falls_back():
    got = await label(CMD, None)
    assert got["displayName"] == "Aden's coding session"


@pytest.mark.asyncio
async def test_a_failed_name_lookup_is_no_name_not_the_user_id():
    with (
        patch("core.services.messaging.human_display_name", AsyncMock(return_value="user_2abcRAW")),
        patch.object(
            relay.repo, "get_session", AsyncMock(return_value={"provider": "codex", "humanUserId": "user_2abcRAW"})
        ),
    ):
        got = await relay._sender_label({**CMD, "sourceHumanUserId": "user_2abcRAW"}, "o", "bob")
    assert got["displayName"] == "Shared native session"
