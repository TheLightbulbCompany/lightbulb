from __future__ import annotations

import json
import unittest
from unittest.mock import patch

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

    def test_rejects_non_message_rows(self):
        with self.assertRaises(ValueError):
            index.process({"ownerId": "org_owner", "roomId": ROOM, "sk": "META"})


if __name__ == "__main__":
    unittest.main()
