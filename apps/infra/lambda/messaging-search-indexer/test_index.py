from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import index

ROOM = "11111111-1111-4111-8111-111111111111"
SK = "MSG#00000000000000000007"
TARGET = f"/{index.index_name('org_owner')}/_doc/{ROOM}:7"


def row(**extra):
    return {
        "pk": f"ROOM#{ROOM}", "sk": SK, "roomId": ROOM, "messageId": "m-1", "messageSeq": 7,
        "createdAt": "2026-10-04T00:00:00Z", "text": "ship the deploy fix",
        "author": {"kind": "human", "id": "user_a", "displayName": "Ann"}, **extra,
    }


def job():
    return {"ownerId": "org_owner", "roomId": ROOM, "sk": SK}


def calls(request):
    return [(c.args[0], c.args[1]) for c in request.call_args_list]


class IndexerTests(unittest.TestCase):
    def test_index_name_matches_the_backend_vector(self):
        self.assertEqual(index.index_name("org_owner"), "messages-603feeb09672e3cefd5c20fc9aa957ba")

    def test_document_indexes_public_text_only(self):
        self.assertEqual(index.document(row())["text"], "ship the deploy fix")
        edited = index.document(row(editedText="new words"))
        self.assertEqual(edited["text"], "new words")
        self.assertNotIn("ship", json.dumps(edited))
        self.assertIsNone(index.document(row(deletedAt="2026-10-04T00:01:00Z")))

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), row()])
    def test_live_row_is_written_then_verified(self, get_row, request):
        index.process(job())
        name = index.index_name("org_owner")
        self.assertEqual(calls(request), [("PUT", f"/{name}"), ("PUT", TARGET)])
        self.assertEqual(get_row.call_count, 2)

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), row(deletedAt="2026-10-04T00:01:00Z")])
    def test_a_delete_that_lands_after_our_write_is_applied(self, get_row, request):
        index.process(job())
        self.assertEqual(calls(request)[-1], ("DELETE", TARGET))

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), row(editedText="newer", messageRevision=2), row(editedText="newer", messageRevision=2)])
    def test_an_edit_that_lands_after_our_write_is_rewritten(self, get_row, request):
        index.process(job())
        last = request.call_args_list[-1]
        self.assertEqual((last.args[0], last.args[1]), ("PUT", TARGET))
        self.assertEqual(last.args[2]["text"], "newer")

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(), None])
    def test_a_wipe_that_lands_after_our_write_removes_it(self, get_row, request):
        index.process(job())
        self.assertEqual(calls(request)[-1], ("DELETE", TARGET))

    @patch("index._request", return_value=(404, b"{}"))
    @patch("index._get_row", return_value=None)
    def test_missing_row_deletes_and_accepts_404(self, get_row, request):
        index.process(job())
        self.assertEqual(calls(request), [("DELETE", TARGET)])

    @patch("index._request", side_effect=[(400, b'{"error":{"type":"resource_already_exists_exception"}}'), (200, b"{}")])
    @patch("index._get_row", side_effect=[row(), row()])
    def test_existing_index_is_fine(self, get_row, request):
        index.process(job())

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[row(editedText=f"v{n}", messageRevision=n) for n in range(1, 20)])
    def test_a_row_that_never_settles_fails_the_job_for_retry(self, get_row, request):
        with self.assertRaises(RuntimeError):
            index.process(job())

    @patch("index.process", side_effect=RuntimeError("boom"))
    def test_handler_reports_the_failed_job(self, process):
        event = {"Records": [{"messageId": "b", "body": json.dumps(job())}]}
        self.assertEqual(index.handler(event, None), {"batchItemFailures": [{"itemIdentifier": "b"}]})

    @patch("index.process")
    def test_sqs_records_path_ignores_drain_absent(self, process):
        event = {"Records": [{"messageId": "a", "body": json.dumps(job())}]}
        self.assertEqual(index.handler(event, None), {"batchItemFailures": []})
        process.assert_called_once_with(job())

    def test_rejects_non_message_rows(self):
        with self.assertRaises(ValueError):
            index.process({"ownerId": "org_owner", "roomId": ROOM, "sk": "META"})


BATCH_URL = "https://sqs.us-east-1.amazonaws.com/1/search-batch"


def sqs_msg(n):
    return {"MessageId": f"m{n}", "ReceiptHandle": f"r{n}", "Body": json.dumps({**job(), "n": n})}


class FakeContext:
    def __init__(self, *remaining):
        self.remaining = list(remaining)

    def get_remaining_time_in_millis(self):
        return self.remaining.pop(0) if len(self.remaining) > 1 else self.remaining[0]


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.saved = (index.SEARCH_INDEX_BATCH_QUEUE_URL, index._sqs_client)
        index.SEARCH_INDEX_BATCH_QUEUE_URL = BATCH_URL
        index._sqs_client = MagicMock()

    def tearDown(self):
        index.SEARCH_INDEX_BATCH_QUEUE_URL, index._sqs_client = self.saved

    def deleted(self):
        return [e["Id"] for c in index._sqs_client.delete_message_batch.call_args_list for e in c.kwargs["Entries"]]

    @patch("index.process")
    def test_drains_across_receives_and_deletes_successes(self, process):
        index._sqs_client.receive_message.side_effect = [
            {"Messages": [sqs_msg(i) for i in range(10)]}, {"Messages": [sqs_msg(10)]}, {}]
        result = index.handler({"drain": True}, FakeContext(300_000))
        self.assertEqual(result, {"drained": 11, "failed": 0})
        self.assertEqual(process.call_count, 11)
        self.assertEqual(len(self.deleted()), 11)
        for c in index._sqs_client.delete_message_batch.call_args_list:
            self.assertLessEqual(len(c.kwargs["Entries"]), 10)
        self.assertEqual(index._sqs_client.receive_message.call_args.kwargs["QueueUrl"], BATCH_URL)
        self.assertEqual(index._sqs_client.receive_message.call_args.kwargs["WaitTimeSeconds"], 1)

    @patch("index.process", side_effect=[None, RuntimeError("boom"), None])
    def test_a_failing_job_is_not_deleted(self, process):
        index._sqs_client.receive_message.side_effect = [{"Messages": [sqs_msg(i) for i in range(3)]}, {}]
        result = index.drain(FakeContext(300_000))
        self.assertEqual(result, {"drained": 2, "failed": 1})
        self.assertEqual(sorted(self.deleted()), ["m0", "m2"])

    @patch("index.process")
    def test_stops_on_an_empty_receive(self, process):
        index._sqs_client.receive_message.return_value = {}
        self.assertEqual(index.drain(FakeContext(300_000)), {"drained": 0, "failed": 0})
        index._sqs_client.receive_message.assert_called_once()
        index._sqs_client.delete_message_batch.assert_not_called()

    @patch("index.process")
    def test_stops_when_time_is_low(self, process):
        index._sqs_client.receive_message.return_value = {"Messages": [sqs_msg(1)]}
        result = index.drain(FakeContext(300_000, 300_000, 59_999))
        self.assertEqual(result, {"drained": 1, "failed": 0})
        index._sqs_client.receive_message.assert_called_once()

    @patch("index.process")
    def test_failed_delete_is_logged_by_count_only(self, process):
        index._sqs_client.receive_message.side_effect = [{"Messages": [sqs_msg(1)]}, {}]
        index._sqs_client.delete_message_batch.return_value = {"Failed": [{"Id": "m1"}]}
        with self.assertLogs(level="WARNING") as logs:
            index.drain(FakeContext(300_000))
        self.assertIn("count=1", logs.output[0])
        self.assertNotIn("m1", logs.output[0])

    @patch("index.process")
    def test_stops_mid_batch_when_time_is_low(self, process):
        index._sqs_client.receive_message.return_value = {"Messages": [sqs_msg(i) for i in range(3)]}
        # loop check, msg0 check, msg1 check (low)
        result = index.drain(FakeContext(300_000, 300_000, 59_999))
        self.assertEqual(result, {"drained": 1, "failed": 0})
        self.assertEqual(process.call_count, 1)
        self.assertEqual(self.deleted(), ["m0"])
        index._sqs_client.receive_message.assert_called_once()
        release = index._sqs_client.change_message_visibility_batch.call_args.kwargs
        self.assertEqual(release["QueueUrl"], BATCH_URL)
        self.assertEqual([(e["Id"], e["ReceiptHandle"], e["VisibilityTimeout"]) for e in release["Entries"]],
                         [("m1", "r1", 0), ("m2", "r2", 0)])

    @patch("index.process")
    def test_failed_release_is_logged_by_count_only(self, process):
        index._sqs_client.receive_message.return_value = {"Messages": [sqs_msg(i) for i in range(3)]}
        index._sqs_client.change_message_visibility_batch.return_value = {"Failed": [{"Id": "m1"}]}
        with self.assertLogs(level="WARNING") as logs:
            index.drain(FakeContext(300_000, 300_000, 59_999))
        self.assertIn("count=1", logs.output[0])
        self.assertNotIn("m1", logs.output[0])

    def test_empty_batch_url_receives_nothing(self):
        index.SEARCH_INDEX_BATCH_QUEUE_URL = ""
        self.assertEqual(index.handler({"drain": True}, FakeContext(300_000)), {"drained": 0, "failed": 0})
        index._sqs_client.receive_message.assert_not_called()


SID = "33333333-3333-4333-8333-333333333333"
SPK = f"LOCALSESSION#org_owner#{SID}"
SESSION_TARGET = f"/{index.index_name('org_owner')}/_doc/session:{SID}:5"


def event(**extra):
    return {"pk": SPK, "sk": "EVENT#00000000000000000005", "seq": 5, "kind": "text", "role": "user",
            "text": "why does the deploy fail", "createdAt": 1790000000, **extra}


def meta(**extra):
    return {"pk": SPK, "sk": "META", "sessionId": SID, "ownerId": "org_owner", "humanUserId": "user_a",
            "provider": "claude", "eventSeq": 5, **extra}


def session_job(sk="EVENT#00000000000000000005", **extra):
    return {"ownerId": "org_owner", "sessionId": SID, "sk": sk, **extra}


class SessionIndexerTests(unittest.TestCase):
    def test_only_prompts_and_replies_are_indexed(self):
        prompt = index.session_document(event(), meta())
        self.assertEqual((prompt["kind"], prompt["authorKind"], prompt["authorId"], prompt["seq"]), ("session", "person", "user_a", 5))
        self.assertEqual(prompt["createdAt"], "2026-09-21T14:13:20Z")
        reply = index.session_document(event(role="assistant", text="It needs a token."), meta())
        self.assertEqual((reply["authorKind"], reply["authorName"], reply["authorId"]), ("agent", "Claude Code", ""))
        teammate = index.session_document(event(actor={"userId": "user_b"}), meta())
        self.assertEqual(teammate["authorId"], "user_b")
        for skipped in (
            event(kind="tool", role="tool", text="$ ls\nsecret.txt"),
            event(kind="thinking", role="assistant"),
            event(kind="lifecycle", role="system", text="turn_end"),
            event(role="system", text="Sharing was paused."),
            event(role="assistant", subagent="agent-1"),
            event(text="   "),
        ):
            self.assertIsNone(index.session_document(skipped, meta()), skipped)

    def test_a_removed_or_missing_session_indexes_nothing(self):
        self.assertIsNone(index.session_document(event(), meta(removedAt=1)))
        self.assertIsNone(index.session_document(event(), None))

    @patch("index._request", side_effect=[(400, b"resource_already_exists"), (200, b"{}"), (201, b"{}")])
    @patch("index._get_row", side_effect=[event(), meta(), meta()])
    def test_an_event_extends_an_old_index_then_writes_and_verifies(self, get_row, request):
        index.process(session_job())
        name = index.index_name("org_owner")
        self.assertEqual(calls(request), [("PUT", f"/{name}"), ("PUT", f"/{name}/_mapping"), ("PUT", SESSION_TARGET)])
        self.assertEqual(get_row.call_args_list[0].args, ("", "EVENT#00000000000000000005", SPK))

    @patch("index._request", return_value=(201, b"{}"))
    @patch("index._get_row", side_effect=[event(), meta(), meta(removedAt=1), event(), meta(removedAt=1)])
    def test_a_removal_that_lands_after_our_write_deletes_it(self, get_row, request):
        index.process(session_job())
        self.assertEqual(calls(request)[-1], ("DELETE", SESSION_TARGET))

    @patch("index._request", return_value=(404, b"{}"))
    @patch("index._get_row", side_effect=[event(kind="tool", role="tool"), meta()])
    def test_tool_output_is_never_written(self, get_row, request):
        index.process(session_job())
        self.assertEqual(calls(request), [("DELETE", SESSION_TARGET)])

    @patch("index._request", return_value=(200, json.dumps({"items": [{"delete": {"status": 404}}]}).encode()))
    @patch("index._get_row", return_value=meta(removedAt=1, eventSeq=1200))
    def test_removal_deletes_every_seq_in_bulk(self, get_row, request):
        index.process(session_job("META"))
        self.assertEqual([c.args[:2] for c in request.call_args_list], [("POST", "/_bulk")] * 3)
        body = request.call_args_list[-1].args[2].decode().splitlines()
        self.assertEqual(len(body), 200)
        self.assertEqual(json.loads(body[-1])["delete"]["_id"], f"session:{SID}:1200")

    @patch("index._request")
    @patch("index._get_row", return_value=meta())
    def test_a_resumed_session_keeps_its_documents(self, get_row, request):
        index.process(session_job("META"))
        request.assert_not_called()

    @patch("index._request", return_value=(200, json.dumps({"items": [{"delete": {"status": 500}}]}).encode()))
    @patch("index._get_row", return_value=meta(removedAt=1, eventSeq=1))
    def test_a_failed_bulk_delete_retries_the_job(self, get_row, request):
        with self.assertRaises(RuntimeError):
            index.process(session_job("META"))


if __name__ == "__main__":
    unittest.main()
