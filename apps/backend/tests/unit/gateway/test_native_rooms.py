"""Synthetic broker tests only; no runtime/auth/isolation qualification evidence."""

import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.config import settings
from core.crypto.operator_device import build_v4_connect_payload, generate_operator_device
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
        frozenset({"friday"}),
        {
            identity(subject).alias: rooms.QualifiedHumanDevice(generate_operator_device(), f"profile-{subject}")
            for subject in ("user_A", "user_B")
        },
    )


class FakeSocket:
    def __init__(self, gateway):
        self.gateway = gateway
        self.human = None
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
        ok = True
        if method == "connect":
            human = next(
                human
                for human in self.gateway.qualification.humans.values()
                if human.identity.device_id == params["device"]["id"]
            )
            self.human = human
            assert params["auth"] == {
                "token": "server-gateway-token",
                "trustedBrokerProfileId": human.profile_id,
            }
            assert params["role"] == "operator"
            assert params["device"]["id"] == human.identity.device_id
            assert params["scopes"] == list(rooms.HUMAN_SCOPES)
            signed = build_v4_connect_payload(
                device_id=params["device"]["id"],
                client_id=params["client"]["id"],
                client_mode=params["client"]["mode"],
                role=params["role"],
                scopes=params["scopes"],
                signed_at_ms=params["device"]["signedAt"],
                token=params["auth"]["token"],
                nonce=params["device"]["nonce"],
                platform=params["client"].get("platform"),
                device_family=params["client"].get("deviceFamily"),
                trusted_broker_profile_id=params["auth"]["trustedBrokerProfileId"],
            )
            human.identity.public_key().verify(
                base64.urlsafe_b64decode(
                    params["device"]["signature"] + "=" * (-len(params["device"]["signature"]) % 4)
                ),
                signed.encode(),
            )
            result = {
                "type": "hello-ok",
                "protocol": 4,
                "server": {"buildId": self.gateway.build_id},
                "auth": {
                    "role": "operator",
                    "scopes": self.gateway.scopes,
                    **self.gateway.auth_overrides,
                },
                "features": {"methods": sorted(self.gateway.methods)},
            }
        elif method == "users.self":
            result = {"profile": {"id": self.gateway.profile_override or self.human.profile_id}}
        elif self.gateway.error is not None:
            ok, result = False, self.gateway.error
        elif method == "sessions.message.append":
            result = self.gateway.receipt
        elif method in self.gateway.results:
            result = self.gateway.results[method]
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
        results={
            "sessions.list": {"sessions": [], "count": 0},
            "sessions.search": {"results": []},
            "users.mentionable": {"users": [], "truncated": False},
        },
        error=None,
        methods=rooms.ROOM_METHODS | {"users.self", "agent.wait"},
        scopes=list(rooms.HUMAN_SCOPES),
        auth_overrides={},
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
    service.begin_native_run = AsyncMock(return_value=True)
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
    pool.gate_chat = AsyncMock(return_value={"blocked": False})
    pool.send_rpc = AsyncMock()
    pool.add_frontend_connection = MagicMock(wraps=pool.add_frontend_connection)
    pool.remove_frontend_connection = MagicMock(wraps=pool.remove_frontend_connection)
    members = {"user_A", "user_B"}

    async def membership(subject, **kwargs):
        return [{"id": "org_team"}] if subject in members else []

    async def dial(uri, **kwargs):
        assert uri == "ws://10.0.0.20:18789"
        assert "additional_headers" not in kwargs
        socket = FakeSocket(gateway)
        gateway.sockets.append(socket)
        return socket

    monkeypatch.setattr(rooms.websockets, "connect", dial)
    monkeypatch.setattr(rooms, "_qualified_owners", {"org_team": proof})
    monkeypatch.setattr("core.services.clerk_admin.list_user_organizations", membership)
    monkeypatch.setattr(
        "core.services.human_broker_provisioning.require_active_human_device",
        AsyncMock(),
    )
    monkeypatch.setattr(route, "get_connection_service", AsyncMock(return_value=service))
    monkeypatch.setattr(route, "get_management_api_client", AsyncMock(return_value=management))
    monkeypatch.setattr(route, "get_gateway_pool", lambda: pool)
    monkeypatch.setattr("core.containers.get_gateway_pool", lambda: pool)
    manager = SimpleNamespace(
        resolve_running_container=AsyncMock(return_value=({"gateway_token": "server-gateway-token"}, "10.0.0.20"))
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


def test_trusted_broker_v4_payload_uses_unambiguous_utf8_length_frames():
    assert build_v4_connect_payload(
        device_id="d",
        client_id="gateway-client",
        client_mode="backend",
        role="operator",
        scopes=("operator.read", "operator.write"),
        signed_at_ms=7,
        token="t|ø",
        nonce="n",
        platform=" LINUX ",
        device_family="Web|X",
        trusted_broker_profile_id="profile_1",
    ) == (
        "v4|1:d|14:gateway-client|7:backend|8:operator|1:2|13:operator.read|"
        "14:operator.write|1:7|4:t|ø|1:n|5:linux|5:web|x|9:profile_1"
    )


def test_human_broker_scopes_are_non_administrative():
    assert rooms.HUMAN_SCOPES == ("operator.read", "operator.write")


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
        ("release_id", ""),
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
        ("auth_overrides", {"deviceToken": "returned-secret"}),
        ("auth_overrides", {"deviceTokens": [{"deviceToken": "returned-secret"}]}),
        ("auth_overrides", {"bootstrapToken": "returned-secret"}),
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
async def test_server_gateway_token_is_required_and_browser_cannot_supply_auth(harness):
    harness.manager.resolve_running_container.return_value = ({}, "10.0.0.20")
    missing = await harness.request("connect")
    assert missing["error"]["code"] == "ROOM_UNAVAILABLE"
    forged = await harness.request("connect", {"auth": {"token": "browser-token"}})
    assert forged["error"]["code"] == "INVALID_REQUEST"
    assert not harness.gateway.sockets


@pytest.mark.asyncio
async def test_warm_native_socket_fails_closed_when_durable_human_device_is_revoked(harness, monkeypatch):
    assert (await harness.request("sessions.list", {}))["ok"] is True
    connection = harness.native.connections[identity()]
    monkeypatch.setattr(
        "core.services.human_broker_provisioning.require_active_human_device",
        AsyncMock(side_effect=RuntimeError("disabled")),
    )

    with pytest.raises(rooms.RoomError, match="revoked or is unavailable"):
        await connection.require_current()


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
    assert len({call["params"]["device"]["id"] for call in h.gateway.calls if call["method"] == "connect"}) == 2
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
        ("chat.history", {"sessionKey": room_key(), "limit": 50}),
        ("sessions.search", {"sessionKeys": [room_key()], "query": "plan", "limit": 10}),
        ("sessions.list", {"agentId": "friday", "limit": 50, "includeDerivedTitles": True}),
        ("sessions.messages.subscribe", {"key": room_key()}),
        ("sessions.messages.unsubscribe", {"key": room_key()}),
        (
            "sessions.presence.heartbeat",
            {"sessionKey": room_key(), "visibility": "visible", "recentInput": "recent"},
        ),
        ("sessions.presence.snapshot", {"sessionKey": room_key()}),
        ("sessions.presence.subscribe", {"sessionKey": room_key()}),
        ("sessions.presence.unsubscribe", {"sessionKey": room_key()}),
        (
            "session.typing",
            {"sessionKey": room_key(), "sessionId": "incarnation-1", "typing": True},
        ),
        ("users.mentionable", {"sessionKey": room_key(), "query": "user"}),
        ("session.members.list", {"sessionKey": room_key()}),
        ("session.members.listEvidence", {"sessionKey": room_key()}),
        ("sessions.files.list", {"sessionKey": room_key(), "path": "", "search": "readme"}),
        ("sessions.files.get", {"sessionKey": room_key(), "path": "README.md"}),
        (
            "sessions.execution.dispatch",
            {
                "sessionKey": room_key(),
                "expectedSessionId": "incarnation-1",
                "inputMessageId": "entry-1",
                "idempotencyKey": "dispatch-1",
                "hopCount": 0,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_reviewed_room_methods_forward_only_their_closed_native_contract(harness, method, params):
    assert (await harness.request(method, params))["ok"] is True
    forwarded = [call for call in harness.gateway.calls if call["method"] == method]
    assert forwarded[-1]["params"] == params
    if method == "sessions.execution.dispatch":
        harness.pool.gate_chat.assert_awaited_once_with(user_id="user_A", owner_id="org_team", session_key=room_key())
    else:
        harness.pool.gate_chat.assert_not_awaited()


def dispatch_params():
    return {
        "sessionKey": room_key(),
        "expectedSessionId": "incarnation-1",
        "inputMessageId": "entry-1",
        "idempotencyKey": "dispatch-1",
        "hopCount": 0,
    }


@pytest.mark.asyncio
async def test_dispatch_hold_survives_tab_disconnect_until_explicit_reconciliation(harness):
    h = harness
    assert (await h.request("sessions.execution.dispatch", dispatch_params()))["ok"] is True
    run_id = rooms.native_execution_run_id("profile-user_A", dispatch_params())
    run = ("org_team", room_key(), run_id)
    h.service.begin_native_run.assert_awaited_once_with(*run)
    assert run in h.native.run_holds
    await h.native.close_connection("tab-a")
    assert run in h.native.run_holds
    h.service.finish_native_run.assert_not_awaited()
    await h.native.finish_run(*run)
    h.service.finish_native_run.assert_awaited_once_with(*run)
    assert run not in h.native.run_holds


@pytest.mark.asyncio
async def test_dispatch_verified_rejection_releases_hold(harness):
    h = harness
    await h.request("connect")
    h.gateway.error = {"code": "INVALID_REQUEST", "message": "execution input is not current"}
    result = await h.request("sessions.execution.dispatch", dispatch_params())
    run = ("org_team", room_key(), rooms.native_execution_run_id("profile-user_A", dispatch_params()))
    assert result["error"] == h.gateway.error
    h.service.finish_native_run.assert_awaited_once_with(*run)
    assert run not in h.native.run_holds


@pytest.mark.asyncio
async def test_dispatch_retry_rejection_preserves_an_existing_hold(harness):
    h = harness
    await h.request("connect")
    run = ("org_team", room_key(), rooms.native_execution_run_id("profile-user_A", dispatch_params()))
    h.native.run_holds.add(run)
    h.service.begin_native_run.return_value = False
    h.gateway.error = {"code": "INVALID_REQUEST", "message": "execution input is not current"}

    result = await h.request("sessions.execution.dispatch", dispatch_params())

    assert result["error"] == h.gateway.error
    h.service.finish_native_run.assert_not_awaited()
    assert run in h.native.run_holds


@pytest.mark.asyncio
async def test_dispatch_uncertain_outcome_preserves_hold_until_matching_terminal_event(harness):
    h = harness
    await h.request("connect")
    conn = h.native.connections[identity()]
    conn.rpc = AsyncMock(side_effect=TimeoutError("unknown dispatch outcome"))
    result = await h.request("sessions.execution.dispatch", dispatch_params())
    run_id = rooms.native_execution_run_id("profile-user_A", dispatch_params())
    run = ("org_team", room_key(), run_id)
    assert result["error"]["code"] == "ROOM_UNAVAILABLE"
    assert run in h.native.run_holds
    h.service.finish_native_run.assert_not_awaited()
    await conn.forward_event(
        {
            "type": "event",
            "event": "chat",
            "payload": {"sessionKey": room_key(), "runId": run_id, "state": "final"},
        }
    )
    h.service.finish_native_run.assert_awaited_once_with(*run)
    assert run not in h.native.run_holds


@pytest.mark.asyncio
async def test_cached_completed_dispatch_retry_wait_releases_hold(harness):
    h = harness
    run_id = rooms.native_execution_run_id("profile-user_A", dispatch_params())
    run = ("org_team", room_key(), run_id)

    assert (await h.request("sessions.execution.dispatch", dispatch_params()))["ok"] is True
    assert run in h.native.run_holds
    h.service.finish_native_run.assert_not_awaited()

    h.gateway.results["agent.wait"] = {"runId": run_id, "status": "ok", "endedAt": 123}
    assert (await h.request("sessions.execution.dispatch", dispatch_params()))["ok"] is True
    assert [call["method"] for call in h.gateway.calls[-2:]] == [
        "sessions.execution.dispatch",
        "agent.wait",
    ]
    assert h.gateway.calls[-1]["params"] == {"runId": run_id, "timeoutMs": 0}
    h.service.finish_native_run.assert_awaited_once_with(*run)
    assert run not in h.native.run_holds


@pytest.mark.parametrize(
    "wait_result",
    [
        {"status": "timeout"},
        {"status": "pending"},
        {"status": "ok", "runId": "foreign-run", "endedAt": 123},
        {
            "status": "timeout",
            "terminalReceipt": {
                "runId": "foreign-run",
                "sessionId": "incarnation-1",
                "turnId": "turn-1",
                "requested": {"provider": "p", "model": "m"},
                "effective": {"provider": "p", "model": "m", "responseModel": "m"},
                "successfulToolNames": [],
                "rerouted": False,
                "terminalDisposition": "visible",
            },
        },
    ],
)
@pytest.mark.asyncio
async def test_dispatch_wait_ambiguous_or_mismatched_result_retains_hold(harness, wait_result):
    h = harness
    run_id = rooms.native_execution_run_id("profile-user_A", dispatch_params())
    h.gateway.results["agent.wait"] = {"runId": run_id, **wait_result}
    assert (await h.request("sessions.execution.dispatch", dispatch_params()))["ok"] is True
    assert ("org_team", room_key(), run_id) in h.native.run_holds
    h.service.finish_native_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_wait_transport_error_retains_hold(harness):
    h = harness
    await h.request("connect")
    conn = h.native.connections[identity()]
    rpc = conn.rpc

    async def fail_wait(method, params):
        if method == "agent.wait":
            raise TimeoutError("wait transport failed")
        return await rpc(method, params)

    conn.rpc = fail_wait
    assert (await h.request("sessions.execution.dispatch", dispatch_params()))["ok"] is True
    run_id = rooms.native_execution_run_id("profile-user_A", dispatch_params())
    assert ("org_team", room_key(), run_id) in h.native.run_holds
    h.service.finish_native_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_timeout_with_ended_at_for_exact_run_releases_hold(harness):
    h = harness
    run_id = rooms.native_execution_run_id("profile-user_A", dispatch_params())
    h.gateway.results["agent.wait"] = {"runId": run_id, "status": "timeout", "endedAt": 123}
    assert (await h.request("sessions.execution.dispatch", dispatch_params()))["ok"] is True
    h.service.finish_native_run.assert_awaited_once_with("org_team", room_key(), run_id)
    assert ("org_team", room_key(), run_id) not in h.native.run_holds


@pytest.mark.asyncio
async def test_shared_room_mark_read_is_not_exposed_to_browser(harness):
    result = await harness.request(
        "sessions.patch",
        {
            "key": room_key(),
            "expectedSessionId": "incarnation-1",
            "expectedMarkedUnreadAt": None,
            "unread": False,
        },
    )
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert not harness.gateway.calls


@pytest.mark.parametrize(
    "method,params",
    [
        ("sessions.search", {"query": "unscoped"}),
        ("sessions.files.reveal", {"key": room_key()}),
        (
            "sessions.files.set",
            {
                "sessionKey": room_key(),
                "path": "README.md",
                "content": "updated",
                "expectedHash": "a" * 64,
            },
        ),
        ("sessions.execution.dispatch", {**append_params(), "inputMessageId": "entry-1", "text": "forged"}),
        (
            "session.typing",
            {"sessionKey": room_key(), "sessionId": "incarnation-1", "typing": True, "actor": "forged"},
        ),
        (
            "session.typing",
            {"sessionKey": room_key(), "sessionId": "incarnation-1", "typing": True, "preview": "private"},
        ),
        ("sessions.presence.heartbeat", {"sessionKey": room_key(), "serverReceivedAtMs": 1_000_000_000_000}),
    ],
)
@pytest.mark.asyncio
async def test_room_capabilities_reject_unscoped_or_server_owned_fields(harness, method, params):
    assert (await harness.request(method, params))["error"]["code"] == "INVALID_REQUEST"
    assert not harness.gateway.calls


@pytest.mark.asyncio
async def test_room_discovery_results_are_filtered_to_qualified_room_targets_and_profiles(harness):
    h = harness
    h.gateway.results["sessions.list"] = {
        "sessions": [{"key": room_key()}, {"key": "agent:friday:user_A"}],
        "count": 2,
    }
    h.gateway.results["sessions.search"] = {
        "results": [
            {"sessionKey": room_key(), "snippet": "room"},
            {"sessionKey": "agent:friday:user_A", "snippet": "private"},
        ]
    }
    h.gateway.results["users.mentionable"] = {
        "users": [
            {"profileId": "profile-user_A", "displayName": "A", "online": True},
            {"profileId": "profile-foreign", "displayName": "Foreign", "online": True},
        ],
        "truncated": False,
    }
    listed = await h.request("sessions.list", {})
    searched = await h.request("sessions.search", {"sessionKeys": [room_key()], "query": "room"})
    mentionable = await h.request("users.mentionable", {"sessionKey": room_key()})
    assert [item["key"] for item in listed["payload"]["sessions"]] == [room_key()]
    assert [item["sessionKey"] for item in searched["payload"]["results"]] == [room_key()]
    assert [item["profileId"] for item in mentionable["payload"]["users"]] == ["profile-user_A"]


@pytest.mark.asyncio
async def test_message_and_presence_subscriptions_gate_native_events_per_tab(harness):
    h = harness
    await h.request("sessions.messages.subscribe", {"key": room_key()})
    conn = h.native.connections[identity()]
    h.management.send_message_async.reset_mock()
    message = {"type": "event", "event": "session.message", "payload": {"sessionKey": room_key()}}
    presence = {"type": "event", "event": "session.presence", "payload": {"roomKey": room_key()}}
    await conn.forward_event(message)
    await conn.forward_event(presence)
    h.management.send_message_async.assert_awaited_once_with("tab-a", message)
    await h.request("sessions.presence.subscribe", {"sessionKey": room_key()})
    h.management.send_message_async.reset_mock()
    await conn.forward_event(presence)
    h.management.send_message_async.assert_awaited_once_with("tab-a", presence)
    await h.request("sessions.messages.unsubscribe", {"key": room_key()})
    await h.request("sessions.presence.unsubscribe", {"sessionKey": room_key()})
    assert not conn.subscriptions and not conn.presence_subscriptions


@pytest.mark.asyncio
async def test_post_rpc_membership_loss_reports_uncertain_without_replaying_append(harness):
    h = harness
    checks = 0

    async def membership(subject, **kwargs):
        nonlocal checks
        checks += 1
        return [{"id": "org_team"}] if checks < 4 else []

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("core.services.clerk_admin.list_user_organizations", membership)
        result = await h.request("sessions.message.append", append_params())
    assert result["ok"] is False and result["error"] == {
        "code": "ROOM_UNAVAILABLE",
        "message": "Native request outcome is uncertain; reconcile canonical room state",
    }
    assert len([call for call in h.gateway.calls if call["method"] == "sessions.message.append"]) == 1


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
        "chat.abort",
        "sessions.send",
        "agent.wait",
        "sessions.assignOwner",
        "session.visibility.set",
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
