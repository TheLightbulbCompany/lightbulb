"""``isDefault`` on a room summary: the seeded starter channel, identified by its
deterministic id.

The flag exists so a client can render the onboarding setup band in exactly one
room. It is derived the same way ``shared`` is — from the id — because the room
is renameable and matching on the name ``general`` would follow the rename.
"""

import uuid

from core.services import messaging

OWNER = "org_owner"
META = {
    "roomId": str(uuid.uuid4()),
    "ownerId": OWNER,
    "kind": "channel",
    "revision": 2,
    "lastSeq": 7,
    "deliveryKey": str(uuid.uuid4()),
    "workspaceId": str(uuid.uuid4()),
    "updatedAt": "2026-09-20T00:00:00Z",
    "lastMessageAt": None,
    "createdAt": "2026-09-20T00:00:00Z",
}
MEMBER = {"kind": "human", "id": "user_a", "ownerId": OWNER, "role": "owner", "joinedSeq": 0}


def test_the_starter_channel_is_recognised_by_its_deterministic_id():
    room_id = messaging.default_room_id(OWNER)
    assert messaging.is_default_room({"roomId": room_id, "ownerId": OWNER})
    assert not messaging.is_default_room(META)


def test_another_owners_starter_channel_is_not_this_owners():
    assert not messaging.is_default_room({"roomId": messaging.default_room_id("org_other"), "ownerId": OWNER})


def test_the_summary_carries_the_flag():
    room_id = messaging.default_room_id(OWNER)
    assert messaging._room_summary_fields({**META, "roomId": room_id}, MEMBER, 0)["is_default"] is True
    assert messaging._room_summary_fields(META, MEMBER, 0)["is_default"] is False


def test_the_flag_reaches_the_wire_as_is_default_camel_cased():
    """What the client actually reads. `to_camel` + `serialize_by_alias` make the
    field `isDefault`; naming it anything else here silently breaks the band."""
    summary = messaging.RoomSummary(
        **messaging._room_summary_fields({**META, "roomId": messaging.default_room_id(OWNER)}, MEMBER, 0)
    )
    assert summary.model_dump(by_alias=True)["isDefault"] is True
