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


def test_the_addressing_stages_agents_ride_in_mentions():
    """Routing v2 §3.1: callers pass ``address``'s agents with the @mentions; all of them run."""
    named = [{"kind": "agent", "id": FRIDAY["id"]}, {"kind": "agent", "id": GAUGE["id"]}]
    assert select_dispatch_targets([FRIDAY, GAUGE, PULSE, HUMAN], named, None, room_kind="channel") == [
        {"agentId": FRIDAY["id"], "eventKind": "user_request"},
        {"agentId": GAUGE["id"], "eventKind": "user_request"},
    ]


def test_group_chat_of_one_person_and_one_agent_is_addressed_like_a_dm():
    """The voice app's "New chat" is a group-dm: "ok thanks" must reach the agent, not Jev's silence."""
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], None, room_kind="group-dm")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "user_request"}]


def test_group_chat_with_two_agents_stays_ambient():
    targets = select_dispatch_targets([FRIDAY, PULSE, HUMAN], [], None, room_kind="group-dm")
    assert targets == [
        {"agentId": PULSE["id"], "eventKind": "room_event"},
        {"agentId": FRIDAY["id"], "eventKind": "room_event"},
    ]


def test_group_chat_with_two_people_stays_ambient():
    other = {"kind": "human", "id": "user_2", "displayName": "Sam"}
    targets = select_dispatch_targets([FRIDAY, HUMAN, other], [], None, room_kind="group-dm")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "room_event"}]


def test_an_agents_message_in_a_one_to_one_group_chat_is_unchanged():
    """An agent-authored line (a Worker's result) stays ambient: two agents must not answer each other."""
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], GAUGE["id"], room_kind="group-dm")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "room_event"}]


def test_a_channel_of_one_person_and_one_agent_stays_ambient():
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], None, room_kind="channel")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "room_event"}]
