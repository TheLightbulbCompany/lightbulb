from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from boto3.dynamodb.types import TypeSerializer
from botocore.credentials import Credentials

import index
import replay

ROOM_ID = "11111111-1111-4111-8111-111111111111"
MESSAGE_ID = "22222222-2222-4222-8222-222222222222"
DELIVERY_KEY = "33333333-3333-4333-8333-333333333333"
OLD_DELIVERY_KEY = "44444444-4444-4444-8444-444444444444"
CHANNEL_KEY = "55555555-5555-4555-8555-555555555555"
EVENT_ID = "66666666-6666-4666-8666-666666666666"
OWNER_ID = "org_owner"
USER_ID = "user_alice"
BOB_ID = "user_bob"
CAROL_ID = "user_carol"
BOB_KEY = "88888888-8888-4888-8888-888888888888"
CAROL_KEY = "99999999-9999-4999-8999-999999999999"
USERS_TABLE = "lightbulb-test-users"
CREATED_AT = "2026-09-10T00:00:00Z"

_serializer = TypeSerializer()


def av_map(item: dict) -> dict:
    return {key: _serializer.serialize(value) for key, value in item.items()}


def stream_record(item: dict, sequence: str = "100") -> dict:
    return {
        "eventID": f"event-{sequence}",
        "eventName": "INSERT",
        "dynamodb": {
            "SequenceNumber": sequence,
            "NewImage": av_map(item),
        },
    }


def message_item(message_id: str = MESSAGE_ID, sequence: int = 1) -> dict:
    return {
        "pk": f"ROOM#{ROOM_ID}",
        "sk": f"MSG#{sequence:020d}",
        "messageId": message_id,
        "roomId": ROOM_ID,
        "messageSeq": sequence,
        "roomRevision": 3,
        "author": {
            "kind": "human",
            "id": USER_ID,
            "displayName": "Alice",
        },
        "text": "hello",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": CREATED_AT,
        "runId": None,
        "deliveryKey": OLD_DELIVERY_KEY,
        "ownerId": OWNER_ID,
        "dispatchTargets": [],
    }


def live_event(event_id: str = EVENT_ID) -> dict:
    return {
        "schemaVersion": 1,
        "eventId": event_id,
        "type": "room.subscription.rotated",
        "occurredAt": CREATED_AT,
        "roomId": ROOM_ID,
        "roomRevision": 3,
        "payload": {
            "roomId": ROOM_ID,
            "deliveryKey": DELIVERY_KEY,
            "roomRevision": 3,
        },
    }


def live_item(event_id: str = EVENT_ID) -> dict:
    return {
        "pk": f"ROOM#{ROOM_ID}",
        "sk": f"LIVE#{event_id}#USER#{USER_ID}",
        "ownerId": OWNER_ID,
        "userId": USER_ID,
        "channelKey": CHANNEL_KEY,
        "event": live_event(event_id),
    }


class FakeDdb:
    def __init__(self, items: dict[tuple[str, str], dict] | None = None):
        self.items = items or {}
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["Key"]
        lookup = (
            (key["user_id"]["S"], "USER") if "user_id" in key
            else (key["pk"]["S"], key["sk"]["S"])
        )
        item = self.items.get(lookup)
        return {"Item": av_map(item)} if item else {}

    def query(self, **kwargs):
        self.calls.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        return {"Items": [av_map(row) for (pk, sk), row in self.items.items()
                          if pk == values[":pk"]["S"] and sk.startswith(values[":prefix"]["S"])]}


class ProjectorTests(unittest.TestCase):
    def test_edit_and_delete_publish_invalidation_without_content_or_notifications(self):
        for deleted in (None, CREATED_AT):
            old = message_item()
            updated = {**old, "messageRevision": 1, "editedText": "changed", "editedAt": CREATED_AT, "deletedAt": deleted}
            index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY}})
            record = stream_record(updated)
            record["eventName"] = "MODIFY"
            record["dynamodb"]["OldImage"] = av_map(old)
            with patch.object(index, "_publish") as publish, patch.object(index, "_notify_members") as notify:
                index.process_record(record)
                event = publish.call_args.args[1]
                self.assertEqual(event["type"], "room.message.updated")
                self.assertEqual(event["payload"], {"roomId": ROOM_ID, "messageId": MESSAGE_ID, "messageSeq": 1})
                notify.assert_not_called()

    def test_delayed_insert_of_deleted_message_does_not_publish_original(self):
        old = message_item()
        latest = {**old, "messageRevision": 1, "editedText": None, "editedAt": CREATED_AT, "deletedAt": CREATED_AT}
        index._ddb_client = FakeDdb({(old["pk"], old["sk"]): latest, (old["pk"], "META"): {"deliveryKey": DELIVERY_KEY}})
        with patch.object(index, "_publish") as publish, patch.object(index, "_notify_members") as notify:
            index.process_record(stream_record(old))
            self.assertEqual(publish.call_args.args[1]["type"], "room.message.updated")
            self.assertNotIn("hello", json.dumps(publish.call_args.args[1]))
            notify.assert_not_called()

    def setUp(self):
        self.old_table = index.MESSAGING_TABLE
        self.old_endpoint = index.HTTP_ENDPOINT
        self.old_region = index.REGION
        self.old_ddb = index._ddb_client
        index.MESSAGING_TABLE = "lightbulb-test-messaging"
        index.HTTP_ENDPOINT = "https://example.appsync-api.us-east-1.amazonaws.com/event"
        index.REGION = "us-east-1"
        index._ddb_client = FakeDdb()

    def tearDown(self):
        index.MESSAGING_TABLE = self.old_table
        index.HTTP_ENDPOINT = self.old_endpoint
        index.REGION = self.old_region
        index._ddb_client = self.old_ddb

    @patch("index._publish")
    def test_ignores_removal_and_nonprojection_records(self, publish):
        index.process_record({"eventName": "REMOVE"})
        unchanged = stream_record({**message_item(), "reactionRevision": 1})
        unchanged["eventName"] = "MODIFY"
        index.process_record(unchanged)
        index.process_record(stream_record({"pk": "ROOM#x", "sk": "WORK#MESSAGE#1"}))
        publish.assert_not_called()
        self.assertEqual(index._ddb_client.calls, [])

    @patch("index._publish")
    def test_room_projection_strong_reads_and_uses_current_delivery_key(self, publish):
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID}})
        index.process_record(stream_record(message_item()))

        self.assertEqual(publish.call_args.args[0], f"/rooms/{ROOM_ID}/{DELIVERY_KEY}")
        event = publish.call_args.args[1]
        self.assertEqual(event["type"], "room.message.created")
        self.assertEqual(event["eventId"], MESSAGE_ID)
        self.assertEqual(event["payload"]["messageSeq"], 1)
        self.assertNotIn("deliveryKey", event["payload"])
        self.assertTrue(index._ddb_client.calls[0]["ConsistentRead"])

    @patch("index._publish")
    def test_private_message_hint_targets_and_rechecks_access_without_content(self, publish):
        recipient = "user_bob"
        member = {"id": recipient, "ownerId": OWNER_ID, "joinedSeq": 0}
        items = {
            (f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "dm"},
            (f"ROOM#{ROOM_ID}", f"MEMBER#human#{recipient}"): member,
            (f"OWNER#{OWNER_ID}", f"USERSTREAM#{recipient}"): {"active": True, "channelKey": CHANNEL_KEY},
            (f"STREAM#{CHANNEL_KEY}", "META"): {"active": True, "ownerId": OWNER_ID, "userId": recipient},
        }
        for kind, mentioned, thread, expected in [
            ("dm", False, False, True), ("group-dm", False, False, True),
            ("channel", False, False, False), ("channel", True, False, True),
            ("channel", False, True, True),
        ]:
            items[(f"ROOM#{ROOM_ID}", "META")]["kind"] = kind
            message = message_item(sequence=2)
            message["mentions"] = [{"kind": "human", "id": recipient}] if mentioned else []
            root = message_item(message_id=EVENT_ID)
            root["author"]["id"] = recipient
            items[(f"ROOM#{ROOM_ID}", root["sk"])] = root
            message["threadRootMessageId"] = EVENT_ID if thread else None
            index._ddb_client = FakeDdb(items)
            publish.reset_mock()
            index._project_message(message)
            channel, event = publish.call_args_list[0].args
            self.assertEqual(channel, f"/users/{CHANNEL_KEY}")
            self.assertEqual(event["payload"]["notify"], expected)
            self.assertNotEqual(event["eventId"], message["messageId"])
            self.assertNotIn("hello", json.dumps(event))
            self.assertNotIn("author", event["payload"])
            self.assertTrue(all(call["ConsistentRead"] for call in index._ddb_client.calls))

        # A removed/rejoined member must not receive an earlier message.
        message["author"]["id"] = recipient
        index._project_message(message)
        self.assertFalse(publish.call_args_list[-2].args[1]["payload"]["notify"])
        message["author"]["id"] = USER_ID
        items[(f"ROOM#{ROOM_ID}", f"READ#human#{recipient}")] = {"lastReadSeq": 2}
        index._project_message(message)
        self.assertFalse(publish.call_args_list[-2].args[1]["payload"]["notify"])
        member["joinedSeq"] = 2
        publish.reset_mock()
        index._project_message(message)
        self.assertEqual(publish.call_count, 1)
        member["joinedSeq"] = 0
        items[(f"STREAM#{CHANNEL_KEY}", "META")]["active"] = False
        publish.reset_mock()
        index._project_message(message)
        self.assertEqual(publish.call_count, 1)

    @patch("index._publish")
    def test_active_user_stream_projects_stored_frozen_event(self, publish):
        index._ddb_client = FakeDdb(
            {
                (f"STREAM#{CHANNEL_KEY}", "META"): {
                    "ownerId": OWNER_ID,
                    "userId": USER_ID,
                    "active": True,
                    "channelKey": CHANNEL_KEY,
                }
            }
        )
        expected = live_event()
        index.process_record(stream_record(live_item()))
        publish.assert_called_once_with(f"/users/{CHANNEL_KEY}", expected)
        self.assertTrue(index._ddb_client.calls[0]["ConsistentRead"])

    @patch("index._publish")
    def test_inactive_missing_or_replaced_user_stream_is_a_successful_noop(self, publish):
        mappings = [
            None,
            {"ownerId": OWNER_ID, "userId": USER_ID, "active": False},
            {"ownerId": OWNER_ID, "userId": "other", "active": True},
            {"ownerId": "other", "userId": USER_ID, "active": True},
            {
                "ownerId": OWNER_ID,
                "userId": USER_ID,
                "active": True,
                "channelKey": OLD_DELIVERY_KEY,
            },
        ]
        for mapping in mappings:
            with self.subTest(mapping=mapping):
                index._ddb_client = FakeDdb({(f"STREAM#{CHANNEL_KEY}", "META"): mapping} if mapping else {})
                index.process_record(stream_record(live_item()))
        publish.assert_not_called()

    def test_sigv4_request_has_exact_publish_shape(self):
        credentials = Credentials("AKID", "SECRET", "TOKEN")
        with patch("index._current_credentials", return_value=credentials):
            request = index._signed_request(f"/users/{CHANNEL_KEY}", live_event())
        prepared = request.prepare()
        body = json.loads(prepared.body)
        self.assertEqual(prepared.method, "POST")
        self.assertEqual(prepared.url, index.HTTP_ENDPOINT)
        self.assertEqual(body["channel"], f"/users/{CHANNEL_KEY}")
        self.assertEqual(json.loads(body["events"][0]), live_event())
        self.assertTrue(prepared.headers["Authorization"].startswith("AWS4-HMAC-SHA256"))
        self.assertEqual(prepared.headers["X-Amz-Security-Token"], "TOKEN")

    def test_http_200_with_per_event_failure_is_not_success(self):
        response = json.dumps({"successful": [], "failed": [{"index": 0, "errorType": "Denied"}]}).encode()
        with (
            patch("index._signed_request", return_value=MagicMock()),
            patch("index._send_request", return_value=(200, response)),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                index._publish(f"/users/{CHANNEL_KEY}", live_event())

    @patch("index._publish")
    def test_partial_batch_reports_only_bad_record_and_replayed_success_is_safe(self, publish):
        second_id = "77777777-7777-4777-8777-777777777777"
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID}})
        good_one = stream_record(message_item(), "10")
        bad = {"eventID": "bad", "eventName": "INSERT", "dynamodb": {"SequenceNumber": "11"}}
        good_two = stream_record(message_item(second_id, 2), "12")

        result = index.handler({"Records": [good_one, bad, good_two]}, object())
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "11"}]})
        self.assertEqual([call.args[1]["eventId"] for call in publish.call_args_list], [MESSAGE_ID, second_id])

        index.handler({"Records": [good_two]}, object())
        self.assertEqual(publish.call_args.args[1]["eventId"], second_id)
        self.assertEqual(publish.call_count, 3)

    def test_payload_over_230_kib_is_rejected_before_http(self):
        oversized = live_event()
        oversized["payload"] = {"text": "x" * (index.MAX_EVENT_BYTES + 1)}
        with self.assertRaisesRegex(ValueError, "230 KiB"):
            index._event_json(oversized)

    def test_malformed_insert_is_returned_as_partial_failure(self):
        record = {
            "eventID": "bad-event",
            "eventName": "INSERT",
            "dynamodb": {"SequenceNumber": "900"},
        }
        self.assertEqual(
            index.handler({"Records": [record]}, object()),
            {"batchItemFailures": [{"itemIdentifier": "900"}]},
        )


def push_world(kind: str = "channel", muted: tuple[str, ...] = (), tokens: dict | None = None) -> dict:
    """Room of Alice (author) + Bob + Carol, each an active stream with one device."""
    keys = {USER_ID: CHANNEL_KEY, BOB_ID: BOB_KEY, CAROL_ID: CAROL_KEY}
    if tokens is None:
        tokens = {USER_ID: ["tok-alice"], BOB_ID: ["tok-bob"], CAROL_ID: ["tok-carol"]}
    items = {
        (f"ROOM#{ROOM_ID}", "META"): {
            "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": kind, "name": "general",
        },
    }
    for user, key in keys.items():
        member = {"id": user, "ownerId": OWNER_ID, "joinedSeq": 0}
        if user in muted:
            member["muted"] = True
        items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{user}")] = member
        items[(f"OWNER#{OWNER_ID}", f"USERSTREAM#{user}")] = {"active": True, "channelKey": key}
        items[(f"STREAM#{key}", "META")] = {"active": True, "ownerId": OWNER_ID, "userId": user}
        devices = tokens.get(user) or []
        if devices:
            items[(user, "USER")] = {
                "user_id": user,
                "push_tokens": {
                    f"device-{n}": {"token": token, "platform": "ios", "updatedAt": CREATED_AT}
                    for n, token in enumerate(devices)
                },
            }
    return items


def push_message(text: str = "hello") -> dict:
    message = message_item(sequence=2)
    message["text"] = text
    message["mentions"] = [{"kind": "human", "id": BOB_ID}, {"kind": "human", "id": CAROL_ID}]
    return message


class PushTests(unittest.TestCase):
    def setUp(self):
        self.saved = (index.MESSAGING_TABLE, index.USERS_TABLE, index._ddb_client)
        index.MESSAGING_TABLE = "lightbulb-test-messaging"
        index.USERS_TABLE = USERS_TABLE
        index._ddb_client = FakeDdb()

    def tearDown(self):
        index.MESSAGING_TABLE, index.USERS_TABLE, index._ddb_client = self.saved

    def project(self, items: dict, message: dict | None = None):
        index._ddb_client = FakeDdb(items)
        with (
            patch.object(index, "_publish") as publish,
            patch("index.urllib.request.urlopen") as urlopen,
        ):
            index._project_message(message or push_message())
        return publish, urlopen

    def test_notified_members_get_one_expo_post_with_every_token(self):
        publish, urlopen = self.project(push_world())
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, index.EXPO_PUSH_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)
        messages = json.loads(request.data)
        self.assertEqual(sorted(m["to"] for m in messages), ["tok-bob", "tok-carol"])
        self.assertTrue(all(m["title"] == "Alice in #general" for m in messages))
        self.assertTrue(all(m["sound"] == "default" for m in messages))
        self.assertEqual(messages[0]["body"], "hello")
        self.assertEqual(messages[0]["data"], {
            "roomId": ROOM_ID, "messageId": MESSAGE_ID, "messageSeq": 2, "threadRootMessageId": None,
        })
        self.assertEqual(publish.call_count, 4)
        users_reads = [c for c in index._ddb_client.calls if c.get("TableName") == USERS_TABLE]
        self.assertEqual(len(users_reads), 2)
        self.assertTrue(all(read["ConsistentRead"] for read in users_reads))

    def test_muted_member_collects_no_token_and_dm_title_is_the_author(self):
        _, urlopen = self.project(push_world(kind="dm", muted=(BOB_ID,)))
        messages = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual([m["to"] for m in messages], ["tok-carol"])
        self.assertEqual(messages[0]["title"], "Alice")

    def test_author_token_is_never_collected(self):
        # A DM notifies every non-author member, so the author clause is the
        # only thing that can keep Alice's token out.
        _, urlopen = self.project(push_world(kind="dm"))
        self.assertNotIn("tok-alice", urlopen.call_args.args[0].data.decode())

    def test_empty_text_bodies_as_an_image_and_long_text_is_truncated(self):
        for blank in ("", "   "):
            _, urlopen = self.project(push_world(), push_message(blank))
            self.assertEqual(json.loads(urlopen.call_args.args[0].data)[0]["body"], "Sent an image")
        _, urlopen = self.project(push_world(), push_message("x" * 500))
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)[0]["body"], "x" * 180)

    def test_members_without_push_tokens_are_skipped(self):
        _, urlopen = self.project(push_world(tokens={BOB_ID: ["tok-bob"]}))
        self.assertEqual([m["to"] for m in json.loads(urlopen.call_args.args[0].data)], ["tok-bob"])
        publish, urlopen = self.project(push_world(tokens={}))
        urlopen.assert_not_called()
        self.assertEqual(publish.call_count, 4)

    def test_expo_failure_leaves_the_record_successful_and_publishes_intact(self):
        index._ddb_client = FakeDdb(push_world())
        with (
            patch.object(index, "_publish") as publish,
            patch("index.urllib.request.urlopen", side_effect=OSError("boom")),
            self.assertLogs(level="WARNING") as logs,
        ):
            result = index.handler({"Records": [stream_record(push_message(), "500")]}, object())
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(publish.call_count, 4)
        self.assertNotIn("tok-bob", "".join(logs.output))
        self.assertNotIn("hello", "".join(logs.output))

    def test_failed_room_publish_sends_no_push(self):
        # The record replays on failure and Expo has no eventId dedupe, so a
        # push before the room publish would banner everyone twice.
        index._ddb_client = FakeDdb(push_world())
        with (
            patch.object(index, "_publish", side_effect=[None, None, None, RuntimeError("throttled")]),
            patch("index.urllib.request.urlopen") as urlopen,
        ):
            result = index.handler({"Records": [stream_record(push_message(), "501")]}, object())
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "501"}]})
        urlopen.assert_not_called()

    def test_nameless_channel_titles_with_the_author_alone(self):
        world = push_world()
        world[(f"ROOM#{ROOM_ID}", "META")]["name"] = None
        _, urlopen = self.project(world)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)[0]["title"], "Alice")

    def test_more_than_one_hundred_tokens_sends_one_hundred_and_warns(self):
        world = push_world(tokens={BOB_ID: [f"tok-{n}" for n in range(101)]})
        index._ddb_client = FakeDdb(world)
        with (
            patch.object(index, "_publish"),
            patch("index.urllib.request.urlopen") as urlopen,
            self.assertLogs(level="WARNING") as logs,
        ):
            index._project_message(push_message())
        self.assertEqual(len(json.loads(urlopen.call_args.args[0].data)), 100)
        self.assertTrue(any("101" in line for line in logs.output))


class ReplayTests(unittest.TestCase):
    def test_replay_reads_one_s3_object_and_reports_ids_only(self):
        record = stream_record(message_item(), "321")
        body = MagicMock()
        body.read.return_value = json.dumps({"requestPayload": {"Records": [record]}}).encode()
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": body}
        with patch("replay.process_record") as process:
            results, failed = replay.replay_uri("s3://failure-bucket/object.json", s3_client=s3)
        process.assert_called_once_with(record)
        self.assertEqual(results, [{"recordId": "321", "status": "success"}])
        self.assertFalse(failed)
        self.assertFalse(hasattr(s3, "delete_object") and s3.delete_object.called)

    def test_replay_reads_lambda_s3_failure_envelope(self):
        record = stream_record(message_item(), "322")
        document = {
            "version": "1.0",
            "timestamp": "2026-09-10T00:00:00.000Z",
            "requestContext": {
                "requestId": "request-id",
                "functionArn": "arn:aws:lambda:us-east-1:123456789012:function:projector",
                "condition": "RetryAttemptsExhausted",
                "approximateInvokeCount": 4,
            },
            "responseContext": {
                "statusCode": 200,
                "executedVersion": "$LATEST",
                "functionError": "Unhandled",
            },
            "payload": json.dumps({"Records": [record]}),
        }
        self.assertEqual(replay.records_from_failure(document), [record])

    def test_replay_reports_failure_without_exposing_record(self):
        record = stream_record(message_item(), "654")
        body = MagicMock()
        body.read.return_value = json.dumps({"Records": [record]}).encode()
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": body}
        with patch("replay.process_record", side_effect=RuntimeError("secret body")):
            results, failed = replay.replay_uri("s3://failure-bucket/object.json", s3_client=s3)
        self.assertEqual(results, [{"recordId": "654", "status": "failure"}])
        self.assertTrue(failed)
        self.assertNotIn("secret body", json.dumps(results))

    def test_replay_rejects_non_s3_uri(self):
        with self.assertRaises(ValueError):
            replay.parse_s3_uri("https://example.com/failure.json")


if __name__ == "__main__":
    unittest.main()
