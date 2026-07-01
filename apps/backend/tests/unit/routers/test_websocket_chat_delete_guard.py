# apps/backend/tests/unit/routers/test_websocket_chat_delete_guard.py
from unittest.mock import AsyncMock, patch

import pytest

from routers import websocket_chat as wc


@pytest.mark.asyncio
async def test_seeded_delete_is_rejected_before_forward():
    sent = {}

    def send_message(connection_id, payload):
        sent["payload"] = payload

    with (
        patch.object(wc, "is_seeded_agent", new=AsyncMock(return_value=True)),
        patch.object(wc, "management_api") as mgmt,
    ):
        mgmt.send_message.side_effect = send_message
        consumed = await wc._guard_seeded_delete(
            owner_id="o1",
            method="agents.delete",
            params={"agentId": "friday-uuid"},
            connection_id="conn1",
            req_id="r1",
        )
    assert consumed is True
    assert sent["payload"]["ok"] is False
    assert "can't be deleted" in sent["payload"]["error"]["message"].lower()


@pytest.mark.asyncio
async def test_non_seeded_delete_is_not_consumed():
    with patch.object(wc, "is_seeded_agent", new=AsyncMock(return_value=False)):
        consumed = await wc._guard_seeded_delete(
            owner_id="o1",
            method="agents.delete",
            params={"agentId": "bob-uuid"},
            connection_id="conn1",
            req_id="r1",
        )
    assert consumed is False


@pytest.mark.asyncio
async def test_non_delete_method_is_not_consumed():
    consumed = await wc._guard_seeded_delete(
        owner_id="o1",
        method="agents.update",
        params={"agentId": "x"},
        connection_id="c",
        req_id="r",
    )
    assert consumed is False


# --- Cross-tenant IDOR guard on session-key-bearing RPCs -------------------

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


async def _run_key_guard(*, user_id, method, params):
    """Invoke the ownership guard, capturing any error frame it emits."""
    sent = {}

    def send_message(connection_id, payload):
        sent["payload"] = payload

    with patch.object(wc, "management_api") as mgmt:
        mgmt.send_message.side_effect = send_message
        consumed = await wc._guard_session_key_ownership(
            user_id=user_id,
            method=method,
            params=params,
            connection_id="conn1",
            req_id="r1",
        )
    return consumed, sent.get("payload")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["sessions.patch", "sessions.reset", "sessions.delete"])
async def test_member_rejected_mutating_another_users_key(method):
    # Member "user_A" cannot patch/reset/delete "user_B"'s session on the
    # shared per-owner container.
    consumed, payload = await _run_key_guard(
        user_id="user_A",
        method=method,
        params={"key": f"agent:agentX:user_B:{_UUID}"},
    )
    assert consumed is True
    assert payload["ok"] is False
    assert "access" in payload["error"]["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [f"paperclip:agentX:{_UUID}", f"cron:job:{_UUID}", "paperclip:something"],
)
async def test_member_rejected_mutating_paperclip_or_cron_key(key):
    # Non-agent keys (Teams / cron sessions) are never owned by a /chat member.
    consumed, payload = await _run_key_guard(
        user_id="user_A",
        method="sessions.delete",
        params={"key": key},
    )
    assert consumed is True
    assert payload["ok"] is False


@pytest.mark.asyncio
async def test_chat_history_rejected_for_another_users_key():
    # chat.history uses `sessionKey`, not `key`. Reading another member's
    # transcript is the same IDOR class.
    consumed, payload = await _run_key_guard(
        user_id="user_A",
        method="chat.history",
        params={"sessionKey": f"agent:agentX:user_B:{_UUID}"},
    )
    assert consumed is True
    assert payload["ok"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["sessions.patch", "sessions.reset", "sessions.delete"])
async def test_member_allowed_own_named_key(method):
    # Owner of a 4-part named session may mutate it — NOT consumed, forwarded.
    consumed, payload = await _run_key_guard(
        user_id="user_A",
        method=method,
        params={"key": f"agent:agentX:user_A:{_UUID}"},
    )
    assert consumed is False
    assert payload is None


@pytest.mark.asyncio
async def test_member_allowed_own_general_key():
    # Legacy 3-part "General" key for the caller is owned — forwarded.
    consumed, _ = await _run_key_guard(
        user_id="user_A",
        method="sessions.patch",
        params={"key": "agent:agentX:user_A"},
    )
    assert consumed is False


@pytest.mark.asyncio
async def test_prefix_boundary_blocks_user_10_masquerade():
    # user_1 must NOT be able to touch user_10's key (bare startswith would
    # false-positive; the `:` boundary check prevents it).
    consumed, _ = await _run_key_guard(
        user_id="user_1",
        method="sessions.delete",
        params={"key": f"agent:agentX:user_10:{_UUID}"},
    )
    assert consumed is True


@pytest.mark.asyncio
async def test_case_insensitive_own_key_allowed():
    # OpenClaw lowercases the whole key; the mixed-case Clerk id must still
    # resolve as owned.
    consumed, _ = await _run_key_guard(
        user_id="User_Abc",
        method="sessions.patch",
        params={"key": f"agent:agentx:user_abc:{_UUID}"},
    )
    assert consumed is False


@pytest.mark.asyncio
async def test_keyless_method_is_not_consumed():
    # Methods outside the key-bearing set are never touched by this guard.
    consumed, _ = await _run_key_guard(
        user_id="user_A",
        method="sessions.list",
        params={},
    )
    assert consumed is False


@pytest.mark.asyncio
async def test_session_method_without_key_param_is_not_consumed():
    # A sessions.patch with no `key` param carries nothing to forge — leave it
    # to the container to reject; the guard does not consume it.
    consumed, _ = await _run_key_guard(
        user_id="user_A",
        method="sessions.patch",
        params={"execNode": "dev-1"},
    )
    assert consumed is False


def test_user_owns_session_key_predicate():
    assert wc._user_owns_session_key(f"agent:x:user_A:{_UUID}", "user_A") is True
    assert wc._user_owns_session_key("agent:x:user_A", "user_A") is True
    assert wc._user_owns_session_key(f"agent:x:user_B:{_UUID}", "user_A") is False
    assert wc._user_owns_session_key(f"agent:x:user_10:{_UUID}", "user_1") is False
    assert wc._user_owns_session_key(f"paperclip:x:{_UUID}", "user_A") is False
    assert wc._user_owns_session_key("", "user_A") is False
    assert wc._user_owns_session_key(None, "user_A") is False
