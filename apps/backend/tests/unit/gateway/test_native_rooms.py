"""Synthetic protocol integration only; not a real 9.3/auth/device qualification."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.config import settings
from core.crypto.operator_device import generate_operator_device
from core.gateway import native_rooms as rooms
from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool
from routers import websocket_chat as route


def row(subject="user_A"):
    return {
        "user_id": subject,
        "org_id": "org_team",
        "connection_type": "native_human",
        "auth_kind": "clerk_user",
        "verified_issuer": settings.CLERK_ISSUER,
        "verified_subject": subject,
        "source_ip": "198.51.100.20",
    }


def identity(subject="user_A", connection_id="tab-a"):
    return rooms.HumanIdentity.from_connection(connection_id, row(subject))


def qualification():
    return rooms.RoomQualification(
        "org_team",
        "10.0.0.20",
        "release-test",
        "build-test",
        "synthetic-only",
        True,
        True,
        True,
        "x-lightbulb-human",
        frozenset({"friday"}),
        {
            identity(subject).alias: rooms.QualifiedHumanDevice(generate_operator_device(), f"profile-{subject}")
            for subject in ("user_A", "user_B")
        },
    )


class FakeSocket:
    def __init__(self, gateway, headers):
        self.gateway = gateway
        self.headers = headers
        self.queue = asyncio.Queue()
        self.queue.put_nowait(
            json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "synthetic-nonce"}})
        )
        self.closed = False

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        self.closed = True

    async def send(self, raw):
        request = json.loads(raw)
        method, params = request["method"], request["params"]
        self.gateway.calls.append(request)
        alias = self.headers["x-lightbulb-human"]
        human = self.gateway.qualification.humans[alias]
        ok = True
        if method == "connect":
            assert "auth" not in params
            assert params["role"] == "operator"
            assert params["device"]["id"] == human.identity.device_id
            assert params["scopes"] == list(rooms.HUMAN_SCOPES)
            assert self.headers["x-openclaw-scopes"] == ",".join(rooms.HUMAN_SCOPES)
            result = {
                "type": "hello-ok",
                "protocol": 4,
                "server": {"buildId": self.gateway.build_id},
                "auth": {"role": "operator", "scopes": self.gateway.scopes},
                "features": {"methods": sorted(rooms.ROOM_METHODS | {"users.self"})},
            }
        elif method == "users.self":
            result = {"profile": {"id": self.gateway.profile_override or human.profile_id, "displayName": alias}}
        elif method == "sessions.create":
            result = self.gateway.sessions.setdefault(
                params["key"],
                {
                    "key": params["key"],
                    "sessionId": f"incarnation-{len(self.gateway.sessions) + 1}",
                    "createdBy": {"type": "human", "id": human.profile_id},
                    "visibility": params["visibility"],
                },
            )
            self.gateway.transcripts[result["sessionId"]] = params["key"]
        elif method == "chat.send":
            messages = self.gateway.history.setdefault(params["sessionKey"], [])
            run_id = params["idempotencyKey"]
            if run_id in self.gateway.dedupe:
                result = self.gateway.dedupe[run_id]
            elif params["expectedLeafEntryId"] != (messages[-1]["id"] if messages else None):
                ok = False
                result = {
                    "code": "STALE_LEAF",
                    "message": "Refresh canonical branch",
                    "details": {"expectedLeafEntryId": messages[-1]["id"]},
                }
            else:
                entry_id = f"entry-{len(messages) + 1}"
                messages.append(
                    {
                        "id": entry_id,
                        "parentId": params["expectedLeafEntryId"],
                        "runId": run_id,
                        "role": "user",
                        "content": params["message"],
                        "sender": {"id": human.profile_id, "identity": {"type": "profile", "id": human.profile_id}},
                    }
                )
                result = {"runId": run_id, "status": "started"}
                self.gateway.dedupe[run_id] = result
        elif method == "chat.history":
            if "sessionId" in params and "messageId" not in params:
                ok, result = False, {"code": "INVALID_REQUEST", "message": "sessionId requires messageId"}
            elif "sessionId" in params and self.gateway.transcripts.get(params["sessionId"]) != params["sessionKey"]:
                ok, result = False, {"code": "INVALID_REQUEST", "message": "sessionId does not belong to sessionKey"}
            else:
                messages = list(self.gateway.history.get(params["sessionKey"], []))
                if "messageId" in params:
                    messages = [message for message in messages if message["id"] == params["messageId"]]
                result = {
                    "sessionKey": params["sessionKey"],
                    "sessionId": params.get("sessionId", "incarnation-1"),
                    "messages": messages,
                }
        elif method == "mentions.list":
            result = {"items": self.gateway.mentions, "gatewayInstanceId": "gateway-test", "revision": 1}
        else:
            result = {"ok": True, **{k: params[k] for k in ("key", "sessionKey") if k in params}}
        self.queue.put_nowait(
            json.dumps({"type": "res", "id": request["id"], "ok": ok, "payload" if ok else "error": result})
        )


@pytest_asyncio.fixture
async def harness(monkeypatch):
    proof = qualification()
    gateway = SimpleNamespace(
        qualification=proof,
        calls=[],
        sessions={},
        transcripts={},
        history={},
        dedupe={},
        mentions=[],
        scopes=list(rooms.HUMAN_SCOPES),
        build_id="build-test",
        profile_override=None,
        sockets=[],
    )
    rows = {"tab-a": row(), "tab-a2": row(), "tab-b": row("user_B")}
    service = MagicMock()
    service.get_connection.side_effect = lambda key: rows.get(key)
    service.delete_connection.side_effect = lambda key: rows.pop(key, None)
    service.touch_connection = AsyncMock()
    service.query_by_owner = AsyncMock(
        side_effect=lambda owner: [
            (key, value["user_id"], value["connection_type"]) for key, value in rows.items() if value["org_id"] == owner
        ]
    )
    management = MagicMock()
    management.send_message_async = AsyncMock(return_value=True)
    pool = GatewayConnectionPool(management, service)
    native = pool.native_rooms
    pool.gate_chat = AsyncMock(return_value={"blocked": False})
    pool.send_rpc = AsyncMock()
    pool.add_frontend_connection = MagicMock(wraps=pool.add_frontend_connection)
    pool.remove_frontend_connection = MagicMock(wraps=pool.remove_frontend_connection)
    members = {"user_A", "user_B"}

    async def membership(subject, **kwargs):
        return [{"id": "org_team"}] if subject in members else []

    async def dial(uri, **kwargs):
        assert uri == "ws://10.0.0.20:18789"
        socket = FakeSocket(gateway, kwargs["additional_headers"])
        gateway.sockets.append(socket)
        return socket

    monkeypatch.setattr(rooms.websockets, "connect", dial)
    monkeypatch.setattr(rooms, "_qualified_owners", {"org_team": proof})
    monkeypatch.setattr("core.services.clerk_admin.list_user_organizations", membership)
    monkeypatch.setattr(route, "get_connection_service", AsyncMock(return_value=service))
    monkeypatch.setattr(route, "get_management_api_client", AsyncMock(return_value=management))
    monkeypatch.setattr(route, "get_gateway_pool", lambda: pool)
    monkeypatch.setattr("core.containers.get_gateway_pool", lambda: pool)
    manager = SimpleNamespace(
        resolve_running_container=AsyncMock(return_value=({"release_update_id": "release-test"}, "10.0.0.20"))
    )
    monkeypatch.setattr(route, "get_ecs_manager", lambda: manager)

    async def request(method, params=None, tab="tab-a", **extra):
        before = management.send_message_async.await_count
        await route._process_native_room_background(
            tab, {"type": "req", "id": "same-client-rpc-id", "method": method, "params": params or {}, **extra}
        )
        assert management.send_message_async.await_count == before + 1
        return management.send_message_async.call_args.args[1]

    state = SimpleNamespace(
        request=request,
        gateway=gateway,
        rows=rows,
        members=members,
        native=native,
        management=management,
        pool=pool,
        proof=proof,
        service=service,
        manager=manager,
    )
    yield state
    await pool.close_all()


def test_alias_is_stable_under_native_normalization_and_distinct_across_issuer_and_case():
    a = identity()
    assert a.alias == a.alias.strip().lower()
    assert len({a.alias, replace(a, subject="user_a").alias, replace(a, issuer="https://other.test").alias}) == 3
    assert a.alias == replace(a, connection_id="reconnected", source_ip="203.0.113.12").alias


@pytest.mark.parametrize(
    "field,value",
    [
        ("verified_issuer", "https://evil.test"),
        ("verified_subject", "user_B"),
        ("auth_kind", "device"),
        ("auth_kind", "service"),
        ("connection_type", "operator"),
        ("org_id", "user_A"),
        ("source_ip", "127.0.0.1"),
        ("source_ip", "198.51.100.1, 127.0.0.1"),
    ],
)
def test_forged_or_machine_identity_rejected(field, value):
    with pytest.raises(rooms.RoomError):
        rooms.HumanIdentity.from_connection("tab", {**row(), field: value})


@pytest.mark.asyncio
async def test_two_humans_reconnect_history_and_unknown_author(harness):
    h = harness
    created = await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new-room"})
    key = created["payload"]["key"]
    assert created["payload"]["createdBy"]["id"] == "profile-user_A"
    first = await h.request(
        "chat.send", {"sessionKey": key, "message": "A", "idempotencyKey": "turn", "expectedLeafEntryId": None}
    )
    second = await h.request(
        "chat.send",
        {"sessionKey": key, "message": "B", "idempotencyKey": "turn", "expectedLeafEntryId": "entry-1"},
        tab="tab-b",
    )
    assert first["payload"]["runId"] != second["payload"]["runId"]
    h.gateway.history[key].append({"id": "old-unknown", "role": "user", "content": "legacy imported without actor"})
    await h.native.close_connection("tab-a")
    restored = await h.request("chat.history", {"sessionKey": key})
    messages = restored["payload"]["messages"]
    assert [message.get("sender", {}).get("id") for message in messages] == ["profile-user_A", "profile-user_B", None]
    assert messages[1]["parentId"] == "entry-1"
    assert all(conn.profile["id"] == conn.device.profile_id for conn in h.native.connections.values())
    h.pool.send_rpc.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_sends_keep_native_conflict_without_replay(harness):
    h = harness
    key = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]["key"]
    params = {"sessionKey": key, "message": "one", "idempotencyKey": "turn", "expectedLeafEntryId": None}
    first = await h.request("chat.send", params)
    retry = await h.request("chat.send", params, tab="tab-a2")
    assert first["payload"] == retry["payload"]
    assert len(h.gateway.history[key]) == 1
    conflict = await h.request("chat.send", {**params, "message": "two"}, tab="tab-b")
    assert conflict["error"]["code"] == "STALE_LEAF"
    assert conflict["error"]["details"] == {"expectedLeafEntryId": "entry-1"}
    assert len(h.gateway.history[key]) == 1
    assert len([call for call in h.gateway.calls if call["method"] == "chat.send"]) == 3


@pytest.mark.asyncio
async def test_independent_tab_subscriptions_and_revocation(harness, monkeypatch):
    h = harness
    key = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]["key"]
    await h.request("sessions.messages.subscribe", {"key": key})
    await h.request("connect", tab="tab-a2")
    first = h.native.connections[identity()]
    second = h.native.connections[identity(connection_id="tab-a2")]
    event = {
        "type": "event",
        "event": "session.message",
        "payload": {"sessionKey": key, "message": {"sender": {"id": "profile-user_B"}}},
    }
    h.management.send_message_async.reset_mock()
    await first.forward_event(event)
    await second.forward_event(event)
    h.management.send_message_async.assert_awaited_once_with("tab-a", event)
    await first.forward_event({**event, "payload": {"sessionKey": "agent:friday:user_B", "secret": "PRIVATE"}})
    assert h.management.send_message_async.await_count == 1
    h.members.remove("user_A")
    with pytest.raises(rooms.RoomError):
        await first.forward_event(event)
    broker = SimpleNamespace(unsubscribe=AsyncMock())
    monkeypatch.setattr("core.services.teams_event_broker_singleton.get_broker", lambda: broker)
    await rooms.revoke_member_connections("org_team", "user_A")
    assert "tab-b" in h.rows and "tab-a" not in h.rows and "tab-a2" not in h.rows
    assert first.closed and second.closed and not first.subscriptions
    assert h.pool.remove_frontend_connection.call_count == 2
    assert broker.unsubscribe.await_count == 2
    assert h.management.close_connection.call_count == 2


@pytest.mark.parametrize(
    "method,params",
    [
        ("chat.history", {"sessionKey": "agent:friday:user_B"}),
        ("chat.history", {"sessionKey": "agent:friday:user_A:01234567-1234-1234-1234-123456789abc"}),
        ("sessions.messages.subscribe", {"key": "agent:friday:user_B"}),
        ("sessions.assignOwner", {"sessionKey": "anything", "owner": {"type": "human", "id": "profile-user_A"}}),
        ("session.members.add", {"key": "anything", "identityId": "profile-user_A"}),
        ("sessions.list", {}),
        ("sessions.search", {"query": "secret"}),
        ("sessions.preview", {"keys": ["agent:friday:user_B"]}),
        ("chat.inject", {"sessionKey": "anything", "message": "forged"}),
        ("tools.invoke", {}),
        ("node.invoke", {}),
        ("config.set", {}),
        ("session.publicShare.set", {}),
        ("chat.attachments.get", {}),
        ("connect", {"role": "node"}),
        ("connect", {"scopes": ["operator.admin"]}),
        ("connect", {"auth": {"token": "service"}}),
    ],
)
@pytest.mark.asyncio
async def test_private_foreign_and_unadmitted_surfaces_never_dial(harness, method, params):
    result = await harness.request(method, params)
    assert result["ok"] is False
    assert harness.gateway.calls == []
    harness.pool.send_rpc.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"sender": {"id": "profile-user_B"}},
        {"ownerId": "org_foreign"},
        {"actor": "admin"},
        {"scopes": ["operator.admin"]},
        {"systemInputProvenance": {}},
        {"attachments": []},
        {"queueMode": "steer-backlog"},
        {"expectedLeafId": None},
    ],
)
@pytest.mark.asyncio
async def test_actor_and_scope_overrides_rejected(harness, override):
    h = harness
    key = rooms.room_prefix("org_team", "friday") + "a" * 64
    result = await h.request(
        "chat.send",
        {"sessionKey": key, "message": "x", "idempotencyKey": "id", "expectedLeafEntryId": None, **override},
    )
    assert result["ok"] is False
    assert h.gateway.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("human_auth_verified", False),
        ("machine_coexistence_verified", False),
        ("legacy_privacy_verified", False),
        ("release_id", "other"),
        ("owner_id", "org_foreign"),
        ("build_id", ""),
    ],
)
@pytest.mark.asyncio
async def test_unknown_or_unqualified_runtime_fails_closed(harness, monkeypatch, field, value):
    monkeypatch.setattr(rooms, "_qualified_owners", {"org_team": replace(harness.proof, **{field: value})})
    result = await harness.request("connect")
    assert result["error"]["code"] == "ROOM_UNAVAILABLE"
    assert harness.gateway.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("scopes", ["operator.admin", "operator.read", "operator.write"]),
        ("profile_override", "profile-user_B"),
        ("build_id", "unknown-runtime"),
    ],
)
@pytest.mark.asyncio
async def test_native_hello_must_match_bounded_human_qualification(harness, field, value):
    setattr(harness.gateway, field, value)
    result = await harness.request("connect")
    assert result["ok"] is False
    assert not harness.native.connections
    assert all(socket.closed for socket in harness.gateway.sockets)


@pytest.mark.asyncio
async def test_budget_gate_and_membership_are_before_send(harness):
    h = harness
    key = rooms.room_prefix("org_team", "friday") + "a" * 64
    params = {"sessionKey": key, "message": "x", "idempotencyKey": "turn", "expectedLeafEntryId": None}
    h.pool.gate_chat.return_value = {"blocked": True, "code": "subscription_inactive"}
    assert (await h.request("chat.send", params))["error"]["code"] == "subscription_inactive"
    h.members.remove("user_A")
    assert (await h.request("chat.send", params))["error"]["code"] == "FORBIDDEN"
    assert not any(call["method"] == "chat.send" for call in h.gateway.calls)


@pytest.mark.asyncio
async def test_native_methods_use_tagged_keys_and_keep_company_audience(harness):
    h = harness
    key = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]["key"]
    assert (await h.request("connect"))["payload"]["companyRooms"]["audience"] == "company"
    for method, params in [
        ("session.members.listEvidence", {"sessionKey": key}),
        ("session.members.add", {"sessionKey": key, "identityId": "profile-user_B"}),
        ("session.members.remove", {"sessionKey": key, "identityId": "profile-user_B"}),
        ("sessions.assignOwner", {"key": key, "owner": {"type": "human", "id": "profile-user_B"}}),
        ("session.typing", {"sessionKey": key, "sessionId": "incarnation-1", "typing": True}),
        ("users.mentionable", {"sessionKey": key, "query": "B"}),
        ("session.visibility.set", {"sessionKey": key, "visibility": "read-only"}),
    ]:
        assert (await h.request(method, params))["ok"]
        assert h.gateway.calls[-1]["params"] == params
    h.gateway.mentions = [{"id": "private", "sessionKey": "agent:friday:user_B"}, {"id": "room", "sessionKey": key}]
    assert (await h.request("mentions.list"))["payload"]["items"] == [{"id": "room", "sessionKey": key}]


def test_foreign_room_key_and_agent_substitution_rejected():
    a, proof = identity(), qualification()
    for key in [
        rooms.room_prefix("org_foreign", "friday") + "a" * 64,
        rooms.room_prefix("org_team", "other") + "a" * 64,
    ]:
        with pytest.raises(rooms.RoomError):
            rooms.admit_rpc("chat.history", {"sessionKey": key}, a, proof)
    key = rooms.room_prefix("org_team", "friday") + "a" * 64
    with pytest.raises(rooms.RoomError):
        rooms.admit_rpc("chat.history", {"sessionKey": key, "agentId": "other"}, a, proof)


def test_internal_edge_secret_required_even_when_legacy_enforcement_off(monkeypatch):
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", False)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "test-edge-secret")
    with pytest.raises(Exception) as exc:
        route._require_native_internal("forged")
    assert exc.value.status_code == 403
    route._require_native_internal("test-edge-secret")


@pytest.mark.asyncio
async def test_simultaneous_humans_get_one_commit_and_one_stale_conflict(harness):
    h = harness
    key = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]["key"]
    await h.request("connect", tab="tab-b")
    h.management.send_message_async.reset_mock()
    await asyncio.gather(
        *(
            route._process_native_room_background(
                tab,
                {
                    "type": "req",
                    "id": tab,
                    "method": "chat.send",
                    "params": {
                        "sessionKey": key,
                        "message": tab,
                        "idempotencyKey": "collision",
                        "expectedLeafEntryId": None,
                    },
                },
            )
            for tab in ("tab-a", "tab-b")
        )
    )
    replies = [call.args[1] for call in h.management.send_message_async.call_args_list]
    assert sorted(reply["ok"] for reply in replies) == [False, True]
    assert next(reply for reply in replies if not reply["ok"])["error"]["code"] == "STALE_LEAF"
    assert len(h.gateway.history[key]) == 1
    assert len([call for call in h.gateway.calls if call["method"] == "chat.send"]) == 2


@pytest.mark.asyncio
async def test_native_http_admission_uses_durable_identity_and_cannot_downgrade(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(settings, "WS_ENFORCE_INTERNAL_SECRET", False)
    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "synthetic-edge")
    app = FastAPI()
    app.include_router(route.router, prefix="/ws")
    headers = {
        "x-connection-id": "tab-a",
        "x-user-id": "forged",
        "x-org-id": "org_foreign",
        "x-client-id": rooms.CLIENT_ID,
        "x-lightbulb-ws-internal": "synthetic-edge",
        "x-verified-issuer": "forged",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/ws/connect", headers=headers)).status_code == 200
        h.service.store_connection.assert_not_called()
        assert h.rows["tab-a"]["verified_subject"] == "user_A"
        assert (
            await client.post(
                "/ws/message", headers=headers, json={"type": "req", "id": "native", "method": "connect", "params": {}}
            )
        ).status_code == 200
        assert (
            h.management.send_message_async.call_args.args[1]["payload"]["companyRooms"]["profile"]["id"]
            == "profile-user_A"
        )
        await client.post(
            "/ws/message", headers=headers, json={"type": "agent_chat", "agent_id": "friday", "message": "bypass"}
        )
        assert h.management.send_message_async.call_args.args[1]["ok"] is False
        assert (
            await client.post("/ws/message", headers={"x-connection-id": "tab-a"}, json={"type": "ping"})
        ).status_code == 403
    h.pool.send_rpc.assert_not_called()
    h.pool.add_frontend_connection.assert_not_called()


@pytest.mark.asyncio
async def test_revoked_qualification_stops_warm_events_and_oversized_event_requests_history(harness, monkeypatch):
    h = harness
    key = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]["key"]
    await h.request("sessions.messages.subscribe", {"key": key})
    conn = h.native.connections[identity()]
    event = {"type": "event", "event": "session.message", "payload": {"sessionKey": key, "message": "x" * 120_000}}
    await conn.forward_event(event)
    assert h.management.send_message_async.call_args.args[1] == {
        "type": "event",
        "event": "native.room.historyRequired",
        "payload": {"sessionKey": key},
    }
    monkeypatch.setattr(rooms, "_qualified_owners", {})
    with pytest.raises(rooms.RoomError):
        await conn.forward_event(event)


@pytest.mark.asyncio
async def test_organization_webhook_revokes_connections_before_other_side_effects(monkeypatch):
    from routers import webhooks

    revoke = AsyncMock(side_effect=RuntimeError("revocation incomplete"))
    provision = AsyncMock()
    monkeypatch.setattr(rooms, "revoke_member_connections", revoke)
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", provision)
    with pytest.raises(RuntimeError, match="revocation incomplete"):
        await webhooks._handle_organization_membership_deleted(
            {"user_id": "user_A", "organization": {"id": "org_team"}}
        )
    revoke.assert_awaited_once_with("org_team", "user_A")
    provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_reader_revokes_frontend_after_membership_removed(harness):
    h = harness
    key = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]["key"]
    await h.request("sessions.messages.subscribe", {"key": key})
    conn = h.native.connections[identity()]
    h.members.remove("user_A")
    conn.ws.queue.put_nowait(json.dumps({"type": "event", "event": "session.message", "payload": {"sessionKey": key}}))
    await asyncio.wait_for(conn.reader, 1)
    assert conn.closed and not conn.subscriptions and "tab-a" not in h.rows
    h.management.close_connection.assert_called_once_with("tab-a")


@pytest.mark.asyncio
async def test_real_pool_owns_native_connections_disconnect_and_shutdown(harness):
    h = harness
    assert isinstance(h.pool, GatewayConnectionPool)
    assert h.pool.native_rooms.conn_svc is h.service
    legacy = GatewayConnection("org_team", "10.0.0.20", "synthetic-token", h.management, set(), conn_svc=h.service)
    assert not hasattr(legacy, "native_rooms")
    await h.request("connect")
    first = h.pool.native_rooms.connections[identity()]
    h.rows.pop("tab-a")  # The disconnect Lambda removes the row first.
    assert (await route.ws_disconnect("tab-a")).status_code == 200
    assert first.closed and identity() not in h.pool.native_rooms.connections
    await h.request("connect", tab="tab-b")
    second = h.pool.native_rooms.connections[identity("user_B", "tab-b")]
    await h.pool.close_all()
    assert second.closed and h.pool.native_rooms.closed and not h.pool.native_rooms.connections


@pytest.mark.asyncio
async def test_real_pool_ordinary_membership_removal_with_native_unavailable(harness, monkeypatch):
    from routers import webhooks

    h = harness
    monkeypatch.setattr(rooms, "_qualified_owners", {})
    h.rows.clear()
    h.rows.update(
        {
            "legacy-a": {"user_id": "user_A", "org_id": "org_team", "connection_type": "chat"},
            "legacy-b": {"user_id": "user_B", "org_id": "org_team", "connection_type": "chat"},
        }
    )
    h.pool.add_frontend_connection("org_team", "legacy-a")
    h.pool.add_frontend_connection("org_team", "legacy-b")
    provisioning = SimpleNamespace(archive_member=AsyncMock())
    close_provisioning = AsyncMock()
    sync_seats = AsyncMock()
    broker = SimpleNamespace(unsubscribe=AsyncMock())
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", AsyncMock(return_value=provisioning))
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", close_provisioning)
    monkeypatch.setattr("core.services.billing_service.sync_team_seat_quantity", sync_seats)
    monkeypatch.setattr("core.services.teams_event_broker_singleton.get_broker", lambda: broker)
    await webhooks._handle_organization_membership_deleted({"user_id": "user_A", "organization": {"id": "org_team"}})
    assert "legacy-a" not in h.rows and "legacy-b" in h.rows
    assert h.pool._frontend_connections["org_team"] == {"legacy-b"}
    h.management.close_connection.assert_called_once_with("legacy-a")
    broker.unsubscribe.assert_awaited_once_with("user_A", "legacy-a")
    provisioning.archive_member.assert_awaited_once_with(user_id="user_A")
    close_provisioning.assert_awaited_once_with(provisioning)
    sync_seats.assert_awaited_once_with("org_team")
    assert not h.pool.native_rooms.connections and not h.gateway.calls


@pytest.mark.parametrize(
    "selectors",
    [
        {"sessionId": "incarnation"},
        {"sessionId": "incarnation", "messageId": "entry", "offset": 0},
        {"messageId": "entry", "cursor": "cursor"},
        {"offset": 0, "cursor": "cursor"},
    ],
)
@pytest.mark.asyncio
async def test_history_rejects_native_invalid_selector_combinations_before_dial(harness, selectors):
    key = rooms.room_prefix("org_team", "friday") + "a" * 64
    result = await harness.request("chat.history", {"sessionKey": key, **selectors})
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert not harness.gateway.calls


@pytest.mark.parametrize("with_session_id", [False, True])
@pytest.mark.asyncio
async def test_history_message_selector_reaches_native_unchanged(harness, with_session_id):
    h = harness
    created = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]
    key = created["key"]
    await h.request(
        "chat.send", {"sessionKey": key, "message": "selected", "idempotencyKey": "turn", "expectedLeafEntryId": None}
    )
    params = {"sessionKey": key, "messageId": "entry-1"}
    if with_session_id:
        params["sessionId"] = created["sessionId"]
    result = await h.request("chat.history", params)
    assert result["ok"] is True and result["payload"]["messages"][0]["content"] == "selected"
    assert h.gateway.calls[-1]["params"] == params


@pytest.mark.asyncio
async def test_history_selected_transcript_ownership_remains_upstream(harness):
    h = harness
    key = (await h.request("sessions.create", {"agentId": "friday", "idempotencyKey": "new"}))["payload"]["key"]
    h.gateway.transcripts["foreign-transcript"] = "agent:friday:user_B"
    params = {"sessionKey": key, "sessionId": "foreign-transcript", "messageId": "private-message"}
    result = await h.request("chat.history", params)
    assert result["ok"] is False
    assert result["error"] == {"code": "INVALID_REQUEST", "message": "sessionId does not belong to sessionKey"}
    assert h.gateway.calls[-1]["params"] == params
    before = len(h.gateway.calls)
    result = await h.request("chat.history", {**params, "sessionKey": "agent:friday:user_B"})
    assert result["error"]["code"] == "FORBIDDEN" and len(h.gateway.calls) == before


@pytest.mark.parametrize("method,field", [("mentions.dismiss", "ids"), ("users.mentionable", "agentId")])
def test_mention_reference_bound_matches_native(method, field):
    # Test the closed schema directly: agent/key equality is a separate gate.
    params = {"sessionKey": "room"} if method == "users.mentionable" else {}
    for length, allowed in [(256, True), (257, False)]:
        value = "a" * length
        params[field] = [value] if field == "ids" else value
        assert rooms._VALIDATORS[method].is_valid(params) is allowed
