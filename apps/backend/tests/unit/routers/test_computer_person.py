"""A signed-in computer (``lightbulb login``) calls the workspace tool routes as its person: each
call signed over its method, path, query, body hash, a timestamp and the calling session's label."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import base64
import hashlib
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.config import settings
from core.services import clerk_admin, network_login, network_proof
from core.services.messaging import ApiProblem
from routers import messaging as router
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401
from tests.unit.routers.test_network_login import Computer

ROOM = "22222222-2222-4222-8222-222222222222"
LABEL = "Claude Code on Adá’s Mac"  # UTF-8: the challenge travels base64url


@pytest.fixture
def env(table, monkeypatch):
    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "test-proof-seed")
    orgs = AsyncMock(return_value=[{"id": "org_1", "role": "org:member"}])
    monkeypatch.setattr(clerk_admin, "list_user_organizations", orgs)
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda _r, exc: JSONResponse(exc.body(), exc.status_code))
    computer = Computer()
    proof = network_proof.mint(computer.key, "user_ada")
    with patch("core.dynamodb._table_prefix", "test-"), TestClient(app) as client:
        yield client, computer, proof, orgs


def signed(computer, proof, method, path, body=b"", *, ts=None, label=LABEL, sign_path=None):
    challenge = " ".join(
        [str(ts or int(time.time())), method, f"/api/v1{sign_path or path}", hashlib.sha256(body).hexdigest(), label]
    )
    sig = computer.private.sign(network_login.message(computer.key, "tools", challenge))
    return {
        "X-Lightbulb-Proof": proof,
        "X-Lightbulb-Sig": base64.urlsafe_b64encode(sig).decode().rstrip("="),
        "X-Lightbulb-Challenge": base64.urlsafe_b64encode(challenge.encode()).decode().rstrip("="),
        "Content-Type": "application/json",
    }


def test_computer_person_accepts_a_fresh_signed_request(env, monkeypatch):
    client, computer, proof, _ = env
    read = AsyncMock(return_value={"rooms": []})
    monkeypatch.setattr(router.hosted_mcp, "rooms_for", read)
    path = "/local-sessions/device/rooms?query=gen"
    response = client.get(f"/api/v1{path}", headers=signed(computer, proof, "GET", path))
    assert response.status_code == 200, response.text
    auth = read.await_args.args[0]
    assert (auth.user_id, auth.org_id) == ("user_ada", "org_1")


def test_computer_person_refuses_replayed_stale_or_other_body(env, monkeypatch):
    client, computer, proof, _ = env
    monkeypatch.setattr(router.task_board, "update_task", AsyncMock(return_value={"ok": True}))
    path = "/local-sessions/device/tasks/ISOA-1"
    body = json.dumps({"status": "done"}).encode()
    headers = signed(computer, proof, "PATCH", path, body)
    assert client.patch(f"/api/v1{path}", content=body, headers=headers).status_code == 200
    # The same signed call again: good once.
    assert client.patch(f"/api/v1{path}", content=body, headers=headers).status_code == 401
    # Nor under another spelling of the same signature (padding, or other unused trailing bits).
    fresh = signed(
        computer, proof, "PATCH", path, body, ts=int(time.time()) - 5
    )  # another call, never the first one's second
    sig = fresh["X-Lightbulb-Sig"]
    abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = abc[abc.index(sig[-1]) + 1]  # 64 bytes: the last character's low 4 bits are unused
    for spelling in (sig + "==", sig[:-1] + last):
        respelled = {**fresh, "X-Lightbulb-Sig": spelling}
        assert client.patch(f"/api/v1{path}", content=body, headers=respelled).status_code == 401
    assert client.patch(f"/api/v1{path}", content=body, headers=fresh).status_code == 200
    # Another body under a signature for this one.
    other = signed(computer, proof, "PATCH", path, body)
    assert client.patch(f"/api/v1{path}", content=b'{"status":"todo"}', headers=other).status_code == 401
    # Older than the window.
    stale = signed(computer, proof, "PATCH", path, body, ts=int(time.time()) - 120)
    assert client.patch(f"/api/v1{path}", content=body, headers=stale).status_code == 401
    # Another path, and another computer's proof.
    elsewhere = signed(computer, proof, "PATCH", path, body, sign_path="/local-sessions/device/tasks/ISOA-2")
    assert client.patch(f"/api/v1{path}", content=body, headers=elsewhere).status_code == 401
    theirs = signed(Computer(), proof, "PATCH", path, body)
    assert client.patch(f"/api/v1{path}", content=body, headers=theirs).status_code == 401


def test_computer_person_needs_one_workspace(env, monkeypatch):
    client, computer, proof, orgs = env
    monkeypatch.setattr(router.hosted_mcp, "rooms_for", AsyncMock(return_value={"rooms": []}))
    path = "/local-sessions/device/rooms"
    for n, found in enumerate(([], [{"id": "org_1"}, {"id": "org_2"}])):
        orgs.return_value = found
        response = client.get(f"/api/v1{path}", headers=signed(computer, proof, "GET", path, label=f"{LABEL} {n}"))
        assert response.status_code == 409 and "WORKSPACE_REQUIRED" in response.text


def test_the_login_workspace_picks_among_several_and_membership_is_rechecked(env, monkeypatch):
    client, computer, _, orgs = env
    read = AsyncMock(return_value={"rooms": []})
    monkeypatch.setattr(router.hosted_mcp, "rooms_for", read)
    proof = network_proof.mint(computer.key, "user_ada", org="org_2")
    path = "/local-sessions/device/rooms"
    orgs.return_value = [{"id": "org_1", "role": "org:member"}, {"id": "org_2", "role": "org:admin"}]
    response = client.get(f"/api/v1{path}", headers=signed(computer, proof, "GET", path, label=f"{LABEL} a"))
    assert response.status_code == 200, response.text
    auth = read.await_args.args[0]
    assert (auth.user_id, auth.org_id, auth.org_role) == ("user_ada", "org_2", "org:admin")
    # Removed from the login's workspace: refused, never moved to another one.
    orgs.return_value = [{"id": "org_1", "role": "org:member"}]
    response = client.get(f"/api/v1{path}", headers=signed(computer, proof, "GET", path, label=f"{LABEL} b"))
    assert response.status_code == 403


def test_computer_identity_is_free_when_in_no_workspace(env, monkeypatch):
    from routers import sessions

    client, computer, proof, orgs = env
    orgs.return_value = []
    app = FastAPI()
    app.include_router(sessions.router, prefix="/api/v1/sessions")
    with TestClient(app) as c:
        listed = AsyncMock(return_value=[])
        monkeypatch.setattr(sessions.session_store, "list_sessions", listed)
        response = c.get("/api/v1/sessions", headers=signed(computer, proof, "GET", "/sessions", sign_path="/sessions"))
    assert response.status_code == 200, response.text
    assert listed.await_args.kwargs["user_id"] == "user_ada" and listed.await_args.kwargs["workspace_id"] is None


def test_only_listed_routes_accept_computer_person():
    def deps(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from deps(dep)

    accepted = sorted(
        (sorted(route.methods)[0], route.path)
        for route in router.router.routes
        if network_login.computer_person in set(deps(route.dependant))
        or router.person_or_computer in set(deps(route.dependant))
    )
    assert accepted == [
        ("GET", "/local-sessions/device/receipt"),
        ("GET", "/local-sessions/device/rooms"),
        ("GET", "/local-sessions/device/rooms/{room_id}/messages"),
        ("GET", "/local-sessions/device/tasks"),
        ("GET", "/local-sessions/device/tasks/{task}"),
        ("PATCH", "/local-sessions/device/tasks/{task}"),
        ("POST", "/local-sessions/device/sessions/browser-place"),
        ("POST", "/messaging/rooms"),
        ("POST", "/messaging/rooms/{room_id}/messages"),
    ]
    # The sessions routes take a computer's identity too, free (no workspace) or in one.
    from routers import sessions

    assert sorted(
        (sorted(route.methods)[0], route.path)
        for route in sessions.router.routes
        if sessions.caller in set(deps(route.dependant))
    ) == [("DELETE", "/{session_id}"), ("GET", ""), ("POST", "")]


def test_room_post_from_a_computer_carries_the_session_label(env, monkeypatch):
    client, computer, proof, _ = env
    post = AsyncMock(side_effect=ApiProblem("ROOM_NOT_FOUND", "stop here"))
    monkeypatch.setattr(router.service, "create_human_message", post)
    monkeypatch.setattr(router.service, "human_display_name", AsyncMock(return_value="Ada"))
    path = f"/messaging/rooms/{ROOM}/messages"
    body = json.dumps(
        {
            "clientMessageId": "33333333-3333-4333-8333-333333333333",
            "text": "hi",
            "replyToMessageId": None,
            "threadRootMessageId": None,
        }
    ).encode()
    response = client.post(f"/api/v1{path}", content=body, headers=signed(computer, proof, "POST", path, body))
    assert response.status_code == 404, response.text
    kwargs = post.await_args.kwargs
    assert kwargs["via"] == "lightbulb_mcp"
    assert kwargs["author_override"]["displayName"] == f"{LABEL} via Ada"
    assert post.await_args.args[0].user_id == "user_ada"
    # A computer opens DMs only, never a channel.
    path = "/messaging/rooms"
    body = json.dumps({"clientRoomId": ROOM, "kind": "channel", "name": "x", "members": []}).encode()
    response = client.post(f"/api/v1{path}", content=body, headers=signed(computer, proof, "POST", path, body))
    assert response.status_code in (403, 422)


def test_browser_place_by_native_id(env, monkeypatch):
    client, computer, proof, _ = env
    place = AsyncMock(return_value={"where": "cloud"})
    monkeypatch.setattr(router.session_browser, "place_by_native", place)
    path = "/local-sessions/device/sessions/browser-place"
    body = json.dumps({"where": "cloud", "native_id": "c1a2"}).encode()
    response = client.post(f"/api/v1{path}", content=body, headers=signed(computer, proof, "POST", path, body))
    assert response.status_code == 200, response.text
    place.assert_awaited_once_with("org_1", "user_ada", "c1a2", "cloud")


def test_a_computer_reply_lands_in_its_parents_thread(env, monkeypatch):
    client, computer, proof, _ = env
    post = AsyncMock(side_effect=ApiProblem("ROOM_NOT_FOUND", "stop here"))
    monkeypatch.setattr(router.service, "create_human_message", post)
    monkeypatch.setattr(router.service, "human_display_name", AsyncMock(return_value="Ada"))
    root, parent = "44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555"
    monkeypatch.setattr(
        router.messaging_repo,
        "get_message_by_id",
        AsyncMock(return_value={"messageId": parent, "threadRootMessageId": root}),
    )
    path = f"/messaging/rooms/{ROOM}/messages"
    body = json.dumps(
        {
            "clientMessageId": "33333333-3333-4333-8333-333333333333",
            "text": "hi",
            "replyToMessageId": parent,
            "threadRootMessageId": None,
        }
    ).encode()
    client.post(f"/api/v1{path}", content=body, headers=signed(computer, proof, "POST", path, body))
    assert str(post.await_args.args[2].thread_root_message_id) == root


def test_a_re_encoded_query_still_names_the_signed_request():
    # API Gateway's HTTP API may re-encode a query; the values, not the bytes, are what was signed.
    same = network_login._same_target
    assert same("/api/v1/x?query=a b&limit=5", "/api/v1/x", "query=a%20b&limit=5")
    assert same("/api/v1/x?query=a%20b", "/api/v1/x", "query=a+b")
    assert same("/api/v1/rooms/a%2Fb", "/api/v1/rooms/a/b", "")
    assert not same("/api/v1/x?query=a", "/api/v1/x", "query=b")
    assert not same("/api/v1/x?query=a", "/api/v1/x", "query=a&limit=5")
    assert not same("/api/v1/x", "/api/v1/y", "")
