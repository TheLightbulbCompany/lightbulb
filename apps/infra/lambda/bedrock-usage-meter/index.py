"""Bedrock invocation-log usage meter — dumb transport Lambda.

CloudWatch Logs subscription delivers gzipped+base64 records of Bedrock
model-invocation logs. This Lambda parses each record, fetches the S3-offloaded
body when present (>100KB → cache tokens live in S3), and POSTs the extracted
record to the backend internal meter endpoint. It does NOT price, attribute, or
deplete — that is the backend's single source of truth.

Field paths are the ones recorded in FIELDS.md (Task 1). The extractor tries the
primary path first, then documented fallbacks, so a Bedrock log-shape change
degrades gracefully instead of silently zeroing cache tokens.

FAIL-SAFE TRANSPORT. A CloudWatch Logs subscription filter invokes this Lambda
SYNCHRONOUSLY, so the Lambda's async-invocation DLQ never fires on a per-record
failure — a backend 5xx, an unparseable S3 ref, or an S3 fetch error would
otherwise silently drop the record (or, worse, POST a zeroed-cache record and
under-bill ~30×). So every per-record failure path here explicitly pushes the
raw failed log event to the SQS DLQ (`_send_to_dlq`) + emits a metric +
continues. A money-correctness regression surfaces on the DLQ / an alarm, never
as a silent zero. The backend endpoint is idempotent (deplete() exactly-once on
requestId), so DLQ redrive replays are safe.

⚠️ BUILD-GATE — UNVERIFIED FIELD PATHS. The FIELDS.md fixtures backing these
paths are SYNTHETIC (hand-built from the AWS-documented Bedrock
model-invocation-logging schema, NOT captured from a real dev invocation —
this plan ran with no live AWS/SSO access). The cache-token paths and the
S3-offload reference field are the two highest-risk unknowns in the whole meter
(a wrong cache path silently zeroes the ~30× cache undercount the meter exists
to fix). The parser is DEFENSIVE on purpose, but the EXACT paths MUST be
confirmed against a real dev record (FIELDS.md's build-gate checklist) BEFORE
`METER_IS_LIVE` is flipped to true. Do NOT treat any path below as confirmed.
"""

import base64
import gzip
import json
import logging
import os
import time
import urllib.request
from urllib.parse import urlparse

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_METER_TOKEN = None


class _S3MergeError(Exception):
    """An offloaded record whose S3 body could not be read.

    Raised instead of returning a zeroed-cache record so the per-record handler
    DLQs the line rather than silently POSTing a ~30×-under-billed zero.
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


def _find_s3_ref(record):
    """Return the S3 reference (key or s3:// URI) if the body was offloaded, else ''."""
    inp = record.get("input", {})
    for key in ("inputBodyS3Path", "s3Location", "inputBodyS3Location", "bodyS3Path"):
        ref = inp.get(key)
        if isinstance(ref, str) and ref:
            return ref
        if isinstance(ref, dict):
            uri = ref.get("uri") or ref.get("s3Uri") or ""
            if uri:
                return uri
    return ""


def _extract(record):
    """Extract the billing-relevant fields from one Bedrock log record."""
    s3_ref = _find_s3_ref(record)
    error_code = record.get("errorCode")
    run_status = "end"
    if error_code:
        run_status = "aborted" if "abort" in str(error_code).lower() else "error"
    return {
        "request_id": record.get("requestId", ""),
        "model_id": record.get("modelId", ""),
        "identity_arn": (record.get("identity") or {}).get("arn"),
        "input_tokens": _first_int(record, ("input", "inputTokenCount")),
        "output_tokens": _first_int(record, ("output", "outputTokenCount")),
        "cache_read_tokens": _first_int(record, ("input", "cacheReadInputTokenCount")),
        "cache_write_tokens": _first_int(record, ("input", "cacheWriteInputTokenCount")),
        "run_status": run_status,
        "region": record.get("region", ""),
        "occurred_at": record.get("timestamp", ""),
        "body_offloaded": bool(s3_ref),
        "s3_ref": s3_ref,
    }


def _parse_s3_ref(s3_ref):
    """Return (bucket, key) from a key or an s3://bucket/key URI."""
    if s3_ref.startswith("s3://"):
        p = urlparse(s3_ref)
        return p.netloc, p.path.lstrip("/")
    # Bare key — the bucket is the configured large-data sink.
    return os.environ.get("LARGE_DATA_BUCKET", ""), s3_ref


def _merge_s3_body(parsed, record):
    """Fetch the offloaded S3 body and overwrite token/cache counts from it.

    The cache tokens are the whole point of going to S3 — on offloaded records
    the inline counts read 0, so we MUST source them from the object body.

    FAIL-SAFE: an offloaded record whose S3 body cannot be read (unparseable ref
    OR a get_object error) raises `_S3MergeError` rather than returning `parsed`
    with the inline-zero cache counts. Returning zeros would silently re-introduce
    the ~30× cache-blind under-bill this meter exists to prevent. The per-record
    handler catches `_S3MergeError`, DLQs the line, and emits meter.s3_merge_failed.
    """
    bucket, key = _parse_s3_ref(parsed["s3_ref"])
    if not bucket or not key:
        raise _S3MergeError(f"unparseable s3_ref={parsed['s3_ref']!r}")
    try:
        body_obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        body = json.loads(body_obj["Body"].read())
    except Exception as exc:  # noqa: BLE001 - any S3/parse error must fail safe, not zero
        raise _S3MergeError(f"s3 get_object failed for s3://{bucket}/{key}: {exc}") from exc
    usage = body.get("usage", body)
    parsed["input_tokens"] = (
        _first_int({"u": usage}, ("u", "inputTokens"), ("u", "inputTokenCount")) or parsed["input_tokens"]
    )
    parsed["output_tokens"] = (
        _first_int({"u": usage}, ("u", "outputTokens"), ("u", "outputTokenCount")) or parsed["output_tokens"]
    )
    parsed["cache_read_tokens"] = _first_int(
        {"u": usage}, ("u", "cacheReadInputTokens"), ("u", "cacheReadInputTokenCount")
    )
    parsed["cache_write_tokens"] = _first_int(
        {"u": usage}, ("u", "cacheWriteInputTokens"), ("u", "cacheWriteInputTokenCount")
    )
    return parsed


def _post_to_backend(parsed):
    """POST one extracted record to the backend internal meter endpoint."""
    payload = {k: v for k, v in parsed.items() if k not in ("body_offloaded", "s3_ref")}
    data = json.dumps(payload).encode()
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
        # Each record is handled independently and fails safe: a parse error, an
        # S3-merge failure, or a backend POST failure DLQs that one record + emits
        # a metric + continues, so one bad/under-billed record never silently
        # drops and never poisons the rest of the batch.
        try:
            record = json.loads(log_event["message"])
        except (ValueError, KeyError):
            logger.warning("meter: unparseable log event, DLQ + skip")
            _emit_metric("meter.parse_failed")
            _send_to_dlq(log_event, "unparseable_log_event")
            continue
        try:
            parsed = _extract(record)
            if parsed["body_offloaded"]:
                parsed = _merge_s3_body(parsed, record)
        except _S3MergeError as exc:
            # Offloaded body unreadable — do NOT POST a zeroed-cache record.
            logger.error("meter: s3 merge failed, DLQ + skip: %s", exc)
            _emit_metric("meter.s3_merge_failed")
            _send_to_dlq(log_event, f"s3_merge_failed: {exc}")
            continue
        try:
            _post_to_backend(parsed)
        except Exception as exc:  # noqa: BLE001 - any transport error must DLQ, not drop
            logger.error("meter: backend POST failed, DLQ + skip: %s", exc)
            _emit_metric("meter.transport_error")
            _send_to_dlq(log_event, f"transport_error: {exc}")
            continue
    return {"statusCode": 200}
