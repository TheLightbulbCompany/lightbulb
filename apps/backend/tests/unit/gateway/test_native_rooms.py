"""Synthetic broker tests only; no runtime/auth/isolation qualification evidence."""

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
                "features": {"methods": sorted(self.gateway.methods)},
            }
        elif method == "users.self":
            result = {"profile": {"id": self.gateway.profile_override or human.profile_id, "displayName": alias}}
        elif self.gateway.error is not None:
            ok, result = False, self.gateway.error
        elif method == "sessions.message.append":
            result = self.gateway.receipt
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
        receipt=receipt(),
        error=None,
        methods=rooms.ROOM_METHODS | {"users.self"},
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
    pending_revocations = set()

    async def begin_revocation(owner, member, key):
        pending_revocations.add(key)
        rows.pop(key, None)

    service.pending_member_revocations = AsyncMock(side_effect=lambda *_: set(pending_revocations))
    service.begin_member_revocation = AsyncMock(side_effect=begin_revocation)
    service.finish_member_revocation = AsyncMock(
        side_effect=lambda _owner, _member, key: pending_revocations.discard(key)
    )
    service.begin_native_run = AsyncMock(side_effect=AssertionError("append must not create a hold"))
    service.finish_native_run = AsyncMock()
    service.query_by_owner = AsyncMock(
        side_effect=lambda owner: [
            (key, value["user_id"], value["connection_type"]) for key, value in rows.items() if value["org_id"] == owner
        ]
    )
    management = MagicMock()
    management.send_message_async = AsyncMock(return_value=True)
    pool = GatewayConnectionPool(management, service)
    native = pool.native_rooms
    pool.gate_chat = AsyncMock(side_effect=AssertionError("append must not enter billing"))
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("human_auth_verified", False),
        ("machine_coexistence_verified", False),
        ("legacy_privacy_verified", False),
        ("release_id", "other"),
        ("owner_id", "org_foreign"),
        ("ip", "10.0.0.21"),
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
        ("methods", {"users.self", "chat.send"}),
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
        assert h.management.send_message_async.call_args.args[1]["payload"]["companyRooms"] == {"enabled": False}
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
async def test_organization_webhook_revocation_failure_does_not_skip_deauthorization(monkeypatch):
    from routers import webhooks

    revoke = AsyncMock(side_effect=RuntimeError("revocation incomplete"))
    provisioning = SimpleNamespace(archive_member=AsyncMock())
    provision = AsyncMock(return_value=provisioning)
    monkeypatch.setattr(rooms, "revoke_member_connections", revoke)
    monkeypatch.setattr(webhooks, "build_paperclip_provisioning", provision)
    monkeypatch.setattr(webhooks, "close_paperclip_provisioning", AsyncMock())
    sync_seats = AsyncMock()
    monkeypatch.setattr("core.services.billing_service.sync_team_seat_quantity", sync_seats)
    with pytest.raises(RuntimeError, match="revocation incomplete"):
        await webhooks._handle_organization_membership_deleted(
            {"user_id": "user_A", "organization": {"id": "org_team"}}
        )
    revoke.assert_awaited_once_with("org_team", "user_A")
    provision.assert_awaited_once()
    provisioning.archive_member.assert_awaited_once_with(user_id="user_A")
    sync_seats.assert_awaited_once_with("org_team")


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


@pytest.mark.asyncio
async def test_idle_stop_waits_for_native_handshake_before_final_local_veto(harness, monkeypatch):
    h = harness
    dialing, release, checked_presence = asyncio.Event(), asyncio.Event(), asyncio.Event()
    dial = rooms.websockets.connect

    async def slow_dial(*args, **kwargs):
        dialing.set()
        await release.wait()
        return await dial(*args, **kwargs)

    async def no_operator(_owner):
        checked_presence.set()
        return False

    monkeypatch.setattr(rooms.websockets, "connect", slow_dial)
    h.service.count_live_chat_connections = AsyncMock(return_value=0)
    h.service.has_live_operator_connection = no_operator
    h.service.get_live_gateway_conn_token = AsyncMock(return_value=None)
    stop = AsyncMock()
    monkeypatch.setattr("core.containers.get_ecs_manager", lambda: SimpleNamespace(stop_user_service=stop))
    monkeypatch.setattr(
        "core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value={"subscription_status": "trial_local"})
    )
    connect = asyncio.create_task(h.request("connect"))
    await asyncio.wait_for(dialing.wait(), 1)
    stopping = asyncio.create_task(h.pool._maybe_stop_trial_container("org_team", trigger="boot_reconcile"))
    await asyncio.wait_for(checked_presence.wait(), 1)
    assert not stopping.done()
    release.set()
    await connect
    await stopping
    assert h.native.connections
    stop.assert_not_awaited()


def room_key():
    return rooms.room_prefix("org_team", "friday") + "a" * 64


def append_params():
    return {
        "sessionKey": room_key(),
        "expectedSessionId": "incarnation-1",
        "idempotencyKey": "turn",
        "text": "hello",
    }


def receipt(**overrides):
    return {
        "sessionKey": room_key(),
        "sessionId": "incarnation-1",
        "messageId": "entry-1",
        "messageSeq": 1,
        "appended": True,
        **overrides,
    }


@pytest.mark.asyncio
async def test_atomic_create_and_typed_members_map_to_reviewed_native_methods(harness):
    h = harness
    members = [{"type": "profile", "id": "profile-user_B"}, {"type": "agent", "id": "friday"}]
    params = {"agentId": "friday", "idempotencyKey": "new", "roomKind": "group-dm", "members": members}
    created = await h.request("sessions.room.create", params)
    key = created["payload"]["key"]
    native = h.gateway.calls[-1]
    assert native["method"] == "sessions.room.create"
    assert native["params"] == {
        **params,
        "key": key,
        "visibility": "restricted",
        "idempotencyKey": rooms._digest(["org_team", identity().issuer, "user_A", key, "new"]),
    }
    assert key == rooms.room_prefix("org_team", "friday") + rooms._digest([identity().issuer, "user_A", "new"])
    for method in ("session.members.add", "session.members.remove"):
        for member in members:
            mutation = {"sessionKey": key, "identity": member}
            assert (await h.request(method, mutation))["ok"] is True
            assert h.gateway.calls[-1]["method"] == method
            assert h.gateway.calls[-1]["params"] == mutation
    h.pool.gate_chat.assert_not_awaited()
    h.service.begin_native_run.assert_not_awaited()


@pytest.mark.parametrize(
    "override",
    [
        {"visibility": "shared"},
        {"key": "caller-controlled"},
        {"message": "start inference"},
        {"model": "model"},
        {"members": [{"type": "profile", "id": "profile-user_A", "role": "owner"}]},
    ],
)
@pytest.mark.asyncio
async def test_create_closed_schema_rejects_runtime_and_acl_overrides(harness, override):
    params = {"agentId": "friday", "idempotencyKey": "new", "roomKind": "channel", "members": []}
    result = await harness.request("sessions.room.create", {**params, **override})
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert not harness.gateway.calls


@pytest.mark.asyncio
async def test_thread_creation_keeps_native_origin_and_fences_foreign_parent(harness):
    params = {
        "agentId": "friday",
        "idempotencyKey": "thread",
        "roomKind": "thread",
        "members": [],
        "parentSessionKey": room_key(),
        "threadOrigin": {"parentRoomKey": room_key(), "originRootMessageId": "entry-root"},
    }
    assert (await harness.request("sessions.room.create", params))["ok"] is True
    assert harness.gateway.calls[-1]["params"]["threadOrigin"] == params["threadOrigin"]
    before = len(harness.gateway.calls)
    for overrides in (
        {"parentSessionKey": "agent:friday:private"},
        {"threadOrigin": {"parentRoomKey": "agent:friday:private"}},
    ):
        result = await harness.request("sessions.room.create", {**params, **overrides})
        assert result["error"]["code"] == "FORBIDDEN"
        assert len(harness.gateway.calls) == before


@pytest.mark.parametrize(
    "override",
    [
        {"sender": {"id": "profile-user_B"}},
        {"role": "assistant"},
        {"displayName": "B"},
        {"display": "B"},
        {"avatar": "url"},
        {"timestamp": 1},
        {"model": "model"},
        {"deliver": False},
        {"delivery": {}},
        {"parentId": "entry"},
        {"expectedLeafEntryId": None},
        {"sessionId": "incarnation-1"},
        {"agentId": "friday"},
        {"ownerId": "org_foreign"},
        {"actor": "admin"},
        {"scopes": ["operator.admin"]},
        {"systemInputProvenance": {}},
        {"attachments": []},
        {"queueMode": "steer"},
        {"message": "legacy"},
        {"text": ""},
        {"mentions": [{"type": "profile", "id": "profile-user_A", "displayName": "A"}]},
        {"mentions": [{"profileId": "profile-user_A", "start": 0, "end": 1}]},
    ],
)
@pytest.mark.asyncio
async def test_append_closed_schema_rejects_overrides_before_dial(harness, override):
    result = await harness.request("sessions.message.append", {**append_params(), **override})
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert not harness.gateway.calls


@pytest.mark.parametrize("field", ["sessionKey", "expectedSessionId", "idempotencyKey", "text"])
def test_append_requires_exact_closed_contract(field):
    params = append_params()
    params.pop(field)
    with pytest.raises(rooms.RoomError):
        rooms.admit_rpc("sessions.message.append", params, identity(), qualification())


@pytest.mark.asyncio
async def test_append_receipt_replay_and_identity_bypass_billing_and_holds(harness):
    h = harness
    params = {
        **append_params(),
        "replyToId": "entry-parent",
        "mentions": [{"type": "agent", "id": "friday"}],
    }
    h.gateway.receipt = receipt(effectiveParentId="entry-parent")
    assert (await h.request("sessions.message.append", params))["payload"] == h.gateway.receipt
    h.gateway.receipt = {**h.gateway.receipt, "appended": False}
    assert (await h.request("sessions.message.append", params, tab="tab-a2"))["payload"] == h.gateway.receipt
    assert (await h.request("sessions.message.append", params, tab="tab-b"))["payload"] == h.gateway.receipt
    appends = [call for call in h.gateway.calls if call["method"] == "sessions.message.append"]
    assert len(appends) == 3 and all(call["params"] == params for call in appends)
    assert h.native.connections[identity()].profile["id"] == "profile-user_A"
    assert h.native.connections[identity("user_B", "tab-b")].profile["id"] == "profile-user_B"
    assert len({socket.headers["x-lightbulb-human"] for socket in h.gateway.sockets}) == 2
    h.pool.gate_chat.assert_not_awaited()
    h.service.begin_native_run.assert_not_awaited()
    h.service.finish_native_run.assert_not_awaited()
    h.pool.send_rpc.assert_not_called()
    assert not h.native.run_holds


@pytest.mark.parametrize(
    "error",
    [
        {"code": "INVALID_REQUEST", "message": "message append idempotency conflict"},
        {"code": "INVALID_REQUEST", "message": "session changed", "details": {"reason": "stale-session"}},
        {"code": "INVALID_REQUEST", "message": "replyToId must identify a message in this session"},
        {"code": "UNAVAILABLE", "message": "message append could not be committed"},
    ],
)
@pytest.mark.asyncio
async def test_append_preserves_native_errors_without_automatic_replay(harness, error):
    harness.gateway.error = error
    result = await harness.request("sessions.message.append", append_params())
    assert result["ok"] is False and result["error"] == error
    assert len([call for call in harness.gateway.calls if call["method"] == "sessions.message.append"]) == 1
    harness.pool.gate_chat.assert_not_awaited()
    harness.service.begin_native_run.assert_not_awaited()


@pytest.mark.parametrize(
    "override",
    [
        {"sessionKey": "agent:friday:private"},
        {"sessionId": "different-incarnation"},
        {"messageSeq": 0},
        {"appended": "yes"},
        {"messageId": ""},
        {"runId": "not-an-append-receipt"},
    ],
)
@pytest.mark.asyncio
async def test_append_rejects_malformed_or_mismatched_native_receipt(harness, override):
    harness.gateway.receipt = receipt(**override)
    result = await harness.request("sessions.message.append", append_params())
    assert result["error"]["code"] == "ROOM_UNAVAILABLE"


@pytest.mark.parametrize(
    "method,params",
    [
        ("sessions.message.append", append_params()),
        (
            "session.members.add",
            {"sessionKey": room_key(), "identity": {"type": "profile", "id": "profile-user_B"}},
        ),
        ("session.members.remove", {"sessionKey": room_key(), "identity": {"type": "agent", "id": "friday"}}),
    ],
)
@pytest.mark.asyncio
async def test_org_prefix_never_substitutes_for_native_membership(harness, method, params):
    assert rooms.owns_room_key(room_key(), identity(), harness.proof)
    # Organization admission is valid, but the native ACL denies this human.
    error = {"code": "FORBIDDEN", "message": "restricted room membership required"}
    harness.gateway.error = error
    result = await harness.request(method, params)
    assert result["error"] == error
    assert harness.gateway.calls[-1]["method"] == method
    assert not harness.native.connections


@pytest.mark.parametrize(
    "method",
    [
        "sessions.create",
        "chat.send",
        "chat.history",
        "chat.abort",
        "sessions.send",
        "sessions.list",
        "sessions.search",
        "sessions.messages.subscribe",
        "sessions.messages.unsubscribe",
        "sessions.assignOwner",
        "session.members.list",
        "session.members.listEvidence",
        "session.visibility.set",
        "session.typing",
        "users.mentionable",
        "mentions.list",
        "mentions.dismiss",
        "dispatch_agent_for_message",
        "set_room_viewers",
        "subscribe_presence",
        "mark_read",
        "tools.invoke",
        "node.invoke",
        "config.set",
        "chat.inject",
        "session.publicShare.set",
    ],
)
@pytest.mark.asyncio
async def test_unimplemented_surfaces_never_dial(harness, method):
    result = await harness.request(method, append_params())
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert not harness.gateway.calls


@pytest.mark.parametrize(
    "params",
    [
        {**append_params(), "sessionKey": "agent:friday:user_A"},
        {**append_params(), "sessionKey": rooms.room_prefix("org_foreign", "friday") + "a" * 64},
        {**append_params(), "sessionKey": rooms.room_prefix("org_team", "other") + "a" * 64},
        {**append_params(), "mentions": [{"type": "profile", "id": "friday"}]},
        {**append_params(), "mentions": [{"type": "agent", "id": "profile-user_A"}]},
    ],
)
@pytest.mark.asyncio
async def test_foreign_targets_and_unqualified_typed_identities_never_dial(harness, params):
    assert (await harness.request("sessions.message.append", params))["error"]["code"] == "FORBIDDEN"
    assert not harness.gateway.calls


@pytest.mark.asyncio
async def test_qualification_and_advertisement_remain_fail_closed(harness, monkeypatch):
    assert (await harness.request("connect"))["payload"] == {"protocol": 4, "companyRooms": {"enabled": False}}
    monkeypatch.setattr(rooms, "_qualified_owners", {})
    assert (await harness.request("connect"))["error"]["code"] == "ROOM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_revocation_closes_durable_edge_human_sockets_and_broker(harness, monkeypatch):
    h = harness
    await h.request("connect")
    await h.request("connect", tab="tab-a2")
    first = h.native.connections[identity()]
    second = h.native.connections[identity(connection_id="tab-a2")]
    first.subscriptions.add(room_key())  # Existing state is still cleared on revocation.
    broker = SimpleNamespace(unsubscribe=AsyncMock())
    monkeypatch.setattr("core.services.teams_event_broker_singleton.get_broker", lambda: broker)
    await rooms.revoke_member_connections("org_team", "user_A")
    assert "tab-b" in h.rows and "tab-a" not in h.rows and "tab-a2" not in h.rows
    assert first.closed and second.closed and not first.subscriptions
    assert h.pool.remove_frontend_connection.call_count == 2
    assert broker.unsubscribe.await_count == 2 and h.management.close_connection.call_count == 2
    assert (await h.request("sessions.message.append", append_params()))["error"]["code"] == "FORBIDDEN"
    assert not any(call["method"] == "sessions.message.append" for call in h.gateway.calls)


@pytest.mark.asyncio
async def test_warm_reader_revokes_frontend_after_membership_removed(harness):
    h = harness
    await h.request("connect")
    conn = h.native.connections[identity()]
    h.members.remove("user_A")
    conn.ws.queue.put_nowait(
        json.dumps({"type": "event", "event": "session.message", "payload": {"sessionKey": room_key()}})
    )
    await asyncio.wait_for(conn.reader, 1)
    assert conn.closed and "tab-a" not in h.rows
    h.management.close_connection.assert_called_once_with("tab-a")
