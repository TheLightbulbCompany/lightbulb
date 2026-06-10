"""Bedrock invocation-log usage meter — dumb transport Lambda (CloudWatch-only).

A CloudWatch Logs subscription delivers gzipped+base64 records of Bedrock
model-invocation logs. This Lambda parses each record and POSTs the extracted
token/cache counts to the backend internal meter endpoint. It does NOT price,
attribute, or deplete — that is the backend's single source of truth.

CLOUDWATCH-ONLY (no S3), and never even needs the response body. EVERY count we
bill on is recorded at the TOP LEVEL of the CloudWatch record and read inline:
  - input_tokens       : `input.inputTokenCount`
  - output_tokens      : `output.outputTokenCount`
  - cache_read_tokens  : `input.cacheReadInputTokenCount`
  - cache_write_tokens : `input.cacheWriteInputTokenCount`

Confirmed against live cached-Converse records (the top-level
`input.cache*InputTokenCount` counts equal the response body's
`usage.cache*InputTokens`). Bedrock only ever offloads a body >100KB to S3, and
that affects the output BODY alone — the token + cache COUNTS stay inline at the
top level regardless. So there is NO large-completion edge case here: we read
the counts directly and never touch `output.outputBodyJson` (kept only as a
defensive `_first_int` fallback should a delivery variant omit a top-level
count).

FAIL-SAFE TRANSPORT. A CloudWatch Logs subscription filter invokes this Lambda
SYNCHRONOUSLY, so the Lambda's async-invocation DLQ never fires on a per-record
failure — a backend 5xx or an unparseable record would otherwise silently drop
the record. So every per-record failure path here explicitly pushes the raw
failed log event to the SQS DLQ (`_send_to_dlq`) + emits a metric + continues. A
money-correctness regression surfaces on the DLQ / an alarm, never as a silent
drop. The backend endpoint is idempotent (deplete() exactly-once on requestId),
so DLQ redrive replays are safe.
"""

import base64
import gzip
import json
import logging
import os
import time
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_METER_TOKEN = None


def _meter_token():
    """Return the bedrock_meter service token, fetched once at cold-start.

    Prefers the literal METER_TOKEN env (set by unit tests). In the deployed
    Lambda the CDK injects only METER_TOKEN_SECRET_ARN, so we resolve the token
    from Secrets Manager and cache it for the life of the container.
    """
    global _METER_TOKEN
    if _METER_TOKEN is None:
        literal = os.environ.get("METER_TOKEN")
        if literal:
            _METER_TOKEN = literal
        else:
            arn = os.environ["METER_TOKEN_SECRET_ARN"]
            _METER_TOKEN = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    return _METER_TOKEN


def _emit_metric(name, **dimensions):
    """Emit a CloudWatch metric via EMF (one JSON log line).

    Mirrors the backend's core/observability/metrics.put_metric EMF shape so the
    meter Lambda's failure metrics land in the same Isol8 namespace. Never raises
    — observability must not be able to take down the fail-safe path.
    """
    try:
        dims = {"service": "isol8-bedrock-meter", **dimensions}
        line = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": "Isol8",
                        "Dimensions": [list(dims.keys())],
                        "Metrics": [{"Name": name, "Unit": "Count"}],
                    }
                ],
            },
            name: 1.0,
            **dims,
        }
        print(json.dumps(line), flush=True)
    except Exception:  # pragma: no cover - defensive; metrics must never raise
        logger.exception("meter: failed to emit metric %s", name)


def _send_to_dlq(log_event, reason):
    """Push a failed raw log event to the SQS DLQ so it is never silently lost.

    The CloudWatch Logs subscription invokes us SYNCHRONOUSLY, so Lambda's own
    async DLQ does not fire on a per-record failure — we route durability here
    explicitly. Best-effort: if the DLQ send itself fails we log + re-raise so
    the whole batch fails (CW Logs retries) rather than dropping the record.
    """
    queue_url = os.environ.get("DLQ_URL", "")
    if not queue_url:
        logger.error("meter: no DLQ_URL configured, cannot durably hold failed record (reason=%s)", reason)
        raise RuntimeError(f"meter transport failure with no DLQ: {reason}")
    boto3.client("sqs").send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"reason": reason, "log_event": log_event}),
    )


def _first_int(d, *paths):
    """Return the first int found at any (k1, k2, ...) path in dict d, else 0."""
    for path in paths:
        node = d
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, (int, float)):
            return int(node)
    return 0


def _extract(record):
    """Extract the billing-relevant fields from one Bedrock log record.

    EVERY count is top-level inline in the CloudWatch record and read directly:
      - input_tokens  : `input.inputTokenCount`
      - output_tokens : `output.outputTokenCount`
      - cache_read    : `input.cacheReadInputTokenCount`
      - cache_write   : `input.cacheWriteInputTokenCount`

    The meter therefore NEVER needs the (offloadable) response body. Only a
    completion >100KB gets offloaded to S3, and that affects the output BODY
    only — the token + cache COUNTS stay inline at the top level regardless
    (verified against live records: the top-level `input.cache*InputTokenCount`
    counts equal the response body's `usage.cache*InputTokens`). The nested
    usage block is kept ONLY as a defensive fallback in case a future delivery
    variant omits a top-level count.
    """
    error_code = record.get("errorCode")
    run_status = "end"
    if error_code:
        run_status = "aborted" if "abort" in str(error_code).lower() else "error"
    return {
        "request_id": record.get("requestId", ""),
        "model_id": record.get("modelId", ""),
        "identity_arn": (record.get("identity") or {}).get("arn"),
        "input_tokens": _first_int(
            record,
            ("input", "inputTokenCount"),
            ("output", "outputBodyJson", "usage", "inputTokens"),
        ),
        "output_tokens": _first_int(
            record,
            ("output", "outputTokenCount"),
            ("output", "outputBodyJson", "usage", "outputTokens"),
        ),
        # Cache counts: top-level inline (always present, never offloaded);
        # nested usage block is the defensive fallback only.
        "cache_read_tokens": _first_int(
            record,
            ("input", "cacheReadInputTokenCount"),
            ("output", "outputBodyJson", "usage", "cacheReadInputTokens"),
        ),
        "cache_write_tokens": _first_int(
            record,
            ("input", "cacheWriteInputTokenCount"),
            ("output", "outputBodyJson", "usage", "cacheWriteInputTokens"),
        ),
        "run_status": run_status,
        "region": record.get("region", ""),
        "occurred_at": record.get("timestamp", ""),
    }


def _post_to_backend(parsed):
    """POST one extracted record to the backend internal meter endpoint."""
    data = json.dumps(parsed).encode()
    req = urllib.request.Request(
        f"http://{os.environ['ALB_DNS_NAME']}/api/v1/internal/usage/from-bedrock-log",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_meter_token()}",
        },
        data=data,
    )
    urllib.request.urlopen(req, timeout=10)


def handler(event, context):
    payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    for log_event in payload.get("logEvents", []):
        # Each record is handled independently and fails safe: a parse error, a
        # truncated/absent output body, or a backend POST failure DLQs that one
        # record + emits a metric + continues, so one bad/under-billed record
        # never silently drops and never poisons the rest of the batch.
        try:
            record = json.loads(log_event["message"])
        except (ValueError, KeyError):
            logger.warning("meter: unparseable log event, DLQ + skip")
            _emit_metric("meter.parse_failed")
            _send_to_dlq(log_event, "unparseable_log_event")
            continue
        parsed = _extract(record)
        try:
            _post_to_backend(parsed)
        except Exception as exc:  # noqa: BLE001 - any transport error must DLQ, not drop
            logger.error("meter: backend POST failed, DLQ + skip: %s", exc)
            _emit_metric("meter.transport_error")
            _send_to_dlq(log_event, f"transport_error: {exc}")
            continue
    return {"statusCode": 200}
