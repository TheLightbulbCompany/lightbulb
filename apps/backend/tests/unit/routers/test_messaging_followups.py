"""Bridge -> backend delegation follow-up route: hook-token gate, validation, cap, replay."""

from unittest.mock import AsyncMock, patch

import pytest

OWNER, TOKEN = "org_a", "hook-token-for-a"
ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
FOLLOW = "33333333-3333-4333-8333-333333333333"
TRIGGER = "44444444-4444-4444-8444-444444444444"
URL = f"/api/v1/internal/messaging/followups/{OWNER}"
H = {"Authorization": f"Bearer {TOKEN}"}
BODY = {
    "followUpId": FOLLOW,
    "roomId": ROOM,
    "agentId": AGENT,
    "triggerMessageId": TRIGGER,
    "eventKind": "user_request",
    "reason": "delegation_completed",
    "text": "review finished: 2 findings",
}
REPO = "core.services.messaging.messaging_repo"


@pytest.fixture
def repo():
    """A valid room/member/trigger with no prior follow-ups; each test bends one piece."""
    with (
        patch(
            "routers.messaging.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}),
        ),
        patch(f"{REPO}.get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": OWNER})) as get_room,
        patch(f"{REPO}.get_member", AsyncMock(return_value={"kind": "agent", "id": AGENT})) as get_member,
        patch(f"{REPO}.get_message_by_id", AsyncMock(return_value={"messageId": TRIGGER})) as get_message,
        patch(f"{REPO}.get_follow_up", AsyncMock(return_value=None)) as get_follow_up,
        patch(f"{REPO}.latest_human_message", AsyncMock(return_value=None)),
        patch(f"{REPO}.count_follow_ups_since", AsyncMock(return_value=0)) as count,
        patch("core.services.messaging._post_follow_up_cap_notice", AsyncMock()) as notice,
        patch(f"{REPO}.create_follow_up", AsyncMock(return_value=True)) as create,
    ):
        yield {
            "get_room": get_room,
            "get_member": get_member,
            "get_message": get_message,
            "get_follow_up": get_follow_up,
            "count": count,
            "notice": notice,
            "create": create,
        }


def test_missing_or_wrong_bearer_is_401(client, repo):
    assert client.post(URL, json=BODY).status_code == 401
    assert client.post(URL, json=BODY, headers={"Authorization": "Bearer WRONG"}).status_code == 401
    repo["create"].assert_not_awaited()


def test_first_write_is_202_accepted(client, repo):
    r = client.post(URL, json=BODY, headers=H)
    assert r.status_code == 202 and r.json() == {"status": "accepted"}
    kwargs = repo["create"].call_args.kwargs
    assert kwargs == {
        "owner_id": OWNER,
        "room_id": ROOM,
        "agent_id": AGENT,
        "follow_up_id": FOLLOW,
        "trigger_message_id": TRIGGER,
        "event_kind": "user_request",
        "reason": "delegation_completed",
        "text": BODY["text"],
    }


def test_an_existing_follow_up_replays_before_the_cap(client, repo):
    """A retry after a lost 202 must not be answered 429 because its own row now counts."""
    repo["get_follow_up"].return_value = {"agentId": AGENT}
    repo["count"].return_value = 99
    r = client.post(URL, json=BODY, headers=H)
    assert r.status_code == 200 and r.json() == {"status": "replayed"}
    repo["create"].assert_not_awaited()


def test_a_write_race_lost_is_a_replay(client, repo):
    repo["create"].return_value = False
    r = client.post(URL, json=BODY, headers=H)
    assert r.status_code == 200 and r.json() == {"status": "replayed"}


@pytest.mark.parametrize(
    ("piece", "value"),
    [
        ("get_room", None),
        ("get_room", {"roomId": ROOM, "ownerId": "org_other"}),
        ("get_member", None),
        ("get_message", None),
    ],
)
def test_unknown_room_member_or_trigger_is_404(client, repo, piece, value):
    repo[piece].return_value = value
    assert client.post(URL, json=BODY, headers=H).status_code == 404
    repo["create"].assert_not_awaited()


def test_the_room_agent_cap_is_429(client, repo):
    repo["count"].return_value = 10
    assert client.post(URL, json=BODY, headers=H).status_code == 429
    assert repo["count"].call_args.args[:2] == (ROOM, AGENT)
    repo["notice"].assert_awaited_once()
    repo["create"].assert_not_awaited()


@pytest.mark.parametrize(
    "patch_body",
    [
        {"reason": "something_else"},
        {"eventKind": "dm"},
        {"followUpId": "not-a-uuid"},
        {"text": ""},
        {"text": "é" * 16385},  # 32770 bytes
        {"extra": 1},
    ],
)
def test_a_malformed_body_is_refused_as_final(client, repo, patch_body):
    # main.py maps every validation error under /api/v1/internal/messaging to
    # INVALID_REQUEST (400); any 4xx is final for the bridge.
    r = client.post(URL, json={**BODY, **patch_body}, headers=H)
    assert r.status_code == 400
    repo["create"].assert_not_awaited()


def test_text_at_the_byte_limit_is_accepted(client, repo):
    assert client.post(URL, json={**BODY, "text": "a" * 32768}, headers=H).status_code == 202
