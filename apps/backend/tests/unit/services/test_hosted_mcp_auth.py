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
    post = AsyncMock(return_value=SimpleNamespace(room_id=uuid.UUID(room_id), message_id=uuid.uuid4(), message_seq=7))
    monkeypatch.setattr(hosted_mcp.messaging, "create_human_message", post)
    monkeypatch.setattr(hosted_mcp, "get_access_token", lambda: SimpleNamespace(client_id="https://x.example/c"))
    monkeypatch.setattr(hosted_mcp.messaging, "human_display_name", AsyncMock(return_value="Ann"))

    monkeypatch.setattr(hosted_mcp, "_pin_destination", AsyncMock())

    await hosted_mcp.send_message(
        "Please coordinate",
        uuid.uuid4(),
        room_id=room_id,
        reply_to_message_id=reply_id,
        mention_ids=[agent_id, human_id],
    )
    body = post.await_args.args[2]
    assert body.reply_to_message_id == uuid.UUID(reply_id)
    assert body.thread_root_message_id == uuid.UUID(root_id)
    assert [(member.kind, member.id) for member in body.mentions] == [("agent", agent_id), ("human", human_id)]
    assert body.session_mentions == []
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
    post = AsyncMock(return_value=SimpleNamespace(room_id=uuid.UUID(room_id), message_id=uuid.uuid4(), message_seq=1))
    monkeypatch.setattr(hosted_mcp.messaging, "create_human_message", post)
    monkeypatch.setattr(hosted_mcp, "_pin_destination", AsyncMock())
    await hosted_mcp.send_message("hi", uuid.uuid4(), room_id=room_id)
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


def _hosted_sender(monkeypatch):
    auth = AuthContext(user_id="user_sender", org_id="org_test")
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_scope: auth)
    monkeypatch.setattr(
        hosted_mcp,
        "get_access_token",
        lambda: SimpleNamespace(client_id="https://claude.ai/oauth/claude-code-client-metadata"),
    )
    pin = AsyncMock()
    monkeypatch.setattr(hosted_mcp, "_pin_destination", pin)
    return auth, pin


@pytest.mark.asyncio
@pytest.mark.parametrize("targets", [{}, {"room_id": "r", "person_id": "p"}, {"room_id": "r", "session_id": "s"}])
async def test_send_needs_exactly_one_destination(monkeypatch, targets):
    _hosted_sender(monkeypatch)
    with pytest.raises(hosted_mcp.messaging.ApiProblem) as error:
        await hosted_mcp.send_message("hi", uuid.uuid4(), **targets)
    assert error.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_person_send_posts_into_the_dm_it_finds_or_opens(monkeypatch):
    auth, pin = _hosted_sender(monkeypatch)
    room_id, request_id = str(uuid.uuid4()), uuid.uuid4()
    pin.return_value = {"destination": "person:user_b"}
    person_dm = AsyncMock(return_value=room_id)
    monkeypatch.setattr(hosted_mcp.native_session_tools, "person_dm", person_dm)
    update = AsyncMock(return_value={"Attributes": {"roomId": room_id}})
    monkeypatch.setattr(hosted_mcp, "run_in_thread", update)
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_table", lambda: SimpleNamespace(update_item=None))
    monkeypatch.setattr(
        hosted_mcp.messaging,
        "get_room",
        AsyncMock(return_value=SimpleNamespace(members=[], kind="dm", room_revision=3)),
    )
    monkeypatch.setattr(hosted_mcp.messaging, "human_display_name", AsyncMock(return_value="Ann"))
    post = AsyncMock(return_value=SimpleNamespace(room_id=uuid.UUID(room_id), message_id=uuid.uuid4(), message_seq=1))
    monkeypatch.setattr(hosted_mcp.messaging, "create_human_message", post)
    result = await hosted_mcp.send_message("hi", request_id, person_id="user_b")
    person_dm.assert_awaited_once_with(auth, "user_b", create_key=str(request_id))
    assert post.await_args.kwargs["expected_room_revision"] == 3  # fenced against a DM->group change
    pin.assert_awaited_once_with(auth, request_id, "person:user_b")  # reserved before the DM opens
    assert update.await_args.kwargs["ExpressionAttributeValues"] == {":room": room_id}  # the DM is pinned
    assert post.await_args.args[1] == room_id and result["roomId"] == room_id

    # A retry keeps the pinned room even after that DM became a group DM: no re-resolution.
    person_dm.reset_mock()
    pin.return_value = {"destination": "person:user_b", "roomId": room_id}
    await hosted_mcp.send_message("hi", request_id, person_id="user_b")
    person_dm.assert_not_awaited()
    assert post.await_args.args[1] == room_id


@pytest.mark.asyncio
async def test_session_send_is_a_follow_up_under_the_persons_control_authority(monkeypatch):
    auth, pin = _hosted_sender(monkeypatch)
    sid, request_id = str(uuid.uuid4()), uuid.uuid4()
    target = {"sessionId": sid, "ownerId": "org_test", "generation": "g1"}
    authorize = AsyncMock(return_value=(target, {}))
    monkeypatch.setattr(hosted_mcp.local_sessions, "authorize_session", authorize)
    send = AsyncMock(return_value={"state": "pending"})
    monkeypatch.setattr(hosted_mcp.native_session_tools, "session_send", send)
    post = AsyncMock()
    monkeypatch.setattr(hosted_mcp.messaging, "create_human_message", post)

    result = await hosted_mcp.send_message("run the tests", request_id, session_id=sid)

    assert result == {"sessionId": sid, "requestId": str(request_id), "state": "pending"}
    authorize.assert_awaited_once_with(auth, None, sid, owner_only=True)
    pin.assert_awaited_once_with(auth, request_id, f"session:{sid}")
    send.assert_awaited_once_with(auth, target, "run the tests", str(request_id), via_client="Claude Code")
    post.assert_not_awaited()  # nothing is posted to a room
    with pytest.raises(hosted_mcp.messaging.ApiProblem):
        await hosted_mcp.send_message("x", uuid.uuid4(), session_id=sid, mention_ids=["a"])


@pytest.mark.asyncio
async def test_receipt_reads_the_pinned_destination(monkeypatch):
    auth = AuthContext(user_id="user_sender", org_id="org_test")
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_scope: auth)
    sid, room_id, request_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
    pins = {}
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_get", AsyncMock(side_effect=lambda pk, sk: pins.get((pk, sk))))
    key = hosted_mcp._sent_key(auth, request_id)
    assert (await hosted_mcp.message_receipt(request_id)) == {"state": "unknown"}

    pins[key] = {"destination": f"session:{sid}"}
    get_command = AsyncMock(return_value=SimpleNamespace(state="applied"))
    monkeypatch.setattr(hosted_mcp.local_sessions, "get_command", get_command)
    assert (await hosted_mcp.message_receipt(request_id))["state"] == "applied"
    get_command.assert_awaited_once_with(auth, None, sid, str(request_id))
    get_command.side_effect = hosted_mcp.messaging.ApiProblem("LOCAL_SESSION_NOT_FOUND", "gone")
    assert (await hosted_mcp.message_receipt(request_id))["state"] == "unknown"

    pins[key] = {"destination": f"room:{room_id}"}
    room_receipt = AsyncMock(return_value={"state": "posted"})
    monkeypatch.setattr(hosted_mcp, "receipt_for", room_receipt)
    assert (await hosted_mcp.message_receipt(request_id)) == {"state": "posted"}
    room_receipt.assert_awaited_once_with(auth, room_id, request_id)

    # A person send's receipt reads its pinned room, so a DM that became a group DM still resolves.
    person_dm = AsyncMock(side_effect=AssertionError("a receipt never re-resolves the DM"))
    monkeypatch.setattr(hosted_mcp.native_session_tools, "person_dm", person_dm)
    pins[key] = {"destination": "person:user_b", "roomId": room_id}
    assert (await hosted_mcp.message_receipt(request_id)) == {"state": "posted"}
    assert room_receipt.await_args.args == (auth, room_id, request_id)
    pins[key] = {"destination": "person:user_b"}  # stopped before its DM was known
    assert (await hosted_mcp.message_receipt(request_id)) == {"state": "not_found"}


@pytest.mark.asyncio
async def test_a_request_id_cannot_be_reused_for_another_destination(monkeypatch):
    from botocore.exceptions import ClientError

    auth, request_id = AuthContext(user_id="user_sender", org_id="org_test"), uuid.uuid4()
    taken = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    monkeypatch.setattr(hosted_mcp, "run_in_thread", AsyncMock(side_effect=taken))
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_table", lambda: SimpleNamespace(put_item=None))
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_get", AsyncMock(return_value={"destination": "room:a"}))
    await hosted_mcp._pin_destination(auth, request_id, "room:a")  # a retry to the same place is fine
    with pytest.raises(hosted_mcp.messaging.ApiProblem) as error:
        await hosted_mcp._pin_destination(auth, request_id, "session:b")
    assert error.value.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_a_refused_person_send_opens_no_dm(monkeypatch):
    _auth, pin = _hosted_sender(monkeypatch)
    pin.return_value = {"destination": "person:user_b"}
    person_dm = AsyncMock(side_effect=hosted_mcp.messaging.ApiProblem("ROOM_NOT_FOUND", "none"))
    monkeypatch.setattr(hosted_mcp.native_session_tools, "person_dm", person_dm)
    with pytest.raises(hosted_mcp.messaging.ApiProblem) as error:
        await hosted_mcp.send_message("   ", uuid.uuid4(), person_id="user_b")
    assert error.value.error.code == "INVALID_REQUEST"
    person_dm.assert_not_awaited()
    pin.assert_not_awaited()
    # A mention (or reply) cannot be valid in a room opened just now: it never opens one.
    with pytest.raises(hosted_mcp.messaging.ApiProblem):
        await hosted_mcp.send_message("hi", uuid.uuid4(), person_id="user_b", mention_ids=["user_b"])
    assert person_dm.await_args.kwargs == {"create_key": None}


@pytest.mark.asyncio
async def test_a_person_send_refuses_a_dm_that_became_a_group(monkeypatch):
    auth, pin = _hosted_sender(monkeypatch)
    room_id, request_id = str(uuid.uuid4()), uuid.uuid4()
    pin.return_value = {"destination": "person:user_b", "roomId": room_id}
    # The DM was converted between the lookup and the write.
    monkeypatch.setattr(
        hosted_mcp.messaging,
        "get_room",
        AsyncMock(return_value=SimpleNamespace(members=[], kind="group-dm", room_revision=4)),
    )
    receipts = {}
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_get", AsyncMock(side_effect=lambda pk, sk: receipts.get(sk)))
    post = AsyncMock()
    monkeypatch.setattr(hosted_mcp.messaging, "create_human_message", post)
    with pytest.raises(hosted_mcp.messaging.ApiProblem) as error:
        await hosted_mcp.send_message("hi", request_id, person_id="user_b")
    assert error.value.error.code == "MESSAGE_CONFLICT"
    post.assert_not_awaited()
    # A retry of a send that already committed before the conversion still replays.
    receipts[hosted_mcp.messaging_repo.human_idemp_sk(auth.user_id, str(request_id))] = {"messageSeq": 1}
    monkeypatch.setattr(hosted_mcp.messaging, "human_display_name", AsyncMock(return_value="Ann"))
    post.return_value = SimpleNamespace(room_id=uuid.UUID(room_id), message_id=uuid.uuid4(), message_seq=1)
    await hosted_mcp.send_message("hi", request_id, person_id="user_b")
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_refused_session_send_says_why_and_its_receipt_reads_rejected(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    auth, _ = _hosted_sender(monkeypatch)
    sid, request_id = str(uuid.uuid4()), uuid.uuid4()
    refusal = hosted_mcp.messaging.ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")
    monkeypatch.setattr(hosted_mcp.local_sessions, "authorize_session", AsyncMock(side_effect=refusal))
    record = AsyncMock()
    monkeypatch.setattr(hosted_mcp, "_record_refusal", record)
    with pytest.raises(ToolError, match="LOCAL_SESSION_FORBIDDEN: Only the session's owner"):
        await hosted_mcp.send_message("hi", request_id, session_id=sid)
    record.assert_awaited_once_with(auth, request_id, "LOCAL_SESSION_FORBIDDEN")

    key = hosted_mcp._sent_key(auth, request_id)
    pins = {key: {"destination": f"session:{sid}", "refusal": "LOCAL_SESSION_FORBIDDEN"}}
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_get", AsyncMock(side_effect=lambda pk, sk: pins.get((pk, sk))))
    gone = hosted_mcp.messaging.ApiProblem("LOCAL_SESSION_NOT_FOUND", "gone")
    monkeypatch.setattr(hosted_mcp.local_sessions, "get_command", AsyncMock(side_effect=gone))
    assert await hosted_mcp.message_receipt(request_id) == {
        "sessionId": sid,
        "requestId": str(request_id),
        "state": "rejected",
        "reason": "LOCAL_SESSION_FORBIDDEN",
    }


@pytest.mark.asyncio
async def test_pin_conflict_on_a_session_send_is_a_readable_tool_error(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    _, pin = _hosted_sender(monkeypatch)
    pin.side_effect = hosted_mcp.messaging.ApiProblem(
        "IDEMPOTENCY_CONFLICT", "request_id was used for another destination."
    )
    with pytest.raises(ToolError, match="IDEMPOTENCY_CONFLICT"):
        await hosted_mcp.send_message("hi", uuid.uuid4(), session_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_receipt_reads_rejected_even_when_get_command_refuses_again(monkeypatch):
    auth = AuthContext(user_id="user_sender", org_id="org_test")
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_scope: auth)
    sid, request_id = str(uuid.uuid4()), uuid.uuid4()
    pin = {"destination": f"session:{sid}", "refusal": "LOCAL_SESSION_FORBIDDEN"}
    monkeypatch.setattr(hosted_mcp.messaging_repo, "_get", AsyncMock(return_value=pin))
    off = hosted_mcp.messaging.ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")
    monkeypatch.setattr(hosted_mcp.local_sessions, "get_command", AsyncMock(side_effect=off))
    result = await hosted_mcp.message_receipt(request_id)
    assert result["state"] == "rejected" and result["reason"] == "LOCAL_SESSION_FORBIDDEN"
