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


def test_observe_member_never_runs_even_when_mentioned_or_in_a_dm():
    observer = {**GAUGE, "listenMode": "observe"}
    mentions = [{"kind": "agent", "id": observer["id"]}]
    assert select_dispatch_targets([FRIDAY, observer, HUMAN], mentions, None, room_kind="channel") == [
        {"agentId": FRIDAY["id"], "eventKind": "room_event"},
    ]
    assert select_dispatch_targets([observer, HUMAN], [], None, room_kind="dm") == []


def test_source_agent_never_receives_its_own_dm():
    targets = select_dispatch_targets([FRIDAY, HUMAN], [], FRIDAY["id"], room_kind="dm")
    assert targets == []


# ---- "nobody talks into silence" (owner ruling 2026-09-23): in the starter #general, a human
# message that selects no agent at all goes to the room's default responder (the CEO).
CEO = {"kind": "agent", "id": "65798fe9-ae99-4e3f-9e9f-0d6179f16c66", "listenMode": "mentions"}
FIN = {"kind": "agent", "id": "7fe247e9-d619-43c6-92d4-b9fb3411855c", "listenMode": "mentions"}


def test_a_plain_message_with_no_target_goes_to_the_default_responder():
    targets = select_dispatch_targets([CEO, FIN, HUMAN], [], None, room_kind="channel", default_agent_id=CEO["id"])
    assert targets == [{"agentId": CEO["id"], "eventKind": "user_request"}]


def test_a_mention_still_addresses_only_the_mentioned_agent():
    mentions = [{"kind": "agent", "id": FIN["id"]}]
    targets = select_dispatch_targets(
        [CEO, FIN, HUMAN], mentions, None, room_kind="channel", default_agent_id=CEO["id"]
    )
    assert targets == [{"agentId": FIN["id"], "eventKind": "user_request"}]


def test_listening_agents_mean_no_forced_default():
    # Paid rooms have Listening candidates, and Jev decides (including "nobody").
    active_fin = {**FIN, "listenMode": "active"}
    targets = select_dispatch_targets(
        [CEO, active_fin, HUMAN], [], None, room_kind="channel", default_agent_id=CEO["id"]
    )
    assert targets == [{"agentId": FIN["id"], "eventKind": "room_event"}]


def test_an_agents_message_never_falls_back_to_the_default():
    targets = select_dispatch_targets([CEO, FIN, HUMAN], [], FIN["id"], room_kind="channel", default_agent_id=CEO["id"])
    assert targets == []


def test_a_default_that_is_off_or_not_a_member_is_ignored():
    off = {**CEO, "listenMode": "observe"}
    assert select_dispatch_targets([off, FIN, HUMAN], [], None, room_kind="channel", default_agent_id=CEO["id"]) == []
    assert select_dispatch_targets([FIN, HUMAN], [], None, room_kind="channel", default_agent_id=CEO["id"]) == []
