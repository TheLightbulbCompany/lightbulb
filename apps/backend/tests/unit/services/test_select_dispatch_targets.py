from core.services.messaging import select_dispatch_targets

FRIDAY = {"kind": "agent", "id": "310e4207-de6d-4d95-8178-df803bbb685b", "listenMode": "active"}
GAUGE = {"kind": "agent", "id": "86358f5a-ccd5-46c5-9607-0ea0e44dea34", "listenMode": "mentions"}
HUMAN = {"kind": "human", "id": "user_1"}


def test_dm_message_is_a_user_request_without_a_mention():
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], None, room_kind="dm")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "user_request"}]


def test_channel_message_without_a_mention_is_ambient_for_active_agents_only():
    targets = select_dispatch_targets([FRIDAY, GAUGE, HUMAN], [], None, room_kind="channel")
    assert targets == [{"agentId": FRIDAY["id"], "eventKind": "room_event"}]


def test_channel_mention_addresses_only_the_mentioned_agent():
    mentions = [{"kind": "agent", "id": GAUGE["id"]}]
    targets = select_dispatch_targets([FRIDAY, GAUGE, HUMAN], mentions, None, room_kind="channel")
    assert targets == [
        {"agentId": FRIDAY["id"], "eventKind": "room_event"},
        {"agentId": GAUGE["id"], "eventKind": "user_request"},
    ]


def test_source_agent_never_receives_its_own_dm():
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], FRIDAY["id"], room_kind="dm")
    assert targets == []
