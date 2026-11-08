"""OpenRouter generation settlement. No money writes without a proven rate contract."""
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class PendingSettlement(Exception):
    """Generation metadata or precision is not yet safe to bill."""


def cost_nanodollars(value):
    try:
        cost = Decimal(str(value))
        with localcontext() as context:
            context.prec = max(28, len(cost.as_tuple().digits) + 10)
            amount = cost * Decimal(1_000_000_000)
        if not cost.is_finite() or cost < 0 or amount != amount.to_integral_value():
            raise PendingSettlement("OpenRouter cost is not an exact nonnegative nanodollar")
        return int(amount)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PendingSettlement("OpenRouter cost is unavailable") from exc


def debit_microcents(nanodollars):
    """Provisional fast-gate debit: provider cost x OPENROUTER_MARKUP, in microcents
    (1 microcent = 1000 nanodollars), rounded up. Must match the Metronome rate
    (openrouter-pricing.json); fails closed when the markup is not configured."""
    markup = Decimal(os.environ["OPENROUTER_MARKUP"])
    if not markup.is_finite() or markup < 1:
        raise ValueError("OPENROUTER_MARKUP must be a decimal >= 1")
    return int((Decimal(nanodollars) * markup / 1000).to_integral_value(rounding=ROUND_CEILING))


def event_for(row, generation):
    if generation.get("id") != row.get("generation_id"):
        raise PendingSettlement("Generation ID mismatch")
    if not generation.get("created_at"):
        raise PendingSettlement("Generation timestamp missing")
    amount = cost_nanodollars(generation.get("total_cost"))
    fields = {
        "provider_cost_nanodollars": amount,
        "model": generation.get("model") or "",
        "provider": generation.get("provider_name") or "",
        "prompt_tokens": generation.get("tokens_prompt") or 0,
        "completion_tokens": generation.get("tokens_completion") or 0,
        "cache_tokens": generation.get("native_tokens_cached") or 0,
        "cancelled": str(bool(generation.get("cancelled"))).lower(),
        "router": generation.get("router") or "",
        "key_hash": row["key_hash"],
        "gate_request_id": row["request_id"],
    }
    return {
        "transaction_id": generation["id"],
        "customer_id": row["owner_id"],
        "timestamp": generation["created_at"],
        "event_type": "openrouter_inference",
        "properties": {key: str(value) for key, value in fields.items()},
    }


# A generation_id whose OpenRouter metadata never shows up (observed on $0 free-model
# generations) must not count as metadata_pending forever; only rows holding no reservation
# are eligible for release -- a nonzero hold is real money and must stay visible.
STALE_METADATA_RELEASE_S = 24 * 3600


def settle_one(row, *, get_generation, ingest, commit, now, commit_free, release_unknown,
                release_stale_metadata):
    mode = row.get("billing_mode")
    # free and capped rows never touch Metronome: a capped owner's valve is the
    # OpenRouter per-key daily limit, so its cost is recorded, not debited.
    unmetered = mode in ("free", "capped")
    if row.get("state") != "pending":
        return "already_processed"
    if not row.get("generation_id"):
        # The gate writes the row before calling upstream, so a gate crash before the
        # first chunk leaves a row that can never settle. Unmetered rows hold nothing;
        # release them after an hour. Metered rows keep their hold for a human.
        if unmetered and now - int(row["created_at"]) >= 3600:
            return release_unknown(row, now)
        return "unknown_generation"
    if now - int(row["created_at"]) >= 30 * 24 * 3600:
        return "expired_gap"  # outside OpenRouter's 30 completed-day activity window
    generation = get_generation(row["generation_id"], row["secret_name"])
    if not generation:
        if (int(row["reservation_microcents"]) == 0
                and now - int(row["created_at"]) >= STALE_METADATA_RELEASE_S):
            return release_stale_metadata(row, now)
        return "metadata_pending"
    event = event_for(row, generation)
    amount = cost_nanodollars(generation["total_cost"])
    if unmetered:
        if mode == "free" and amount:
            raise PendingSettlement("Free OpenRouter generation reported positive cost")
        outcome = commit_free(row, event, now, amount)
    else:
        debit_microcents(amount)  # a bad markup must fail before the Metronome ingest, not after
        ingest(event)  # Metronome deduplicates by generation ID for 34 days.
        outcome = commit(row, event, amount, now)
    return outcome


def activity_date_allowed(day, today):
    day, today = date.fromisoformat(day), date.fromisoformat(today)
    return today - timedelta(days=30) <= day < today


# ponytail: /activity `usage` is a float, so exact equality false-alarms; a real gap below
# max($0.000001, 1e-6 x the bucket) goes unseen. Tighten if OpenRouter ever returns exact decimals.
AUDIT_TOLERANCE = Decimal("0.000001")


def _gap(actual, expected):
    """actual - expected, or 0 when the difference is float noise."""
    diff = actual - expected
    return diff if abs(diff) > max(AUDIT_TOLERANCE, AUDIT_TOLERANCE * abs(expected)) else Decimal(0)


def activity_total(activity_rows, date):
    # OpenRouter reports the day as "YYYY-MM-DD 00:00:00" (probed 2026-09-23).
    return sum((Decimal(str(item["usage"])) for item in activity_rows if item["date"][:10] == date), Decimal(0))


def activity_gap(activity_rows, settled_rows, *, key_hash, date):
    settled = sum((Decimal(str(item["total_cost"])) for item in settled_rows
                   if item["date"] == date and item["key_hash"] == key_hash), Decimal(0))
    return _gap(activity_total(activity_rows, date), settled)


def _unmarshal(item):
    return {key: next(iter(value.values())) for key, value in item.items()}


class Store:
    def __init__(self, ddb, *, requests_table, fast_table, keys_table=None, usage_table=None):
        self.ddb, self.requests_table, self.fast_table, self.keys_table = ddb, requests_table, fast_table, keys_table
        self.usage_table = usage_table

    def _spend(self, row, event, actual_nanodollars):
        """The backend's usage counters (``usage_repo``) have no price for an OpenRouter model, so
        /billing/account's spend reads 0 unless settlement adds the real cost. Spend only: the
        backend's ``record_usage`` already counts the turn's tokens and requests. Rides the
        settle transaction, so the generation claim makes it exactly-once."""
        if not self.usage_table or not actual_nanodollars:
            return []
        return [{"Update": {
            "TableName": self.usage_table,
            "Key": {"owner_id": {"S": row["owner_id"]}, "period": {"S": period}},
            "UpdateExpression": "ADD total_spend_microdollars :spend",
            "ExpressionAttributeValues": {":spend": {"N": str((actual_nanodollars + 999) // 1000)}},  # USD x 1e6
        }} for period in (event["timestamp"][:7], "lifetime")]

    def pending(self, page_size=25, max_pages=100):
        rows, cursor = [], None
        for _ in range(max_pages):
            args = {
                "TableName": self.requests_table, "IndexName": "state-created-index",
                "KeyConditionExpression": "#state = :pending",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":pending": {"S": "pending"}},
                "Limit": page_size,
            }
            if cursor:
                args["ExclusiveStartKey"] = cursor
            response = self.ddb.query(**args)
            rows.extend(_unmarshal(item) for item in response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return rows
        raise RuntimeError("OpenRouter pending query incomplete")

    def all_requests(self, max_pages=100):
        # ponytail: bounded table scan for the initial cohort; add a key/date index before fleet-wide audit.
        rows, cursor = [], None
        for _ in range(max_pages):
            args = {"TableName": self.requests_table}
            if cursor:
                args["ExclusiveStartKey"] = cursor
            response = self.ddb.scan(**args)
            rows.extend(_unmarshal(item) for item in response.get("Items", []) if "state" in item)
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return rows
        raise RuntimeError("OpenRouter outbox scan incomplete")

    def retirement_candidates(self, max_pages=100):
        rows, cursor = [], None
        for _ in range(max_pages):
            args = {
                "TableName": self.keys_table,
                "FilterExpression": "#state IN (:retiring, :processing) OR (#state = :pending AND attribute_exists(retire_requested_at))",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":retiring": {"S": "retiring"}, ":processing": {"S": "retirement_processing"}, ":pending": {"S": "pending"}},
            }
            if cursor:
                args["ExclusiveStartKey"] = cursor
            response = self.ddb.scan(**args)
            rows.extend(_unmarshal(item) for item in response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return rows
        raise RuntimeError("OpenRouter retiring-key scan incomplete")

    def observe_retirement(self, row, now):
        response = self.ddb.update_item(
            TableName=self.keys_table, Key={"owner_id": {"S": row["owner_id"]}},
            UpdateExpression="SET retirement_observed_at = if_not_exists(retirement_observed_at, :now)",
            ConditionExpression="#state = :retiring AND key_hash = :hash",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":now": {"N": str(now)}, ":retiring": {"S": "retiring"}, ":hash": {"S": row["key_hash"]}},
            ReturnValues="ALL_NEW",
        )
        return int(response["Attributes"]["retirement_observed_at"]["N"])

    def claim_retirement(self, row, now, grace_seconds=960, lease_seconds=300):
        retiring = row["state"] == "retiring"
        condition = ("#state = :retiring AND retirement_observed_at <= :grace_before" if retiring
                     else "#state = :processing AND retirement_processing_at <= :stale_before")
        values = {":processing": {"S": "retirement_processing"}, ":now": {"N": str(now)}, ":hash": {"S": row["key_hash"]}}
        if retiring:
            values.update({":retiring": {"S": "retiring"}, ":grace_before": {"N": str(now - grace_seconds)}})
        else:
            values[":stale_before"] = {"N": str(now - lease_seconds)}
        try:
            self.ddb.update_item(
                TableName=self.keys_table, Key={"owner_id": {"S": row["owner_id"]}},
                UpdateExpression="SET #state = :processing, retirement_processing_at = :now",
                ConditionExpression=condition + " AND key_hash = :hash",
                ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues=values,
            )
            return True
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def mark_disabled(self, row, now):
        self.ddb.update_item(
            TableName=self.keys_table, Key={"owner_id": {"S": row["owner_id"]}},
            UpdateExpression="SET #state = :disabled, disabled_at = :now",
            ConditionExpression="#state = :processing AND key_hash = :hash",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":disabled": {"S": "disabled"}, ":processing": {"S": "retirement_processing"},
                                       ":hash": {"S": row["key_hash"]}, ":now": {"N": str(now)}},
        )

    def claim_pending_retirement(self, row, now, grace_seconds=960, lease_seconds=300):
        pending = row["state"] == "pending"
        condition = ("#state = :pending AND retire_requested_at <= :grace_before" if pending
                     else "#state = :processing AND retirement_processing_at <= :stale_before")
        values = {
            ":processing": {"S": "retirement_processing"}, ":now": {"N": str(now)},
            ":requested": {"N": str(row["retire_requested_at"])},
        }
        if pending:
            values.update({":pending": {"S": "pending"}, ":grace_before": {"N": str(now - grace_seconds)}})
        else:
            values[":stale_before"] = {"N": str(now - lease_seconds)}
        claim_id = row.get("claim_id")
        condition += " AND retire_requested_at = :requested AND attribute_not_exists(key_hash)"
        if claim_id:
            condition += " AND claim_id = :claim"
            values[":claim"] = {"S": claim_id}
        else:
            condition += " AND attribute_not_exists(claim_id)"
        try:
            self.ddb.update_item(
                TableName=self.keys_table, Key={"owner_id": {"S": row["owner_id"]}},
                UpdateExpression="SET #state = :processing, retirement_processing_at = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues=values,
            )
            return True
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def tombstone_pending(self, row, now):
        condition = "#state = :processing AND retire_requested_at = :requested AND attribute_not_exists(key_hash)"
        values = {
            ":processing": {"S": "retirement_processing"}, ":requested": {"N": str(row["retire_requested_at"])},
            ":disabled": {"S": "disabled"}, ":now": {"N": str(now)},
        }
        claim_id = row.get("claim_id")
        if claim_id:
            condition += " AND claim_id = :claim"
            values[":claim"] = {"S": claim_id}
        else:
            condition += " AND attribute_not_exists(claim_id)"
        self.ddb.update_item(
            TableName=self.keys_table, Key={"owner_id": {"S": row["owner_id"]}},
            UpdateExpression="SET #state = :disabled, disabled_at = :now",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#state": "state"}, ExpressionAttributeValues=values,
        )

    def commit(self, row, event, actual_nanodollars, now):
        if not isinstance(actual_nanodollars, int) or actual_nanodollars < 0:
            raise ValueError("Actual OpenRouter cost must be nonnegative integer nanodollars")
        generation_id = event["transaction_id"]
        request_id = row["request_id"]
        debit = debit_microcents(actual_nanodollars)
        projection = {
            "TableName": self.fast_table,
            "Key": {"owner_id": {"S": row["owner_id"]}},
            "UpdateExpression": "SET openrouter_reserved_microcents = openrouter_reserved_microcents - :reservation ADD remaining_microcents :debit",
            "ConditionExpression": "attribute_exists(owner_id) AND openrouter_reserved_microcents >= :reservation",
            "ExpressionAttributeValues": {":debit": {"N": str(-debit)}, ":reservation": {"N": str(row["reservation_microcents"])}},
        }
        claim_key = f"generation#{generation_id}"
        transaction = [
            {"Put": {
                "TableName": self.requests_table,
                "Item": {"request_id": {"S": claim_key}, "owner_id": {"S": row["owner_id"]},
                         "key_hash": {"S": row["key_hash"]}, "source_request_id": {"S": request_id}},
                "ConditionExpression": "attribute_not_exists(request_id)",
            }},
            {"Update": {
                "TableName": self.requests_table,
                "Key": {"request_id": {"S": request_id}},
                "UpdateExpression": "SET #state = :settled, total_cost = :cost, provider_cost_nanodollars = :actual, settled_at = :now, #date = :date, actual_model = :model, provider_name = :provider",
                "ConditionExpression": "#state = :pending AND generation_id = :generation",
                "ExpressionAttributeNames": {"#state": "state", "#date": "date"},
                "ExpressionAttributeValues": {
                    ":settled": {"S": "settled"}, ":pending": {"S": "pending"},
                    ":generation": {"S": generation_id}, ":cost": {"S": format(Decimal(actual_nanodollars) / Decimal(1_000_000_000), "f") if actual_nanodollars else "0"},
                    ":actual": {"N": str(actual_nanodollars)}, ":now": {"N": str(now)},
                    ":date": {"S": event["timestamp"][:10]},
                    ":model": {"S": event["properties"]["model"]},
                    ":provider": {"S": event["properties"]["provider"]},
                },
            }},
            {"Update": projection},
            *self._spend(row, event, actual_nanodollars),
        ]
        try:
            self.ddb.transact_write_items(TransactItems=transaction)
            overshoot = debit - int(row["reservation_microcents"])
            if overshoot > 0:
                # The real debit is kept (Metronome bills it anyway). A charge above the
                # gate's ceiling can drive the pool negative, which admission reads as
                # exhausted. EMF line -> LightbulbAI metric, alarmed by W81 (observability-stack).
                print(json.dumps({"_aws": {"Timestamp": now * 1000, "CloudWatchMetrics": [{
                    "Namespace": "LightbulbAI", "Dimensions": [["env", "service"]],
                    "Metrics": [{"Name": "openrouter.settlement.overshoot_microcents", "Unit": "None"}]}]},
                    "env": os.environ.get("GATE_ENV", ""), "service": "openrouter-settlement", "owner_id": row["owner_id"], "request_id": request_id,
                    "openrouter.settlement.overshoot_microcents": overshoot}))
            return "settled"
        except Exception as exc:
            if exc.__class__.__name__ != "TransactionCanceledException":
                raise
            current = self.ddb.get_item(TableName=self.requests_table, Key={"request_id": {"S": request_id}}).get("Item", {})
            if current.get("state", {}).get("S") == "settled":
                # Loser of a concurrent settle: the winning run (try path above) already committed
                # and reports the cost. A distinct outcome keeps this run from re-reporting the same
                # generation (double-count) while still counting as resolved.
                return "settled_elsewhere"
            claim = self.ddb.get_item(TableName=self.requests_table, Key={"request_id": {"S": claim_key}}).get("Item", {})
            if not claim:
                raise
            if claim.get("owner_id", {}).get("S") != row["owner_id"] or claim.get("key_hash", {}).get("S") != row["key_hash"]:
                raise RuntimeError("OpenRouter generation claim owner/key mismatch") from exc
            if claim.get("source_request_id", {}).get("S") == request_id:
                raise  # another write failed; retry the full transaction
            self._mark_duplicate(row, now)
            return "duplicate_generation"

    def commit_free(self, row, event, now, actual_nanodollars=0):
        """Settle a free or capped row: no Metronome, no fast-gate hold."""
        mode = row.get("billing_mode")
        if mode not in ("free", "capped") or (mode == "free" and actual_nanodollars):
            raise ValueError("Only capped rows may record a positive unmetered cost")
        generation_id = event["transaction_id"]
        request_id = row["request_id"]
        transaction = [
            {"Put": {
                "TableName": self.requests_table,
                "Item": {"request_id": {"S": f"generation#{generation_id}"}, "owner_id": {"S": row["owner_id"]},
                         "key_hash": {"S": row["key_hash"]}, "source_request_id": {"S": request_id}},
                "ConditionExpression": "attribute_not_exists(request_id)",
            }},
            {"Update": {
                "TableName": self.requests_table,
                "Key": {"request_id": {"S": request_id}},
                "UpdateExpression": "SET #state = :settled, total_cost = :cost, provider_cost_nanodollars = :actual, settled_at = :now, #date = :date, actual_model = :model, provider_name = :provider",
                "ConditionExpression": "#state = :pending AND generation_id = :generation AND billing_mode = :mode",
                "ExpressionAttributeNames": {"#state": "state", "#date": "date"},
                "ExpressionAttributeValues": {
                    ":settled": {"S": "settled"}, ":pending": {"S": "pending"}, ":mode": {"S": mode},
                    ":generation": {"S": generation_id},
                    ":cost": {"S": format(Decimal(actual_nanodollars) / Decimal(1_000_000_000), "f") if actual_nanodollars else "0"},
                    ":actual": {"N": str(actual_nanodollars)},
                    ":now": {"N": str(now)}, ":date": {"S": event["timestamp"][:10]},
                    ":model": {"S": event["properties"]["model"]},
                    ":provider": {"S": event["properties"]["provider"]},
                },
            }},
            *self._spend(row, event, actual_nanodollars),
        ]
        try:
            self.ddb.transact_write_items(TransactItems=transaction)
            return "settled"
        except Exception as exc:
            if exc.__class__.__name__ != "TransactionCanceledException":
                raise
            current = self.ddb.get_item(TableName=self.requests_table, Key={"request_id": {"S": request_id}}).get("Item", {})
            if current.get("state", {}).get("S") == "settled":
                # Loser of a concurrent settle: the winning run (try path above) already committed
                # and reports the cost. A distinct outcome keeps this run from re-reporting the same
                # generation (double-count) while still counting as resolved.
                return "settled_elsewhere"
            raise

    def release_unknown(self, row, now):
        """Release an unmetered row that never got a generation ID. No money moves."""
        self.ddb.update_item(
            TableName=self.requests_table, Key={"request_id": {"S": row["request_id"]}},
            UpdateExpression="SET #state = :released, released_at = :now",
            ConditionExpression="#state = :pending AND attribute_not_exists(generation_id) AND billing_mode IN (:free, :capped)",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":released": {"S": "released"}, ":pending": {"S": "pending"}, ":now": {"N": str(now)},
                                       ":free": {"S": "free"}, ":capped": {"S": "capped"}},
        )
        return "released_no_generation"

    def release_stale_metadata(self, row, now):
        """Release a pending row whose OpenRouter generation metadata never arrived and holds
        no reservation (reservation_microcents == 0). No money moves. Mirrors release_unknown."""
        self.ddb.update_item(
            TableName=self.requests_table, Key={"request_id": {"S": row["request_id"]}},
            UpdateExpression="SET #state = :released, released_at = :now",
            ConditionExpression="#state = :pending AND reservation_microcents = :zero",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":released": {"S": "released"}, ":pending": {"S": "pending"}, ":now": {"N": str(now)},
                                       ":zero": {"N": "0"}},
        )
        return "released_no_generation"

    def _mark_duplicate(self, row, now):
        updates = [{"Update": {
            "TableName": self.requests_table,
            "Key": {"request_id": {"S": row["request_id"]}},
            "UpdateExpression": "SET #state = :duplicate, settled_at = :now",
            "ConditionExpression": "#state = :pending",
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": {":duplicate": {"S": "duplicate"}, ":pending": {"S": "pending"}, ":now": {"N": str(now)}},
        }}]
        updates.append({"Update": {
            "TableName": self.fast_table,
            "Key": {"owner_id": {"S": row["owner_id"]}},
            "UpdateExpression": "SET openrouter_reserved_microcents = openrouter_reserved_microcents - :reservation",
            "ConditionExpression": "attribute_exists(owner_id) AND openrouter_reserved_microcents >= :reservation",
            "ExpressionAttributeValues": {":reservation": {"N": str(row["reservation_microcents"])}},
        }})
        self.ddb.transact_write_items(TransactItems=updates)


def retire_keys(store, *, now, workspace_hashes, disable, environment=None, orphan_keys=None, grace_seconds=960, lease_seconds=300):
    """Drain a retiring owner before disabling its upstream key; retries are idempotent."""
    outcomes = {"grace": 0, "pending": 0, "lease": 0, "raced": 0, "disabled": 0, "orphan_keys_disabled": 0, "failed": 0}
    known_hashes = None
    for row in store.retirement_candidates():
        try:
            if not row.get("owner_id"):
                raise RuntimeError("OpenRouter retiring key has no owner")
            if row["state"] == "pending" or (row["state"] == "retirement_processing" and not row.get("key_hash")):
                if not row.get("retire_requested_at") or not environment or not orphan_keys:
                    raise RuntimeError("Pending OpenRouter retirement is incomplete")
                if row["state"] == "pending" and now - int(row["retire_requested_at"]) < grace_seconds:
                    outcomes["grace"] += 1
                    continue
                if row["state"] == "retirement_processing" and now - int(row.get("retirement_processing_at") or 0) < lease_seconds:
                    outcomes["lease"] += 1
                    continue
                if not store.claim_pending_retirement(row, now):
                    outcomes["raced"] += 1
                    continue
                name = f"lightbulb-{environment}-{row['owner_id']}"
                for key in orphan_keys(row["owner_id"]):
                    if key.get("name") != name or key.get("disabled") is True:
                        continue
                    if not key.get("hash"):
                        raise RuntimeError("OpenRouter orphan key has no hash")
                    disable(key["hash"])
                    outcomes["orphan_keys_disabled"] += 1
                store.tombstone_pending(row, now)
                outcomes["disabled"] += 1
                continue
            if not row.get("key_hash"):
                raise RuntimeError("OpenRouter retiring key has no hash")
            if row["state"] == "retiring":
                observed = int(row.get("retirement_observed_at") or store.observe_retirement(row, now))
                if now - observed < grace_seconds:
                    outcomes["grace"] += 1
                    continue
            elif row["state"] == "retirement_processing":
                if now - int(row.get("retirement_processing_at") or 0) < lease_seconds:
                    outcomes["lease"] += 1
                    continue
            else:
                continue
            if any(pending["owner_id"] == row["owner_id"] for pending in store.pending()):
                outcomes["pending"] += 1
                continue
            if not store.claim_retirement(row, now):
                outcomes["raced"] += 1  # resubscribe or another worker won the claim
                continue
            if any(pending["owner_id"] == row["owner_id"] for pending in store.pending()):
                outcomes["pending"] += 1
                continue
            if known_hashes is None:
                known_hashes = workspace_hashes()
            if row["key_hash"] not in known_hashes:
                raise RuntimeError("Retiring OpenRouter key is absent from configured workspace")
            disable(row["key_hash"])
            store.mark_disabled(row, now)
            outcomes["disabled"] += 1
        except Exception as exc:
            # One bad key must not strand every other retirement (or the settlement
            # run that follows); its row keeps its state and is retried next tick.
            print(f"openrouter retirement failed: {exc.__class__.__name__}")
            outcomes["failed"] += 1
    return outcomes


def _http_json(url, key):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response, parse_float=Decimal)


def _secret(name):
    import boto3
    return boto3.client("secretsmanager").get_secret_value(SecretId=name)["SecretString"]


def _get_generation(generation_id, secret_name):
    url = "https://openrouter.ai/api/v1/generation?" + urllib.parse.urlencode({"id": generation_id})
    try:
        return _http_json(url, _secret(secret_name)).get("data")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # OpenRouter generation metadata can lag the stream.
            return None
        raise


def _ingest(event):
    token = _secret(os.environ["METRONOME_API_KEY_SECRET_ID"])
    request = urllib.request.Request(
        os.environ.get("METRONOME_INGEST_URL", "https://api.metronome.com/v1/ingest"),
        method="POST", data=json.dumps([event]).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def list_workspace_keys(management_key, workspace_id, *, fetch=_http_json, max_pages=100):
    if not workspace_id:
        raise ValueError("OpenRouter workspace ID is required for key audit")
    keys, offset = [], 0
    for _ in range(max_pages):
        url = "https://openrouter.ai/api/v1/keys?" + urllib.parse.urlencode({
            "workspace_id": workspace_id, "include_disabled": "true", "offset": offset,
        })
        page = fetch(url, management_key).get("data")
        if not isinstance(page, list):
            raise RuntimeError("OpenRouter key list is incomplete")
        if not page:
            return keys
        keys.extend(page)
        offset += len(page)
    raise RuntimeError("OpenRouter key list pagination exceeded")


def _patch_disable_key(key_hash, management_key, workspace_id):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key_hash):
        raise RuntimeError("Invalid OpenRouter key hash")
    request = urllib.request.Request(
        f"https://openrouter.ai/api/v1/keys/{key_hash}", method="PATCH",
        data=b'{"disabled":true}',
        headers={"Authorization": f"Bearer {management_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response).get("data", {})
    if data.get("hash") != key_hash or data.get("workspace_id") != workspace_id or data.get("disabled") is not True:
        raise RuntimeError("OpenRouter key disable was not confirmed")


def _retire_scheduled(store, now):
    management_key = None
    workspace_keys = None
    workspace_id = os.environ["OPENROUTER_WORKSPACE_ID"]

    def keys():
        nonlocal management_key, workspace_keys
        if workspace_keys is None:
            management_key = _secret(os.environ["OPENROUTER_MANAGEMENT_KEY_SECRET_ID"])
            workspace_keys = list_workspace_keys(management_key, workspace_id)
        return workspace_keys

    def hashes():
        return {key["hash"] for key in keys()}

    def disable(key_hash):
        _patch_disable_key(key_hash, management_key, workspace_id)

    return retire_keys(store, now=now, workspace_hashes=hashes, orphan_keys=lambda _owner: keys(),
                       environment=os.environ["GATE_ENV"], disable=disable)


def _audit(store, day, today):
    if not activity_date_allowed(day, today):
        raise ValueError("Activity date must be a completed UTC day within 30 days")
    management_key = _secret(os.environ["OPENROUTER_MANAGEMENT_KEY_SECRET_ID"])
    workspace_id = os.environ["OPENROUTER_WORKSPACE_ID"]
    keys = list_workspace_keys(management_key, workspace_id)
    prefix = f"lightbulb-{os.environ['GATE_ENV']}-"
    rows = store.all_requests()
    hashes = {row["key_hash"] for row in rows if row.get("key_hash")}
    hashes.update(key["hash"] for key in keys
                  if (key.get("name") or key.get("label") or "").startswith(prefix))
    settled = [row for row in rows if row.get("state") == "settled"]
    gaps, known_total = {}, Decimal(0)
    for key_hash in hashes:
        url = "https://openrouter.ai/api/v1/activity?" + urllib.parse.urlencode({"date": day, "api_key_hash": key_hash, "workspace_id": workspace_id})
        activity = _http_json(url, management_key).get("data", [])
        known_total += activity_total(activity, day)
        gap = activity_gap(activity, settled, key_hash=key_hash, date=day)
        if gap:
            gaps[key_hash] = str(gap)
    # Spend on a key outside the known set shows only in the workspace total.
    url = "https://openrouter.ai/api/v1/activity?" + urllib.parse.urlencode({"date": day, "workspace_id": workspace_id})
    unattributed = _gap(activity_total(_http_json(url, management_key).get("data", []), day), known_total)
    if unattributed > 0:
        gaps["unattributed"] = str(unattributed)
    return gaps


# Anything else (unknown_generation, expired_gap, an exception name, a new outcome) fails the run.
RESOLVED = {"settled", "settled_elsewhere", "metadata_pending", "already_processed", "duplicate_generation", "deferred", "released_no_generation"}


def handler(event, context):
    """Scheduled settlement, or {audit_date: YYYY-MM-DD} for a daily key-usage audit."""
    event = event or {}
    import boto3
    store = Store(
        boto3.client("dynamodb"), requests_table=os.environ["REQUESTS_TABLE"],
        fast_table=os.environ["FAST_GATE_TABLE"],
        keys_table=os.environ.get("OPENROUTER_KEYS_TABLE"),
        usage_table=os.environ.get("USAGE_COUNTERS_TABLE"),
    )
    now = int(datetime.now(timezone.utc).timestamp())
    if event.get("retire_keys"):
        if not store.keys_table:
            raise RuntimeError("OpenRouter keys table is not configured")
        return {"retirement": _retire_scheduled(store, now)}
    today = datetime.now(timezone.utc).date()
    audit_day = ((today - timedelta(days=1)).isoformat() if event and event.get("audit_previous_day")
                 else event.get("audit_date") if event else None)
    if audit_day:
        gaps = _audit(store, audit_day, today.isoformat())
        if gaps:
            raise RuntimeError(f"OpenRouter key activity gap on {audit_day}: {len(gaps)} key(s)")
        return {"audited_date": audit_day, "keys_with_gaps": 0}
    outcomes = {}
    # Settle before retiring, so a wiped owner's last rows settle and its key retires in
    # the same run. Each row is isolated: one that raises stays pending for the next tick.
    rows = store.pending()
    for index, row in enumerate(rows):
        if context and context.get_remaining_time_in_millis() < 60_000:
            outcomes["deferred"] = len(rows) - index  # a backlog must not time out the run before retirement
            break
        if now - int(row["created_at"]) < 300:
            continue  # a stream may still be running
        try:
            outcome = settle_one(row, get_generation=_get_generation, ingest=_ingest,
                                 commit=store.commit, commit_free=store.commit_free,
                                 release_unknown=store.release_unknown,
                                 release_stale_metadata=store.release_stale_metadata, now=now)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        except Exception as exc:
            # No prompts, keys, or customer IDs in logs. The row stays pending.
            outcomes[exc.__class__.__name__] = outcomes.get(exc.__class__.__name__, 0) + 1
    retirement = _retire_scheduled(store, now) if store.keys_table else None
    if set(outcomes) - RESOLVED or (retirement or {}).get("failed"):
        raise RuntimeError(f"OpenRouter settlement has unresolved rows: {outcomes} retirement: {retirement}")
    return {"settlement": outcomes, "retirement": retirement}
