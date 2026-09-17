"""Humans are mentionable: every human room member carries a handle."""

import re
import uuid
from unittest.mock import AsyncMock

import pytest
from core.services import messaging


AGENT_ID = str(uuid.UUID(int=1))
ROOM_ID = str(uuid.UUID(int=9))


def agent(agent_id: str, name: str, handle: str) -> dict:
    return {
        "kind": "agent",
        "id": agent_id,
        "displayName": name,
        "mentionHandle": handle,
        "role": "member",
        "listenMode": "mentions",
        "joinedAt": "2026-09-10T00:00:00Z",
        "joinedSeq": 1,
    }


def human(user_id: str, name: str, joined_seq: int = 1) -> dict:
    return {
        "kind": "human",
        "id": user_id,
        "displayName": name,
        "mentionHandle": None,
        "role": "member",
        "listenMode": None,
        "joinedAt": "2026-09-10T00:00:00Z",
        "joinedSeq": joined_seq,
    }


def handles(members: list[dict], emails: dict[str, str] | None = None) -> dict[str, str | None]:
    return {row["id"]: row.get("mentionHandle") for row in messaging.with_human_handles(members, emails)}


def test_every_human_member_gets_a_handle_from_their_display_name():
    resolved = handles([human("user_a", "Prasiddha Parthsarthy"), agent(AGENT_ID, "Friday", "friday")])
    assert resolved == {"user_a": "prasiddha-parthsarthy", AGENT_ID: "friday"}


def test_human_handle_prefers_the_email_local_part_over_display_name():
    resolved = handles(
        [human("user_a", "Prasiddha Parthsarthy")],
        emails={"user_a": "prasiddha@gmail.com"},
    )
    assert resolved == {"user_a": "prasiddha"}


def test_human_with_no_email_on_record_falls_back_to_display_name():
    resolved = handles([human("user_a", "Prasiddha Parthsarthy")], emails={})
    assert resolved == {"user_a": "prasiddha-parthsarthy"}


def test_two_humans_sharing_an_email_local_part_collide_in_join_order():
    members = [human("user_b", "Bee", joined_seq=1), human("user_a", "Ay", joined_seq=2)]
    emails = {"user_b": "aden@example.com", "user_a": "aden@another.example.com"}
    resolved = handles(members, emails)
    assert resolved["user_b"] == "aden"  # earlier joiner wins the bare handle
    assert resolved["user_a"].startswith("aden-")
    assert resolved["user_a"] != "aden"


def test_email_local_part_colliding_with_an_agent_handle_is_suffixed():
    resolved = handles(
        [human("user_a", "Someone Else"), agent(AGENT_ID, "Friday", "friday")],
        emails={"user_a": "friday@example.com"},
    )
    assert resolved[AGENT_ID] == "friday"
    assert resolved["user_a"].startswith("friday-")
    assert resolved["user_a"] != "friday"


def test_email_local_part_with_dot_and_plus_tag_normalises_to_a_legal_handle():
    resolved = handles([human("user_a", "Ignored Name")], emails={"user_a": "first.last+tag@example.com"})
    assert resolved["user_a"] == "first-last-tag"
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", resolved["user_a"])


def test_human_colliding_with_an_agent_is_suffixed_and_the_agent_keeps_its_handle():
    resolved = handles([human("user_a", "Friday"), agent(AGENT_ID, "Friday", "friday")])
    assert resolved[AGENT_ID] == "friday"
    assert resolved["user_a"].startswith("friday-")
    assert resolved["user_a"] != "friday"


def test_two_humans_with_the_same_name_get_deterministic_distinct_handles():
    members = [human("user_b", "Aden"), human("user_a", "Aden")]
    first = handles(members)
    assert first["user_a"] == "aden"  # lowest user id wins the bare handle
    assert first["user_b"].startswith("aden-")
    assert first == handles(list(reversed(members)))


def test_a_later_joiner_never_displaces_an_existing_members_handle():
    # The newcomer sorts FIRST by user id, so only the join order keeps the
    # sitting member's handle stable — a reassigned handle silently re-points
    # every stored "@aden" in the room's history at the new person.
    sitting = human("user_zz", "Aden", joined_seq=2)
    newcomer = human("user_aa", "Aden", joined_seq=7)
    resolved = handles([sitting, newcomer])
    assert resolved["user_zz"] == "aden"
    assert resolved["user_aa"].startswith("aden-")


def test_a_human_with_no_ascii_in_their_name_falls_back_to_a_user_handle():
    resolved = handles([human("user_a", "日本語")])
    assert resolved["user_a"].startswith("user-")
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", resolved["user_a"])


def test_parse_agent_mentions_resolves_a_human_handle():
    members = messaging.with_human_handles([human("user_a", "Aden Kim"), agent(AGENT_ID, "Friday", "friday")])
    assert messaging.parse_agent_mentions("thanks @aden-kim", members)[0] == [{"kind": "human", "id": "user_a"}]
    assert messaging.parse_agent_mentions("@friday and @aden-kim", members)[0] == [
        {"kind": "agent", "id": AGENT_ID},
        {"kind": "human", "id": "user_a"},
    ]
    assert messaging.parse_agent_mentions("nobody here", members) == ([], [])


def _room_detail_meta() -> dict:
    return {
        "roomId": ROOM_ID,
        "ownerId": "org_1",
        "kind": "channel",
        "name": "general",
        "revision": 3,
        "lastSeq": 5,
        "updatedAt": "2026-09-10T00:00:00Z",
        "deliveryKey": str(uuid.UUID(int=8)),
        "createdAt": "2026-09-10T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_messaging_member_listing_carries_the_human_handle(monkeypatch):
    from core.repositories import user_repo

    members = [human("user_a", "Aden Kim"), agent(AGENT_ID, "Friday", "friday")]
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "get_read_cursor", AsyncMock(return_value=0))
    monkeypatch.setattr(messaging.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[]))
    # No email on record -- falls back to the display-name derivation.
    monkeypatch.setattr(user_repo, "get", AsyncMock(return_value=None))

    detail = await messaging._room_detail(_room_detail_meta(), "user_a", members[0])
    assert {row.id: row.mention_handle for row in detail.members} == {"user_a": "aden-kim", AGENT_ID: "friday"}


@pytest.mark.asyncio
async def test_messaging_member_listing_uses_the_users_row_email(monkeypatch):
    from core.repositories import user_repo

    members = [human("user_a", "Aden Kim"), agent(AGENT_ID, "Friday", "friday")]
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(messaging.messaging_repo, "get_read_cursor", AsyncMock(return_value=0))
    monkeypatch.setattr(messaging.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        user_repo,
        "get",
        AsyncMock(side_effect=lambda user_id: {"email": "prasiddha@gmail.com"} if user_id == "user_a" else None),
    )

    detail = await messaging._room_detail(_room_detail_meta(), "user_a", members[0])
    assert {row.id: row.mention_handle for row in detail.members} == {"user_a": "prasiddha", AGENT_ID: "friday"}


@pytest.mark.asyncio
async def test_human_emails_lookup_failure_falls_back_instead_of_raising(monkeypatch):
    from core.repositories import user_repo

    monkeypatch.setattr(user_repo, "get", AsyncMock(side_effect=RuntimeError("table not found")))
    resolved = await messaging.human_emails([human("user_a", "Aden Kim")])
    assert resolved == {}


@pytest.mark.asyncio
async def test_agent_ingress_participants_expose_human_handles(monkeypatch):
    from core.services import openclaw_work

    monkeypatch.setattr(openclaw_work.messaging_repo, "list_messages_before", AsyncMock(return_value=[]))
    body = await openclaw_work.build_ingress_body(
        meta={"roomId": ROOM_ID, "name": "general", "kind": "channel", "revision": 1},
        target_agent_id=AGENT_ID,
        event_kind="user_request",
        message={
            "messageId": str(uuid.uuid4()),
            "messageSeq": 4,
            "roomId": ROOM_ID,
            "roomRevision": 1,
            "author": {"kind": "human", "id": "user_a", "displayName": "Aden Kim"},
            "text": "hi",
            "mentions": [],
            "createdAt": "2026-09-10T00:00:00Z",
        },
        members=messaging.with_human_handles([human("user_a", "Aden Kim"), agent(AGENT_ID, "Friday", "friday")]),
    )
    people = {row["id"]: row.get("mentionHandle") for row in body["participants"]}
    assert people == {"user_a": "aden-kim", AGENT_ID: "friday"}
