"""DynamoDB-backed ledger (interim, Phase 1).

Implements `LedgerBackend` against the existing `credits` (atomic balance) and
`credit-transactions` (immutable audit) tables. `deplete()` is exactly-once on
`event.request_id` via a single TransactWriteItems (see Task 4). This backend
is destined for deletion once the Metronome adapter is live (Phase 3); it
exists only so the new every-run meter has a money store to bill in the interim.
"""

from __future__ import annotations

import os
import time

import boto3
from botocore.exceptions import ClientError

from core.billing.ledger_interface import DepleteResult
from core.billing.usage_event import UsageEvent
from core.config import settings


def _credits_table_name() -> str:
    name = os.environ.get("CREDITS_TABLE") or settings.CREDITS_TABLE
    if not name:
        raise RuntimeError("CREDITS_TABLE is empty — backend is misconfigured.")
    return name


def _txns_table_name() -> str:
    name = os.environ.get("CREDIT_TRANSACTIONS_TABLE") or settings.CREDIT_TRANSACTIONS_TABLE
    if not name:
        raise RuntimeError("CREDIT_TRANSACTIONS_TABLE is empty — backend is misconfigured.")
    return name


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ddb_client():
    return boto3.client("dynamodb", region_name=settings.AWS_REGION)


class DdbLedgerBackend:
    """LedgerBackend over DynamoDB. See module docstring."""

    async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int:
        table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_credits_table_name())
        resp = table.get_item(Key={"user_id": owner_id}, ConsistentRead=consistent)
        item = resp.get("Item")
        if not item:
            return 0
        return int(item.get("balance_microcents", 0))

    async def deplete(self, event: UsageEvent) -> DepleteResult:
        """Apply one usage event to the owner's balance, exactly once on
        event.request_id. Balance is allowed to go negative (post-hoc meter)."""
        now = _now_iso()
        tx_id = f"deduct#{event.request_id}"
        client = _ddb_client()
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": _credits_table_name(),
                            "Key": {"user_id": {"S": event.owner_id}},
                            "UpdateExpression": "ADD balance_microcents :neg SET updated_at = :now",
                            "ExpressionAttributeValues": {
                                ":neg": {"N": str(-event.billed_microcents)},
                                ":now": {"S": now},
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": _txns_table_name(),
                            "Item": {
                                "user_id": {"S": event.owner_id},
                                "tx_id": {"S": tx_id},
                                "type": {"S": "deduct"},
                                "amount_microcents": {"N": str(-event.billed_microcents)},
                                "raw_cost_microcents": {"N": str(event.raw_cost_microcents)},
                                "markup_multiplier": {"N": str(event.markup_multiplier)},
                                "bedrock_invocation_id": {"S": event.request_id},
                                "model_id": {"S": event.model_id},
                                "input_tokens": {"N": str(event.input_tokens)},
                                "output_tokens": {"N": str(event.output_tokens)},
                                "cache_read_tokens": {"N": str(event.cache_read_tokens)},
                                "cache_write_tokens": {"N": str(event.cache_write_tokens)},
                                "region": {"S": event.region},
                                "run_status": {"S": event.run_status},
                                "source": {"S": event.source},
                                "occurred_at": {"S": event.occurred_at},
                                "created_at": {"S": now},
                            },
                            "ConditionExpression": "attribute_not_exists(tx_id)",
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = e.response.get("CancellationReasons", [])
            # Index 1 is the Put (dedup) item. A conditional failure there means
            # this request_id was already applied — a replay. Anything else is
            # a real error we must not swallow.
            put_reason = reasons[1]["Code"] if len(reasons) > 1 else ""
            if put_reason != "ConditionalCheckFailed":
                raise
            current = await self.get_balance(event.owner_id, consistent=True)
            return DepleteResult(balance_microcents=current, applied=False)

        new_balance = await self.get_balance(event.owner_id, consistent=True)
        return DepleteResult(balance_microcents=new_balance, applied=True)
