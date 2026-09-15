from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from boto3.dynamodb.types import TypeSerializer
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

import index
import replay

ROOM_ID = "11111111-1111-4111-8111-111111111111"
MESSAGE_ID = "22222222-2222-4222-8222-222222222222"
DELIVERY_KEY = "33333333-3333-4333-8333-333333333333"
OLD_DELIVERY_KEY = "44444444-4444-4444-8444-444444444444"
CHANNEL_KEY = "55555555-5555-4555-8555-555555555555"
EVENT_ID = "66666666-6666-4666-8666-666666666666"
ATTACHMENT_ID = "77777777-7777-4777-8777-777777777777"
WORKSPACE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
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

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        key = kwargs["Key"]
        lookup = (key["pk"]["S"], key["sk"]["S"])
        item = self.items.setdefault(lookup, {})
        if kwargs.get("ConditionExpression") == "attribute_not_exists(workspaceId)" and "workspaceId" in item:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already set"}},
                "UpdateItem",
            )
        item["workspaceId"] = kwargs["ExpressionAttributeValues"][":ws"]["S"]


class ProjectorTests(unittest.TestCase):
    def test_edit_and_delete_publish_invalidation_without_content_or_notifications(self):
        for deleted in (None, CREATED_AT):
            old = message_item()
            updated = {**old, "messageRevision": 1, "editedText": "changed", "editedAt": CREATED_AT, "deletedAt": deleted}
            index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "workspaceId": WORKSPACE_ID}})
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
        index._ddb_client = FakeDdb({(old["pk"], old["sk"]): latest, (old["pk"], "META"): {"deliveryKey": DELIVERY_KEY, "workspaceId": WORKSPACE_ID}})
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
    def test_local_session_rows_never_become_room_or_private_user_projections(self, publish):
        for pk, sk in [
            ("LOCALUSER#org#human", "DEVICE#mac"), ("LOCALUSER#org#human", "SESSION#id"),
            ("LOCALSESSION#org#id", "META"), ("LOCALSESSION#org#id", "EVENT#00000000000000000001"),
            ("LOCALSESSION#org#id", "COMMAND#request"),
        ]:
            index.process_record(stream_record({"pk": pk, "sk": sk, "text": "private transcript"}))
        publish.assert_not_called()
        self.assertEqual(index._ddb_client.calls, [])

    @patch("index._publish")
    def test_room_projection_strong_reads_and_uses_current_delivery_key(self, publish):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        index.process_record(stream_record(message_item()))

        self.assertEqual(publish.call_args.args[0], f"/ws/{WORKSPACE_ID}/rooms/{ROOM_ID}/{DELIVERY_KEY}")
        event = publish.call_args.args[1]
        self.assertEqual(event["type"], "room.message.created")
        self.assertEqual(event["eventId"], MESSAGE_ID)
        self.assertEqual(event["payload"]["messageSeq"], 1)
        self.assertNotIn("deliveryKey", event["payload"])
        self.assertTrue(index._ddb_client.calls[0]["ConsistentRead"])

    @patch("index._publish")
    def test_image_message_publishes_its_attachments(self, publish):
        # The live payload is what a client renders from. Dropping `attachments`
        # here made every image message arrive with none: the shared guard
        # allows `attachments === undefined`, so the event validated, merged
        # over the HTTP copy that DID carry them, and the thumbnail vanished.
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        item = message_item()
        item["text"] = ""
        item["attachments"] = [
            {"id": ATTACHMENT_ID, "fileName": "Screenshot.png", "contentType": "image/png", "size": 58573}
        ]
        index.process_record(stream_record(item))

        self.assertEqual(publish.call_args.args[1]["payload"]["attachments"], item["attachments"])

    @patch("index._publish")
    def test_message_without_attachments_omits_the_key(self, publish):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        index.process_record(stream_record(message_item()))
        self.assertNotIn("attachments", publish.call_args.args[1]["payload"])

    @patch("index._publish")
    def test_malformed_attachment_fails_the_record_and_publishes_nothing(self, publish):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        item = message_item()
        item["attachments"] = [{"id": ATTACHMENT_ID, "fileName": "x.png", "contentType": "image/png", "size": 0}]
        result = index.handler({"Records": [stream_record(item, "42")]}, object())
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "42"}]})
        publish.assert_not_called()

    @patch("index._publish")
    def test_room_meta_without_workspace_id_heals_from_owner_row_and_stamps(self, publish):
        index._ddb_client = FakeDdb({
            (f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID},
            (f"OWNER#{OWNER_ID}", "WORKSPACE"): {"workspaceId": WORKSPACE_ID},
        })
        result = index.handler({"Records": [stream_record(message_item(), "42")]}, object())
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(publish.call_args.args[0], f"/ws/{WORKSPACE_ID}/rooms/{ROOM_ID}/{DELIVERY_KEY}")
        self.assertEqual(index._ddb_client.items[(f"ROOM#{ROOM_ID}", "META")]["workspaceId"], WORKSPACE_ID)

    @patch("index._publish")
    def test_room_meta_without_workspace_id_and_no_owner_row_is_skipped_not_failed(self, publish):
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID}})
        with self.assertLogs(level="WARNING"):
            result = index.handler({"Records": [stream_record(message_item(), "42")]}, object())
        self.assertEqual(result, {"batchItemFailures": []})
        publish.assert_not_called()

    @patch("index._publish")
    def test_stream_meta_without_workspace_id_and_no_owner_row_skips_live_event(self, publish):
        index._ddb_client = FakeDdb(
            {(f"STREAM#{CHANNEL_KEY}", "META"): {
                "ownerId": OWNER_ID, "userId": USER_ID, "active": True, "channelKey": CHANNEL_KEY,
            }}
        )
        with self.assertLogs(level="WARNING"):
            result = index.handler({"Records": [stream_record(live_item(), "43")]}, object())
        self.assertEqual(result, {"batchItemFailures": []})
        publish.assert_not_called()

    @patch("index._publish")
    def test_private_message_hint_targets_and_rechecks_access_without_content(self, publish):
        recipient = "user_bob"
        member = {"id": recipient, "ownerId": OWNER_ID, "joinedSeq": 0}
        items = {
            (f"ROOM#{ROOM_ID}", "META"): {
                "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "dm", "workspaceId": WORKSPACE_ID,
            },
            (f"ROOM#{ROOM_ID}", f"MEMBER#human#{recipient}"): member,
            (f"OWNER#{OWNER_ID}", f"USERSTREAM#{recipient}"): {"active": True, "channelKey": CHANNEL_KEY},
            (f"STREAM#{CHANNEL_KEY}", "META"): {
                "active": True, "ownerId": OWNER_ID, "userId": recipient, "workspaceId": WORKSPACE_ID,
            },
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
            self.assertEqual(channel, f"/ws/{WORKSPACE_ID}/users/{CHANNEL_KEY}")
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
    def test_foreign_member_gets_the_hint_on_their_home_stream(self, publish):
        vendor = "org_vendor"
        meta = {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "dm", "workspaceId": WORKSPACE_ID}
        items = {
            (f"ROOM#{ROOM_ID}", "META"): meta,
            (f"ROOM#{ROOM_ID}", f"MEMBER#human#{BOB_ID}"): {"id": BOB_ID, "ownerId": OWNER_ID, "homeOwnerId": vendor, "joinedSeq": 0},
            (f"OWNER#{vendor}", f"USERSTREAM#{BOB_ID}"): {"active": True, "channelKey": BOB_KEY},
            (f"STREAM#{BOB_KEY}", "META"): {"active": True, "ownerId": vendor, "userId": BOB_ID, "workspaceId": WORKSPACE_ID},
        }
        index._ddb_client = FakeDdb(items)
        index._notify_members(message_item(), meta)
        self.assertEqual([c.args[0] for c in publish.call_args_list], [f"/ws/{WORKSPACE_ID}/users/{BOB_KEY}"])
        self.assertTrue(publish.call_args_list[0].args[1]["payload"]["notify"])

    @patch("index._publish")
    def test_active_user_stream_projects_stored_frozen_event(self, publish):
        index._ddb_client = FakeDdb(
            {
                (f"STREAM#{CHANNEL_KEY}", "META"): {
                    "ownerId": OWNER_ID,
                    "userId": USER_ID,
                    "active": True,
                    "channelKey": CHANNEL_KEY,
                    "workspaceId": WORKSPACE_ID,
                }
            }
        )
        expected = live_event()
        index.process_record(stream_record(live_item()))
        publish.assert_called_once_with(f"/ws/{WORKSPACE_ID}/users/{CHANNEL_KEY}", expected)
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
                "workspaceId": WORKSPACE_ID,
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
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
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


class WorkspaceHealTests(unittest.TestCase):
    """Direct unit coverage of the shared heal helper all four projector call
    sites route through — see the ProjectorTests/PushTests integration cases
    above for how a poisoned META no longer wedges the shard."""

    def setUp(self):
        self.saved = (index.MESSAGING_TABLE, index._ddb_client)
        index.MESSAGING_TABLE = "lightbulb-test-messaging"

    def tearDown(self):
        index.MESSAGING_TABLE, index._ddb_client = self.saved

    def test_present_and_valid_short_circuits_with_no_ddb_write(self):
        index._ddb_client = FakeDdb()
        meta = {"workspaceId": WORKSPACE_ID}
        result = index._workspace_id_for_meta(f"ROOM#{ROOM_ID}", "META", meta)
        self.assertEqual(result, WORKSPACE_ID)
        self.assertEqual(index._ddb_client.calls, [])

    def test_missing_heals_from_owner_row_and_stamps_conditionally(self):
        index._ddb_client = FakeDdb({(f"OWNER#{OWNER_ID}", "WORKSPACE"): {"workspaceId": WORKSPACE_ID}})
        meta = {"ownerId": OWNER_ID}
        result = index._workspace_id_for_meta(f"ROOM#{ROOM_ID}", "META", meta)
        self.assertEqual(result, WORKSPACE_ID)
        self.assertEqual(meta["workspaceId"], WORKSPACE_ID)
        updates = [c for c in index._ddb_client.calls if "UpdateExpression" in c]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["ConditionExpression"], "attribute_not_exists(workspaceId)")

    def test_missing_heals_survive_a_concurrent_stamp_race(self):
        ddb = FakeDdb({(f"OWNER#{OWNER_ID}", "WORKSPACE"): {"workspaceId": WORKSPACE_ID}})
        ddb.items[(f"ROOM#{ROOM_ID}", "META")] = {"workspaceId": WORKSPACE_ID}  # another writer already stamped it
        index._ddb_client = ddb
        meta = {"ownerId": OWNER_ID}
        result = index._workspace_id_for_meta(f"ROOM#{ROOM_ID}", "META", meta)
        self.assertEqual(result, WORKSPACE_ID)

    def test_no_owner_row_is_unresolvable(self):
        index._ddb_client = FakeDdb()
        meta = {"ownerId": OWNER_ID}
        with self.assertLogs(level="WARNING"):
            result = index._workspace_id_for_meta(f"ROOM#{ROOM_ID}", "META", meta)
        self.assertIsNone(result)

    def test_owner_row_with_malformed_workspace_id_is_unresolvable(self):
        index._ddb_client = FakeDdb({(f"OWNER#{OWNER_ID}", "WORKSPACE"): {"workspaceId": "not-a-uuid"}})
        meta = {"ownerId": OWNER_ID}
        result = index._workspace_id_for_meta(f"ROOM#{ROOM_ID}", "META", meta)
        self.assertIsNone(result)


def push_world(kind: str = "channel", muted: tuple[str, ...] = (), tokens: dict | None = None) -> dict:
    """Room of Alice (author) + Bob + Carol, each an active stream with one device."""
    keys = {USER_ID: CHANNEL_KEY, BOB_ID: BOB_KEY, CAROL_ID: CAROL_KEY}
    if tokens is None:
        tokens = {USER_ID: ["tok-alice"], BOB_ID: ["tok-bob"], CAROL_ID: ["tok-carol"]}
    items = {
        (f"ROOM#{ROOM_ID}", "META"): {
            "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": kind, "name": "general",
            "workspaceId": WORKSPACE_ID,
        },
    }
    for user, key in keys.items():
        member = {"id": user, "ownerId": OWNER_ID, "joinedSeq": 0}
        if user in muted:
            member["muted"] = True
        items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{user}")] = member
        items[(f"OWNER#{OWNER_ID}", f"USERSTREAM#{user}")] = {"active": True, "channelKey": key}
        items[(f"STREAM#{key}", "META")] = {
            "active": True, "ownerId": OWNER_ID, "userId": user, "workspaceId": WORKSPACE_ID,
        }
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


def push_message(text: str = "hello", mentions: tuple[str, ...] = (BOB_ID, CAROL_ID)) -> dict:
    message = message_item(sequence=2)
    message["text"] = text
    message["mentions"] = [{"kind": "human", "id": user} for user in mentions]
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
        self.assertTrue(all(m["title"] == "Alice mentioned you in #general" for m in messages))
        self.assertTrue(all(m["sound"] == "default" for m in messages))
        # APNs thread-id: one conversation groups into one stack on iOS.
        self.assertTrue(all(m["threadId"] == ROOM_ID for m in messages))
        self.assertEqual(messages[0]["body"], "hello")
        self.assertEqual(messages[0]["data"], {
            "roomId": ROOM_ID, "messageId": MESSAGE_ID, "messageSeq": 2, "threadRootMessageId": None,
        })
        self.assertEqual(publish.call_count, 4)
        users_reads = [c for c in index._ddb_client.calls if c.get("TableName") == USERS_TABLE]
        self.assertEqual(len(users_reads), 2)
        self.assertTrue(all(read["ConsistentRead"] for read in users_reads))

    def test_room_meta_heals_workspace_id_from_owner_row_and_push_still_happens(self):
        world = push_world()
        del world[(f"ROOM#{ROOM_ID}", "META")]["workspaceId"]
        world[(f"OWNER#{OWNER_ID}", "WORKSPACE")] = {"workspaceId": WORKSPACE_ID}
        publish, urlopen = self.project(world)
        self.assertEqual(publish.call_args_list[-1].args[0], f"/ws/{WORKSPACE_ID}/rooms/{ROOM_ID}/{DELIVERY_KEY}")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(index._ddb_client.items[(f"ROOM#{ROOM_ID}", "META")]["workspaceId"], WORKSPACE_ID)

    def test_one_recipients_unresolvable_stream_meta_is_skipped_not_fatal(self):
        world = push_world()
        del world[(f"STREAM#{BOB_KEY}", "META")]["workspaceId"]
        publish, urlopen = self.project(world)
        channels = [call.args[0] for call in publish.call_args_list]
        self.assertNotIn(f"/ws/{WORKSPACE_ID}/users/{BOB_KEY}", channels)
        self.assertIn(f"/ws/{WORKSPACE_ID}/users/{CAROL_KEY}", channels)
        self.assertEqual(publish.call_args_list[-1].args[0], f"/ws/{WORKSPACE_ID}/rooms/{ROOM_ID}/{DELIVERY_KEY}")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(sorted(m["to"] for m in json.loads(urlopen.call_args.args[0].data)), ["tok-carol"])

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

    def test_nameless_channel_drops_the_room_but_keeps_the_mention(self):
        world = push_world()
        world[(f"ROOM#{ROOM_ID}", "META")]["name"] = None
        _, urlopen = self.project(world)
        self.assertEqual(
            json.loads(urlopen.call_args.args[0].data)[0]["title"], "Alice mentioned you"
        )

    def test_group_dm_names_the_room_and_titles_each_reader_separately(self):
        # A group DM notifies every member, so one message carries both titles:
        # Bob was @mentioned, Carol was not. The "#" is a channel's, not a
        # group DM's.
        _, urlopen = self.project(
            push_world(kind="group-dm"), push_message(mentions=(BOB_ID,))
        )
        titles = {m["to"]: m["title"] for m in json.loads(urlopen.call_args.args[0].data)}
        self.assertEqual(
            titles,
            {"tok-bob": "Alice mentioned you in general", "tok-carol": "Alice in general"},
        )

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


def capture_stdout(fn, *args, **kwargs) -> tuple[object, list[dict]]:
    """Run fn, returning its result plus every printed line parsed as JSON."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def metric_lines(lines: list[dict], name: str) -> list[dict]:
    return [line for line in lines if name in line]


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.saved = (index.MESSAGING_TABLE, index.USERS_TABLE, index._ddb_client)
        index.MESSAGING_TABLE = "lightbulb-test-messaging"
        index.USERS_TABLE = USERS_TABLE
        index._ddb_client = FakeDdb()

    def tearDown(self):
        index.MESSAGING_TABLE, index.USERS_TABLE, index._ddb_client = self.saved

    def test_emit_metric_prints_one_well_formed_emf_line(self):
        _, lines = capture_stdout(
            index._emit_metric, "messaging.projector.push", 1, "Count", {"outcome": "ok"}
        )
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["messaging.projector.push"], 1)
        self.assertEqual(line["env"], "test")
        self.assertEqual(line["service"], "messaging-projector")
        self.assertEqual(line["outcome"], "ok")
        self.assertIsInstance(line["_aws"]["Timestamp"], int)
        metric = line["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(metric["Namespace"], "LightbulbAI")
        self.assertEqual(metric["Metrics"], [{"Name": "messaging.projector.push", "Unit": "Count"}])
        self.assertEqual(set(metric["Dimensions"][0]), {"env", "service", "outcome"})

    def test_push_ok_emits_once_per_successful_expo_batch(self):
        index._ddb_client = FakeDdb(push_world())
        with patch.object(index, "_publish"), patch("index.urllib.request.urlopen"):
            _, lines = capture_stdout(index._project_message, push_message())
        outcomes = [line["outcome"] for line in metric_lines(lines, "messaging.projector.push")]
        self.assertEqual(outcomes.count("ok"), 1)

    def test_push_send_error_outcome_on_swallowed_exception(self):
        index._ddb_client = FakeDdb(push_world())
        with (
            patch.object(index, "_publish"),
            patch("index.urllib.request.urlopen", side_effect=OSError("boom")),
        ):
            _, lines = capture_stdout(index._project_message, push_message())
        outcomes = [line["outcome"] for line in metric_lines(lines, "messaging.projector.push")]
        self.assertIn("send_error", outcomes)
        self.assertNotIn("ok", outcomes)

    def test_push_capped_outcome_when_over_the_token_limit(self):
        world = push_world(tokens={BOB_ID: [f"tok-{n}" for n in range(101)]})
        index._ddb_client = FakeDdb(world)
        with patch.object(index, "_publish"), patch("index.urllib.request.urlopen"):
            _, lines = capture_stdout(index._project_message, push_message())
        outcomes = [line["outcome"] for line in metric_lines(lines, "messaging.projector.push")]
        self.assertIn("capped", outcomes)
        self.assertIn("ok", outcomes)

    def test_push_muted_outcome_and_muted_member_gets_no_expo_send(self):
        index._ddb_client = FakeDdb(push_world(muted=(BOB_ID,)))
        with patch.object(index, "_publish"), patch("index.urllib.request.urlopen") as urlopen:
            _, lines = capture_stdout(index._project_message, push_message())
        outcomes = [line["outcome"] for line in metric_lines(lines, "messaging.projector.push")]
        self.assertIn("muted", outcomes)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual([m["to"] for m in sent], ["tok-carol"])

    def test_token_read_error_outcome_on_users_table_fault(self):
        class RaisingDdb(FakeDdb):
            def get_item(self, **kwargs):
                if "user_id" in kwargs["Key"]:
                    raise RuntimeError("boom")
                return super().get_item(**kwargs)

        index._ddb_client = RaisingDdb()
        with self.assertLogs(level="WARNING"):
            tokens, lines = capture_stdout(index._push_tokens, USER_ID)
        self.assertEqual(tokens, [])
        outcomes = [line["outcome"] for line in metric_lines(lines, "messaging.projector.push")]
        self.assertEqual(outcomes, ["token_read_error"])

    def test_notify_latency_uses_elapsed_time_and_room_kind_dimension(self):
        index._ddb_client = FakeDdb(
            {
                (f"ROOM#{ROOM_ID}", "META"): {
                    "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "channel", "workspaceId": WORKSPACE_ID,
                }
            }
        )
        message = message_item()
        message["createdAt"] = "2020-01-01T00:00:00Z"
        with patch.object(index, "_publish"):
            _, lines = capture_stdout(index._project_message, message)
        latency_lines = metric_lines(lines, "messaging.projector.notify.latency")
        self.assertEqual(len(latency_lines), 1)
        line = latency_lines[0]
        self.assertEqual(line["room_kind"], "channel")
        self.assertGreater(line["messaging.projector.notify.latency"], 0)
        metric = line["_aws"]["CloudWatchMetrics"][0]
        self.assertEqual(
            metric["Metrics"], [{"Name": "messaging.projector.notify.latency", "Unit": "Milliseconds"}]
        )

    def test_notify_latency_not_emitted_on_edit_path(self):
        old = message_item()
        updated = {**old, "messageRevision": 1, "editedText": "changed", "editedAt": CREATED_AT, "deletedAt": None}
        index._ddb_client = FakeDdb(
            {
                (old["pk"], old["sk"]): updated,
                (f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "workspaceId": WORKSPACE_ID},
            }
        )
        with patch.object(index, "_publish"):
            _, lines = capture_stdout(index._project_message, old)
        self.assertEqual(metric_lines(lines, "messaging.projector.notify.latency"), [])

    def test_notify_latency_parses_production_shaped_created_at(self):
        # core.dynamodb.utc_now_iso() writes "+00:00" with microseconds and no
        # "Z" -- confirm _parse_rfc3339 (shared with _require_timestamp) and
        # the latency metric both handle that shape, not just the "Z" one.
        index._ddb_client = FakeDdb(
            {
                (f"ROOM#{ROOM_ID}", "META"): {
                    "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "dm", "workspaceId": WORKSPACE_ID,
                }
            }
        )
        message = message_item()
        message["createdAt"] = "2020-01-01T00:00:00.123456+00:00"
        with patch.object(index, "_publish"):
            _, lines = capture_stdout(index._project_message, message)
        latency_lines = metric_lines(lines, "messaging.projector.notify.latency")
        self.assertEqual(len(latency_lines), 1)
        self.assertGreater(latency_lines[0]["messaging.projector.notify.latency"], 0)

    def test_emit_metric_never_raises_on_a_non_serializable_dimension(self):
        class Unserializable:
            pass

        _, lines = capture_stdout(
            index._emit_metric, "messaging.projector.push", 1, "Count", {"outcome": Unserializable()}
        )
        self.assertEqual(lines, [])


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
