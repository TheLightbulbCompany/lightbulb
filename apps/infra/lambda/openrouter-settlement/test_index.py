"""Focused money-path checks; run with `python3 -m unittest discover -s .`."""
import importlib.util
import io
import json
import pathlib
import unittest
from decimal import Decimal
from unittest.mock import patch
import os
import sys
import time
import types
from datetime import date, timedelta

spec = importlib.util.spec_from_file_location("openrouter_settlement", pathlib.Path(__file__).with_name("index.py"))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class SettlementTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"OPENROUTER_MARKUP": "1.2"})
        env.start()
        self.addCleanup(env.stop)
        self.row = {
            "request_id": "req-1", "owner_id": "owner-1", "key_hash": "old-hash",
            "secret_name": "old-secret", "generation_id": "gen-1", "state": "pending",
            "created_at": 1_700_000_000, "reservation_microcents": 300_000,
        }
        self.generation = {
            "id": "gen-1", "total_cost": Decimal("0.012345678"),
            "created_at": "2023-11-14T22:13:20Z", "model": "anthropic/claude-sonnet-4.5",
            "provider_name": "Anthropic", "cancelled": True, "tokens_prompt": 123,
            "tokens_completion": 45, "native_tokens_cached": 6, "router": "openrouter/auto",
        }
        # A required settle_one callback a given test does not expect to be reached.
        self._unused = lambda *_: self.fail("unexpected callback invocation")

    def test_exact_nanodollars_and_canceled_generation(self):
        event = worker.event_for(self.row, self.generation)
        self.assertEqual(event["transaction_id"], "gen-1")
        self.assertEqual(event["customer_id"], "owner-1")
        self.assertEqual(event["event_type"], "openrouter_inference")
        self.assertEqual(event["properties"]["provider_cost_nanodollars"], "12345678")
        self.assertEqual(event["properties"]["cancelled"], "true")

    def test_fractional_nanodollar_is_not_rounded(self):
        with self.assertRaises(worker.PendingSettlement):
            worker.cost_nanodollars(Decimal("0.0000000001"))
        with self.assertRaises(worker.PendingSettlement):
            worker.cost_nanodollars(Decimal("0.00000000100000000000000000000000000001"))

    def test_one_nanodollar_is_billable_without_zero_rounding(self):
        generation = {**self.generation, "total_cost": Decimal("0.000000001")}
        event = worker.event_for(self.row, generation)
        self.assertEqual(event["properties"]["provider_cost_nanodollars"], "1")
        calls = []
        class Ddb:
            def transact_write_items(self, **kwargs): calls.append(kwargs)
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        store.commit(self.row, event, 1, 1_700_000_001)
        self.assertEqual(calls[0]["TransactItems"][2]["Update"]["ExpressionAttributeValues"][":debit"]["N"], "-1")
        self.assertEqual(calls[0]["TransactItems"][1]["Update"]["ExpressionAttributeValues"][":cost"]["S"], "0.000000001")

    def test_debit_applies_markup_and_fails_closed_without_it(self):
        # $0.001 provider cost x 1.2 = $0.0012 = 1200 microcents (the Metronome rate's result).
        self.assertEqual(worker.debit_microcents(1_000_000), 1200)
        self.assertEqual(worker.debit_microcents(1), 1)  # rounds up, never to zero
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                worker.debit_microcents(1)
        with patch.dict(os.environ, {"OPENROUTER_MARKUP": "0.9"}):
            with self.assertRaises(ValueError):
                worker.debit_microcents(1)

    def test_unknown_generation_stays_pending(self):
        row = {**self.row, "generation_id": ""}
        calls = []
        result = worker.settle_one(row, get_generation=lambda *_: calls.append("lookup"),
                                   ingest=lambda *_: calls.append("ingest"),
                                   commit=lambda *_: calls.append("commit"), now=1_700_000_001,
                                   commit_free=self._unused, release_unknown=self._unused,
                                   release_stale_metadata=self._unused)
        self.assertEqual(result, "unknown_generation")
        self.assertEqual(calls, [])

    def test_metadata_pending_zero_reservation_released_after_24h_nonzero_stays_pending(self):
        old_created = 1_700_000_001 - worker.STALE_METADATA_RELEASE_S
        zero_row = {**self.row, "created_at": old_created, "reservation_microcents": 0}
        calls = []
        result = worker.settle_one(
            zero_row, get_generation=lambda *_: None, ingest=lambda *_: self.fail("ingested"),
            commit=lambda *_: self.fail("committed"), now=1_700_000_001,
            commit_free=self._unused, release_unknown=self._unused,
            release_stale_metadata=lambda row, now: calls.append((row, now)) or "released_no_generation",
        )
        self.assertEqual(result, "released_no_generation")
        self.assertEqual(len(calls), 1)

        held_row = {**self.row, "created_at": old_created}  # reservation_microcents=300_000 from fixture
        result = worker.settle_one(
            held_row, get_generation=lambda *_: None, ingest=lambda *_: self.fail("ingested"),
            commit=lambda *_: self.fail("committed"), now=1_700_000_001,
            commit_free=self._unused, release_unknown=self._unused,
            release_stale_metadata=lambda *_: self.fail("released a row holding a reservation"),
        )
        self.assertEqual(result, "metadata_pending")

    def test_bad_markup_fails_before_metronome_ingest(self):
        with patch.dict(os.environ, {"OPENROUTER_MARKUP": "0.9"}):
            with self.assertRaises(ValueError):
                worker.settle_one(self.row, get_generation=lambda *_: self.generation,
                                  ingest=lambda *_: self.fail("ingested"), commit=lambda *_: self.fail("committed"),
                                  now=1_700_000_001, commit_free=self._unused, release_unknown=self._unused,
                                  release_stale_metadata=self._unused)

    def test_free_zero_cost_skips_metronome(self):
        row = {**self.row, "billing_mode": "free", "reservation_microcents": 0}
        generation = {**self.generation, "total_cost": Decimal("0")}
        calls = []
        result = worker.settle_one(
            row, get_generation=lambda *_: generation,
            ingest=lambda *_: self.fail("free generation ingested into Metronome"),
            commit=lambda *_: self.fail("paid commit used"),
            commit_free=lambda *args: calls.append(args) or "settled",
            release_unknown=self._unused, release_stale_metadata=self._unused, now=1_700_000_001,
        )
        self.assertEqual(result, "settled")
        self.assertEqual(len(calls), 1)

    def test_free_positive_cost_fails_closed_without_ingest(self):
        row = {**self.row, "billing_mode": "free", "reservation_microcents": 0}
        with self.assertRaisesRegex(worker.PendingSettlement, "positive cost"):
            worker.settle_one(
                row, get_generation=lambda *_: self.generation,
                ingest=lambda *_: self.fail("unexpected cost ingested"),
                commit=lambda *_: self.fail("paid commit used"),
                commit_free=lambda *_: self.fail("free commit used"),
                release_unknown=self._unused, release_stale_metadata=self._unused, now=1_700_000_001,
            )

    def test_capped_positive_cost_settles_without_metronome(self):
        row = {**self.row, "billing_mode": "capped", "reservation_microcents": 0}
        calls = []
        result = worker.settle_one(
            row, get_generation=lambda *_: self.generation,
            ingest=lambda *_: self.fail("capped generation ingested into Metronome"),
            commit=lambda *_: self.fail("paid commit used"),
            commit_free=lambda *args: calls.append(args) or "settled",
            release_unknown=self._unused, release_stale_metadata=self._unused, now=1_700_000_001,
        )
        self.assertEqual(result, "settled")
        self.assertEqual(calls[0][3], 12345678)

    def test_rotation_uses_historical_secret_and_retry_is_idempotent(self):
        seen = []
        committed = set()
        def lookup(generation_id, row):
            seen.append((generation_id, row["secret_name"]))
            return self.generation
        def ingest(event):
            seen.append(event["transaction_id"])
        def commit(row, event, amount, now):
            if event["transaction_id"] in committed:
                return "settled"
            committed.add(event["transaction_id"])
            return "settled"
        for _ in range(2):
            self.assertEqual(worker.settle_one(self.row, get_generation=lookup, ingest=ingest,
                                                commit=commit, now=1_700_000_001, commit_free=self._unused,
                                                release_unknown=self._unused,
                                                release_stale_metadata=self._unused), "settled")
        self.assertEqual(seen[:2], [("gen-1", "old-secret"), "gen-1"])
        self.assertEqual(len(committed), 1)

    def test_distinct_retry_generations_remain_distinct_events(self):
        events = []
        for generation_id in ("gen-1", "gen-2"):
            row = {**self.row, "request_id": generation_id, "generation_id": generation_id}
            generation = {**self.generation, "id": generation_id}
            worker.settle_one(row, get_generation=lambda *_: generation,
                              ingest=events.append, commit=lambda *_: "settled",
                              now=1_700_000_001, commit_free=self._unused,
                              release_unknown=self._unused, release_stale_metadata=self._unused)
        self.assertEqual([event["transaction_id"] for event in events], ["gen-1", "gen-2"])

    def test_activity_gap_and_30_day_completed_utc_boundary(self):
        self.assertTrue(worker.activity_date_allowed("2026-08-21", "2026-09-20"))
        self.assertFalse(worker.activity_date_allowed("2026-08-20", "2026-09-20"))
        self.assertFalse(worker.activity_date_allowed("2026-09-20", "2026-09-20"))
        result = worker.activity_gap(
            [{"date": "2026-09-19", "usage": Decimal("0.002")}, {"date": "2026-09-19", "usage": Decimal("0.001")}],
            [{"date": "2026-09-19", "key_hash": "old-hash", "total_cost": "0.002"}],
            key_hash="old-hash", date="2026-09-19",
        )
        self.assertEqual(result, Decimal("0.001"))
        self.assertEqual(worker.activity_gap(
            [{"date": "2026-09-19", "usage": Decimal("0.012345678")}],
            [{"date": "2026-09-19", "key_hash": "old-hash", "total_cost": "0.012345678"}],
            key_hash="old-hash", date="2026-09-19"), Decimal(0))
        self.assertEqual(worker.activity_gap(
            [{"date": "2026-09-19", "usage": Decimal("0.012345679")}],
            [{"date": "2026-09-19", "key_hash": "old-hash", "total_cost": "0.012345678"}],
            key_hash="old-hash", date="2026-09-19"), Decimal(0))  # float noise is within tolerance
        # The live /activity shape carries a time on the date.
        self.assertEqual(worker.activity_gap(
            [{"date": "2026-09-19 00:00:00", "usage": Decimal("0.003")}],
            [{"date": "2026-09-19", "key_hash": "old-hash", "total_cost": "0.002"}],
            key_hash="old-hash", date="2026-09-19"), Decimal("0.001"))

    def test_float_rounded_activity_is_not_a_gap_but_a_cent_is(self):
        settled = [{"date": "2026-09-19", "key_hash": "k", "total_cost": "0.1"},
                   {"date": "2026-09-19", "key_hash": "k", "total_cost": "0.2"}]
        float_sum = [{"date": "2026-09-19 00:00:00", "usage": Decimal(str(0.1 + 0.2))}]
        self.assertEqual(worker.activity_gap(float_sum, settled, key_hash="k", date="2026-09-19"), Decimal(0))
        self.assertEqual(worker.activity_gap([{"date": "2026-09-19", "usage": Decimal("0.31")}], settled,
                                             key_hash="k", date="2026-09-19"), Decimal("0.01"))

    def test_audit_reports_workspace_spend_on_unknown_keys_as_unattributed(self):
        class Store:
            def all_requests(self):
                return [{"key_hash": "known", "state": "settled", "date": "2026-09-19", "total_cost": "0.5"}]
        def fetch(url, _key):
            if "/keys?" in url:
                return {"data": []}
            usage = "0.5" if "api_key_hash=known" in url else "0.75"
            return {"data": [{"date": "2026-09-19 00:00:00", "usage": Decimal(usage)}]}
        env = {"OPENROUTER_MANAGEMENT_KEY_SECRET_ID": "m", "OPENROUTER_WORKSPACE_ID": "ws", "GATE_ENV": "dev"}
        with patch.object(worker, "_secret", return_value="key"), patch.object(worker, "_http_json", side_effect=fetch), \
                patch.object(worker, "list_workspace_keys", return_value=[]), patch.dict(os.environ, env):
            gaps = worker._audit(Store(), "2026-09-19", "2026-09-20")
        self.assertEqual(gaps, {"unattributed": "0.25"})

    def test_commit_claims_generation_and_settles_projection_atomically(self):
        calls = []
        class Ddb:
            def transact_write_items(self, **kwargs):
                calls.append(kwargs)
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        result = store.commit(self.row, worker.event_for(self.row, self.generation), 12345678, 1_700_000_001)
        self.assertEqual(result, "settled")
        self.assertEqual(len(calls[0]["TransactItems"]), 3)
        self.assertEqual(calls[0]["TransactItems"][0]["Put"]["Item"]["request_id"]["S"], "generation#gen-1")
        self.assertEqual(calls[0]["TransactItems"][2]["Update"]["ExpressionAttributeValues"][":debit"]["N"], "-14815")
        self.assertEqual(calls[0]["TransactItems"][1]["Update"]["ExpressionAttributeValues"][":cost"]["S"], "0.012345678")
        self.assertEqual(calls[0]["TransactItems"][1]["Update"]["ExpressionAttributeValues"][":actual"]["N"], "12345678")

    def test_overshoot_keeps_real_debit_and_emits_metric(self):
        calls = []
        class Ddb:
            def transact_write_items(self, **kwargs): calls.append(kwargs)
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        row = {**self.row, "reservation_microcents": 10_000}
        out = io.StringIO()
        with patch.dict(os.environ, {"GATE_ENV": "dev"}), patch("sys.stdout", out):
            self.assertEqual(store.commit(row, worker.event_for(row, self.generation), 12345678, 1_700_000_001), "settled")
        self.assertEqual(calls[0]["TransactItems"][2]["Update"]["ExpressionAttributeValues"][":debit"]["N"], "-14815")
        line = json.loads(out.getvalue())
        self.assertEqual(line["openrouter.settlement.overshoot_microcents"], 4815)
        self.assertEqual((line["env"], line["service"]), ("dev", "openrouter-settlement"))
        self.assertEqual(line["_aws"]["CloudWatchMetrics"][0]["Namespace"], "LightbulbAI")

    def test_within_reservation_emits_nothing(self):
        class Ddb:
            def transact_write_items(self, **kwargs): pass
        out = io.StringIO()
        with patch("sys.stdout", out):
            worker.Store(Ddb(), requests_table="requests", fast_table="fast").commit(
                self.row, worker.event_for(self.row, self.generation), 12345678, 1_700_000_001)
        self.assertEqual(out.getvalue(), "")

    def test_free_commit_never_touches_fast_gate(self):
        calls = []
        class Ddb:
            def transact_write_items(self, **kwargs):
                calls.append(kwargs)
        row = {**self.row, "billing_mode": "free", "reservation_microcents": 0}
        generation = {**self.generation, "total_cost": Decimal("0")}
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        result = store.commit_free(row, worker.event_for(row, generation), 1_700_000_001)
        self.assertEqual(result, "settled")
        self.assertEqual(len(calls[0]["TransactItems"]), 2)
        self.assertTrue(all(operation.get("Update", {}).get("TableName") != "fast" for operation in calls[0]["TransactItems"]))

    def test_capped_commit_records_cost_and_never_touches_fast_gate(self):
        calls = []
        class Ddb:
            def transact_write_items(self, **kwargs):
                calls.append(kwargs)
        row = {**self.row, "billing_mode": "capped", "reservation_microcents": 0}
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        result = store.commit_free(row, worker.event_for(row, self.generation), 1_700_000_001, 12345678)
        self.assertEqual(result, "settled")
        self.assertEqual(len(calls[0]["TransactItems"]), 2)
        values = calls[0]["TransactItems"][1]["Update"]["ExpressionAttributeValues"]
        self.assertEqual(values[":mode"]["S"], "capped")
        self.assertEqual(values[":actual"]["N"], "12345678")
        self.assertEqual(values[":cost"]["S"], "0.012345678")

    def test_settled_cost_is_added_to_the_owner_usage_counters_once(self):
        calls = []
        class Ddb:
            def transact_write_items(self, **kwargs):
                calls.append(kwargs)
        row = {**self.row, "billing_mode": "capped", "reservation_microcents": 0}
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast", usage_table="usage")
        store.commit_free(row, worker.event_for(row, self.generation), 1_700_000_001, 12345678)
        store.commit_free(row, worker.event_for(row, {**self.generation, "total_cost": Decimal("0")}), 1_700_000_001, 0)
        spend = [op["Update"] for op in calls[0]["TransactItems"] if op.get("Update", {}).get("TableName") == "usage"]
        self.assertEqual([u["Key"]["period"]["S"] for u in spend], ["2023-11", "lifetime"])
        self.assertTrue(all(u["Key"]["owner_id"]["S"] == "owner-1" for u in spend))
        self.assertTrue(all(u["ExpressionAttributeValues"][":spend"]["N"] == "12346" for u in spend))
        self.assertEqual(len(calls[1]["TransactItems"]), 2)  # a zero-cost generation adds nothing

    def test_late_settlement_releases_only_openrouter_hold_without_stale_reset(self):
        calls = []
        class Ddb:
            def transact_write_items(self, **kwargs):
                calls.append(kwargs)
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        store.commit(self.row, worker.event_for(self.row, self.generation), 12345678, 1_700_002_000)
        update = calls[0]["TransactItems"][2]["Update"]
        self.assertEqual(update["ExpressionAttributeValues"][":reservation"]["N"], "300000")
        self.assertIn("openrouter_reserved_microcents", update["UpdateExpression"])
        self.assertNotIn("SET reserved_microcents", update["UpdateExpression"])

    def test_generation_json_keeps_decimal_precision(self):
        with patch.object(worker.urllib.request, "urlopen", return_value=io.BytesIO(b'{"data":{"total_cost":0.012345678}}')):
            result = worker._http_json("https://openrouter.ai/api/v1/generation?id=gen-1", "secret")
        self.assertEqual(result["data"]["total_cost"], Decimal("0.012345678"))

    def test_duplicate_transaction_response_is_idempotent(self):
        class TransactionCanceledException(Exception):
            pass
        class Ddb:
            def transact_write_items(self, **kwargs):
                raise TransactionCanceledException()
            def get_item(self, **kwargs):
                return {"Item": {"state": {"S": "settled"}}}
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        # The loser of a concurrent settle (row already settled) resolves as "settled_elsewhere",
        # distinct from the winner's "settled", so it will not re-report the cost.
        self.assertEqual(store.commit(self.row, worker.event_for(self.row, self.generation), 12345678, 1_700_000_001), "settled_elsewhere")

    def test_pending_pages_past_stuck_old_requests(self):
        class Ddb:
            def query(self, **kwargs):
                if "ExclusiveStartKey" not in kwargs:
                    return {"Items": [{"request_id": {"S": "stuck"}}], "LastEvaluatedKey": {"request_id": {"S": "stuck"}}}
                return {"Items": [{"request_id": {"S": "new"}}]}
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        self.assertEqual([row["request_id"] for row in store.pending()], ["stuck", "new"])

    def test_key_audit_pages_the_configured_workspace_including_disabled_keys(self):
        urls = []
        def fetch(url, _key):
            urls.append(url)
            return {"data": [{"hash": "old", "name": "lightbulb-dev-owner", "disabled": True}]} if "offset=0" in url else {"data": []}
        keys = worker.list_workspace_keys("management", "workspace-123", fetch=fetch)
        self.assertEqual([key["hash"] for key in keys], ["old"])
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("workspace_id=workspace-123" in url and "include_disabled=true" in url for url in urls))

    def test_scheduled_previous_day_audit_uses_completed_utc_day(self):
        seen = []
        fake_boto = types.SimpleNamespace(client=lambda _: object())
        with patch.object(worker, "Store", return_value=object()), patch.object(worker, "_audit", side_effect=lambda _store, day, today: seen.append((day, today)) or {}), patch.dict(sys.modules, {"boto3": fake_boto}), patch.dict(os.environ, {"REQUESTS_TABLE": "requests", "FAST_GATE_TABLE": "fast"}):
            result = worker.handler({"audit_previous_day": True}, None)
        self.assertEqual(date.fromisoformat(seen[0][0]), date.fromisoformat(seen[0][1]) - timedelta(days=1))
        self.assertEqual(result["audited_date"], seen[0][0])

    def test_retiring_key_waits_for_pending_generation(self):
        class Store:
            def retirement_candidates(self): return [{"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring", "retirement_observed_at": "100"}]
            def pending(self): return [{"owner_id": "owner-1", "state": "pending"}]
            def claim_retirement(self, *_): self.fail("claimed")
        result = worker.retire_keys(Store(), now=2000, workspace_hashes=lambda: {"hash-1"}, disable=lambda *_: self.fail("disabled"))
        self.assertEqual(result["pending"], 1)

    def test_retirement_grace_prevents_stale_gate_request_race(self):
        observed = []
        class Store:
            def retirement_candidates(self): return [{"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring"}]
            def pending(self): return []
            def observe_retirement(self, row, now): observed.append(now); return now
        result = worker.retire_keys(Store(), now=2000, workspace_hashes=lambda: {"hash-1"}, disable=lambda *_: self.fail("disabled"))
        self.assertEqual(observed, [2000])
        self.assertEqual(result["grace"], 1)

    def test_claim_precedes_patch_and_stale_processing_retries(self):
        order = []
        class Store:
            rows = [{"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring", "retirement_observed_at": "100"}]
            def retirement_candidates(self): return self.rows
            def pending(self): return []
            def claim_retirement(self, row, now): order.append("claim"); return True
            def mark_disabled(self, row, now): order.append("disabled-row"); self.rows = []
        store = Store()
        result = worker.retire_keys(store, now=2000, workspace_hashes=lambda: {"hash-1"}, disable=lambda key_hash: order.append(f"patch:{key_hash}"))
        self.assertEqual(order, ["claim", "patch:hash-1", "disabled-row"])
        self.assertEqual(result["disabled"], 1)
        self.assertEqual(worker.retire_keys(store, now=2001, workspace_hashes=lambda: set(), disable=lambda *_: self.fail("patched twice"))["disabled"], 0)

    def test_retirement_claim_uses_conditional_state_and_stale_lease(self):
        calls = []
        class Ddb:
            def update_item(self, **kwargs): calls.append(kwargs)
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast", keys_table="keys")
        store.claim_retirement({"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring"}, 2000)
        store.claim_retirement({"owner_id": "owner-1", "key_hash": "hash-1", "state": "retirement_processing"}, 2600)
        self.assertIn("#state = :retiring", calls[0]["ConditionExpression"])
        self.assertIn("#state = :processing", calls[1]["ConditionExpression"])
        self.assertEqual(calls[1]["ExpressionAttributeValues"][":stale_before"]["N"], "2300")

    def test_stale_processing_retries_but_live_lease_does_not(self):
        order = []
        class Store:
            def retirement_candidates(self): return [{"owner_id": "owner-1", "key_hash": "hash-1", "state": "retirement_processing", "retirement_processing_at": "1000"}]
            def pending(self): return []
            def claim_retirement(self, row, now): order.append("reclaim"); return True
            def mark_disabled(self, row, now): order.append("disabled-row")
        store = Store()
        self.assertEqual(worker.retire_keys(store, now=1100, workspace_hashes=lambda: {"hash-1"}, disable=lambda *_: self.fail("premature patch"))["lease"], 1)
        self.assertEqual(worker.retire_keys(store, now=1400, workspace_hashes=lambda: {"hash-1"}, disable=lambda *_: order.append("patch"))["disabled"], 1)
        self.assertEqual(order, ["reclaim", "patch", "disabled-row"])

    def test_unretire_winning_conditional_claim_prevents_patch(self):
        class Store:
            def retirement_candidates(self): return [{"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring", "retirement_observed_at": "100"}]
            def pending(self): return []
            def claim_retirement(self, row, now): return False
        result = worker.retire_keys(Store(), now=2000, workspace_hashes=lambda: self.fail("workspace fetch"), disable=lambda *_: self.fail("patch"))
        self.assertEqual(result["raced"], 1)

    def test_retirement_event_runs_without_metronome_proof(self):
        fake_boto = types.SimpleNamespace(client=lambda _: object())
        with patch.object(worker, "Store", return_value=types.SimpleNamespace(keys_table="keys")), patch.object(worker, "_retire_scheduled", return_value={"disabled": 1}) as retire, patch.dict(sys.modules, {"boto3": fake_boto}), patch.dict(os.environ, {"REQUESTS_TABLE": "requests", "FAST_GATE_TABLE": "fast", "OPENROUTER_KEYS_TABLE": "keys"}):
            result = worker.handler({"retire_keys": True}, None)
        self.assertEqual(result["retirement"]["disabled"], 1)
        retire.assert_called_once()

    def test_disable_uses_management_patch_and_confirms_workspace(self):
        key_hash = "a" * 64
        def urlopen(request, timeout):
            self.assertEqual(request.get_method(), "PATCH")
            self.assertEqual(request.data, b'{"disabled":true}')
            self.assertTrue(request.full_url.endswith(key_hash))
            return io.BytesIO(b'{"data":{"hash":"' + key_hash.encode() + b'","workspace_id":"ws-1","disabled":true}}')
        with patch.object(worker.urllib.request, "urlopen", side_effect=urlopen):
            worker._patch_disable_key(key_hash, "management", "ws-1")
            with self.assertRaises(RuntimeError):
                worker._patch_disable_key(key_hash, "management", "other-workspace")

    def test_pending_retirement_waits_for_creation_grace(self):
        class Store:
            def retirement_candidates(self): return [{"owner_id": "owner-1", "state": "pending", "retire_requested_at": "1000", "claim_id": "claim-1"}]
        result = worker.retire_keys(Store(), now=1500, environment="dev", workspace_hashes=lambda: self.fail("listed"),
                                    orphan_keys=lambda *_: self.fail("listed"), disable=lambda *_: self.fail("patched"))
        self.assertEqual(result["grace"], 1)

    def test_pending_retirement_disables_only_active_deterministic_orphans_then_tombstones(self):
        actions = []
        class Store:
            def retirement_candidates(self): return [{"owner_id": "owner-1", "state": "pending", "retire_requested_at": "100", "claim_id": "claim-1"}]
            def claim_pending_retirement(self, row, now): actions.append("claim"); return True
            def tombstone_pending(self, row, now): actions.append("tombstone")
        keys = [
            {"name": "lightbulb-dev-owner-1", "hash": "active", "disabled": False},
            {"name": "lightbulb-dev-owner-1", "hash": "already-off", "disabled": True},
            {"name": "lightbulb-dev-other", "hash": "other", "disabled": False},
        ]
        result = worker.retire_keys(Store(), now=2000, environment="dev", workspace_hashes=lambda: self.fail("hashes"),
                                    orphan_keys=lambda *_: keys, disable=lambda key_hash: actions.append(f"patch:{key_hash}"))
        self.assertEqual(actions, ["claim", "patch:active", "tombstone"])
        self.assertEqual(result["orphan_keys_disabled"], 1)

    def test_pending_retirement_api_failure_preserves_row_for_retry(self):
        class Store:
            def retirement_candidates(self): return [{"owner_id": "owner-1", "state": "pending", "retire_requested_at": "100", "claim_id": "claim-1"}]
            def claim_pending_retirement(self, row, now): return True
            def tombstone_pending(self, row, now): self.fail("tombstoned before disable")
        result = worker.retire_keys(Store(), now=2000, environment="dev", workspace_hashes=lambda: set(),
                                    orphan_keys=lambda *_: [{"name": "lightbulb-dev-owner-1", "hash": "active", "disabled": False}],
                                    disable=lambda *_: (_ for _ in ()).throw(RuntimeError("OpenRouter outage")))
        self.assertEqual(result["failed"], 1)

    def test_pending_orphan_retries_after_stale_processing_lease(self):
        actions = []
        class Store:
            row = {"owner_id": "owner-1", "state": "pending", "retire_requested_at": "100", "claim_id": "claim-1"}
            def retirement_candidates(self): return [self.row]
            def claim_pending_retirement(self, row, now): self.row = {**row, "state": "retirement_processing", "retirement_processing_at": str(now)}; return True
            def tombstone_pending(self, row, now): actions.append("tombstone"); self.row = {**row, "state": "disabled"}
        store = Store()
        orphans = lambda *_: [{"name": "lightbulb-dev-owner-1", "hash": "active", "disabled": False}]
        self.assertEqual(worker.retire_keys(store, now=2000, environment="dev", workspace_hashes=lambda: set(), orphan_keys=orphans,
                                            disable=lambda *_: (_ for _ in ()).throw(RuntimeError("outage")))["failed"], 1)
        self.assertEqual(store.row["state"], "retirement_processing")
        self.assertEqual(worker.retire_keys(store, now=2100, environment="dev", workspace_hashes=lambda: set(), orphan_keys=orphans,
                                            disable=lambda *_: self.fail("lease not stale"))["lease"], 1)
        worker.retire_keys(store, now=2301, environment="dev", workspace_hashes=lambda: set(), orphan_keys=orphans,
                           disable=lambda *_: actions.append("patch"))
        self.assertEqual(actions, ["patch", "tombstone"])

    def test_pending_tombstone_is_conditional_on_same_retirement_and_claim(self):
        calls = []
        class Ddb:
            def update_item(self, **kwargs): calls.append(kwargs)
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast", keys_table="keys")
        row = {"owner_id": "owner-1", "state": "pending", "retire_requested_at": "100", "claim_id": "claim-1"}
        store.claim_pending_retirement(row, 2000)
        store.tombstone_pending(row, 2000)
        self.assertIn("#state = :pending", calls[0]["ConditionExpression"])
        self.assertIn("#state = :processing", calls[1]["ConditionExpression"])
        self.assertEqual(calls[0]["ExpressionAttributeValues"][":requested"]["N"], "100")
        self.assertEqual(calls[0]["ExpressionAttributeValues"][":claim"]["S"], "claim-1")

    def test_retirement_scan_includes_pending_rows_with_request_flag(self):
        calls = []
        class Ddb:
            def scan(self, **kwargs): calls.append(kwargs); return {"Items": []}
        worker.Store(Ddb(), requests_table="requests", fast_table="fast", keys_table="keys").retirement_candidates()
        self.assertIn("attribute_exists(retire_requested_at)", calls[0]["FilterExpression"])


    def test_one_failing_retirement_does_not_strand_the_next(self):
        class Store:
            def retirement_candidates(self): return [
                {"owner_id": "gone", "key_hash": "missing", "state": "retiring", "retirement_observed_at": "100"},
                {"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring", "retirement_observed_at": "100"}]
            def pending(self): return []
            def claim_retirement(self, row, now): return True
            def mark_disabled(self, row, now): disabled.append(row["owner_id"])
        disabled = []
        result = worker.retire_keys(Store(), now=2000, workspace_hashes=lambda: {"hash-1"}, disable=lambda *_: None)
        self.assertEqual((result["failed"], result["disabled"], disabled), (1, 1, ["owner-1"]))


class ScheduledRunTests(unittest.TestCase):
    """One scheduled tick end to end: settle free/capped rows, never metered, then retire."""

    class Store:
        keys_table = "keys"

        def __init__(self, rows, keys=()):
            self.rows = {row["request_id"]: dict(row) for row in rows}
            self.keys = {key["owner_id"]: dict(key) for key in keys}

        def pending(self):
            return [dict(row) for row in self.rows.values() if row["state"] == "pending"]

        def commit_free(self, row, event, now, amount=0):
            self.rows[row["request_id"]].update(state="settled", provider_cost_nanodollars=amount)
            return "settled"

        def commit(self, *_):
            raise AssertionError("metered row reached the Metronome commit")

        def release_unknown(self, row, now):
            self.rows[row["request_id"]]["state"] = "released"
            return "released_no_generation"

        def release_stale_metadata(self, row, now):
            self.rows[row["request_id"]]["state"] = "released"
            return "released_no_generation"

        def retirement_candidates(self):
            return [dict(key) for key in self.keys.values() if key["state"] in ("retiring", "retirement_processing")]

        def claim_retirement(self, row, now):
            self.keys[row["owner_id"]]["state"] = "retirement_processing"
            return True

        def mark_disabled(self, row, now):
            self.keys[row["owner_id"]]["state"] = "disabled"

    def run_tick(self, store, context=None):
        now = int(time.time())
        generations = {row["generation_id"]: {"id": row["generation_id"], "total_cost": cost, "created_at": "2026-09-22T00:00:00Z"}
                       for row, cost in self.costs}
        env = {"REQUESTS_TABLE": "requests", "FAST_GATE_TABLE": "fast", "OPENROUTER_KEYS_TABLE": "keys", "GATE_ENV": "dev",
               "OPENROUTER_WORKSPACE_ID": "ws-1", "OPENROUTER_MANAGEMENT_KEY_SECRET_ID": "management"}
        self.patched = []
        with patch.dict(os.environ, env), patch.dict(sys.modules, {"boto3": types.SimpleNamespace(client=lambda _: object())}), \
                patch.object(worker, "Store", return_value=store), \
                patch.object(worker, "_get_generation", side_effect=lambda gen, _secret: generations[gen]), \
                patch.object(worker, "_ingest", side_effect=AssertionError("Metronome ingest")) as ingest, \
                patch.object(worker, "_secret", return_value="management-key"), \
                patch.object(worker, "list_workspace_keys", return_value=[{"hash": "hash-1"}]), \
                patch.object(worker, "_patch_disable_key", side_effect=lambda key_hash, *_: self.patched.append(key_hash)):
            try:
                return worker.handler({"source": "aws.events"}, context)
            finally:
                ingest.assert_not_called()

    def row(self, request_id, owner, mode):
        return {"request_id": request_id, "owner_id": owner, "key_hash": "hash-1", "secret_name": "s", "billing_mode": mode,
                "generation_id": f"gen-{request_id}", "state": "pending", "created_at": int(time.time()) - 600,
                "reservation_microcents": 0}

    def test_capped_and_free_settle_at_cost_without_metronome(self):
        capped, free = self.row("capped", "owner-1", "capped"), self.row("free", "owner-1", "free")
        self.costs = [(capped, Decimal("0.012345678")), (free, Decimal("0"))]
        store = self.Store([capped, free])
        result = self.run_tick(store)
        self.assertEqual(result["settlement"], {"settled": 2})
        self.assertEqual(store.rows["capped"]["provider_cost_nanodollars"], 12_345_678)
        self.assertEqual(store.rows["free"]["provider_cost_nanodollars"], 0)

    def test_wiped_owner_settles_then_retires_in_the_same_tick(self):
        last = self.row("last", "owner-1", "capped")
        self.costs = [(last, Decimal("0.001"))]
        store = self.Store([last], keys=[{"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring",
                                          "retirement_observed_at": str(int(time.time()) - 961)}])
        result = self.run_tick(store)
        self.assertEqual((store.rows["last"]["state"], store.keys["owner-1"]["state"], self.patched), ("settled", "disabled", ["hash-1"]))
        self.assertEqual(result["retirement"]["disabled"], 1)

    def test_unmetered_row_without_generation_is_released_after_an_hour_metered_is_not(self):
        old = int(time.time()) - 3601
        rows = [{**self.row(name, "owner-1", mode), "created_at": created} for name, mode, created in
                [("stale", "capped", old), ("fresh", "free", int(time.time()) - 600), ("paid", "paid", old)]]
        for row in rows:
            del row["generation_id"]
        self.costs = []
        store = self.Store(rows)
        with self.assertRaisesRegex(RuntimeError, "unknown_generation") as raised:
            self.run_tick(store)
        self.assertIn("released_no_generation", str(raised.exception))
        self.assertEqual({key: row["state"] for key, row in store.rows.items()}, {"stale": "released", "fresh": "pending", "paid": "pending"})
        store.rows["fresh"]["created_at"] = old
        del store.rows["paid"]
        self.assertEqual(self.run_tick(store)["settlement"], {"released_no_generation": 1})

    def test_release_unknown_is_conditional_on_no_generation_and_unmetered(self):
        seen = {}
        class Ddb:
            def update_item(self, **kwargs): seen.update(kwargs)
        row = {"request_id": "req-1", "billing_mode": "capped"}
        self.assertEqual(worker.Store(Ddb(), requests_table="requests", fast_table="fast").release_unknown(row, 5), "released_no_generation")
        self.assertIn("attribute_not_exists(generation_id)", seen["ConditionExpression"])
        self.assertIn("billing_mode IN (:free, :capped)", seen["ConditionExpression"])

    def test_backlog_defers_near_the_timeout_and_still_retires(self):
        row = self.row("late", "owner-2", "capped")
        self.costs = [(row, Decimal("0.001"))]
        store = self.Store([row], keys=[{"owner_id": "owner-1", "key_hash": "hash-1", "state": "retiring",
                                         "retirement_observed_at": str(int(time.time()) - 961)}])
        result = self.run_tick(store, types.SimpleNamespace(get_remaining_time_in_millis=lambda: 30_000))
        self.assertEqual((result["settlement"], store.rows["late"]["state"], result["retirement"]["disabled"]), ({"deferred": 1}, "pending", 1))


class OwnerKeyTests(unittest.TestCase):
    """The settlement opens the envelope the backend's integration_secret_store seals."""

    def test_opens_the_envelope_and_falls_back_to_the_legacy_secret(self):
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        row = {"owner_id": "org_a", "key_hash": "h1", "secret_name": "lightbulb/dev/openrouter/owners/org_a/h1"}
        provider = "openrouter/h1"
        data_key, nonce = os.urandom(32), os.urandom(12)
        sealed = AESGCM(data_key).encrypt(nonce, json.dumps({"key": "sk-or-new"}).encode(), b"org_a\0openrouter/h1")
        b64 = lambda b: {"S": base64.b64encode(b).decode()}  # noqa: E731
        items = {("org_a", provider): {"data_key": b64(b"wrapped"), "nonce": b64(nonce), "ciphertext": b64(sealed)}}
        contexts = []

        class Ddb:
            def get_item(self, TableName, Key, ConsistentRead):
                item = items.get((Key["owner_id"]["S"], Key["provider"]["S"]))
                return {"Item": item} if item else {}

        class Kms:
            def decrypt(self, CiphertextBlob, EncryptionContext):
                contexts.append(EncryptionContext)
                return {"Plaintext": data_key}

        fake_boto3 = types.SimpleNamespace(client=lambda name: {"dynamodb": Ddb(), "kms": Kms()}[name])
        with patch.dict(sys.modules, {"boto3": fake_boto3}), \
                patch.dict(os.environ, {"CREDENTIAL_ENVELOPES_TABLE": "credential-envelopes"}), \
                patch.object(worker, "_secret", side_effect=lambda name: f"legacy:{name}"):
            self.assertEqual(worker._owner_key(row), "sk-or-new")
            self.assertEqual(contexts, [{"owner_id": "org_a", "provider": provider}])
            items.clear()
            self.assertEqual(worker._owner_key(row), f"legacy:{row['secret_name']}")


if __name__ == "__main__":
    unittest.main()
