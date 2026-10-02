"""Hosted MCP accepts only current, resource-bound Clerk opaque grants."""

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from core.auth import AuthContext
from core.services import hosted_mcp


@pytest.mark.asyncio
async def test_oauth_verifier_binds_audience_org_and_revocation(monkeypatch):
    now = int(time.time())
    grant = {
        "subject": "user_test",
        "client_id": "client_test",
        "scopes": ["user:org:read", hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE],
        "expiration": now + 300,
        "revoked": False,
        "expired": False,
        "aud": [hosted_mcp.RESOURCE],
    }
    identity = {"sub": "user_test", "org_id": "org_allowed"}
    monkeypatch.setattr(
        hosted_mcp,
        "settings",
        SimpleNamespace(CLERK_SECRET_KEY="sk_test", CLERK_ISSUER="https://clerk.example.com"),
    )

    async def memberships(*_, **__):
        return [{"id": "org_allowed"}]

    class ClerkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _, **kwargs):
            return httpx.Response(200, json=grant)

        async def get(self, _, **kwargs):
            return httpx.Response(200, json=identity)

    monkeypatch.setattr(hosted_mcp.clerk_admin, "list_user_organizations", memberships)
    monkeypatch.setattr(hosted_mcp.httpx, "AsyncClient", lambda **_: ClerkClient())
    verifier = hosted_mcp.ClerkOAuthVerifier()
    assert (await verifier.verify_token("opaque-test")).subject == "user_test"
    grant["scopes"].remove(hosted_mcp.WRITE_SCOPE)
    assert (await verifier.verify_token("opaque-test")).subject == "user_test"
    grant["scopes"].append(hosted_mcp.WRITE_SCOPE)
    grant["aud"] = ["https://other.example.com/mcp"]
    assert await verifier.verify_token("opaque-test") is None
    grant["aud"] = [hosted_mcp.RESOURCE]
    identity["org_id"] = "org_other"
    assert await verifier.verify_token("opaque-test") is None
    identity["org_id"] = "org_allowed"
    grant["revoked"] = True
    assert await verifier.verify_token("opaque-test") is None
    assert await verifier.verify_token("jwt.part.signature") is None


@pytest.mark.asyncio
async def test_send_resolves_thread_root_and_member_mentions(monkeypatch):
    room_id, reply_id, root_id = (str(uuid.uuid4()) for _ in range(3))
    agent_id, human_id = str(uuid.uuid4()), "user_teammate"
    monkeypatch.setattr(hosted_mcp, "_caller", lambda _scope: AuthContext(user_id="user_sender", org_id="org_test"))
    monkeypatch.setattr(
        hosted_mcp.messaging,
        "get_room",
        AsyncMock(
            return_value=SimpleNamespace(
                members=[SimpleNamespace(id=agent_id, kind="agent"), SimpleNamespace(id=human_id, kind="human")]
            )
        ),
    )
    monkeypatch.setattr(
        hosted_mcp.messaging_repo,
        "get_message_by_id",
        AsyncMock(return_value={"messageId": reply_id, "threadRootMessageId": root_id}),
    )
    post = AsyncMock(
        return_value=SimpleNamespace(
            room_id=uuid.UUID(room_id), message_id=uuid.uuid4(), message_seq=7, session_sends=[]
        )
    )
    monkeypatch.setattr(hosted_mcp.messaging, "create_human_message", post)
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: SimpleNamespace(client_id="https://x.example/c"))
    monkeypatch.setattr(hosted_mcp.messaging, "human_display_name", AsyncMock(return_value="Ann"))

    await hosted_mcp.send_message(
        room_id,
        "Please coordinate",
        str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        reply_to_message_id=reply_id,
        mention_ids=[agent_id, human_id],
    )
    body = post.await_args.args[2]
    assert body.reply_to_message_id == uuid.UUID(reply_id)
    assert body.thread_root_message_id == uuid.UUID(root_id)
    assert [(member.kind, member.id) for member in body.mentions] == [("agent", agent_id), ("human", human_id)]
    assert len(body.session_mentions) == 1
    assert post.await_args.kwargs["via"] == "lightbulb_mcp"


@pytest.mark.asyncio
async def test_room_target_search_does_not_offer_offline_queue(monkeypatch):
    session = SimpleNamespace(
        session_id=str(uuid.uuid4()),
        human_user_id="user_sender",
        provider="claude",
        display_title="Offline",
        title=None,
        cwd_name="lightbulb",
        status="offline",
        capabilities=SimpleNamespace(send_mode="queue"),
        capabilities_verified=True,
        control_unavailable_reason=None,
        team_control=False,
        allowed_senders=[],
    )
    monkeypatch.setattr(hosted_mcp, "_caller", lambda: AuthContext(user_id="user_sender", org_id="org_test"))
    monkeypatch.setattr(
        hosted_mcp.local_sessions,
        "list_workspace_sessions",
        AsyncMock(return_value=SimpleNamespace(sessions=[session])),
    )
    monkeypatch.setattr(hosted_mcp.messaging, "human_display_name", AsyncMock(return_value="Tester"))
    result = await hosted_mcp.find_sessions()
    assert result["sessions"][0]["canSend"] is False


def test_client_identity_names_known_clients_and_falls_back_to_host():
    assert hosted_mcp.client_identity("https://claude.ai/oauth/claude-code-client-metadata") == (
        "claude-code",
        "Claude Code",
    )
    assert hosted_mcp.client_identity("https://chatgpt.com/oauth/codex/client.json") == ("codex", "Codex")
    assert hosted_mcp.client_identity("https://cursor.example.com/meta.json") == (
        "cursor.example.com",
        "cursor.example.com",
    )


@pytest.mark.asyncio
async def test_send_authors_as_the_client_on_the_humans_behalf(monkeypatch):
    room_id = str(uuid.uuid4())
    monkeypatch.setattr(hosted_mcp, "_caller", lambda _scope: AuthContext(user_id="user_sender", org_id="org_test"))
    monkeypatch.setattr(
        hosted_mcp,
        "get_access_token",
        lambda: SimpleNamespace(client_id="https://claude.ai/oauth/claude-code-client-metadata"),
    )
    monkeypatch.setattr(hosted_mcp.messaging, "get_room", AsyncMock(return_value=SimpleNamespace(members=[])))
    monkeypatch.setattr(hosted_mcp.messaging, "human_display_name", AsyncMock(return_value="Ann"))
    post = AsyncMock(
        return_value=SimpleNamespace(
            room_id=uuid.UUID(room_id), message_id=uuid.uuid4(), message_seq=1, session_sends=[]
        )
    )
    monkeypatch.setattr(hosted_mcp.messaging, "create_human_message", post)
    await hosted_mcp.send_message(room_id, "hi", str(uuid.uuid4()))
    assert post.await_args.args[0].user_id == "user_sender"
    assert post.await_args.kwargs["author_override"] == {
        "kind": "system",
        "id": "mcp:user_sender:claude-code",
        "displayName": "Claude Code via Ann",
    }


@pytest.mark.asyncio
async def test_receipt_finds_the_client_authored_row_by_its_accountable_human(monkeypatch):
    room_id, request_id = str(uuid.uuid4()), uuid.uuid4()
    auth = AuthContext(user_id="user_sender", org_id="org_test")
    row = {"via": "lightbulb_mcp", "actorUserId": "user_sender", "author": {"kind": "system", "id": "mcp:x"}}
    monkeypatch.setattr(hosted_mcp.messaging, "require_room_member", AsyncMock())
    get = AsyncMock(return_value={"messageSeq": 4})
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_get", get)
    monkeypatch.setattr(hosted_mcp.messaging_repo, "get_message", AsyncMock(return_value=row))
    monkeypatch.setattr(
        hosted_mcp.messaging,
        "canonical_message",
        lambda _m: SimpleNamespace(model_dump=lambda **_: {"messageSeq": 4}),
    )
    assert (await hosted_mcp.receipt_for(auth, room_id, request_id))["state"] == "posted"
    assert get.await_args.args[1] == hosted_mcp.messaging_repo.human_idemp_sk("user_sender", str(request_id))
    row["actorUserId"] = "user_other"
    assert (await hosted_mcp.receipt_for(auth, room_id, request_id)) == {"state": "not_found"}
