from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from boto3.dynamodb.types import TypeSerializer

SCRIPT = Path(__file__).with_name("replay-openclaw-work-pipe-failure.py")
spec = importlib.util.spec_from_file_location("work_pipe_replay", SCRIPT)
replay = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(replay)

ROOM_ID = "11111111-1111-4111-8111-111111111111"
AGENT_ID = "22222222-2222-4222-8222-222222222222"
_serializer = TypeSerializer()


def work_event(*, sk="WORK#MESSAGE#00000000000000000001#AGENT#agent", payload=None):
    pointer = payload or {
        "kind": "room_dispatch",
        "roomId": ROOM_ID,
        "agentId": AGENT_ID,
        "throughSeq": 1,
    }
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "sk": {"S": sk},
                "payload": {"M": {key: _serializer.serialize(value) for key, value in pointer.items()}},
                "messageGroupId": {"S": "a" * 64},
                "messageDeduplicationId": {"S": "b" * 64},
            }
        },
    }


class FakeSqs:
    def __init__(self, event, *, send_response=None, messages=None):
        self.calls = []
        self.message = {
            "MessageId": "pipe-message-1",
            "ReceiptHandle": "receipt-1",
            "Body": json.dumps(event),
        }
        self.messages = [self.message] if messages is None else messages
        self.send_response = {"MessageId": "fifo-message-1"} if send_response is None else send_response
        self.received = False

    def receive_message(self, **kwargs):
        self.calls.append(("receive", kwargs))
        if self.received:
            return {}
        self.received = True
        return {"Messages": self.messages}

    def send_message(self, **kwargs):
        self.calls.append(("send", kwargs))
        return self.send_response

    def delete_message(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {}

    def change_message_visibility(self, **kwargs):
        self.calls.append(("visibility", kwargs))
        return {}


class WorkPipeReplayTests(unittest.TestCase):
    def test_replays_valid_pointer_before_deleting_dlq_message(self):
        event = work_event()
        sqs = FakeSqs(event)
        replay.replay_message(
            pipe_dlq_url="https://sqs.us-east-1.amazonaws.com/1/pipe-dlq",
            work_queue_url="https://sqs.us-east-1.amazonaws.com/1/work.fifo",
            message_id="pipe-message-1",
            sqs_client=sqs,
        )
        actions = [name for name, _ in sqs.calls]
        self.assertLess(actions.index("send"), actions.index("delete"))
        sent = next(kwargs for name, kwargs in sqs.calls if name == "send")
        # The worker expects the raw stream record, exactly as the Pipe sends it.
        self.assertEqual(json.loads(sent["MessageBody"]), event)
        self.assertEqual(sent["MessageGroupId"], "a" * 64)
        self.assertEqual(sent["MessageDeduplicationId"], "b" * 64)

    def test_releases_other_messages_received_after_the_selected_message(self):
        sqs = FakeSqs(work_event())
        sqs.messages.append(
            {
                "MessageId": "other-message",
                "ReceiptHandle": "other-receipt",
                "Body": "{}",
            }
        )
        replay.replay_message(
            pipe_dlq_url="https://sqs.us-east-1.amazonaws.com/1/pipe-dlq",
            work_queue_url="https://sqs.us-east-1.amazonaws.com/1/work.fifo",
            message_id="pipe-message-1",
            sqs_client=sqs,
        )
        released = [kwargs["ReceiptHandle"] for name, kwargs in sqs.calls if name == "visibility"]
        self.assertEqual(released, ["other-receipt"])

    def test_missing_selected_receipt_handle_fails_before_enqueue(self):
        sqs = FakeSqs(work_event())
        del sqs.message["ReceiptHandle"]
        with self.assertRaises(ValueError):
            replay.replay_message(
                pipe_dlq_url="https://sqs.us-east-1.amazonaws.com/1/pipe-dlq",
                work_queue_url="https://sqs.us-east-1.amazonaws.com/1/work.fifo",
                message_id="pipe-message-1",
                sqs_client=sqs,
            )
        actions = [name for name, _ in sqs.calls]
        self.assertNotIn("send", actions)
        self.assertNotIn("delete", actions)

    def test_malformed_or_nonwork_events_fail_closed(self):
        malformed = [
            {"eventName": "MODIFY", "dynamodb": {"NewImage": {}}},
            work_event(sk="MSG#00000000000000000001"),
            work_event(payload={"kind": "unexpected"}),
            {"Records": [work_event(), work_event()]},
        ]
        for event in malformed:
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    replay.validate_work_event(event)

    def test_enqueue_failure_never_deletes_dlq_message(self):
        sqs = FakeSqs(work_event(), send_response={})
        with self.assertRaises(RuntimeError):
            replay.replay_message(
                pipe_dlq_url="https://sqs.us-east-1.amazonaws.com/1/pipe-dlq",
                work_queue_url="https://sqs.us-east-1.amazonaws.com/1/work.fifo",
                message_id="pipe-message-1",
                sqs_client=sqs,
            )
        actions = [name for name, _ in sqs.calls]
        self.assertIn("send", actions)
        self.assertNotIn("delete", actions)
        self.assertIn("visibility", actions)


if __name__ == "__main__":
    unittest.main()
