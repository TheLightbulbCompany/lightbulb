import json

import pytest

from core.services.openclaw_work import deserialize_pointer

ROOM = "42907618-2eee-582d-9d17-ab8efedc1da4"
AGENT = "310e4207-de6d-4d95-8178-df803bbb685b"


def _record(payload: dict) -> str:
    # Shape of one DynamoDB stream record as EventBridge Pipes hands it to SQS.
    return json.dumps(
        {
            "eventID": "1",
            "eventName": "INSERT",
            "dynamodb": {
                "Keys": {"pk": {"S": f"ROOM#{ROOM}"}, "sk": {"S": "WORK#MESSAGE#1#AGENT#x"}},
                "NewImage": {
                    "pk": {"S": f"ROOM#{ROOM}"},
                    "messageGroupId": {"S": "g"},
                    "payload": {"M": payload},
                },
            },
        }
    )


def test_room_dispatch_pointer_is_read_from_the_stream_record():
    body = _record(
        {
            "kind": {"S": "room_dispatch"},
            "roomId": {"S": ROOM},
            "agentId": {"S": AGENT},
            "throughSeq": {"N": "1"},
        }
    )
    assert deserialize_pointer(body) == {
        "kind": "room_dispatch",
        "roomId": ROOM,
        "agentId": AGENT,
        "throughSeq": 1,
    }


def test_bare_attribute_map_is_no_longer_a_valid_body():
    body = json.dumps({"kind": {"S": "room_dispatch"}, "roomId": {"S": ROOM}})
    with pytest.raises(ValueError, match="invalid DynamoDB AttributeValue pointer"):
        deserialize_pointer(body)


def test_java_map_text_is_rejected_not_crashed():
    with pytest.raises(ValueError, match="invalid DynamoDB AttributeValue pointer"):
        deserialize_pointer("{kind={S=room_dispatch}}")
