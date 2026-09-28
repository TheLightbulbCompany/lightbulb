"""Lambda entry point for the openclaw-work.fifo SQS event source. Runs from the backend
image (``python -m awslambdaric scripts.work_lambda.handler``) and drives each record
through the SAME ``OpenClawWorkWorker._process_message`` the in-process loop used.

Environment: rebuilt exactly as ``provision_lambda`` does (SSM plain env + Secrets
Manager), on the first invoke, before anything imports ``core.config``.

Retry mapping: ``_process_message`` already deletes a completed message and, on a
``WorkRetry``, re-times it with ``ChangeMessageVisibility`` (10 s, or Jev's backoff).
Lambda leaves a reported failure's visibility alone, so that re-time stands; any
other failure waits out the queue's visibility timeout, as it did in the loop.
Every non-completed record is returned in ``batchItemFailures``.

FIFO: once a record fails, every later record of the SAME message group in this
batch is reported failed too (and released at visibility 0 -- the group stays
locked by the failed one, so order holds) rather than run out of order.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from scripts.provision_lambda import load_environment

logging.basicConfig(stream=sys.stdout)
logging.getLogger().setLevel(logging.INFO)

# One loop and one worker for the container's life: module-level clients (httpx, boto)
# must not outlive the loop they were bound to.
_loop = asyncio.new_event_loop()
_worker = None


def _get_worker():
    global _worker
    if _worker is None:
        load_environment()  # a failure raises: the whole batch retries, which is right
        import boto3

        from core.config import settings  # imports core.config: env first
        from core.services.openclaw_work import OpenClawWorkWorker

        _worker = OpenClawWorkWorker(sqs_client=boto3.client("sqs", region_name=settings.AWS_REGION))
    return _worker


def _as_sqs_message(record: dict) -> dict:
    """The event record in ReceiveMessage's shape, which ``_process_message`` reads."""
    return {
        "MessageId": record["messageId"],
        "ReceiptHandle": record["receiptHandle"],
        "Body": record.get("body", ""),
        "Attributes": record.get("attributes") or {},
    }


async def process_batch(worker, records: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for record in records:
        group = (record.get("attributes") or {}).get("MessageGroupId") or f"invalid:{record['messageId']}"
        groups.setdefault(group, []).append(record)

    async def run_group(batch: list[dict]) -> list[str]:
        failed: list[str] = []
        for record in batch:
            message = _as_sqs_message(record)
            if failed:
                await worker._return_visibility(message)
            elif await worker._process_message(message):
                continue
            failed.append(record["messageId"])
        return failed

    results = await asyncio.gather(*(run_group(batch) for batch in groups.values()))
    return {"batchItemFailures": [{"itemIdentifier": mid} for failed in results for mid in failed]}


def handler(event, context):
    return _loop.run_until_complete(process_batch(_get_worker(), event.get("Records") or []))
