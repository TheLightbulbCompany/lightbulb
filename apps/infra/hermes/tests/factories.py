import uuid

ROOM = "11111111-1111-4111-8111-111111111111"


def message(**over):
    mid = over.pop("messageId", str(uuid.uuid4()))
    base = {
        "messageId": mid, "roomId": ROOM, "messageSeq": 7, "roomRevision": 3,
        "author": {"kind": "human", "id": "user_1", "displayName": "Prez"},
        "text": "ship it", "mentions": [], "replyToMessageId": None,
        "threadRootMessageId": None, "createdAt": "2026-09-16T10:00:00Z", "runId": None,
    }
    base.update(over)
    return base


def ingress(**over):
    msg = over.pop("message", message())
    base = {
        "version": 1, "eventId": msg["messageId"], "targetAgentId": "ceo",
        "eventKind": "user_request",
        "room": {"id": ROOM, "name": "general", "kind": "channel", "revision": 3},
        "sender": msg["author"], "message": msg, "mentions": msg["mentions"],
        "participants": [
            {"kind": "human", "id": "user_1", "displayName": "Prez"},
            {"kind": "agent", "id": "ceo", "displayName": "CEO", "listenMode": "active"},
        ],
        "recentHistory": [],
        "run": {"runId": "22222222-2222-4222-8222-222222222222", "workspaceId": ROOM,
                "frameChannel": f"/ws/{ROOM}/rooms/{ROOM}/deadbeef",
                "frameEndpoint": "https://events.example.invalid/event"},
    }
    base.update(over)
    return base
