"""Shared (vendor) rooms: foreign members are authorised and notified by their HOME workspace."""

import uuid
from unittest.mock import AsyncMock, call

import pytest
from botocore.exceptions import ClientError
from core.auth import AuthContext
from core.services import messaging
from core.services.messaging import ApiProblem

ROOM = str(uuid.uuid4())
CUSTOMER, VENDOR = "org_customer", "org_vendor"
META = {
    "roomId": ROOM,
    "ownerId": CUSTOMER,
    "kind": "channel",
    "revision": 2,
    "lastSeq": 7,
    "deliveryKey": str(uuid.uuid4()),
    "workspaceId": str(uuid.uuid4()),
    "updatedAt": "2026-09-14T00:00:00Z",
    "lastMessageAt": None,
    "createdAt": "2026-09-14T00:00:00Z",
}


def _human(user_id: str, home: str, role: str = "member") -> dict:
    return {
        "kind": "human",
        "id": user_id,
        "ownerId": CUSTOMER,
        "homeOwnerId": home,
        "role": role,
        "joinedSeq": 0,
        "displayName": user_id,
        "mentionHandle": None,
        "listenMode": None,
        "joinedAt": "2026-09-14T00:00:00Z",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member, owner_id, expected",
    [
        (_human("user_v", VENDOR), VENDOR, None),
        (_human("user_v", VENDOR), CUSTOMER, "ROOM_NOT_FOUND"),
        (None, CUSTOMER, "ROOM_FORBIDDEN"),
        (None, VENDOR, "ROOM_NOT_FOUND"),
    ],
)
async def test_require_room_member_follows_the_members_home(monkeypatch, member, owner_id, expected):
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=member))
    if expected is None:
        assert await messaging.require_room_member(owner_id, "user_v", ROOM) == (META, member)
        return
    with pytest.raises(ApiProblem) as exc:
        await messaging.require_room_member(owner_id, "user_v", ROOM)
    assert exc.value.error.code == expected
    assert (exc.value.error.reconcile_from_seq == 7) is (expected == "ROOM_FORBIDDEN")


@pytest.mark.asyncio
async def test_a_member_row_left_behind_by_a_meta_only_migration_still_opens_its_room(monkeypatch):
    # The #2627 personal->org migration rewrote META.ownerId but never the MEMBER# rows.
    legacy = {
        "kind": "human",
        "id": "user_x",
        "ownerId": "user_x",
        "memberKey": "OWNER#user_x#human#user_x",
        "role": "owner",
        "joinedSeq": 0,
    }
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=legacy))
    assert await messaging.require_room_member(CUSTOMER, "user_x", ROOM) == (META, legacy)


@pytest.mark.asyncio
async def test_removal_notices_go_to_each_humans_home_stream(monkeypatch):
    members = [_human("user_c", CUSTOMER, "owner"), _human("user_v", VENDOR)]
    streams = AsyncMock(side_effect=lambda owner, user: f"key-{owner}-{user}")
    remove = AsyncMock(return_value={**META, "revision": 3})
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", streams)
    monkeypatch.setattr(messaging.messaging_repo, "remove_member", remove)

    await messaging._remove_member_with_notices(META, "human", "user_v")

    assert streams.await_args_list == [call(CUSTOMER, "user_c"), call(VENDOR, "user_v")]
    notices = remove.await_args.kwargs["live_events"]
    assert [(n["userId"], n["ownerId"], n["channelKey"]) for n in notices] == [
        ("user_c", CUSTOMER, f"key-{CUSTOMER}-user_c"),
        ("user_v", VENDOR, f"key-{VENDOR}-user_v"),
    ]
    assert [n["event"]["type"] for n in notices] == ["room.subscription.rotated", "room.access.removed"]


@pytest.mark.asyncio
async def test_revoking_an_org_membership_only_touches_rooms_where_that_org_is_home(monkeypatch):
    foreign = _human("user_v", VENDOR)
    discovered = [{"meta": META, "member": foreign}]
    # Each revoke lists twice: before and after the stream goes off (the removal empties the second).
    monkeypatch.setattr(
        messaging.messaging_repo, "list_rooms_for_member", AsyncMock(side_effect=[discovered, [], discovered, []])
    )
    # Seated in META's room only: revoke also sweeps #general and the feed by id (no seat there).
    monkeypatch.setattr(
        messaging.messaging_repo,
        "get_member",
        AsyncMock(side_effect=lambda room_id, *_: foreign if room_id == META["roomId"] else None),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "deactivate_user_stream", AsyncMock())
    monkeypatch.setattr(messaging.messaging_repo, "release_autojoin", AsyncMock())
    removed = AsyncMock(return_value=META)
    monkeypatch.setattr(messaging, "_remove_member_with_notices", removed)

    await messaging.revoke_human_membership(VENDOR, "user_v")
    removed.assert_awaited_once_with(META, "human", "user_v")

    removed.reset_mock()
    await messaging.revoke_human_membership(CUSTOMER, "user_v")
    removed.assert_not_awaited()


@pytest.fixture
def vendor(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", VENDOR)
    return VENDOR


def test_vendor_room_is_recognised_by_its_deterministic_id(vendor):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    assert messaging.is_vendor_room({"roomId": room_id, "ownerId": CUSTOMER})
    assert not messaging.is_vendor_room({"roomId": ROOM, "ownerId": CUSTOMER})
    fields = messaging._room_summary_fields({**META, "roomId": room_id}, _human("user_c", CUSTOMER), 0)
    assert fields["shared"] is True and fields["workspace_id"] == META["workspaceId"]
    assert messaging._room_summary_fields(META, _human("user_c", CUSTOMER), 0)["shared"] is False


def test_feature_off_means_no_room_is_ever_shared(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "")
    assert not messaging.is_vendor_room(
        {"roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER), "ownerId": CUSTOMER}
    )


@pytest.mark.asyncio
async def test_vendor_room_membership_and_name_are_platform_managed(vendor, monkeypatch):
    meta = {**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)}
    auth = AuthContext(user_id="user_c", org_id=CUSTOMER)
    monkeypatch.setattr(
        messaging, "require_room_admin", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER, "owner")))
    )
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER))))
    attempts = [
        messaging.upsert_member(
            auth,
            meta["roomId"],
            messaging.UpsertRoomMemberRequest(kind="human", id="user_x", role="member", listen_mode=None),
        ),
        messaging.remove_member(auth, meta["roomId"], "human", "user_v"),
        messaging.leave_room(auth, meta["roomId"]),
        messaging.patch_room(auth, meta["roomId"], messaging.PatchRoomRequest(name="renamed")),
    ]
    for attempt in attempts:
        with pytest.raises(ApiProblem) as exc:
            await attempt
        assert exc.value.error.code == "ROOM_FORBIDDEN"


@pytest.mark.asyncio
async def test_vendor_room_still_lets_admins_change_the_agents_listen_mode(vendor, monkeypatch):
    meta = {**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)}
    auth = AuthContext(user_id="user_c", org_id=CUSTOMER)
    agent = {
        "kind": "agent",
        "id": str(uuid.uuid4()),
        "role": "member",
        "listenMode": "mentions",
        "displayName": "CEO",
        "mentionHandle": "ceo",
    }
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=(meta, _human("user_c", CUSTOMER, "owner")))
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=agent))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": agent["id"], "name": "CEO"}]))
    monkeypatch.setattr(messaging, "_resolved_member", AsyncMock(return_value={**agent, "listenMode": "active"}))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[agent]))
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))
    result = await messaging.upsert_member(
        auth,
        meta["roomId"],
        messaging.UpsertRoomMemberRequest(kind="agent", id=agent["id"], role="member", listen_mode="active"),
    )
    assert result == "detail"


@pytest.mark.asyncio
async def test_attachment_download_reads_the_rooms_owner_workspace_not_the_callers(monkeypatch):
    from types import SimpleNamespace

    from routers import messaging as router

    seen: list[str] = []
    monkeypatch.setattr(router.service, "require_room_member", AsyncMock(return_value=(META, _human("user_v", VENDOR))))
    monkeypatch.setattr(
        router.service,
        "load_attachment",
        lambda owner, room, attachment_id: (
            seen.append(owner),
            (SimpleNamespace(content_type="image/png", file_name="a.png"), b"x"),
        )[1],
    )
    response = await router.get_room_attachment(ROOM, str(uuid.uuid4()), AuthContext(user_id="user_v", org_id=VENDOR))
    assert response.status_code == 200 and seen == [CUSTOMER]


@pytest.mark.asyncio
async def test_a_foreign_members_message_is_written_under_the_rooms_owner(monkeypatch):
    sender = _human("user_v", VENDOR)
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, sender)))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[sender]))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    stored = {
        **META,
        "messageId": str(uuid.uuid4()),
        "messageSeq": 8,
        "roomRevision": 2,
        "author": {"kind": "human", "id": "user_v", "displayName": "Vee"},
        "text": "hi",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": "2026-09-14T00:00:00Z",
        "runId": None,
        "dispatchTargets": [],
    }
    create = AsyncMock(return_value=(stored, False))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    touched, captured = [], []
    monkeypatch.setattr(messaging, "_touch_last_interactive_quietly", lambda owner: touched.append(owner) or _noop())
    monkeypatch.setattr(
        "core.services.onboarding_brief.capture_first_workspace_message",
        lambda owner, item: captured.append(owner) or _noop(),
    )
    await messaging.create_human_message(
        AuthContext(user_id="user_v", org_id=VENDOR),
        ROOM,
        messaging.CreateMessageRequest(
            client_message_id=uuid.uuid4(),
            text="hi",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=None,
        ),
    )
    assert create.await_args.kwargs["owner_id"] == CUSTOMER
    assert touched == [CUSTOMER] and captured == [CUSTOMER]


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_a_foreign_admin_can_only_add_the_rooms_own_org_members(monkeypatch):
    admin = _human("user_v", VENDOR, "admin")
    agent_id = str(uuid.uuid4())
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, admin)))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    load_agent_roster = AsyncMock(return_value=[{"id": agent_id, "name": "CEO"}])
    monkeypatch.setattr(messaging, "load_agent_roster", load_agent_roster)
    monkeypatch.setattr(messaging, "_resolved_member", AsyncMock(return_value={"kind": "agent", "id": agent_id}))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[]))
    upsert_member = AsyncMock(return_value=META)
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert_member)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(return_value="detail"))

    await messaging.upsert_member(
        AuthContext(user_id="user_v", org_id=VENDOR),
        ROOM,
        messaging.UpsertRoomMemberRequest(kind="agent", id=agent_id, role="member", listen_mode="mentions"),
    )
    assert load_agent_roster.await_args.args == (CUSTOMER,)
    assert upsert_member.await_args.kwargs["owner_id"] == CUSTOMER


def _membership(user_id: str, role: str = "org:member", first: str = "", email: str = "") -> dict:
    return {
        "user_id": user_id,
        "first_name": first,
        "last_name": "",
        "email": email or f"{user_id}@example.com",
        "image_url": "",
        "role": role,
    }


CEO = str(uuid.uuid4())


def _seed_env(
    monkeypatch, *, company_status="active", starter=None, room_exists=False, customer=None, vendor_humans=None
):
    from types import SimpleNamespace

    company = SimpleNamespace(status=company_status, starter_agent_ids={"ceo": CEO} if starter is None else starter)
    repo_cls = lambda *_a, **_k: SimpleNamespace(resolve_owner_row=AsyncMock(return_value=company))  # noqa: E731
    monkeypatch.setattr("core.repositories.workspace_repo.WorkspaceRepo", repo_cls)
    monkeypatch.setattr(
        messaging.messaging_repo, "get_room", AsyncMock(return_value={"roomId": "x"} if room_exists else None)
    )
    get_org = AsyncMock(return_value={"id": CUSTOMER, "slug": "acme-1789347059550170402", "created_by": "user_c"})
    monkeypatch.setattr("core.repositories.company_slug_repo.get_by_owner", AsyncMock(return_value={"slug": "acme"}))
    list_members = AsyncMock(
        side_effect=lambda org_id: (
            (customer if customer is not None else [_membership("user_c", "org:admin", "Cal"), _membership("user_d")])
            if org_id == CUSTOMER
            else (vendor_humans if vendor_humans is not None else [_membership("user_v", first="Vee")])
        )
    )
    monkeypatch.setattr("core.services.clerk_admin.get_organization", get_org)
    monkeypatch.setattr("core.services.clerk_admin.list_organization_members", list_members)
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": CEO, "name": "CEO"}]))
    streams = AsyncMock(return_value="key")
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", streams)
    create = AsyncMock(return_value={**META, "roomId": messaging.messaging_repo.vendor_room_id(CUSTOMER)})
    monkeypatch.setattr(messaging.messaging_repo, "create_room", create)
    monkeypatch.setattr(messaging, "_emit_room_created", lambda *a, **k: None)
    return SimpleNamespace(get_org=get_org, list_members=list_members, streams=streams, create=create)


@pytest.mark.asyncio
async def test_ensure_vendor_room_seeds_both_orgs_and_the_ceo(vendor, monkeypatch):
    env = _seed_env(monkeypatch)
    assert await messaging.ensure_vendor_room(CUSTOMER) == messaging.messaging_repo.vendor_room_id(CUSTOMER)
    kwargs = env.create.await_args.kwargs
    assert kwargs["owner_id"] == CUSTOMER and kwargs["creator_user_id"] == "user_c"
    assert kwargs["name"] == "lightbulb-acme" and kwargs["kind"] == "channel"
    assert kwargs["client_room_id"] == f"vendor-room-v1:{CUSTOMER}"
    members = kwargs["members"]
    assert [(m["kind"], m["id"], m["role"], m.get("homeOwnerId"), m.get("listenMode")) for m in members] == [
        ("human", "user_c", "owner", CUSTOMER, None),
        ("agent", CEO, "member", None, "mentions"),
        ("human", "user_d", "member", CUSTOMER, None),
        ("human", "user_v", "member", VENDOR, None),
    ]
    assert members[0]["displayName"] == "Cal" and members[3]["displayName"] == "Vee"
    assert env.streams.await_args_list == [call(CUSTOMER, "user_c"), call(CUSTOMER, "user_d"), call(VENDOR, "user_v")]


@pytest.mark.asyncio
async def test_ensure_vendor_room_falls_back_to_the_clerk_slug_without_its_suffix(vendor, monkeypatch):
    env = _seed_env(monkeypatch)
    monkeypatch.setattr("core.repositories.company_slug_repo.get_by_owner", AsyncMock(return_value=None))
    await messaging.ensure_vendor_room(CUSTOMER)
    assert env.create.await_args.kwargs["name"] == "lightbulb-acme"


@pytest.mark.asyncio
async def test_ensure_vendor_room_short_circuits_when_the_room_exists(vendor, monkeypatch):
    env = _seed_env(monkeypatch, room_exists=True)
    assert await messaging.ensure_vendor_room(CUSTOMER) == messaging.messaging_repo.vendor_room_id(CUSTOMER)
    env.get_org.assert_not_awaited()
    env.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_id, status", [(VENDOR, "active"), ("user_personal", "active"), (CUSTOMER, "pending")])
async def test_ensure_vendor_room_waits_or_skips(vendor, monkeypatch, owner_id, status):
    env = _seed_env(monkeypatch, company_status=status)
    assert await messaging.ensure_vendor_room(owner_id) is None
    env.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_vendor_room_is_off_without_a_vendor_org(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "")
    env = _seed_env(monkeypatch)
    assert await messaging.ensure_vendor_room(CUSTOMER) is None
    env.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_vendor_room_is_humans_only_for_a_legacy_roster(vendor, monkeypatch):
    env = _seed_env(monkeypatch, starter={})
    await messaging.ensure_vendor_room(CUSTOMER)
    assert all(m["kind"] == "human" for m in env.create.await_args.kwargs["members"])


@pytest.mark.asyncio
async def test_ensure_vendor_room_never_seeds_from_a_short_clerk_roster(vendor, monkeypatch):
    env = _seed_env(monkeypatch, vendor_humans=[])
    assert await messaging.ensure_vendor_room(CUSTOMER) is None
    env.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_vendor_room_caps_customer_first(vendor, monkeypatch):
    customer = [_membership("user_c", "org:admin")] + [_membership(f"user_c{i:02d}") for i in range(20)]
    vendor_humans = [_membership(f"user_v{i:02d}") for i in range(20)]
    env = _seed_env(monkeypatch, customer=customer, vendor_humans=vendor_humans)
    metrics = []
    monkeypatch.setattr(messaging, "put_metric", lambda name, **kw: metrics.append(name))
    await messaging.ensure_vendor_room(CUSTOMER)
    members = env.create.await_args.kwargs["members"]
    assert len(members) == messaging.messaging_repo.MAX_ROOM_MEMBERS
    assert [m["id"] for m in members[:2]] == ["user_c", CEO]
    assert all(m.get("homeOwnerId") == CUSTOMER for m in members[2:22])
    assert [m["id"] for m in members[22:]] == [f"user_v{i:02d}" for i in range(10)]
    assert "messaging.vendor_room.capped" in metrics


@pytest.mark.asyncio
async def test_add_vendor_room_member_adds_a_missing_human_with_their_home(vendor, monkeypatch):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    meta = {**META, "roomId": room_id}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    streams = AsyncMock(return_value="key")
    upsert = AsyncMock(return_value={**meta, "revision": 3})
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", streams)
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())

    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is True
    streams.assert_awaited_once_with(VENDOR, "user_v")
    member = upsert.await_args.kwargs["member"]
    assert (member["id"], member["role"], member["homeOwnerId"], member["displayName"]) == (
        "user_v",
        "member",
        VENDOR,
        "Vee",
    )
    assert upsert.await_args.kwargs["owner_id"] == CUSTOMER


@pytest.mark.asyncio
async def test_add_vendor_room_member_is_a_no_op_without_a_room_or_when_present(vendor, monkeypatch):
    upsert = AsyncMock()
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock())
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=None))
    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is False
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=_human("user_v", VENDOR)))
    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is False
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_join_adds_to_that_customers_room(vendor, monkeypatch):
    add = AsyncMock(return_value=True)
    monkeypatch.setattr(messaging, "add_vendor_room_member", add)
    await messaging.on_org_member_joined(CUSTOMER, "user_d")
    add.assert_awaited_once_with(CUSTOMER, "user_d", home_owner_id=CUSTOMER)


@pytest.mark.asyncio
async def test_vendor_join_fans_out_to_every_org_owner(vendor, monkeypatch):
    add = AsyncMock(return_value=True)
    monkeypatch.setattr(messaging, "add_vendor_room_member", add)
    rows = [{"owner_id": CUSTOMER}, {"owner_id": "org_other"}, {"owner_id": "user_personal"}, {"owner_id": VENDOR}]
    scan_all = AsyncMock(return_value=rows)
    monkeypatch.setattr(messaging.container_repo, "scan_all", scan_all)
    await messaging.on_org_member_joined(VENDOR, "user_new")
    assert add.await_args_list == [
        call(CUSTOMER, "user_new", home_owner_id=VENDOR),
        call("org_other", "user_new", home_owner_id=VENDOR),
    ]
    scan_all.assert_awaited_once_with(consistent=True)


@pytest.mark.asyncio
async def test_joins_are_ignored_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(messaging.settings, "VENDOR_ORG_ID", "")
    add = AsyncMock()
    monkeypatch.setattr(messaging, "add_vendor_room_member", add)
    await messaging.on_org_member_joined(CUSTOMER, "user_d")
    add.assert_not_awaited()


def _cancelled_write() -> ClientError:
    return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "x"}}, "TransactWriteItems")


@pytest.mark.asyncio
async def test_add_vendor_room_member_retries_a_lost_race(vendor, monkeypatch):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    meta = {**META, "roomId": room_id}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock(return_value="key"))
    upsert = AsyncMock(side_effect=[_cancelled_write(), {**meta, "revision": 3}])
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())

    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is True
    assert upsert.await_count == 2


@pytest.mark.asyncio
async def test_add_vendor_room_member_is_a_no_op_when_the_race_already_landed(vendor, monkeypatch):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    meta = {**META, "roomId": room_id}
    get_member = AsyncMock(side_effect=[None, _human("user_v", VENDOR)])
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", get_member)
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock(return_value="key"))
    upsert = AsyncMock(side_effect=_cancelled_write())
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)

    assert await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR) is False
    upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_vendor_room_member_reraises_a_non_cancellation_client_error(vendor, monkeypatch):
    room_id = messaging.messaging_repo.vendor_room_id(CUSTOMER)
    meta = {**META, "roomId": room_id}
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=meta))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Vee"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock(return_value="key"))
    error = ClientError({"Error": {"Code": "ValidationException", "Message": "x"}}, "TransactWriteItems")
    upsert = AsyncMock(side_effect=error)
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", upsert)

    with pytest.raises(ClientError):
        await messaging.add_vendor_room_member(CUSTOMER, "user_v", home_owner_id=VENDOR)
    upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_vendor_fan_out_survives_one_failing_room(vendor, monkeypatch):
    metrics = []
    monkeypatch.setattr(messaging, "put_metric", lambda name, **kw: metrics.append(name))
    add = AsyncMock(side_effect=[RuntimeError("boom"), True])
    monkeypatch.setattr(messaging, "add_vendor_room_member", add)
    rows = [{"owner_id": CUSTOMER}, {"owner_id": "org_other"}]
    monkeypatch.setattr(messaging.container_repo, "scan_all", AsyncMock(return_value=rows))

    await messaging.on_org_member_joined(VENDOR, "user_new")

    assert add.await_args_list == [
        call(CUSTOMER, "user_new", home_owner_id=VENDOR),
        call("org_other", "user_new", home_owner_id=VENDOR),
    ]
    assert metrics.count("messaging.vendor_room.error") == 1


def test_summary_marks_a_foreign_member_as_guest():
    assert messaging._room_summary_fields(META, _human("user_v", VENDOR), 0)["guest"] is True
    assert messaging._room_summary_fields(META, _human("user_c", CUSTOMER), 0)["guest"] is False
