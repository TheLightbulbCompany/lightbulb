"""Focused money-path checks; run with `python3 -m unittest discover -s .`."""
import importlib.util
import io
import pathlib
import unittest
from decimal import Decimal
from unittest.mock import patch
import os
import sys
import types
from datetime import date, timedelta

spec = importlib.util.spec_from_file_location("openrouter_settlement", pathlib.Path(__file__).with_name("index.py"))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class SettlementTests(unittest.TestCase):
    def setUp(self):
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

    def test_unknown_generation_stays_pending(self):
        row = {**self.row, "generation_id": ""}
        calls = []
        result = worker.settle_one(row, get_generation=lambda *_: calls.append("lookup"),
                                   ingest=lambda *_: calls.append("ingest"),
                                   commit=lambda *_: calls.append("commit"), verified=True,
                                   now=1_700_000_001)
        self.assertEqual(result, "unknown_generation")
        self.assertEqual(calls, [])

    def test_unverified_money_contract_never_ingests(self):
        with self.assertRaises(worker.MoneyContractUnverified):
            worker.settle_one(self.row, get_generation=lambda *_: self.generation,
                              ingest=lambda *_: self.fail("ingested"), commit=lambda *_: None,
                              verified=False, now=1_700_000_001)

    def test_free_zero_cost_skips_metronome_and_money_proof(self):
        row = {**self.row, "billing_mode": "free", "reservation_microcents": 0}
        generation = {**self.generation, "total_cost": Decimal("0")}
        calls = []
        result = worker.settle_one(
            row, get_generation=lambda *_: generation,
            ingest=lambda *_: self.fail("free generation ingested into Metronome"),
            commit=lambda *_: self.fail("paid commit used"),
            commit_free=lambda *args: calls.append(args) or "settled",
            verified=False, now=1_700_000_001,
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
                verified=True, now=1_700_000_001,
            )

    def test_capped_positive_cost_settles_without_metronome_even_unverified(self):
        row = {**self.row, "billing_mode": "capped", "reservation_microcents": 0}
        calls = []
        result = worker.settle_one(
            row, get_generation=lambda *_: self.generation,
            ingest=lambda *_: self.fail("capped generation ingested into Metronome"),
            commit=lambda *_: self.fail("paid commit used"),
            commit_free=lambda *args: calls.append(args) or "settled",
            verified=False, now=1_700_000_001,
        )
        self.assertEqual(result, "settled")
        self.assertEqual(calls[0][3], 12345678)

    def test_rotation_uses_historical_secret_and_retry_is_idempotent(self):
        seen = []
        committed = set()
        def lookup(generation_id, secret_name):
            seen.append((generation_id, secret_name))
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
                                                commit=commit, verified=True, now=1_700_000_001), "settled")
        self.assertEqual(seen[:2], [("gen-1", "old-secret"), "gen-1"])
        self.assertEqual(len(committed), 1)

    def test_distinct_retry_generations_remain_distinct_events(self):
        events = []
        for generation_id in ("gen-1", "gen-2"):
            row = {**self.row, "request_id": generation_id, "generation_id": generation_id}
            generation = {**self.generation, "id": generation_id}
            worker.settle_one(row, get_generation=lambda *_: generation,
                              ingest=events.append, commit=lambda *_: "settled",
                              verified=True, now=1_700_000_001)
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
            key_hash="old-hash", date="2026-09-19"), Decimal("0.000000001"))

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
        self.assertEqual(calls[0]["TransactItems"][2]["Update"]["ExpressionAttributeValues"][":debit"]["N"], "-12346")
        self.assertEqual(calls[0]["TransactItems"][1]["Update"]["ExpressionAttributeValues"][":cost"]["S"], "0.012345678")
        self.assertEqual(calls[0]["TransactItems"][1]["Update"]["ExpressionAttributeValues"][":actual"]["N"], "12345678")

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
        self.assertEqual(store.commit(self.row, worker.event_for(self.row, self.generation), 12345678, 1_700_000_001), "settled")

    def test_pending_pages_past_stuck_old_requests(self):
        class Ddb:
            def query(self, **kwargs):
                if "ExclusiveStartKey" not in kwargs:
                    return {"Items": [{"request_id": {"S": "stuck"}}], "LastEvaluatedKey": {"request_id": {"S": "stuck"}}}
                return {"Items": [{"request_id": {"S": "new"}}]}
        store = worker.Store(Ddb(), requests_table="requests", fast_table="fast")
        self.assertEqual([row["request_id"] for row in store.pending()], ["stuck", "new"])

    def test_lambda_handler_fails_closed_without_money_proof(self):
        with patch.dict(os.environ, {"OPENROUTER_MONEY_CONTRACT_VERIFIED": "false", "OPENROUTER_RATE_CARD_PROOF_ID": ""}):
            with self.assertRaises(worker.MoneyContractUnverified):
                worker.handler({}, None)

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
        with patch.object(worker, "_verified", return_value=True), patch.object(worker, "Store", return_value=object()), patch.object(worker, "_audit", side_effect=lambda _store, day, today: seen.append((day, today)) or {}), patch.dict(sys.modules, {"boto3": fake_boto}), patch.dict(os.environ, {"REQUESTS_TABLE": "requests", "FAST_GATE_TABLE": "fast"}):
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
        with patch.object(worker, "_verified", return_value=False), patch.object(worker, "Store", return_value=types.SimpleNamespace(keys_table="keys")), patch.object(worker, "_retire_scheduled", return_value={"disabled": 1}) as retire, patch.dict(sys.modules, {"boto3": fake_boto}), patch.dict(os.environ, {"REQUESTS_TABLE": "requests", "FAST_GATE_TABLE": "fast", "OPENROUTER_KEYS_TABLE": "keys"}):
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
        with self.assertRaisesRegex(RuntimeError, "OpenRouter outage"):
            worker.retire_keys(Store(), now=2000, environment="dev", workspace_hashes=lambda: set(),
                               orphan_keys=lambda *_: [{"name": "lightbulb-dev-owner-1", "hash": "active", "disabled": False}],
                               disable=lambda *_: (_ for _ in ()).throw(RuntimeError("OpenRouter outage")))

    def test_pending_orphan_retries_after_stale_processing_lease(self):
        actions = []
        class Store:
            row = {"owner_id": "owner-1", "state": "pending", "retire_requested_at": "100", "claim_id": "claim-1"}
            def retirement_candidates(self): return [self.row]
            def claim_pending_retirement(self, row, now): self.row = {**row, "state": "retirement_processing", "retirement_processing_at": str(now)}; return True
            def tombstone_pending(self, row, now): actions.append("tombstone"); self.row = {**row, "state": "disabled"}
        store = Store()
        orphans = lambda *_: [{"name": "lightbulb-dev-owner-1", "hash": "active", "disabled": False}]
        with self.assertRaisesRegex(RuntimeError, "outage"):
            worker.retire_keys(store, now=2000, environment="dev", workspace_hashes=lambda: set(), orphan_keys=orphans,
                               disable=lambda *_: (_ for _ in ()).throw(RuntimeError("outage")))
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


if __name__ == "__main__":
    unittest.main()
