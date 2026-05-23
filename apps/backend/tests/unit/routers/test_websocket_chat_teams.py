"""Unit tests for the new teams.subscribe / teams.unsubscribe routes
on the /ws/message dispatcher."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from fastapi.responses import Response

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


def _conn_record(user_id: str = "u1", org_id: str | None = None) -> dict:
    return {"user_id": user_id, "org_id": org_id, "connection_type": "chat"}


@pytest.mark.asyncio
async def test_teams_subscribe_calls_broker_subscribe():
    from routers.websocket_chat import ws_message

    fake_conn_svc = MagicMock()
    fake_conn_svc.get_connection = MagicMock(return_value=_conn_record())
    fake_broker = MagicMock()
    fake_broker.subscribe = AsyncMock()
    fake_broker.unsubscribe = AsyncMock()

    with (
        patch("routers.websocket_chat.get_connection_service", AsyncMock(return_value=fake_conn_svc)),
        patch("routers.websocket_chat.get_management_api_client", AsyncMock(return_value=MagicMock())),
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=MagicMock())),
        patch("core.services.teams_event_broker_singleton.get_broker", return_value=fake_broker),
    ):
        resp = await ws_message(
            body={"type": "teams.subscribe"},
            background_tasks=BackgroundTasks(),
            x_connection_id="conn_abc",
        )

    assert isinstance(resp, Response)
    assert resp.status_code == 200
    fake_broker.subscribe.assert_awaited_once_with("u1", "conn_abc")
    fake_broker.unsubscribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_teams_unsubscribe_calls_broker_unsubscribe():
    from routers.websocket_chat import ws_message

    fake_conn_svc = MagicMock()
    fake_conn_svc.get_connection = MagicMock(return_value=_conn_record())
    fake_broker = MagicMock()
    fake_broker.subscribe = AsyncMock()
    fake_broker.unsubscribe = AsyncMock()

    with (
        patch("routers.websocket_chat.get_connection_service", AsyncMock(return_value=fake_conn_svc)),
        patch("routers.websocket_chat.get_management_api_client", AsyncMock(return_value=MagicMock())),
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=MagicMock())),
        patch("core.services.teams_event_broker_singleton.get_broker", return_value=fake_broker),
    ):
        resp = await ws_message(
            body={"type": "teams.unsubscribe"},
            background_tasks=BackgroundTasks(),
            x_connection_id="conn_abc",
        )

    assert resp.status_code == 200
    fake_broker.unsubscribe.assert_awaited_once_with("u1", "conn_abc")
    fake_broker.subscribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_teams_subscribe_no_op_when_broker_unavailable():
    """If the singleton is None (dev without Paperclip env), subscribe
    silently 200s — /teams just won't get live updates."""
    from routers.websocket_chat import ws_message

    fake_conn_svc = MagicMock()
    fake_conn_svc.get_connection = MagicMock(return_value=_conn_record())

    with (
        patch("routers.websocket_chat.get_connection_service", AsyncMock(return_value=fake_conn_svc)),
        patch("routers.websocket_chat.get_management_api_client", AsyncMock(return_value=MagicMock())),
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=MagicMock())),
        patch("core.services.teams_event_broker_singleton.get_broker", return_value=None),
    ):
        resp = await ws_message(
            body={"type": "teams.subscribe"},
            background_tasks=BackgroundTasks(),
            x_connection_id="conn_abc",
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_existing_chat_message_types_still_routed():
    """Regression: adding teams.* must not break ping/agent_chat dispatch."""
    from routers.websocket_chat import ws_message

    fake_conn_svc = MagicMock()
    fake_conn_svc.get_connection = MagicMock(return_value=_conn_record())
    fake_mgmt = MagicMock()
    fake_mgmt.send_message = MagicMock()

    with (
        patch("routers.websocket_chat.get_connection_service", AsyncMock(return_value=fake_conn_svc)),
        patch("routers.websocket_chat.get_management_api_client", AsyncMock(return_value=fake_mgmt)),
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=MagicMock())),
    ):
        resp = await ws_message(
            body={"type": "ping"},
            background_tasks=BackgroundTasks(),
            x_connection_id="conn_abc",
        )

    assert resp.status_code == 200
    fake_mgmt.send_message.assert_called_once()
    sent_payload = fake_mgmt.send_message.call_args.args[1]
    assert sent_payload == {"type": "pong"}


@pytest.mark.asyncio
async def test_teams_subscribe_emits_dispatch_ok_metric():
    """Happy path: a successful teams.subscribe dispatch emits
    teams.dispatch with outcome=ok and type=teams.subscribe."""
    from routers.websocket_chat import ws_message

    fake_conn_svc = MagicMock()
    fake_conn_svc.get_connection = MagicMock(return_value=_conn_record())
    fake_broker = MagicMock()
    fake_broker.subscribe = AsyncMock()
    fake_broker.unsubscribe = AsyncMock()

    with (
        patch("routers.websocket_chat.get_connection_service", AsyncMock(return_value=fake_conn_svc)),
        patch("routers.websocket_chat.get_management_api_client", AsyncMock(return_value=MagicMock())),
        patch("routers.websocket_chat.get_gateway_pool", MagicMock(return_value=MagicMock())),
        patch("core.services.teams_event_broker_singleton.get_broker", return_value=fake_broker),
        patch("routers.websocket_chat.put_metric") as put_metric_mock,
    ):
        await ws_message(
            body={"type": "teams.subscribe"},
            background_tasks=BackgroundTasks(),
            x_connection_id="conn_abc",
        )

    matching = [
        c
        for c in put_metric_mock.call_args_list
        if c.args
        and c.args[0] == "teams.dispatch"
        and c.kwargs.get("dimensions", {}).get("outcome") == "ok"
        and c.kwargs.get("dimensions", {}).get("type") == "teams.subscribe"
    ]
    assert matching, (
        f"expected teams.dispatch ok/teams.subscribe; got: "
        f"{[(c.args, c.kwargs) for c in put_metric_mock.call_args_list]}"
    )


@pytest.mark.asyncio
async def test_disconnect_unsubscribes_dead_browser_from_broker():
    """A browser tab crashing without sending teams.unsubscribe must NOT
    leave a stale conn-id in the broker's subscriber set — that would
    block grace teardown of the backing Paperclip WS forever.
    /ws/disconnect cleans up via broker.unsubscribe."""
    from routers.websocket_chat import ws_disconnect

    fake_conn_svc = MagicMock()
    fake_conn_svc.get_connection = MagicMock(return_value=_conn_record())
    fake_conn_svc.delete_connection = MagicMock()
    fake_broker = MagicMock()
    fake_broker.unsubscribe = AsyncMock()
    fake_pool = MagicMock()
    fake_pool.remove_frontend_connection = MagicMock()

    with (
        patch(
            "routers.websocket_chat.get_connection_service",
            AsyncMock(return_value=fake_conn_svc),
        ),
        patch(
            "routers.websocket_chat.get_gateway_pool",
            MagicMock(return_value=fake_pool),
        ),
        patch(
            "routers.websocket_chat.is_node_connection",
            MagicMock(return_value=False),
        ),
        patch(
            "core.services.teams_event_broker_singleton.get_broker",
            return_value=fake_broker,
        ),
    ):
        resp = await ws_disconnect(x_connection_id="conn_abc")

    assert resp.status_code == 200
    fake_broker.unsubscribe.assert_awaited_once_with("u1", "conn_abc")


@pytest.mark.asyncio
async def test_disconnect_no_op_when_broker_unavailable():
    """If the broker singleton is None (dev without Paperclip),
    /ws/disconnect still succeeds — broker cleanup is best-effort."""
    from routers.websocket_chat import ws_disconnect

    fake_conn_svc = MagicMock()
    fake_conn_svc.get_connection = MagicMock(return_value=_conn_record())
    fake_conn_svc.delete_connection = MagicMock()
    fake_pool = MagicMock()

    with (
        patch(
            "routers.websocket_chat.get_connection_service",
            AsyncMock(return_value=fake_conn_svc),
        ),
        patch(
            "routers.websocket_chat.get_gateway_pool",
            MagicMock(return_value=fake_pool),
        ),
        patch(
            "routers.websocket_chat.is_node_connection",
            MagicMock(return_value=False),
        ),
        patch(
            "core.services.teams_event_broker_singleton.get_broker",
            return_value=None,
        ),
    ):
        resp = await ws_disconnect(x_connection_id="conn_abc")

    assert resp.status_code == 200
