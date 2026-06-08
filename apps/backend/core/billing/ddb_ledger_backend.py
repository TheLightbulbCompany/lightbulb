"""DynamoDB-backed ledger (interim, Phase 1).

Implements `LedgerBackend` against the existing `credits` (atomic balance) and
`credit-transactions` (immutable audit) tables. `deplete()` is exactly-once on
`event.request_id` via a single TransactWriteItems (see Task 4). This backend
is destined for deletion once the Metronome adapter is live (Phase 3); it
exists only so the new every-run meter has a money store to bill in the interim.
"""

from __future__ import annotations

import os

import boto3

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


class DdbLedgerBackend:
    """LedgerBackend over DynamoDB. See module docstring."""

    async def get_balance(self, owner_id: str, *, consistent: bool = False) -> int:
        table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(
            _credits_table_name()
        )
        resp = table.get_item(Key={"user_id": owner_id}, ConsistentRead=consistent)
        item = resp.get("Item")
        if not item:
            return 0
        return int(item.get("balance_microcents", 0))
