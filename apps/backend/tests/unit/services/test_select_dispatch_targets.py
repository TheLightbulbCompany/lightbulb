import pytest

from core.services.messaging import select_dispatch_targets

FRIDAY = {
    "kind": "agent",
    "id": "310e4207-de6d-4d95-8178-df803bbb685b",
    "listenMode": "active",
    "displayName": "Friday",
    "mentionHandle": "friday",
}
GAUGE = {
    "kind": "agent",
    "id": "86358f5a-ccd5-46c5-9607-0ea0e44dea34",
    "listenMode": "mentions",
    "displayName": "Gauge",
    "mentionHandle": "gauge",
}
PULSE = {
    "kind": "agent",
    "id": "0a0e4207-de6d-4d95-8178-df803bbb685b",
    "listenMode": "active",
    "displayName": "Pulse",
    "mentionHandle": "pulse",
}
HUMAN = {"kind": "human", "id": "user_1", "displayName": "Aden"}


def test_dm_message_is_a_user_request_without_a_mention():
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], None, room_kind="dm")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "user_request"}]


def test_channel_message_without_a_mention_is_ambient_for_active_agents_only():
    targets = select_dispatch_targets([FRIDAY, GAUGE, HUMAN], [], None, room_kind="channel")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "room_event"}]


def test_a_mention_is_not_ambient_for_anyone_else():
    """Prod 2026-09-27: '@friday ...' woke Friday AND a Listening Pulse beside her."""
    mentions = [{"kind": "agent", "id": GAUGE["id"]}]
    targets = select_dispatch_targets([FRIDAY, GAUGE, PULSE, HUMAN], mentions, None, room_kind="channel")
    assert targets == [{"agentId": GAUGE["id"], "eventKind": "user_request"}]


def test_observe_member_never_runs_even_when_mentioned_or_in_a_dm():
    """Nobody ran for the mention, so the Listening agents still hear the message."""
    observer = {**GAUGE, "listenMode": "observe"}
    mentions = [{"kind": "agent", "id": observer["id"]}]
    assert select_dispatch_targets([FRIDAY, observer, HUMAN], mentions, None, room_kind="channel") == [
        {"agentId": FRIDAY["id"], "eventKind": "room_event"},
    ]
    assert select_dispatch_targets([observer, HUMAN], [], None, room_kind="dm") == []


def test_source_agent_never_receives_its_own_dm():
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], FRIDAY["id"], room_kind="dm")
    assert targets == []


def test_a_message_to_a_native_session_is_not_ambient():
    """Prod 2026-10-02: '@claude-ready430 reply pong' in the Atlas DM ran the session and Atlas."""
    assert select_dispatch_targets([FRIDAY, HUMAN], [], None, room_kind="dm", session_addressed=True) == []
    mentions = [{"kind": "agent", "id": FRIDAY["id"]}]
    assert select_dispatch_targets([FRIDAY, HUMAN], mentions, None, room_kind="dm", session_addressed=True) == [
        {"agentId": FRIDAY["id"], "eventKind": "user_request"},
    ]
    assert select_dispatch_targets([FRIDAY, HUMAN], [], None, room_kind="channel", session_addressed=True) == []


@pytest.mark.parametrize(
    "text",
    [
        # Prod 2026-09-27, #demo channel: Pulse answered this.
        "Hey Friday, I know we were having issues with supademo, if you check my latest session",
        "Friday, can you check the deploy?",
        "Hi Friday can you look at this",
        "Friday can you pull the numbers",
        "Friday please take this one",
        "Friday - what do you think?",
        "  hey, @friday, are you there",
    ],
)
def test_a_message_that_opens_with_an_agents_name_is_addressed_to_it(text):
    members = [{**FRIDAY, "listenMode": "mentions"}, PULSE, HUMAN]
    targets = select_dispatch_targets(members, [], None, room_kind="channel", text=text)
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "user_request"}]


@pytest.mark.parametrize(
    "text",
    [
        "I'll send the investor update by Friday, remind me if I forget",
        "Friday is the deadline for the launch",
        "thanks Friday, that's perfect",
        "Fridays are slow",
        "Friday-night deploys are risky",
        "what did we spend on cloud last month?",
        "",
        # The starter roster's names are ordinary words: a header or a subject is not an address.
        "Friday: 3pm standup moved",
        "hi friday is the deadline, right?",
        "Hey Friday team, ideas?",
        "Hey Fridayé, ça va?",
    ],
)
def test_a_name_that_does_not_open_the_message_addresses_nobody(text):
    targets = select_dispatch_targets([FRIDAY, PULSE, HUMAN], [], None, room_kind="channel", text=text)
    assert targets == [
        {"agentId": PULSE["id"], "eventKind": "room_event"},
        {"agentId": FRIDAY["id"], "eventKind": "room_event"},
    ]


def test_a_name_two_agents_share_addresses_neither():
    twin = {**PULSE, "displayName": "Friday", "mentionHandle": "friday-2"}
    targets = select_dispatch_targets([FRIDAY, twin, HUMAN], [], None, room_kind="channel", text="Friday, you there?")
    assert {t["eventKind"] for t in targets} == {"room_event"}
