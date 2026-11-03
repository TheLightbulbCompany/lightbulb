"""Outside A2A clients on the door through Clerk OAuth: who is recorded, which scope gates what, discovery."""

import httpx

from core.auth import AuthContext
from core.config import settings
from core.services import a2a_auth, a2a_tasks

URI = "https://thelightbulb.company/a2a/ext/authority/v1"
BASE = "/a2a/org_test_456/friday"
CIMD = "cimd:https://example/client.json"
AUTH = {
    "operation": {"skill": "message", "resource": "agent:friday", "operands": {"text": "hello"}},
    "intent": "say hello",
}
MSG = {
    "messageId": "m1",
    "role": "ROLE_USER",
    "parts": [{"text": "hello"}],
    "extensions": [URI],
    "metadata": {URI: AUTH},
}


def rpc(method, params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def as_oauth(app, *scopes):
    async def _caller():
        return a2a_auth.A2ACaller(
            auth=AuthContext(user_id="user_test_123", org_id="org_test_456"),
            verified_by="clerk-oauth",
            client_id=CIMD,
            scopes=frozenset({"user:org:read", *scopes}),
        )

    app.dependency_overrides[a2a_auth.a2a_caller] = _caller


def test_oauth_send_mints_with_client_as_first_hop_and_verified_by_clerk_oauth(app, client, a2a_env):
    as_oauth(app, a2a_auth.SEND_SCOPE, a2a_auth.READ_SCOPE_A2A)
    task = client.post(BASE, json=rpc("SendMessage", {"message": MSG})).json()["result"]["task"]
    got = client.post(BASE, json=rpc("GetTask", {"id": task["id"]})).json()["result"]
    record = a2a_tasks._claims(got["metadata"][URI]["record"])
    assert record["input"]["chain"][0]["agent"] == CIMD
    assert record["input"]["chain"][0]["person"] == "user_test_123"
    assert record["input"]["requester"]["verified_by"] == "clerk-oauth"


def test_read_only_token_can_get_task_but_not_send(app, client, a2a_env):
    task_id = client.post(BASE, json=rpc("SendMessage", {"message": MSG})).json()["result"]["task"]["id"]
    as_oauth(app, a2a_auth.READ_SCOPE_A2A)
    assert client.post(BASE, json=rpc("GetTask", {"id": task_id})).json()["result"]["id"] == task_id
    assert client.get(f"{BASE}/tasks/{task_id}/record").status_code == 200
    r = client.post(BASE, json=rpc("SendMessage", {"message": MSG}))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "scope_required"
    as_oauth(app, a2a_auth.SEND_SCOPE)
    assert client.post(BASE, json=rpc("GetTask", {"id": task_id})).status_code == 403
    assert client.get(f"{BASE}/tasks/{task_id}/record").status_code == 403


def test_card_lists_two_security_requirements_and_oauth_urls(client, a2a_env, monkeypatch):
    meta = {
        "authorization_endpoint": "https://clerk.example/oauth/authorize?x",
        "token_endpoint": "https://clerk.example/t",
    }
    monkeypatch.setattr(a2a_auth, "_endpoints", {})
    monkeypatch.setattr(a2a_auth, "_clerk_metadata", lambda: _async(meta))
    card = client.get(f"{BASE}/agent-card.json").json()
    assert card["securityRequirements"] == [{"schemes": {"clerk": {}}}, {"schemes": {"oauth": {}}}]
    flow = card["securitySchemes"]["oauth"]["oauth2SecurityScheme"]["flows"]["authorizationCode"]
    assert flow["authorizationUrl"] == meta["authorization_endpoint"] and flow["tokenUrl"] == meta["token_endpoint"]
    assert set(flow["scopes"]) == {a2a_auth.SEND_SCOPE, a2a_auth.READ_SCOPE_A2A}


def test_card_falls_back_to_the_issuer_paths_when_clerk_metadata_fails(client, a2a_env, monkeypatch):
    async def down():
        raise httpx.ConnectError("down")

    monkeypatch.setattr(a2a_auth, "_endpoints", {})
    monkeypatch.setattr(a2a_auth, "_clerk_metadata", down)
    card = client.get(f"{BASE}/agent-card.json").json()
    flow = card["securitySchemes"]["oauth"]["oauth2SecurityScheme"]["flows"]["authorizationCode"]
    issuer = settings.CLERK_ISSUER.rstrip("/")
    assert (flow["authorizationUrl"], flow["tokenUrl"]) == (f"{issuer}/oauth/authorize", f"{issuer}/oauth/token")


def test_protected_resource_metadata_a2a(client):
    assert client.get("/.well-known/oauth-protected-resource/a2a").json() == {
        "resource": a2a_auth.RESOURCE_A2A,
        "authorization_servers": [settings.CLERK_ISSUER],
        "scopes_supported": ["user:org:read", a2a_auth.READ_SCOPE_A2A, a2a_auth.SEND_SCOPE],
        "bearer_methods_supported": ["header"],
    }


async def _async(value):
    return value
