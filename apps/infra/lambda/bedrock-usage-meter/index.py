"""Bedrock invocation-log usage meter — dumb transport Lambda (CloudWatch-only).

A CloudWatch Logs subscription delivers gzipped+base64 records of Bedrock
model-invocation logs. This Lambda parses each record and POSTs the extracted
token/cache counts to the backend internal meter endpoint. It does NOT price,
attribute, or deplete — that is the backend's single source of truth.

CLOUDWATCH-ONLY (no S3). The prompt/cached-input body (`inputBodyJson`) is the
huge thing on cache-heavy turns, and we never need it. The OUTPUT body
(completion + the Converse `usage` block) is almost always <100KB, so Bedrock
inlines it in the CloudWatch record. We therefore read everything we need from
CloudWatch and never touch S3:
  - input_tokens       : `input.inputTokenCount`  (top-level, inline)
  - output_tokens      : `output.outputTokenCount` (top-level, inline)
  - cache_read_tokens  : `output.outputBodyJson.usage.cacheReadInputTokens`
  - cache_write_tokens : `output.outputBodyJson.usage.cacheWriteInputTokens`

Field paths are the ones recorded in FIELDS.md (confirmed against a real
cached-Converse record before flipping the meter live). The extractor keeps the
`_first_int` multi-path helper so a documented Bedrock log-shape change degrades
gracefully instead of silently zeroing cache tokens.

FAIL-SAFE TRANSPORT. A CloudWatch Logs subscription filter invokes this Lambda
SYNCHRONOUSLY, so the Lambda's async-invocation DLQ never fires on a per-record
failure — a backend 5xx, an unparseable record, or a truncated output body would
otherwise silently drop the record (or, worse, POST a zeroed-cache record and
under-bill ~30×). So every per-record failure path here explicitly pushes the
raw failed log event to the SQS DLQ (`_send_to_dlq`) + emits a metric +
continues. A money-correctness regression surfaces on the DLQ / an alarm, never
as a silent zero. The backend endpoint is idempotent (deplete() exactly-once on
requestId), so DLQ redrive replays are safe.

The one rare edge: a >100KB completion body. Bedrock offloads only bodies
>100KB to S3, and the cached INPUT prompt (not the output) is what's huge on
cache-heavy turns, so the output body is inline in practice. But if a completion
IS large enough to be offloaded, `output.outputBodyJson` is absent and the cache
counts can't be read — rather than POST an under-billed (cache=0) record we DLQ
it + emit `meter.output_body_truncated` so the gap is visible and redrivable.
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


class _OutputBodyTruncatedError(Exception):
    """An output body too large to inline (>100KB → offloaded to S3).

    Raised instead of returning a zeroed-cache record so the per-record handler
    DLQs the line rather than silently POSTing a ~30×-under-billed zero. This is
    the rare large-completion edge; the cached INPUT prompt is what's normally
    huge, and we never need it, so the output body is inline in practice.
    """


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


def _output_usage(record):
    """Return the Converse `usage` block from the inline output body, or None.

    The cache counts live in the RESPONSE body (`output.outputBodyJson.usage`),
    which Bedrock inlines in CloudWatch when the body is <100KB (the normal case
    — the huge thing on cache-heavy turns is the cached INPUT prompt, not the
    completion). Returns None when the output body is absent (>100KB completion
    offloaded to S3) so the caller can fail safe rather than zero the cache.

    `outputBodyJson` is an embedded JSON object in the CloudWatch record; if a
    given delivery serializes it as a JSON string we parse it once.
    """
    output = record.get("output")
    if not isinstance(output, dict):
        return None
    body = output.get("outputBodyJson")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    return usage if isinstance(usage, dict) else None


def _extract(record):
    """Extract the billing-relevant fields from one Bedrock log record.

    Token counts are top-level inline (`input.inputTokenCount` /
    `output.outputTokenCount`); cache counts live in the inline output body's
    `usage` block. Raises `_OutputBodyTruncatedError` when the output body is
    absent (>100KB completion offloaded to S3) so the handler DLQs the record
    instead of POSTing a zeroed-cache (~30×-under-billed) row.
    """
    usage = _output_usage(record)
    if usage is None:
        raise _OutputBodyTruncatedError(
            f"output.outputBodyJson.usage missing for requestId={record.get('requestId', '')!r} "
            "(likely a >100KB completion offloaded to S3)"
        )
    error_code = record.get("errorCode")
    run_status = "end"
    if error_code:
        run_status = "aborted" if "abort" in str(error_code).lower() else "error"
    return {
        "request_id": record.get("requestId", ""),
        "model_id": record.get("modelId", ""),
        "identity_arn": (record.get("identity") or {}).get("arn"),
        # Token counts are top-level inline; fall back to the output body's
        # usage block if a delivery omits the top-level counts.
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
        # Cache counts live ONLY in the response body's usage block.
        "cache_read_tokens": _first_int({"u": usage}, ("u", "cacheReadInputTokens")),
        "cache_write_tokens": _first_int({"u": usage}, ("u", "cacheWriteInputTokens")),
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
        try:
            parsed = _extract(record)
        except _OutputBodyTruncatedError as exc:
            # Output body offloaded (>100KB completion) — cache counts unreadable.
            # Do NOT POST a zeroed-cache record.
            logger.error("meter: output body truncated, DLQ + skip: %s", exc)
            _emit_metric("meter.output_body_truncated")
            _send_to_dlq(log_event, f"output_body_truncated: {exc}")
            continue
        try:
            _post_to_backend(parsed)
        except Exception as exc:  # noqa: BLE001 - any transport error must DLQ, not drop
            logger.error("meter: backend POST failed, DLQ + skip: %s", exc)
            _emit_metric("meter.transport_error")
            _send_to_dlq(log_event, f"transport_error: {exc}")
            continue
    return {"statusCode": 200}
