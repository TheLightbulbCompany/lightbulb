"""Hosted apps on the free network: a free account (no workspace) reaches the network through
its most recently connected logged-in computer's route link; nothing is queued when none is
online; ``wake_me`` wakes a bot with one content-free POST once its person entered the key."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from core.auth import AuthContext, get_current_user
from core.services import app_wake, hosted_mcp, network_login, network_member, network_proof, network_route
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

USER = "user_free"
PERSON = network_member.app_person(USER)
HERE = "http://10.0.0.1:8000"


class Computer:
    """A logged-in computer's daemon at the other end of a route link, in the engine's shapes."""

    def __init__(self, key: str):
        self.key = key
        self.ops: list[dict] = []
        self.mail = [{"id": "m1", "request_id": "r1", "from": "ab12/claude-5", "text": "hi"}]

    def answer(self, frame: dict) -> dict:
        self.ops.append(frame)
        op = frame["op"]
        result = {
            "app.send": {"request_id": frame.get("request_id"), "state": "delivered", "id": "m-sent"},
            "app.inbox": {"records": list(self.mail)},
            "app.ack": {"acked": len(frame.get("ids") or [])},
            "app.unread": {"unread": len(self.mail)},
            "app.receipt": {"request_id": frame.get("request_id"), "state": "delivered"},
            "directory": {"sessions": [{"session_id": "ab12/claude-5", "handle": "claude-5"}]},
        }.get(op)
        if op == "app.ack":
            self.mail = [m for m in self.mail if m["id"] not in frame["ids"]]
        return {"id": frame["id"], "ok": True, "result": result}


@pytest.fixture
def free(table, monkeypatch):
    """Ann has a free account (no workspace) and two logged-in computers online on this task."""
    monkeypatch.setattr("core.services.task_address.own_address", lambda: HERE)
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Ann Lee"))
    rows = [
        {"key": "kold", "userId": USER, "generation": "g1", "connectedAt": 100, "taskAddress": HERE, "name": "mbp"},
        {"key": "knew", "userId": USER, "generation": "g2", "connectedAt": 200, "taskAddress": HERE, "name": "mac"},
        {"key": "kother", "userId": "user_x", "generation": "g3", "connectedAt": 300, "taskAddress": HERE},
    ]
    connections = SimpleNamespace(
        routes_for_user=AsyncMock(side_effect=lambda u: [r for r in rows if r["userId"] == u]),
        live_routes=AsyncMock(side_effect=lambda keys: [r for r in rows if r["key"] in keys]),
    )
    monkeypatch.setattr(network_route, "_connections", lambda: connections)
    computers = {}
    for key, gen in (("kold", "g1"), ("knew", "g2")):
        computer = computers[key] = Computer(key)

        def make_send(c, k):
            async def send(frame):
                network_route.links[k].pending[frame["id"]].set_result(c.answer(frame))

            return send

        network_route.links[key] = network_route.Link(key, USER, gen, make_send(computer, key))
    token = SimpleNamespace(
        client_id="https://claude.ai/oauth/mcp",
        scopes=[hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE],
        subject=USER,
        claims={"org_id": None},
    )
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: token)
    yield SimpleNamespace(computers=computers, connections=connections, rows=rows)
    network_route.links.clear()


@pytest.mark.asyncio
async def test_a_new_app_is_pinned_to_the_newest_computer_and_never_moves_off_it(free):
    sent = await hosted_mcp.send_message("hello", "req-1", session_id="ab12/claude-5")
    assert sent["state"] == "delivered" and sent["destination"] == "network:ab12/claude-5"
    op = free.computers["knew"].ops[0]
    assert free.computers["kold"].ops == []
    assert (op["op"], op["app"], op["person"], op["title"]) == ("app.send", "claude", PERSON, "Claude via Ann")
    assert (op["to"], op["text"], op["request_id"]) == ("ab12/claude-5", "hello", "req-1")
    receipt = await hosted_mcp.message_receipt("req-1")
    assert receipt["destination"] == "network:ab12/claude-5"
    pin = await network_route.get_pin(USER, "https://claude.ai/oauth/mcp")
    assert pin["route"] == {"kind": "computer", "key": "knew", "name": "mac"}
    # The pinned computer goes offline: a clear refusal naming it, never the other computer.
    free.rows[:] = [r for r in free.rows if r["key"] != "knew"]
    with pytest.raises(ToolError) as offline:
        await hosted_mcp.send_message("again", "req-1b", session_id="ab12/claude-5")
    assert str(offline.value) == ("NETWORK_OFFLINE: mac is offline. Open it.")
    assert free.computers["kold"].ops == []


@pytest.mark.asyncio
async def test_check_inbox_and_find_sessions_through_the_computer(free):
    found = await hosted_mcp.find_sessions()
    assert found["sessions"][0]["session_id"] == "ab12/claude-5" and "agents" not in found
    inbox = await hosted_mcp.check_inbox()
    assert [m["id"] for m in inbox["messages"]] == ["m1"]
    ops = [o for o in free.computers["knew"].ops if o["op"] != "app.unread"]  # the unread line's
    assert [o["op"] for o in ops][-2:] == ["app.inbox", "app.ack"] and ops[-2]["peek"] is True
    assert free.computers["knew"].mail == []


@pytest.mark.asyncio
async def test_no_computer_online_is_a_clear_refusal_and_nothing_is_queued(free):
    free.rows.clear()
    with pytest.raises(ToolError) as refused:
        await hosted_mcp.send_message("hello", "req-2", session_id="ab12/claude-5")
    assert str(refused.value).startswith("NETWORK_NO_ROUTE: None of your computers is online")
    with pytest.raises(ToolError, match="None of your computers is online"):
        await hosted_mcp.find_sessions()


@pytest.mark.asyncio
async def test_a_free_account_gets_no_workspace_tool(free):
    with pytest.raises(ToolError) as refused:
        await hosted_mcp.find_rooms()
    assert str(refused.value).startswith("WORKSPACE_REQUIRED")
    with pytest.raises(ToolError):
        await hosted_mcp.send_message("hi", "00000000-0000-0000-0000-000000000001", room_id="x")


@pytest.mark.asyncio
async def test_only_route_ops_go_over_a_link_and_a_stale_generation_is_refused(free):
    with pytest.raises(RuntimeError) as bad:
        await network_route.local_call("knew", "g2", "team.leave", {}, 1)
    assert json.loads(str(bad.value))["code"] == "INVALID_REQUEST"
    with pytest.raises(RuntimeError) as stale:
        await network_route.local_call("knew", "g-old", "app.unread", {}, 1)
    assert json.loads(str(stale.value))["code"] == "UNAVAILABLE"


# ---- the socket and the cross-task door ---------------------------------------------------------


class Key:
    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        raw = self.private.public_key().public_bytes_raw()
        self.key = base64.b32encode(raw).decode().lower().rstrip("=")

    def hello(self, challenge: str, purpose: str = "route", user: str | None = USER) -> dict:
        sig = self.private.sign(network_login.message(self.key, purpose, challenge))
        proof = network_proof.mint(self.key, user) if user else None
        return {
            "type": "hello",
            "key": self.key,
            "proof": proof,
            "sig": base64.urlsafe_b64encode(sig).decode().rstrip("="),
        }


@pytest.fixture
def door(monkeypatch):
    from routers import network_route as router

    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-proof-seed")
    presence = SimpleNamespace(
        put_route=AsyncMock(),
        refresh_route=AsyncMock(return_value=True),
        delete_route=AsyncMock(),
        routes_for_user=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(network_route, "_connections", lambda: presence)
    monkeypatch.setattr("core.config.settings.WS_INTERNAL_SECRET", "s3cret")
    revoked: set[str] = set()
    monkeypatch.setattr(network_login, "is_revoked", AsyncMock(side_effect=lambda _user, key: key in revoked))
    notify = AsyncMock()
    monkeypatch.setattr(app_wake, "notify", notify)
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, presence=presence, notify=notify, revoked=revoked)
    network_route.links.clear()


def test_a_signed_in_computer_links_and_answers_ops_forwarded_from_another_task(door):
    computer = Key()
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        challenge = ws.receive_json()
        assert challenge["type"] == "challenge"
        ws.send_json(computer.hello(challenge["challenge"]))
        assert ws.receive_json() == {"type": "ready", "person": PERSON}
        put = door.presence.put_route.await_args.kwargs
        assert (put["key"], put["user_id"]) == (computer.key, USER)

        answers = []
        body = {
            "key": computer.key,
            "generation": put["generation"],
            "op": "app.unread",
            "params": {"app": "claude", "person": PERSON},
            "timeoutS": 5,
        }
        headers = {"x-lightbulb-ws-internal": "s3cret"}
        thread = threading.Thread(
            target=lambda: answers.append(
                door.client.post("/api/v1/network/internal/route", json=body, headers=headers)
            )
        )
        thread.start()
        frame = ws.receive_json()
        assert (frame["op"], frame["app"], frame["person"]) == ("app.unread", "claude", PERSON)
        ws.send_json({"id": frame["id"], "ok": True, "result": {"unread": 2}})
        thread.join(10)
        assert answers[0].json() == {"ok": True, "result": {"unread": 2}}

        # A mail notice wakes the app's bot (if it asked).
        ws.send_json({"type": "mail", "session": f"claude-{PERSON}"})
        for _ in range(50):
            if door.notify.await_count:
                break
            threading.Event().wait(0.05)
        door.notify.assert_awaited_once_with(USER, f"claude-{PERSON}")
    door.presence.delete_route.assert_awaited_once_with(computer.key, put["generation"])
    assert door.client.post("/api/v1/network/internal/route", json=body).status_code == 403


def test_serve_records_the_proofs_workspace_and_pushes_its_rules_once_until_close(door, monkeypatch):
    pushes, cancelled = [], []

    async def push(link, org):  # stands in for push_current_rules: it runs until the link closes
        pushes.append((link.key, link.org, org))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(link.key)
            raise

    monkeypatch.setattr(network_route, "push_current_rules", push)
    computer = Key()
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        hello = computer.hello(ws.receive_json()["challenge"])
        hello["proof"] = network_proof.mint(computer.key, USER, org="org_ws")
        ws.send_json(hello)
        assert ws.receive_json()["type"] == "ready"
        assert door.presence.put_route.await_args.kwargs["org"] == "org_ws"
        for _ in range(50):
            if pushes:
                break
            threading.Event().wait(0.05)
        assert cancelled == []
    for _ in range(50):
        if cancelled:
            break
        threading.Event().wait(0.05)
    assert pushes == [(computer.key, "org_ws", "org_ws")]
    assert cancelled == [computer.key]


def test_a_bad_proof_or_a_computer_nobody_signed_in_is_closed(door):
    from starlette.websockets import WebSocketDisconnect

    computer = Key()
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        challenge = ws.receive_json()["challenge"]
        ws.send_json(computer.hello(challenge, purpose="login"))  # another purpose's signature
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == network_route.CLOSE_BAD_PROOF
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        ws.send_json(computer.hello(ws.receive_json()["challenge"], user=None))  # never logged in
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == network_route.CLOSE_NOT_SIGNED_IN
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        hello = computer.hello(ws.receive_json()["challenge"])
        hello["proof"] = network_proof.mint(Key().key, USER)  # another computer's proof
        ws.send_json(hello)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == network_route.CLOSE_NOT_SIGNED_IN
    door.presence.put_route.assert_not_awaited()


def test_a_signed_out_computer_is_refused_and_its_live_link_closes_at_once(door):
    from starlette.websockets import WebSocketDisconnect

    computer = Key()
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        ws.send_json(computer.hello(ws.receive_json()["challenge"]))
        assert ws.receive_json()["type"] == "ready"
        generation = door.presence.put_route.await_args.kwargs["generation"]
        # Its person signs it out from the website, on whichever task: the link closes now.
        closed = door.client.post(
            "/api/v1/network/internal/route",
            json={"key": computer.key, "generation": generation, "op": "close", "params": {}, "timeoutS": 5},
            headers={"x-lightbulb-ws-internal": "s3cret"},
        )
        assert closed.json() == {"ok": True, "result": {}}
        with pytest.raises(WebSocketDisconnect) as gone:
            ws.receive_json()
    assert gone.value.code == network_route.CLOSE_NOT_SIGNED_IN
    door.revoked.add(computer.key)
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        ws.send_json(computer.hello(ws.receive_json()["challenge"]))
        with pytest.raises(WebSocketDisconnect) as refused:
            ws.receive_json()
    assert refused.value.code == network_route.CLOSE_NOT_SIGNED_IN


# ---- wake_me ------------------------------------------------------------------------------------


@pytest.fixture
def wakes(table, monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr("core.secrets_manager.create_or_update_secret", lambda _sm, n, v: secrets.__setitem__(n, v))
    monkeypatch.setattr(app_wake, "_sm", lambda: None)

    async def credential(owner, agent):
        return json.loads(secrets[app_wake.secret_name(owner, agent)])

    monkeypatch.setattr(app_wake, "_credential", credential)
    monkeypatch.setattr(app_wake, "check_url", lambda url: url)
    post = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(app_wake, "post", post)
    return SimpleNamespace(secrets=secrets, post=post)


@pytest.mark.asyncio
async def test_wake_me_needs_the_key_from_the_page_then_wakes_once_per_window(wakes, monkeypatch):
    token = SimpleNamespace(
        client_id="agentkey:k1",
        scopes=[hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE],
        subject=USER,
        claims={"org_id": None, "agentKey": {"keyId": "k1", "name": "Grok Bot"}},
    )
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: token)
    answer = await hosted_mcp.wake_me("https://hooks.example/routine")
    assert answer["state"] == "needs_key" and "/auth/network/apps" in answer["page"]
    (stored,) = wakes.secrets.values()
    assert json.loads(stored) == {"url": "https://hooks.example/routine", "key": ""}

    session = f"agent-k1-{PERSON}"
    assert await app_wake.notify(USER, session) is False  # no key yet: nothing is posted
    await app_wake.set_key(USER, "agent-k1", "whk_secret")
    assert json.loads(next(iter(wakes.secrets.values())))["key"] == "whk_secret"
    assert await app_wake.notify(USER, session) is True
    assert await app_wake.notify(USER, session) is False  # inside the window
    (owner, agent, payload), _ = wakes.post.await_args
    assert (owner, agent, payload["type"]) == (USER, "wake-agent-k1", "lightbulb.wake")
    assert "check_inbox" in payload["text"] and "hi" not in json.dumps(payload)
    assert await app_wake.notify(USER, f"agent-k1-{network_member.app_person('user_x')}") is False
    assert [w["state"] for w in await app_wake.list_wakes(USER)] == ["ready"]
    assert wakes.post.await_count == 1


def test_the_wake_page_routes_serve_a_free_account(wakes, monkeypatch):
    from routers import network_route as router

    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=USER, org_id=None)
    asyncio.run(app_wake.request(USER, "grok", "Grok", "https://hooks.example/x"))
    with TestClient(app) as client:
        assert [w["app"] for w in client.get("/api/v1/network/wakes").json()["wakes"]] == ["grok"]
        assert client.put("/api/v1/network/wakes/grok", json={"key": "k"}).json()["state"] == "ready"
        assert client.put("/api/v1/network/wakes/nope", json={"key": "k"}).status_code == 404
        monkeypatch.setattr(
            app_wake,
            "_sm",
            lambda: SimpleNamespace(
                delete_secret=lambda **_: None, exceptions=SimpleNamespace(ResourceNotFoundException=KeyError)
            ),
        )
        assert client.delete("/api/v1/network/wakes/grok").status_code == 204
        assert client.get("/api/v1/network/wakes").json() == {"wakes": []}


@pytest.mark.asyncio
async def test_a_user_owned_agent_key_is_a_free_accounts_network_caller(monkeypatch):
    row = {"keyId": "k1", "name": "Grok Bot", "userId": USER, "ownerId": USER, "expiresAt": None}
    monkeypatch.setattr(hosted_mcp.agent_keys, "resolve", AsyncMock(return_value=row))
    orgs = AsyncMock()
    monkeypatch.setattr(hosted_mcp.clerk_admin, "list_user_organizations", orgs)
    user = AsyncMock(return_value={"id": USER})
    monkeypatch.setattr(hosted_mcp.clerk_admin, "get_user_strict", user)
    token = await hosted_mcp.LightbulbVerifier().verify_token("lbk_x")
    assert token.subject == USER and token.claims["org_id"] is None
    orgs.assert_not_awaited()
    # Only while the account is live; a Clerk blip fails closed.
    for gone in (None, {"id": USER, "banned": True}, {"id": USER, "locked": True}):
        user.return_value = gone
        assert await hosted_mcp.LightbulbVerifier().verify_token("lbk_x") is None
    user.side_effect = hosted_mcp.clerk_admin.ClerkLookupError("blip")
    assert await hosted_mcp.LightbulbVerifier().verify_token("lbk_x") is None


def test_the_apps_page_lists_pins_and_moves_an_app_to_an_online_computer(free):
    from routers import network_route as router

    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=USER, org_id=None)
    route = {"kind": "computer", "key": "knew", "name": "mac"}
    asyncio.run(network_route.set_pin(USER, "chatgpt-client", route, "chatgpt", "ChatGPT"))
    with TestClient(app) as client:
        listed = client.get("/api/v1/network/apps").json()
        assert [a["clientId"] for a in listed["apps"]] == ["chatgpt-client"]
        assert [r["key"] for r in listed["routes"]] == ["knew", "kold"]  # newest first
        moved = client.put("/api/v1/network/apps/route", json={"clientId": "chatgpt-client", "key": "kold"})
        assert moved.json()["route"] == {"kind": "computer", "key": "kold", "name": "mbp"}
        offline = client.put("/api/v1/network/apps/route", json={"clientId": "chatgpt-client", "key": "k-gone"})
        assert offline.status_code == 409
        unknown = client.put("/api/v1/network/apps/route", json={"clientId": "nope", "key": "knew"})
        assert unknown.status_code == 404


def test_a_person_holds_at_most_ten_links_and_a_link_sends_thirty_mail_notices_a_minute(door):
    from starlette.websockets import WebSocketDisconnect

    door.presence.routes_for_user.return_value = [{"key": f"k{i}"} for i in range(network_route.MAX_LINKS_PER_USER)]
    computer = Key()
    with door.client.websocket_connect("/api/v1/gateway/network") as ws:
        ws.send_json(computer.hello(ws.receive_json()["challenge"]))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == network_route.CLOSE_TOO_MANY
    door.presence.put_route.assert_not_awaited()

    link = network_route.Link("k", USER, "g", AsyncMock())
    allowed = [network_route._mail_allowed(link) for _ in range(network_route.MAIL_PER_MINUTE + 5)]
    assert allowed.count(True) == network_route.MAIL_PER_MINUTE and not any(allowed[network_route.MAIL_PER_MINUTE :])
    link.mail_window -= 61  # a minute later
    assert network_route._mail_allowed(link)


def test_a_person_signs_out_one_of_their_connected_computers(table, monkeypatch):
    from unittest.mock import patch

    from core.services import clerk_admin
    from routers import network_route as router

    mine, theirs = Key().key, Key().key
    rows = [{"key": mine, "userId": USER, "generation": "g", "connectedAt": 1, "name": "mac"}]
    monkeypatch.setattr(
        network_route, "_connections", lambda: SimpleNamespace(routes_for_user=AsyncMock(return_value=rows))
    )
    closed = AsyncMock()
    monkeypatch.setattr(network_route, "sign_out", closed)
    published = AsyncMock()
    monkeypatch.setattr(network_member, "publish_sessions", published)
    monkeypatch.setattr(clerk_admin, "list_user_organizations", AsyncMock(return_value=[{"id": "org_1"}]))
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id=USER, org_id="org_1")
    with patch("core.dynamodb._table_prefix", "test-"), TestClient(app) as client:
        # Someone else's computer (or one not connected as you) never enters revoked_keys.
        assert client.post(f"/api/v1/network/computers/{theirs}/sign-out").status_code == 404
        assert client.post(f"/api/v1/network/computers/{mine}/sign-out").status_code == 204
        assert asyncio.run(network_login.is_revoked(USER, mine))
        assert asyncio.run(network_login.revoked_keys([USER])) == [mine]
    closed.assert_awaited_once_with(USER, mine)
    published.assert_awaited_once_with("org_1")
