import json
import uuid

from bridge import turn

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


def split_turn_input(user_message):
    """Split a `turn._turn_input()`-produced user message -- a plain string, or the
    `_message_content` list form when a native image rides along -- into (turn context dict,
    user text). The header is the JSON line after `TURN_CONTEXT_PREFIX`; the user text is
    everything after the first newline."""
    text = user_message[0]["text"] if isinstance(user_message, list) else user_message
    header, _, rest = text.partition("\n")
    return json.loads(header.removeprefix(turn.TURN_CONTEXT_PREFIX)), rest
