"""Room-local resources: real DynamoDB conditions under Moto, no AWS or harness."""

import time
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from pydantic import ValidationError

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service
from core.services.messaging import ApiProblem

OWNER = "org_test"
ALICE = AuthContext(user_id="user_alice", org_id=OWNER)
BOB = AuthContext(user_id="user_bob", org_id=OWNER)
OUTSIDER = AuthContext(user_id="user_outsider", org_id=OWNER, org_role="org:admin")
ROOM = "11111111-1111-4111-8111-111111111111"
OTHER_ROOM = "22222222-2222-4222-8222-222222222222"
MAC = "a" * 64
CLI = "b" * 64


@pytest.fixture
def db():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-messaging",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("pk", "sk")],
            BillingMode="PAY_PER_REQUEST",
        )
        containers = resource.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        containers.put_item(
            Item={
                "owner_id": OWNER,
                "paired_devices": {
                    MAC: {"userId": ALICE.user_id, "clientId": "openclaw-macos", "publicKey": "private-to-server"},
                    CLI: {"userId": ALICE.user_id, "clientId": "node-host", "publicKey": "also-private"},
                    "c" * 64: {"userId": BOB.user_id, "clientId": "node-host", "publicKey": "bob-key"},
                },
            }
        )
        for room in (ROOM, OTHER_ROOM):
            table.put_item(Item={"pk": f"ROOM#{room}", "sk": "META", "ownerId": OWNER, "roomId": room})
            for user in (ALICE.user_id, BOB.user_id):
                table.put_item(
                    Item={
                        "pk": f"ROOM#{room}",
                        "sk": f"MEMBER#human#{user}",
                        "kind": "human",
                        "id": user,
                        "ownerId": OWNER,
                    }
                )
        users = resource.create_table(
            TableName="test-users",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with (
            patch("core.dynamodb._dynamodb_resource", resource),
            patch("core.repositories.container_repo._get_table", return_value=containers),
            patch("core.repositories.user_repo._get_table", return_value=users),
            patch.object(messaging_repo.settings, "MESSAGING_TABLE", table.name),
        ):
            yield table, containers


def command(service, **changes):
    body = dict(requestId="request-1", expectedGeneration="generation-1", kind="send", text="continue")
    body.update(changes)
    return service.LocalSessionCommand(**body)


def test_invocation_grant_rejects_wrong_purpose_types_scope_and_lifetime(monkeypatch):
    import jwt
    from core.services import local_session_grants as grants

    key = "hermetic-signing-key"
    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", key)
    token = grants.mint(OWNER, ROOM, "33333333-3333-4333-8333-333333333333", OTHER_ROOM)
    original = jwt.decode(token, options={"verify_signature": False})
    from core.services import service_token

    with pytest.raises(jwt.InvalidTokenError):
        service_token.verify(token)
    with pytest.raises(ApiProblem):
        grants.verify(service_token.mint(OWNER))
    for changes in (
        {"aud": "paperclip"},
        {"kind": "paperclip_service"},
        {"exp": int(time.time()) - 1},
        {"exp": original["iat"] + 1801},
        {"iat": True},
        {"ownerId": 2},
        {"roomId": OTHER_ROOM},
        {"sessionKey": "agent:other:room"},
    ):
        bad = jwt.encode({**original, **changes}, key, algorithm="HS256")
        with pytest.raises(ApiProblem) as rejected:
            grants.verify(bad)
        assert bad not in str(rejected.value)


@pytest.mark.asyncio
async def test_agent_router_derives_room_only_from_verified_bearer(db, monkeypatch):
    from core.services import local_session_grants as grants
    from routers import messaging as routes
    from starlette.requests import Request

    monkeypatch.setenv("PAPERCLIP_SERVICE_TOKEN_KEY", "hermetic-signing-key")
    agent = "33333333-3333-4333-8333-333333333333"
    db[0].put_item(Item={"pk": f"ROOM#{ROOM}", "sk": f"MEMBER#agent#{agent}", "kind": "agent", "id": agent})
    token = grants.mint(OWNER, ROOM, agent, OTHER_ROOM)
    request = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
    caller = await routes.local_session_invocation(request)
    assert caller.room_id == ROOM
    assert await routes.agent_local_session_resources(caller) == await service.list_resources(caller, ROOM)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer owner-hook-token")]})
    with pytest.raises(ApiProblem):
        await routes.local_session_invocation(request)


def test_workspace_public_summary_is_bounded_and_never_accepts_local_paths():
    summary = service.WorkspaceSummary(revision=2, coverage="bounded", changed=True, overlap=True)
    assert summary.model_dump(by_alias=True) == {"revision": 2, "coverage": "bounded", "changed": True, "overlap": True}
    with pytest.raises(ValidationError):
        service.WorkspaceSummary(revision=2, coverage="bounded", changed=True, overlap=True, root="/private/path")


def _cancelled(reason: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": reason}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"actorUserId": "user_alice"},
        {"deviceId": MAC},
        {"ownerId": OWNER},
        {"kind": "shell"},
        {"text": "😀" * 5000},
        {"requestId": "bad#id"},
        {"text": "  "},
    ],
)
def test_command_trust_boundary_rejects_forgery_and_unbounded_input(extra):
    with pytest.raises(ValidationError):
        command(service, **extra)


def test_command_reserves_space_for_server_framing_inside_wire_envelope():
    # Both requests fit 16 KiB but exceed the reserved 8 KiB command budget.
    for text in ("x" * 8100, "é" * 4100):
        with pytest.raises(ValidationError):
            command(service, text=text)


def test_normalized_event_envelope_is_utf8_bounded_and_forbids_raw_payloads():
    for extras in ({"text": "😀" * 5000}, {"raw": {"config": "secret"}}):
        with pytest.raises(ValidationError):
            service.LocalSessionEvent(**{"seq": 1, "kind": "text", "role": "assistant", "text": "hello", **extras})


async def test_live_sessions_for_room_does_not_truncate_a_long_human_user_id():
    # human_user_id carries no Field(max_length=...) anywhere in the LocalSession
    # chain, unlike title/cwd_name -- constructed directly rather than through the
    # moto fixture, which has no way to register a session under an over-long id.
    # The plugin (channel.ts) is the trust boundary and truncates on ingress;
    # the backend no longer duplicates that bound (truncating an id is wrong
    # anyway, and Clerk ids are ~32 chars).
    long_id = "x" * 200
    session = service.LocalSession(
        session_id="session-1",
        device_id=MAC,
        human_user_id=long_id,
        provider="codex",
        generation="generation-1",
        status="idle",
        provider_version="1.0",
        active_turn_id=None,
        capabilities=service.Capabilities(),
        capabilities_verified=True,
    )

    async def fake_live(_owner, _humans):
        return [session]

    members = [{"kind": "human", "id": long_id, "displayName": "Long"}]
    with patch.object(service, "_live_public_sessions", fake_live):
        rows = await service.live_sessions_for_room(OWNER, ROOM, members)
    assert rows[0]["humanUserId"] == long_id


async def test_live_sessions_for_room_blanks_for_a_foreign_member_without_querying(db):
    """The vendor shared channel (#2644): a foreign member's home differs from
    the room owner, so the whole summary is withheld -- and cheaply, before any
    session lookup runs (the guard now lives here, not in the caller)."""
    members = [
        {"kind": "human", "id": ALICE.user_id, "displayName": "Alice"},
        {"kind": "human", "id": "user_vendor", "displayName": "Vendor Rep", "homeOwnerId": "org_vendor"},
    ]
    with patch.object(
        repo, "list_user_sessions", side_effect=AssertionError("must not query when a member is foreign")
    ):
        assert await service.live_sessions_for_room(OWNER, ROOM, members) == []


def test_session_terminal_trusts_the_mac_and_falls_back_to_its_holder():
    assert service.session_terminal({"terminal": "attach"}) == "attach"
    assert service.session_terminal({"terminal": "held"}) == "held"
    assert service.session_terminal({"terminal": "none", "holderId": "ab12cd34"}) is None
    assert service.session_terminal({"holderId": "ab12cd34"}) == "held", "a desktop that predates `terminal`"
    assert service.session_terminal({}) is None


def test_an_ended_origin_is_listed_once_as_its_successor():
    def s(sid, status, forked=None, human="alice", seq=1):
        return service.LocalSession.model_construct(
            session_id=sid,
            human_user_id=human,
            provider="claude",
            status=status,
            forked_from=forked,
            latest_event_seq=seq,
        )

    found = [
        s("new", "busy", "old"),
        s("old", "ended"),
        s("copy", "idle", "running"),
        s("running", "idle"),
        s("bobs", "ended", human="bob"),
        s("not-bobs", "idle", "bobs", human="mallory"),
        # A Mac draining an ended origin re-registers it, then lets it lapse: offline, not a second row.
        s("lapsed", "offline"),
        s("lapsed-fork", "busy", "lapsed"),
    ]
    assert [x.session_id for x in service._without_moved(found)] == [
        "new",
        "copy",
        "running",
        "bobs",
        "not-bobs",
        "lapsed-fork",
    ]

    # A fork that ended before its first turn has no history and cannot be resumed: it is hidden,
    # and its origin (which holds the history) stays. A running empty fork stays (it has a terminal).
    found = [
        s("empty-fork", "ended", "origin", seq=0),
        s("origin", "ended"),
        s("live-empty", "busy", "origin2", seq=0),
        s("origin2", "ended"),
    ]
    assert [x.session_id for x in service._without_moved(found)] == ["origin", "live-empty"]


def test_a_session_that_ended_before_its_first_message_is_not_listed():
    def s(sid, status, seq, reason=None, provider="claude", held=None, on_purpose=None, forked_from=None):
        return service.LocalSession.model_construct(
            session_id=sid,
            human_user_id="alice",
            provider=provider,
            status=status,
            latest_event_seq=seq,
            control_unavailable_reason=reason,
            was_held=held,
            ended_on_purpose=on_purpose,
            forked_from=forked_from,
        )

    found = [
        s("empty", "ended", 0),
        s("starting", "idle", 0),
        s("used", "ended", 3),
        # The Mac could not read its history: it may hold a real conversation.
        s("unread", "ended", 0, "history-gap"),
        # Codex keeps the thread in its rollout before any event is public: `codex resume` finds it.
        s("codex", "ended", 0, provider="codex"),
        # It had a live terminal: its first events take up to a minute to arrive, so zero events
        # does not mean nothing happened in it.
        s("held", "ended", 0, held=True),
        # Ended on purpose: listed until removed (owner ruling 2026-10-27).
        s("left", "ended", 3, on_purpose=True),
        s("left-held", "ended", 0, held=True, on_purpose=True),
        s("left-codex", "ended", 0, provider="codex", on_purpose=True),
        # A fork ended on purpose stands in for its origin: the origin does not come back.
        s("origin", "ended", 5),
        s("fork", "ended", 2, on_purpose=True, forked_from="origin"),
    ]
    assert [x.session_id for x in service._without_moved(found)] == [
        "starting",
        "used",
        "unread",
        "codex",
        "held",
        "left",
        "left-held",
        "left-codex",
        "fork",
    ]
