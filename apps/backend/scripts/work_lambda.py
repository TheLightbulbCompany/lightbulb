"""Lambda consumer for one openclaw-work.fifo record per invocation."""

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
        from core.services.super_agent_flags import env_enabled

        if env_enabled():  # admission signals the Router (bounded; a failed connect makes admits retry)
            from core.temporal.client import connect_and_set

            _loop.run_until_complete(connect_and_set())

        _worker = OpenClawWorkWorker(sqs_client=boto3.client("sqs", region_name=settings.AWS_REGION))
    return _worker


async def process_batch(worker, records: list[dict]) -> dict:
    if len(records) != 1:
        raise ValueError("expected exactly one SQS record")
    record = records[0]
    attributes = record["attributes"]
    if not isinstance(attributes.get("MessageGroupId"), str) or not attributes["MessageGroupId"]:
        raise ValueError("missing FIFO message group")
    message = {
        "MessageId": record["messageId"],
        "ReceiptHandle": record["receiptHandle"],
        "Body": record["body"],
        "Attributes": attributes,
    }
    completed = await worker._process_message(message)
    return {"batchItemFailures": [] if completed else [{"itemIdentifier": record["messageId"]}]}


def handler(event, context):
    return _loop.run_until_complete(process_batch(_get_worker(), event["Records"]))
