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


def test_network_wake_pointer_is_exact():
    pointer = {"kind": {"S": "network_wake"}, "ownerId": {"S": "org_1"}}
    assert deserialize_pointer(_record(pointer)) == {"kind": "network_wake", "ownerId": "org_1"}
    with pytest.raises(ValueError, match="invalid network_wake pointer"):
        deserialize_pointer(_record({**pointer, "token": {"S": "org_1.secret"}}))
    with pytest.raises(ValueError, match="invalid ownerId"):
        deserialize_pointer(_record({**pointer, "ownerId": {"S": ""}}))


def test_voice_usage_pointer_is_exact_and_owner_scoped():
    pointer = {
        "kind": {"S": "voice_usage"},
        "ownerId": {"S": "org_1"},
        "transactionId": {"S": "meeting-1"},
    }
    assert deserialize_pointer(_record(pointer)) == {
        "kind": "voice_usage",
        "ownerId": "org_1",
        "transactionId": "meeting-1",
    }
    with pytest.raises(ValueError, match="invalid voice_usage pointer"):
        deserialize_pointer(_record({**pointer, "ownerId": {"S": ""}}))
    with pytest.raises(ValueError, match="invalid voice_usage pointer"):
        deserialize_pointer(_record({**pointer, "unexpected": {"S": "value"}}))


async def test_a_network_wake_invokes_network_wake_rpc(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from core.services import openclaw_work as work

    rpc = AsyncMock(return_value={})
    monkeypatch.setattr("core.services.container_rpc.call", rpc)
    sqs = MagicMock()
    worker = work.OpenClawWorkWorker(sqs_client=sqs)
    body = _record({"kind": {"S": "network_wake"}, "ownerId": {"S": "org_1"}})

    assert await worker._process_message({"Body": body, "ReceiptHandle": "rh"}) is True
    rpc.assert_awaited_once_with("org_1", "network.wake", {})
    sqs.delete_message.assert_called_once()


async def test_voice_usage_work_is_acked_only_after_durable_delivery(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from core.services import openclaw_work as work

    delivery = AsyncMock()
    monkeypatch.setattr("core.billing.voice_billing.deliver_usage_event", delivery)
    sqs = MagicMock()
    worker = work.OpenClawWorkWorker(sqs_client=sqs)
    body = _record(
        {
            "kind": {"S": "voice_usage"},
            "ownerId": {"S": "org_1"},
            "transactionId": {"S": "meeting-1"},
        }
    )

    assert await worker._process_message({"Body": body, "ReceiptHandle": "rh"}) is True
    delivery.assert_awaited_once_with("org_1", "meeting-1")
    sqs.delete_message.assert_called_once()


async def test_voice_usage_delivery_failure_leaves_fifo_message_for_retry(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from core.services import openclaw_work as work

    delivery = AsyncMock(side_effect=RuntimeError("provider temporarily unavailable"))
    monkeypatch.setattr("core.billing.voice_billing.deliver_usage_event", delivery)
    sqs = MagicMock()
    worker = work.OpenClawWorkWorker(sqs_client=sqs)
    body = _record(
        {
            "kind": {"S": "voice_usage"},
            "ownerId": {"S": "org_1"},
            "transactionId": {"S": "meeting-1"},
        }
    )

    assert await worker._process_message({"Body": body, "ReceiptHandle": "rh"}) is False
    delivery.assert_awaited_once_with("org_1", "meeting-1")
    sqs.delete_message.assert_not_called()
