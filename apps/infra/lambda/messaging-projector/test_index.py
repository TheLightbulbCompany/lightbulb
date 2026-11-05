from __future__ import annotations

import contextlib
import io
import json
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
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
_deserializer = TypeDeserializer()


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

    def put_item(self, **kwargs):
        self.calls.append(kwargs)
        item = {key: _deserializer.deserialize(value) for key, value in kwargs["Item"].items()}
        lookup = (item["pk"], item["sk"])
        if kwargs.get("ConditionExpression") == "attribute_not_exists(pk)" and lookup in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem"
            )
        self.items[lookup] = item

    def activity(self) -> dict[str, dict]:
        """``{pk: row}`` for every ACTIVITY# row written."""
        return {pk: row for (pk, _sk), row in self.items.items() if pk.startswith("ACTIVITY#")}


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
    def test_addressed_agent_ids_ride_the_created_payload(self, publish):
        """The live event carries the agents this message addresses, not just
        @mentions -- a name-addressed message ("Hey Friday, ...") has none."""
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        agent_id = "aaaaaaaa-1111-4111-8111-111111111111"
        item = {**message_item(), "mentions": [], "dispatchTargets": [{"agentId": agent_id, "eventKind": "user_request"}]}
        index.process_record(stream_record(item))
        self.assertEqual(publish.call_args.args[1]["payload"]["addressedAgentIds"], [agent_id])

    @patch("index._publish")
    def test_a_message_with_no_dispatch_targets_carries_an_empty_list(self, publish):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        index.process_record(stream_record(message_item()))
        self.assertEqual(publish.call_args.args[1]["payload"]["addressedAgentIds"], [])

    @patch("index._publish")
    def test_a_room_event_candidate_is_not_an_addressee(self, publish):
        """An `active` listener is merely eligible for Jev to route -- Jev picks
        at most one (or none), so a room_event target must not appear here."""
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        agent_id = "aaaaaaaa-1111-4111-8111-111111111111"
        item = {**message_item(), "dispatchTargets": [{"agentId": agent_id, "eventKind": "room_event"}]}
        index.process_record(stream_record(item))
        self.assertEqual(publish.call_args.args[1]["payload"]["addressedAgentIds"], [])

    @patch("index._publish")
    def test_malformed_dispatch_target_fails_the_record_and_publishes_nothing(self, publish):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        item = {**message_item(), "dispatchTargets": [{"eventKind": "user_request"}]}
        with self.assertRaises(ValueError):
            index._project_message(item)
        publish.assert_not_called()

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
    def test_session_reference_rides_the_live_event(self, publish):
        # A session chip is a reference (owner ruling 2026-10-02). The live copy merges over the
        # sender's POST response at the same revision, so a payload without these fields would
        # strip the sender's card and show recipients none until an HTTP reload.
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        session_id = "88888888-8888-4888-8888-888888888888"
        item = message_item()
        item["sessionMentions"] = [session_id]
        item["sessionTargetLabels"] = {session_id: "Codex · Launch check · Owner", "stale-id": "dropped"}
        index.process_record(stream_record(item))

        payload = publish.call_args.args[1]["payload"]
        self.assertEqual(payload["sessionMentions"], [session_id])
        self.assertEqual(payload["sessionTargetLabels"], {session_id: "Codex · Launch check · Owner"})

    @patch("index._publish")
    def test_message_without_session_reference_omits_the_keys(self, publish):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        index.process_record(stream_record(message_item()))
        payload = publish.call_args.args[1]["payload"]
        self.assertNotIn("sessionMentions", payload)
        self.assertNotIn("sessionTargetLabels", payload)

    @patch("index._publish")
    def test_malformed_session_reference_fails_the_record_and_publishes_nothing(self, publish):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        item = message_item()
        item["sessionMentions"] = ["not-a-uuid"]
        with self.assertRaises(ValueError):
            index.process_record(stream_record(item))
        publish.assert_not_called()

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
    def test_private_message_hint_targets_and_rechecks_access_with_only_a_preview(self, publish):
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
            message["threadRootSeq"] = 1 if thread else None
            index._ddb_client = FakeDdb(items)
            publish.reset_mock()
            index._project_message(message)
            channel, event = publish.call_args_list[0].args
            self.assertEqual(channel, f"/ws/{WORKSPACE_ID}/users/{CHANNEL_KEY}")
            self.assertEqual(event["payload"]["notify"], expected)
            self.assertNotEqual(event["eventId"], message["messageId"])
            # Only the <=140-char preview rides the user channel, never the message body.
            self.assertEqual(event["payload"]["lastMessage"]["textPreview"], "hello")
            self.assertNotIn("text", event["payload"])
            # The banner rides only for a reader it notifies.
            push = event["payload"].get("push")
            self.assertEqual(push is not None, expected)
            if push:
                self.assertEqual(push["body"], "hello")
                self.assertEqual(push["data"]["messageId"], message["messageId"])
            self.assertNotIn("author", event["payload"])
            self.assertTrue(all(call["ConsistentRead"] for call in index._ddb_client.calls if "Item" not in call))

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
    def test_parallel_fanout_publishes_one_hint_per_member_and_a_failure_still_fails_the_record(self, publish):
        users = [f"user_{n:02d}" for n in range(12)]
        keys = {user: str(uuid.uuid5(uuid.NAMESPACE_OID, user)) for user in users}
        items = {(f"ROOM#{ROOM_ID}", "META"): {
            "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "channel", "workspaceId": WORKSPACE_ID,
        }}
        for user in users:
            items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{user}")] = {"id": user, "ownerId": OWNER_ID, "joinedSeq": 0}
            items[(f"OWNER#{OWNER_ID}", f"USERSTREAM#{user}")] = {"active": True, "channelKey": keys[user]}
            items[(f"STREAM#{keys[user]}", "META")] = {
                "active": True, "ownerId": OWNER_ID, "userId": user, "workspaceId": WORKSPACE_ID,
            }
        index._ddb_client = FakeDdb(items)
        # A member removed after discovery but before delivery: listed by the query, no row at recheck.
        with patch("index._member_candidates", return_value=users + ["user_removed"]):
            index.process_record(stream_record(message_item()))
        calls = [c.args for c in publish.call_args_list]
        room = f"/ws/{WORKSPACE_ID}/rooms/{ROOM_ID}/{DELIVERY_KEY}"
        self.assertEqual(calls[-1][0], room)  # the room event still goes after every member
        hints = {channel: event for channel, event in calls[:-1]}
        self.assertEqual(set(hints), {f"/ws/{WORKSPACE_ID}/users/{keys[u]}" for u in users})  # nothing for user_removed
        for user in users:
            event = hints[f"/ws/{WORKSPACE_ID}/users/{keys[user]}"]
            self.assertEqual(event["eventId"], str(uuid.uuid5(uuid.UUID(MESSAGE_ID), f"notify:{user}")))
            self.assertEqual(event["payload"]["messageId"], MESSAGE_ID)

        bad = f"/ws/{WORKSPACE_ID}/users/{keys[users[5]]}"
        publish.reset_mock()
        publish.side_effect = lambda channel, _event: (_ for _ in ()).throw(RuntimeError("boom")) if channel == bad else None
        with self.assertRaises(RuntimeError):
            index.process_record(stream_record(message_item()))
        self.assertNotIn(room, [c.args[0] for c in publish.call_args_list])  # no room event, no push
        result = index.handler({"Records": [stream_record(message_item())]}, None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "100"}]})

    def _one_member_room(self, recipient=BOB_ID, **meta):
        items = {
            (f"ROOM#{ROOM_ID}", "META"): {
                "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": "channel", "workspaceId": WORKSPACE_ID,
                **meta,
            },
            (f"ROOM#{ROOM_ID}", f"MEMBER#human#{recipient}"): {"id": recipient, "ownerId": OWNER_ID, "joinedSeq": 0},
            (f"OWNER#{OWNER_ID}", f"USERSTREAM#{recipient}"): {"active": True, "channelKey": BOB_KEY},
            (f"STREAM#{BOB_KEY}", "META"): {
                "active": True, "ownerId": OWNER_ID, "userId": recipient, "workspaceId": WORKSPACE_ID,
            },
        }
        index._ddb_client = FakeDdb(items)
        return items

    @patch("index._publish")
    def test_message_hint_carries_the_preview_and_mentioned_humans(self, publish):
        self._one_member_room()
        message = message_item()
        message["text"] = "**Ship** it @bob " + "z" * 300
        message["mentions"] = [{"kind": "human", "id": BOB_ID}, {"kind": "agent", "id": "friday"},
                               {"kind": "human", "id": CAROL_ID}]
        index._project_message(message)
        channel, event = publish.call_args_list[0].args
        self.assertEqual(channel, f"/ws/{WORKSPACE_ID}/users/{BOB_KEY}")
        payload = event["payload"]
        self.assertEqual(payload["mentionedUserIds"], [BOB_ID, CAROL_ID])
        self.assertNotIn("previewOnly", payload)
        self.assertEqual(payload["lastMessage"], {
            "messageId": MESSAGE_ID, "seq": 1, "authorId": USER_ID, "authorKind": "human",
            "authorName": "Alice", "textPreview": ("Ship it @bob " + "z" * 300)[:140],
            "hasAttachments": False, "createdAt": CREATED_AT, "threadRootMessageId": None,
        })

    @patch("index._publish")
    def test_mentioned_user_ids_never_include_the_author(self, publish):
        # A self-mention must not bump the author's own live mentionCount.
        self._one_member_room()
        message = message_item()
        message["mentions"] = [{"kind": "human", "id": USER_ID}, {"kind": "human", "id": BOB_ID}]
        index._project_message(message)
        self.assertEqual(publish.call_args_list[0].args[1]["payload"]["mentionedUserIds"], [BOB_ID])

    @patch("index._publish")
    def test_message_hint_preview_flags_attachments(self, publish):
        # An attachment-only message still has a preview that says so.
        self._one_member_room()
        message = message_item()
        message["text"] = ""
        message["attachments"] = [{"id": "att-1", "fileName": "report.pdf", "contentType": "application/pdf", "size": 10}]
        index._project_message(message)
        _channel, event = publish.call_args_list[0].args
        self.assertTrue(event["payload"]["lastMessage"]["hasAttachments"])

    @patch("index._publish")
    def test_member_removed_before_publish_receives_nothing(self, publish):
        # Discovery (the MEMBER# query) still lists Bob, but his MEMBER row is gone
        # by the strong per-member read at publish time: no preview reaches him.
        items = self._one_member_room()
        del items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{BOB_ID}")]
        with patch.object(index, "_query_rows", return_value=[{"id": BOB_ID}]):
            index._project_message(message_item())
        channels = [c.args[0] for c in publish.call_args_list]
        self.assertEqual(channels, [f"/ws/{WORKSPACE_ID}/rooms/{ROOM_ID}/{DELIVERY_KEY}"])

    def _edit(self, item):
        record = stream_record(item)
        record["eventName"] = "MODIFY"
        index.process_record(record)

    @patch("index._publish")
    def test_editing_the_current_preview_republishes_it_preview_only(self, publish):
        preview = {"messageId": MESSAGE_ID, "seq": 1, "textPreview": "changed"}
        self._one_member_room(lastMessage=preview)
        self._edit({**message_item(), "messageRevision": 1, "editedText": "changed", "editedAt": CREATED_AT})
        room_event, user_event = (c.args for c in publish.call_args_list)
        self.assertEqual(room_event[1]["type"], "room.message.updated")
        self.assertEqual(user_event[0], f"/ws/{WORKSPACE_ID}/users/{BOB_KEY}")
        payload = user_event[1]["payload"]
        self.assertEqual((payload["previewOnly"], payload["notify"], payload["lastMessage"]), (True, False, preview))

    @patch("index._publish")
    def test_deleting_the_preview_publishes_its_replacement_or_null(self, publish):
        older = {"messageId": EVENT_ID, "seq": 1, "textPreview": "older"}
        for replacement in (older, None):
            publish.reset_mock()
            self._one_member_room(lastMessage=replacement)
            self._edit({**message_item(sequence=2), "messageRevision": 1, "editedText": None,
                        "editedAt": CREATED_AT, "deletedAt": CREATED_AT})
            self.assertEqual(publish.call_args_list[1].args[1]["payload"]["lastMessage"], replacement)

    @patch("index._publish")
    def test_edits_that_do_not_touch_the_preview_publish_no_preview(self, publish):
        newer = {"messageId": EVENT_ID, "seq": 5, "textPreview": "newer"}
        edited = {**message_item(), "messageRevision": 1, "editedText": "x", "editedAt": CREATED_AT}
        for meta, item in (
            ({"lastMessage": newer}, edited),  # an older message
            ({"lastMessage": None}, {**edited, "threadRootMessageId": EVENT_ID}),  # a thread reply
            ({}, edited),  # a room from before META.lastMessage existed
        ):
            publish.reset_mock()
            self._one_member_room(**meta)
            self._edit(item)
            self.assertEqual([c.args[1]["type"] for c in publish.call_args_list], ["room.message.updated"])

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

    def test_signing_reuses_one_botocore_session_across_publishes(self):
        session = MagicMock()
        session.get_credentials.return_value = Credentials("AKID", "SECRET", "TOKEN")
        old_session, index._botocore_session = index._botocore_session, None
        try:
            with patch("index.botocore.session.get_session", return_value=session) as get_session:
                for _ in range(20):
                    index._signed_request(f"/users/{CHANNEL_KEY}", live_event())
        finally:
            index._botocore_session = old_session
        get_session.assert_called_once()

    def test_http_200_with_per_event_failure_is_not_success(self):
        response = json.dumps({"successful": [], "failed": [{"index": 0, "errorType": "Denied"}]}).encode()
        with (
            patch("index._signed_request", return_value=MagicMock()),
            patch("index._send_request", return_value=(200, response)),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                index._publish(f"/users/{CHANNEL_KEY}", live_event())

    @patch("index._publish")
    def test_partial_batch_stops_at_the_bad_record_and_leaves_the_rest_to_the_retry(self, publish):
        second_id = "77777777-7777-4777-8777-777777777777"
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        good_one = stream_record(message_item(), "10")
        bad = {"eventID": "bad", "eventName": "INSERT", "dynamodb": {"SequenceNumber": "11"}}
        good_two = stream_record(message_item(second_id, 2), "12")

        result = index.handler({"Records": [good_one, bad, good_two]}, object())
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "11"}]})
        # The stream resumes FROM the bad record, so good_two is processed once, on the retry.
        self.assertEqual([call.args[1]["eventId"] for call in publish.call_args_list], [MESSAGE_ID])

        index.handler({"Records": [good_two]}, object())
        self.assertEqual(publish.call_args.args[1]["eventId"], second_id)
        self.assertEqual(publish.call_count, 2)

    def test_a_batch_fans_out_in_parallel_but_publishes_room_events_in_seq_order(self):
        index._ddb_client = FakeDdb(
            {(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}}
        )
        ids = [str(uuid.UUID(int=n, version=4)) for n in range(1, 5)]
        records = [stream_record(message_item(mid, seq), str(seq)) for seq, mid in enumerate(ids, 1)]
        # Every fan-out must be in flight at once to pass the barrier (serial would time out),
        # and the LAST message's fan-out finishes first.
        barrier = threading.Barrier(len(ids), timeout=5)

        def fan_out(message, *_args):
            barrier.wait()
            time.sleep(0.01 * (len(ids) - message["messageSeq"]))
            return []

        with (
            patch.object(index, "_notify_members", side_effect=fan_out),
            patch.object(index, "_enqueue_search_index_on_insert"),
            patch.object(index, "_publish") as publish,
        ):
            result = index.handler({"Records": records}, object())
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual([c.args[1]["messageSeq"] for c in publish.call_args_list], [1, 2, 3, 4])

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

    def test_dm_push_reads_like_a_text_from_the_named_teammate(self):
        message = push_message("invoice 1042 is ready to send. OK?", ())
        copy = index._render_push(message, {"kind": "dm"}, False)
        self.assertEqual(copy["title"], "Alice")
        self.assertEqual(copy["body"], "invoice 1042 is ready to send. OK?")

    def test_notified_members_get_one_expo_post_with_every_token(self):
        publish, urlopen = self.project(push_world())
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, index.EXPO_PUSH_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)
        messages = json.loads(request.data)
        self.assertEqual(sorted(m["to"] for m in messages), ["tok-bob", "tok-carol"])
        # Line 1 is who · where; there is no third (subtitle) line.
        self.assertTrue(all(m["title"] == "Alice mentioned you · #general" for m in messages))
        self.assertTrue(all("subtitle" not in m for m in messages))
        self.assertTrue(all(m["sound"] == "default" for m in messages))
        # APNs thread-id: one conversation groups into one stack on iOS.
        self.assertTrue(all(m["threadId"] == ROOM_ID for m in messages))
        self.assertEqual(messages[0]["body"], "hello")
        self.assertEqual(messages[0]["data"], {
            "roomId": ROOM_ID, "messageId": MESSAGE_ID, "messageSeq": 2, "threadRootMessageId": None, "roomKind": "channel",
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

    def test_feed_room_posts_push_nobody_but_still_deliver_live(self):
        """The workspace feed (id derived from the owner, as messaging_repo.feed_room_id does)
        sends no phone push and no desktop banner; the room event and the hints still go out."""
        feed = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-feed-room|{OWNER_ID}"))
        world = {(pk.replace(ROOM_ID, feed), sk): row for (pk, sk), row in push_world().items()}
        message = {**push_message(), "pk": f"ROOM#{feed}", "roomId": feed}
        publish, urlopen = self.project(world, message)
        urlopen.assert_not_called()
        payloads = {c.args[0]: c.args[1].get("payload") for c in publish.call_args_list}
        self.assertTrue(all("push" not in (p or {}) for p in payloads.values()))
        self.assertIn(f"/ws/{WORKSPACE_ID}/rooms/{feed}/{DELIVERY_KEY}", payloads)
        self.assertIn(f"/ws/{WORKSPACE_ID}/users/{BOB_KEY}", payloads)

    def test_muted_member_collects_no_token_and_dm_title_is_the_author(self):
        _, urlopen = self.project(push_world(kind="dm", muted=(BOB_ID,)))
        messages = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual([m["to"] for m in messages], ["tok-carol"])
        self.assertEqual(messages[0]["title"], "Alice")
        # Desktop gets the phone's exact banner; a muted reader gets none.
        publish, _ = self.project(push_world(kind="dm", muted=(BOB_ID,)))
        payloads = {c.args[0]: c.args[1]["payload"] for c in publish.call_args_list}
        self.assertNotIn("push", payloads[f"/ws/{WORKSPACE_ID}/users/{BOB_KEY}"])
        phone = {k: v for k, v in messages[0].items() if k not in ("to", "sound")}
        self.assertEqual(payloads[f"/ws/{WORKSPACE_ID}/users/{CAROL_KEY}"]["push"], phone)

    def test_author_token_is_never_collected(self):
        # A DM notifies every non-author member, so the author clause is the
        # only thing that can keep Alice's token out.
        _, urlopen = self.project(push_world(kind="dm"))
        self.assertNotIn("tok-alice", urlopen.call_args.args[0].data.decode())

    def test_human_author_remains_accountable_when_actor_user_id_disagrees(self):
        # actorUserId belongs to delegated system posts. It must never override
        # the public author on a direct human message, even on a malformed row.
        for actor_user_id in (BOB_ID, "", 123):
            with self.subTest(actor_user_id=actor_user_id):
                message = push_message()
                message["actorUserId"] = actor_user_id
                _, urlopen = self.project(push_world(kind="dm"), message)
                self.assertNotIn("tok-alice", urlopen.call_args.args[0].data.decode())
                self.assertNotIn(f"ACTIVITY#{OWNER_ID}#{USER_ID}", index._ddb_client.activity())

    def test_accountable_human_behind_a_system_authored_post_is_not_notified(self):
        # Native sessions and hosted MCP clients render as system authors, but
        # actorUserId records the human who actually sent the message. Their own
        # delegated post must not come back as Activity or a phone push.
        world = push_world(kind="dm")
        message = push_message()
        message["author"] = {"kind": "system", "id": "native-session", "displayName": "Codex session"}
        message["actorUserId"] = USER_ID
        message["mentions"] = [
            {"kind": "human", "id": USER_ID},
            {"kind": "human", "id": BOB_ID},
        ]

        publish, urlopen = self.project(world, message)

        self.assertNotIn("tok-alice", urlopen.call_args.args[0].data.decode())
        self.assertNotIn(f"ACTIVITY#{OWNER_ID}#{USER_ID}", index._ddb_client.activity())
        alice = next(
            call.args[1]["payload"]
            for call in publish.call_args_list
            if call.args[0] == f"/ws/{WORKSPACE_ID}/users/{CHANNEL_KEY}"
        )
        self.assertFalse(alice["notify"])
        self.assertNotIn("push", alice)
        self.assertEqual(alice["mentionedUserIds"], [BOB_ID])
        self.assertEqual(alice["accountableHumanId"], USER_ID)

    def test_empty_text_bodies_as_an_image_and_long_text_is_truncated(self):
        for blank in ("", "   "):
            _, urlopen = self.project(push_world(), push_message(blank))
            self.assertEqual(json.loads(urlopen.call_args.args[0].data)[0]["body"], "Sent an image")
        _, urlopen = self.project(push_world(), push_message("x" * 500))
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)[0]["body"], "x" * 180)

    def test_push_body_is_plain_text_not_markdown(self):
        reply = "Great question.\n\n---\n\n## This Week's Focus\n\nYou're selling **handmade mugs**, the gift market."
        _, urlopen = self.project(push_world(), push_message(reply))
        self.assertEqual(
            json.loads(urlopen.call_args.args[0].data)[0]["body"],
            "Great question. This Week's Focus You're selling handmade mugs, the gift market.",
        )
        # Same rules as the web toast's plainPreview test.
        self.assertEqual(index._plain_preview("- see [the docs](https://x.y)\n1. run `pnpm test`\n```ts\nconst a = 1;\n```"), "see the docs run pnpm test const a = 1;")
        self.assertEqual(index._plain_preview("5 * 3 = 15, snake_case stays"), "5 * 3 = 15, snake_case stays")
        self.assertEqual(index._plain_preview("这是*重点*内容"), "这是*重点*内容")
        self.assertLessEqual(len(index._plain_preview("*" * 400_000)), 1000)

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

    def test_one_token_on_two_device_rows_gets_one_banner(self):
        # A reinstall or a second account on the same phone must not double the banner.
        _, urlopen = self.project(push_world(tokens={BOB_ID: ["tok-same", "tok-same"], CAROL_ID: ["tok-same"]}))
        self.assertEqual([m["to"] for m in json.loads(urlopen.call_args.args[0].data)], ["tok-same"])

    def test_a_silent_line_files_no_activity_and_sends_no_push(self):
        message = push_message("Forge and Friday paused after 2 replies.")
        message["silent"] = True
        publish, urlopen = self.project(push_world(kind="dm"), message)
        urlopen.assert_not_called()
        self.assertFalse(any(k[0].startswith("ACTIVITY#") for k in index._ddb_client.items))
        hints = [c.args[1]["payload"] for c in publish.call_args_list if "/users/" in c.args[0]]
        self.assertTrue(hints and all(h["notify"] is False and "push" not in h for h in hints))

    def test_a_progress_line_is_marked_on_the_room_event_and_only_then(self):
        # The backend stores a progress line `silent` too, so the test above is its no-push proof.
        message = push_message("Pulled the invoices; totals next.")
        message["silent"] = message["progress"] = True
        publish, urlopen = self.project(push_world(), message)
        urlopen.assert_not_called()
        room = [c.args[1]["payload"] for c in publish.call_args_list if "/rooms/" in c.args[0]]
        self.assertEqual([p.get("progress") for p in room], [True])
        publish, _ = self.project(push_world(), push_message())
        room = [c.args[1]["payload"] for c in publish.call_args_list if "/rooms/" in c.args[0]]
        self.assertTrue(room and all("progress" not in p for p in room))

    def test_an_agent_reply_names_its_run_in_the_push(self):
        message = push_message()
        message["runId"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        _, urlopen = self.project(push_world(), message)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)[0]["data"]["runId"], message["runId"])

    def test_records_after_a_failed_one_are_left_for_the_retry(self):
        # The stream replays from the failed record; pushing the later ones now
        # would banner them again on that replay.
        # (The later record's member fan-out may already have run beside it; only its
        # ordered half -- room event and push -- is held back.)
        index._ddb_client = FakeDdb(push_world())
        finish = MagicMock()

        def prepare(record):
            if record["dynamodb"]["SequenceNumber"] == "600":
                raise RuntimeError("throttled")
            return finish

        with patch.object(index, "prepare_record", side_effect=prepare), self.assertLogs(level="ERROR"):
            result = index.handler(
                {"Records": [stream_record(push_message(), "600"), stream_record(push_message(), "601")]}, object()
            )
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "600"}]})
        finish.assert_not_called()

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

    def test_nameless_rooms_still_say_what_kind_they_are(self):
        for kind, where in (("channel", "Channel"), ("group-dm", "Group chat")):
            world = push_world(kind=kind)
            world[(f"ROOM#{ROOM_ID}", "META")]["name"] = None
            _, urlopen = self.project(world)
            message = json.loads(urlopen.call_args.args[0].data)[0]
            self.assertTrue(message["title"].endswith(f"· {where}"))

    def test_group_dm_says_group_chat_and_titles_each_reader_separately(self):
        # A group DM notifies every member, so one message carries both titles:
        # Bob was @mentioned, Carol was not. Line 1 names the group, so it never
        # reads like a DM or a channel.
        _, urlopen = self.project(
            push_world(kind="group-dm"), push_message(mentions=(BOB_ID,))
        )
        messages = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(
            {m["to"]: m["title"] for m in messages},
            {"tok-bob": "Alice mentioned you \u00b7 general", "tok-carol": "Alice \u00b7 general"},
        )

    def test_a_thread_reply_says_so_in_line_one(self):
        self.assertEqual(index._push_where({"kind": "channel", "name": "gtm"}, True), "Thread in #gtm")
        self.assertEqual(index._push_where({"kind": "dm", "name": None}, False), "DM")

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


def activity_world(kind: str = "channel", members: tuple[str, ...] = (USER_ID, BOB_ID, CAROL_ID)) -> dict:
    """A room whose humans are current members; only Bob has an active stream."""
    items = {
        (f"ROOM#{ROOM_ID}", "META"): {
            "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "kind": kind, "name": "general",
            "workspaceId": WORKSPACE_ID,
        },
        (f"OWNER#{OWNER_ID}", f"USERSTREAM#{BOB_ID}"): {"active": True, "channelKey": BOB_KEY},
        (f"STREAM#{BOB_KEY}", "META"): {
            "active": True, "ownerId": OWNER_ID, "userId": BOB_ID, "workspaceId": WORKSPACE_ID,
        },
    }
    for user in members:
        items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{user}")] = {"id": user, "ownerId": OWNER_ID, "joinedSeq": 0}
    return items


class ActivityTests(unittest.TestCase):
    """ACTIVITY# pointer rows, written by this stream consumer after the message commits."""

    def setUp(self):
        self.saved = (index.MESSAGING_TABLE, index.USERS_TABLE, index._ddb_client)
        index.MESSAGING_TABLE = "lightbulb-test-messaging"
        index.USERS_TABLE = ""

    def tearDown(self):
        index.MESSAGING_TABLE, index.USERS_TABLE, index._ddb_client = self.saved

    def project(self, items: dict, message: dict) -> FakeDdb:
        index._ddb_client = FakeDdb(items)
        with patch.object(index, "_publish"):
            index._project_message(message)
        return index._ddb_client

    def test_dm_writes_one_pointer_row_for_the_other_human_never_the_author(self):
        ddb = self.project(activity_world("dm", (USER_ID, BOB_ID)), message_item(sequence=2))
        rows = ddb.activity()
        self.assertEqual(list(rows), [f"ACTIVITY#{OWNER_ID}#{BOB_ID}"])
        row = rows[f"ACTIVITY#{OWNER_ID}#{BOB_ID}"]
        self.assertEqual(row["sk"], f"{CREATED_AT}#{MESSAGE_ID}")
        self.assertEqual(
            (row["kind"], row["roomId"], row["messageId"], int(row["seq"]), row["actor"], row["mentioned"]),
            ("dm", ROOM_ID, MESSAGE_ID, 2, {"kind": "human", "id": USER_ID}, False),
        )
        # A pointer only: no message text or author name ever lands on the row.
        self.assertNotIn("hello", json.dumps(row, default=str))
        self.assertNotIn("Alice", json.dumps(row, default=str))
        # 90-day TTL on the table's TTL attribute.
        created = index._parse_rfc3339(CREATED_AT).timestamp()
        self.assertEqual(int(row["expiresAt"]), int(created) + 90 * 24 * 3600)

    def test_mention_reaches_only_the_mentioned_human(self):
        message = message_item(sequence=2)
        message["mentions"] = [{"kind": "human", "id": CAROL_ID}, {"kind": "agent", "id": "friday"}]
        rows = self.project(activity_world(), message).activity()
        self.assertEqual(list(rows), [f"ACTIVITY#{OWNER_ID}#{CAROL_ID}"])
        self.assertEqual(rows[f"ACTIVITY#{OWNER_ID}#{CAROL_ID}"]["kind"], "mention")

    def test_mention_inside_a_dm_is_a_dm_that_remembers_the_mention(self):
        message = message_item(sequence=2)
        message["mentions"] = [{"kind": "human", "id": BOB_ID}]
        row = self.project(activity_world("dm", (USER_ID, BOB_ID)), message).activity()[f"ACTIVITY#{OWNER_ID}#{BOB_ID}"]
        self.assertEqual((row["kind"], row["mentioned"]), ("dm", True))

    def test_thread_reply_reaches_root_author_participants_and_root_mentions_without_a_scan(self):
        root = message_item(message_id=EVENT_ID, sequence=1)
        root["author"] = {"kind": "agent", "id": "friday", "displayName": "Friday"}
        root["mentions"] = [{"kind": "human", "id": CAROL_ID}]
        root["threadParticipants"] = {BOB_ID, USER_ID}
        items = activity_world(members=(USER_ID, BOB_ID, CAROL_ID, "user_dan"))
        items[(f"ROOM#{ROOM_ID}", "MEMBER#human#user_dan")] = {"id": "user_dan", "ownerId": OWNER_ID, "joinedSeq": 0}
        items[(root["pk"], root["sk"])] = root
        reply = message_item(sequence=3)
        reply["threadRootMessageId"] = EVENT_ID
        reply["threadRootSeq"] = 1
        ddb = self.project(items, reply)
        rows = ddb.activity()
        self.assertEqual(sorted(rows), [f"ACTIVITY#{OWNER_ID}#{BOB_ID}", f"ACTIVITY#{OWNER_ID}#{CAROL_ID}"])
        self.assertTrue(all(r["kind"] == "thread_reply" and r["threadRootMessageId"] == EVENT_ID for r in rows.values()))
        # The root is found by its key, not by scanning the room's messages.
        prefixes = [c["ExpressionAttributeValues"][":prefix"]["S"] for c in ddb.calls if "KeyConditionExpression" in c]
        self.assertNotIn("MSG#", prefixes)

    def test_thread_reply_notify_uses_the_participant_set(self):
        root = message_item(message_id=EVENT_ID, sequence=1)
        root["author"]["id"] = CAROL_ID
        root["threadParticipants"] = {BOB_ID}
        items = activity_world()
        items[(root["pk"], root["sk"])] = root
        reply = message_item(sequence=3)
        reply["threadRootMessageId"] = EVENT_ID
        reply["threadRootSeq"] = 1
        index._ddb_client = FakeDdb(items)
        with patch.object(index, "_publish") as publish:
            index._project_message(reply)
        bob_hint = next(c.args[1] for c in publish.call_args_list if c.args[0].endswith(BOB_KEY))
        self.assertTrue(bob_hint["payload"]["notify"])

    def test_thread_reply_reaches_accountable_human_who_started_delegated_root(self):
        root = message_item(message_id=EVENT_ID, sequence=1)
        root["author"] = {"kind": "system", "id": "native-session", "displayName": "Codex session"}
        root["actorUserId"] = CAROL_ID
        items = activity_world()
        items[(f"OWNER#{OWNER_ID}", f"USERSTREAM#{CAROL_ID}")] = {"active": True, "channelKey": CAROL_KEY}
        items[(f"STREAM#{CAROL_KEY}", "META")] = {
            "active": True, "ownerId": OWNER_ID, "userId": CAROL_ID, "workspaceId": WORKSPACE_ID,
        }
        items[(root["pk"], root["sk"])] = root
        reply = message_item(sequence=3)
        reply["threadRootMessageId"] = EVENT_ID
        reply["threadRootSeq"] = 1
        index._ddb_client = FakeDdb(items)

        with patch.object(index, "_publish") as publish:
            index._project_message(reply)

        rows = index._ddb_client.activity()
        self.assertEqual(list(rows), [f"ACTIVITY#{OWNER_ID}#{CAROL_ID}"])
        self.assertEqual(rows[f"ACTIVITY#{OWNER_ID}#{CAROL_ID}"]["kind"], "thread_reply")
        carol_hint = next(c.args[1] for c in publish.call_args_list if c.args[0].endswith(CAROL_KEY))
        self.assertTrue(carol_hint["payload"]["notify"])
        self.assertEqual(carol_hint["payload"]["accountableHumanId"], USER_ID)

    def test_replay_is_idempotent(self):
        items = activity_world("dm", (USER_ID, BOB_ID))
        message = message_item(sequence=2)
        ddb = self.project(items, message)
        before = dict(ddb.activity())
        with patch.object(index, "_publish"):
            index._project_message(message)  # the stream redelivers the same record
        self.assertEqual(ddb.activity(), before)
        puts = [c for c in ddb.calls if "Item" in c]
        self.assertTrue(all(c["ConditionExpression"] == "attribute_not_exists(pk)" for c in puts))

    def test_only_members_current_at_write_time_get_a_row(self):
        items = activity_world("dm", (USER_ID, BOB_ID, CAROL_ID))
        del items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{CAROL_ID}")]  # removed after discovery
        items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{BOB_ID}")]["joinedSeq"] = 2  # joined at/after this message
        discovered = [{"id": USER_ID}, {"id": BOB_ID}, {"id": CAROL_ID}]
        index._ddb_client = FakeDdb(items)
        with patch.object(index, "_publish"), patch.object(index, "_query_rows", return_value=discovered):
            index._project_message(message_item(sequence=2))
        self.assertEqual(index._ddb_client.activity(), {})

    def test_member_without_a_live_stream_still_gets_activity(self):
        # Carol has never opened a live connection (no USERSTREAM): her row is written anyway.
        message = message_item(sequence=2)
        message["mentions"] = [{"kind": "human", "id": CAROL_ID}]
        rows = self.project(activity_world(), message).activity()
        self.assertIn(f"ACTIVITY#{OWNER_ID}#{CAROL_ID}", rows)

    def test_foreign_member_row_lands_in_their_home_workspace(self):
        items = activity_world("dm", (USER_ID, BOB_ID))
        items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{BOB_ID}")]["homeOwnerId"] = "org_vendor"
        rows = self.project(items, message_item(sequence=2)).activity()
        self.assertEqual(list(rows), [f"ACTIVITY#org_vendor#{BOB_ID}"])

    def test_plain_channel_message_writes_nothing(self):
        self.assertEqual(self.project(activity_world(), message_item(sequence=2)).activity(), {})

    def away(self, ddb: FakeDdb) -> dict[str, dict]:
        return {pk: row for (pk, _sk), row in ddb.items.items() if pk.startswith("AWAY#")}

    def test_a_dm_queues_an_away_email_check_five_minutes_out(self):
        """T15: the backend sweeper emails it if it is still unread then."""
        rows = self.away(self.project(activity_world("dm", (USER_ID, BOB_ID)), message_item(sequence=2)))
        self.assertEqual(list(rows), [f"AWAY#{BOB_ID}"])
        row = rows[f"AWAY#{BOB_ID}"]
        created = int(index._parse_rfc3339(CREATED_AT).timestamp())
        self.assertEqual(
            (row["sweep"], int(row["deadlineAt"]), row["kind"], row["sk"], row["ownerId"]),
            ("away", created + 300, "dm", f"M#{ROOM_ID}#{MESSAGE_ID}", OWNER_ID),
        )
        self.assertNotIn("hello", json.dumps(row, default=str))

    def test_a_muted_member_and_a_thread_reply_queue_no_away_email(self):
        items = activity_world("dm", (USER_ID, BOB_ID))
        items[(f"ROOM#{ROOM_ID}", f"MEMBER#human#{BOB_ID}")]["muted"] = True
        self.assertEqual(self.away(self.project(items, message_item(sequence=2))), {})
        self.assertEqual(self.away(self.project(activity_world(), message_item(sequence=2))), {})


class ThreadRootModifyTests(unittest.TestCase):
    """A thread reply ADDs to its root's threadParticipants: that MODIFY is not an edit."""

    def setUp(self):
        self.saved = (index.MESSAGING_TABLE, index._ddb_client)
        index.MESSAGING_TABLE = "lightbulb-test-messaging"
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {
            "deliveryKey": DELIVERY_KEY, "ownerId": OWNER_ID, "workspaceId": WORKSPACE_ID}})

    def tearDown(self):
        index.MESSAGING_TABLE, index._ddb_client = self.saved

    def modify(self, item):
        record = stream_record(item)
        record["eventName"] = "MODIFY"
        with patch.object(index, "_publish") as publish:
            index.process_record(record)
        return publish

    def test_participant_add_on_an_edited_root_publishes_nothing(self):
        root = {**message_item(), "messageRevision": 1, "editedText": "x", "editedAt": "2026-09-10T00:01:00Z",
                "threadParticipants": {BOB_ID}, "threadTouchedAt": "2026-09-10T00:05:00Z"}
        self.modify(root).assert_not_called()

    def test_an_edit_after_a_participant_add_still_publishes(self):
        # The edit REMOVEs threadTouchedAt, so its image never carries it.
        root = {**message_item(), "messageRevision": 2, "editedText": "y", "editedAt": "2026-09-10T00:09:00Z",
                "threadParticipants": {BOB_ID}}
        publish = self.modify(root)
        self.assertEqual(publish.call_args_list[0].args[1]["type"], "room.message.updated")

    def test_an_edit_stamped_before_a_reply_but_committed_after_still_publishes(self):
        # The clock race: the edit's editedAt is OLDER than the reply's touch, yet it
        # committed last. Its image has no threadTouchedAt, so it is never skipped.
        root = {**message_item(), "messageRevision": 2, "editedText": "", "deletedAt": "2026-09-10T00:04:59Z",
                "editedAt": "2026-09-10T00:04:59Z", "threadParticipants": {BOB_ID}}
        publish = self.modify(root)
        self.assertEqual(publish.call_args_list[0].args[1]["type"], "room.message.updated")


IMMEDIATE_URL = "https://sqs.us-east-1.amazonaws.com/1/search"
BATCH_URL = "https://sqs.us-east-1.amazonaws.com/1/search-batch"
SESSION_ID = "44444444-4444-4444-8444-444444444444"


class SearchEnqueueTests(unittest.TestCase):
    def setUp(self):
        self.saved = (index.MESSAGING_TABLE, index.HTTP_ENDPOINT, index.REGION, index._ddb_client,
                      index.SEARCH_INDEX_QUEUE_URL, index._sqs_client, index.SEARCH_INDEX_BATCH_QUEUE_URL)
        index.MESSAGING_TABLE = "lightbulb-test-messaging"
        index.HTTP_ENDPOINT = "https://example.appsync-api.us-east-1.amazonaws.com/event"
        index.REGION = "us-east-1"
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {
            "deliveryKey": DELIVERY_KEY, "ownerId": "org_room_owner", "workspaceId": WORKSPACE_ID}})
        index.SEARCH_INDEX_QUEUE_URL = IMMEDIATE_URL
        index.SEARCH_INDEX_BATCH_QUEUE_URL = BATCH_URL
        index._sqs_client = MagicMock()

    def tearDown(self):
        (index.MESSAGING_TABLE, index.HTTP_ENDPOINT, index.REGION, index._ddb_client,
         index.SEARCH_INDEX_QUEUE_URL, index._sqs_client, index.SEARCH_INDEX_BATCH_QUEUE_URL) = self.saved

    def _body(self):
        return json.loads(index._sqs_client.send_message.call_args.kwargs["MessageBody"])

    @patch("index._send_push")
    @patch("index._notify_members", return_value=[])
    @patch("index._publish")
    def test_new_message_enqueues_after_publish_before_push(self, publish, notify, push):
        order = []
        publish.side_effect = lambda *a, **k: order.append("publish")
        index._sqs_client.send_message.side_effect = lambda **_: order.append("enqueue")
        push.side_effect = lambda *a, **k: order.append("push")
        index.process_record(stream_record(message_item()))
        self.assertEqual(self._body(), {"ownerId": "org_room_owner", "roomId": ROOM_ID, "sk": message_item()["sk"]})
        self.assertEqual(order, ["publish", "enqueue", "push"])

    @patch("index._send_push")
    @patch("index._notify_members", return_value=[])
    @patch("index._publish")
    def test_failed_insert_enqueue_still_pushes(self, publish, notify, push):
        index._sqs_client.send_message.side_effect = RuntimeError("sqs down")
        with patch("index._emit_metric") as metric:
            index.process_record(stream_record(message_item()))
        push.assert_called_once()
        self.assertIn("messaging.projector.search_enqueue.failed", [c.args[0] for c in metric.call_args_list])

    def _queue_url(self):
        return index._sqs_client.send_message.call_args.kwargs["QueueUrl"]

    @patch("index._send_push")
    @patch("index._notify_members", return_value=[])
    @patch("index._publish")
    def test_new_message_goes_to_the_batch_queue(self, publish, notify, push):
        index.process_record(stream_record(message_item()))
        self.assertEqual(self._queue_url(), BATCH_URL)

    @patch("index._send_push")
    @patch("index._notify_members", return_value=[])
    @patch("index._publish")
    def test_new_message_falls_back_to_immediate_without_batch_env(self, publish, notify, push):
        index.SEARCH_INDEX_BATCH_QUEUE_URL = ""
        index.process_record(stream_record(message_item()))
        self.assertEqual(self._queue_url(), IMMEDIATE_URL)

    @patch("index._publish")
    def test_edit_and_delete_go_to_the_immediate_queue(self, publish):
        for extra in ({"editedText": "new"}, {"editedText": None, "deletedAt": "2026-10-04T00:00:02Z"}):
            item = {**message_item(), "messageRevision": 2, "editedAt": "2026-10-04T00:00:01Z", **extra}
            record = stream_record(item)
            record["eventName"] = "MODIFY"
            index.process_record(record)
            self.assertEqual(self._queue_url(), IMMEDIATE_URL)

    @patch("index._publish")
    def test_failed_edit_enqueue_still_raises(self, publish):
        index._sqs_client.send_message.side_effect = RuntimeError("sqs down")
        item = {**message_item(), "messageRevision": 2, "editedText": "x", "editedAt": "2026-10-04T00:00:01Z"}
        record = stream_record(item)
        record["eventName"] = "MODIFY"
        with self.assertRaises(RuntimeError):
            index.process_record(record)

    @patch("index._publish")
    def test_edit_enqueues(self, publish):
        item = {**message_item(), "messageRevision": 2, "editedText": "new words", "editedAt": "2026-10-04T00:00:01Z"}
        record = stream_record(item)
        record["eventName"] = "MODIFY"
        index.process_record(record)
        self.assertEqual(self._body()["sk"], item["sk"])

    @patch("index._publish")
    def test_edit_enqueues_even_when_the_workspace_is_unresolvable(self, publish):
        index._ddb_client = FakeDdb({(f"ROOM#{ROOM_ID}", "META"): {"deliveryKey": DELIVERY_KEY, "ownerId": "org_room_owner"}})
        item = {**message_item(), "messageRevision": 2, "editedText": None, "deletedAt": "2026-10-04T00:00:02Z",
                "editedAt": "2026-10-04T00:00:02Z"}
        with patch("index._workspace_id_for_meta", return_value=None):
            record = stream_record(item)
            record["eventName"] = "MODIFY"
            index.process_record(record)
        self.assertEqual(self._body()["sk"], item["sk"])
        publish.assert_not_called()

    @patch("index._send_push")
    @patch("index._notify_members", return_value=[])
    @patch("index._publish")
    def test_search_off_enqueues_nothing(self, publish, notify, push):
        index.SEARCH_INDEX_QUEUE_URL = index.SEARCH_INDEX_BATCH_QUEUE_URL = ""
        index.process_record(stream_record(message_item()))
        index._sqs_client.send_message.assert_not_called()

    @patch("index._publish")
    def test_reaction_modify_enqueues_nothing(self, publish):
        record = stream_record({**message_item(), "reactionRevision": 1})
        record["eventName"] = "MODIFY"
        index.process_record(record)
        index._sqs_client.send_message.assert_not_called()

    def _session(self, sk, event_name="INSERT", **extra):
        record = stream_record({"pk": "LOCALSESSION#org_room_owner#" + SESSION_ID, "sk": sk, **extra})
        record["eventName"] = event_name
        return record

    @patch("index._publish")
    def test_a_session_event_goes_to_the_batch_queue_and_publishes_nothing(self, publish):
        index.process_record(self._session("EVENT#00000000000000000003", seq=3, kind="text", role="user", text="hi"))
        self.assertEqual(self._queue_url(), BATCH_URL)
        self.assertEqual(self._body(), {"ownerId": "org_room_owner", "sessionId": SESSION_ID, "sk": "EVENT#00000000000000000003"})
        publish.assert_not_called()

    def test_a_failed_session_event_enqueue_does_not_stall_the_shard(self):
        index._sqs_client.send_message.side_effect = RuntimeError("sqs down")
        with patch("index._emit_metric") as metric:
            index.process_record(self._session("EVENT#00000000000000000003"))
        self.assertIn("messaging.projector.search_enqueue.failed", [c.args[0] for c in metric.call_args_list])

    def test_a_removed_session_is_deleted_at_once_and_a_failure_retries(self):
        index.process_record(self._session("META", "MODIFY", removedAt=1790000000, eventSeq=42))
        self.assertEqual(self._queue_url(), IMMEDIATE_URL)
        self.assertEqual(self._body()["eventSeq"], 42)
        index._sqs_client.send_message.side_effect = RuntimeError("sqs down")
        with self.assertRaises(RuntimeError):
            index.process_record(self._session("META", "MODIFY", removedAt=1790000000, eventSeq=42))

    def test_other_session_rows_enqueue_nothing(self):
        for record in (self._session("META", "MODIFY", eventSeq=4), self._session("COMMAND#r1"),
                       self._session("EVENT#00000000000000000003", "MODIFY")):
            index.process_record(record)
        index._sqs_client.send_message.assert_not_called()

    @patch("index._publish")
    def test_thread_touch_only_modify_enqueues_nothing(self, publish):
        record = stream_record({**message_item(), "messageRevision": 1, "threadTouchedAt": "2026-10-04T00:00:00Z"})
        record["eventName"] = "MODIFY"
        index.process_record(record)
        index._sqs_client.send_message.assert_not_called()


TASK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TASKS_ARN = "arn:aws:dynamodb:us-east-1:1:table/lightbulb-dev-tasks/stream/2026-11-04T00:00:00.000"


def task_record(sk: str, *, event: str = "INSERT", version: int = 2, old_version: int | None = None, **extra) -> dict:
    row = {"pk": f"WS#{OWNER_ID}", "sk": sk, "version": version, "wsChannel": WORKSPACE_ID,
           "title": "secret title", "status": "working", **extra}
    record = {**stream_record(row), "eventName": event, "eventSourceARN": TASKS_ARN}
    if old_version is not None:
        record["dynamodb"]["OldImage"] = av_map({**row, "version": old_version})
    return record


@patch("index._publish")
class TaskChangedTests(unittest.TestCase):
    def test_publishes_ids_only(self, publish):
        parts = {
            f"TASK#{TASK_ID}": "task",
            f"TASK#{TASK_ID}#ATT#0001": "attempt",
            f"TASK#{TASK_ID}#Q#q1": "question",
            f"TASK#{TASK_ID}#SUB#room:x": "subscription",
        }
        for sk, part in parts.items():
            publish.reset_mock()
            index.process_record(task_record(sk, event="MODIFY", old_version=1))
            channel, event = publish.call_args.args
            self.assertEqual(channel, f"/ws/{WORKSPACE_ID}/tasks")
            self.assertEqual(set(event), {"type", "data", "at"})
            self.assertEqual(event["type"], "task.changed")
            self.assertEqual(event["data"], {"taskId": TASK_ID, "part": part, "version": 2})

    def test_skips_same_version(self, publish):
        index.process_record(task_record(f"TASK#{TASK_ID}", event="MODIFY", version=3, old_version=3))
        publish.assert_not_called()

    def test_skips_rows_without_ws_channel(self, publish):
        index.process_record(task_record(f"TASK#{TASK_ID}#ATT#0001", wsChannel=None))
        record = task_record(f"TASK#{TASK_ID}")
        del record["dynamodb"]["NewImage"]["wsChannel"]
        index.process_record(record)
        publish.assert_not_called()

    def test_non_task_rows_ignored(self, publish):
        for sk in ("THREAD#room:x", f"NEEDS#{USER_ID}#{TASK_ID}#q1", "SLOTS", "MSG#00000000000000000001"):
            index.process_record(task_record(sk))
        index.process_record({**task_record(f"TASK#{TASK_ID}"), "eventName": "REMOVE"})
        publish.assert_not_called()
