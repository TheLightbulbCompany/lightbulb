"""Bedrock invocation-log usage meter — direct Metronome ingest.

A CloudWatch Logs subscription delivers gzipped+base64 records of Bedrock
model-invocation logs. This Lambda parses each record, attributes it to an
owner via the per-env aip-owners reverse table, and POSTs one event per record
straight to Metronome's /v1/ingest. Metronome rates the event (rate card),
maintains the prepaid balance, and dedups exactly-once on ``transaction_id``
(the Bedrock requestId, 34-day window) — so DLQ redrives and CW-Logs batch
retries are always safe to replay. There is no backend hop and no JWT: the
only credential is the vendor-issued Metronome API key.

ONE CODEBASE, TWO FUNCTION INSTANCES. The account-scoped meter stack deploys
this asset twice — a dev instance and a prod instance — because Bedrock
invocation logging is account+region-level and ONE log group receives BOTH
envs' records. The env split happens at the subscription filters: every record
carries ``identity.arn`` = the per-owner role
(``…assumed-role/isol8-{env}-owner-bedrock-{owner}/…``), so a CloudWatch term
filter routes dev records to the dev instance and prod records to the prod
instance. The instances differ ONLY in env vars (never code conditionals):
  - METRONOME_API_KEY_SECRET_ARN : dev -> sandbox token, prod -> live token
  - AIP_OWNERS_TABLE             : isol8-{env}-bedrock-aip-owners
  - DLQ_URL                      : that instance's own SQS DLQ
  - METER_ENV                    : the env dimension on emitted metrics

CLOUDWATCH-ONLY, and never even needs the response body. EVERY count we bill
on is recorded at the TOP LEVEL of the CloudWatch record and read inline:
  - input_tokens       : `input.inputTokenCount`
  - output_tokens      : `output.outputTokenCount`
  - cache_read_tokens  : `input.cacheReadInputTokenCount`
  - cache_write_tokens : `input.cacheWriteInputTokenCount`

Confirmed against live cached-Converse records (the top-level
`input.cache*InputTokenCount` counts equal the response body's
`usage.cache*InputTokens`). Bedrock only ever offloads a body >100KB to S3, and
that affects the output BODY alone — the token + cache COUNTS stay inline at
the top level regardless. So there is NO large-completion edge case here: we
read the counts directly and never touch `output.outputBodyJson` (kept only as
a defensive `_first_int` fallback should a delivery variant omit a top-level
count).

FAIL-SAFE TRANSPORT. A CloudWatch Logs subscription filter invokes this Lambda
SYNCHRONOUSLY, so the Lambda's async-invocation DLQ never fires on a per-record
failure — a Metronome non-2xx, an unattributable record, or an unparseable
record would otherwise silently drop. So every per-record failure path here
explicitly pushes the raw failed log event to the SQS DLQ (`_send_to_dlq`) +
emits a metric + continues. A money-correctness regression surfaces on the
DLQ / an alarm, never as a silent drop. An unattributed record is NEVER
ingested (it would land in Metronome's unmatched-events bin and vanish — a
revenue leak must alarm instead).
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

METRONOME_INGEST_URL_DEFAULT = "https://api.metronome.com/v1/ingest"

# Cold-start caches: the Metronome bearer token and the boto3 clients are
# fetched/built once per Lambda container and reused across invocations.
_METRONOME_TOKEN = None
_DDB = None
_SQS = None


def _ddb():
    global _DDB
    if _DDB is None:
        _DDB = boto3.client("dynamodb")
    return _DDB


def _sqs():
    global _SQS
    if _SQS is None:
        _SQS = boto3.client("sqs")
    return _SQS


def _metronome_token():
    """Return the Metronome API bearer token, fetched once at cold-start.

    Prefers the literal METRONOME_API_KEY env (set by unit tests). In the
    deployed Lambda the CDK injects only METRONOME_API_KEY_SECRET_ARN, so we
    resolve the token from Secrets Manager and cache it for the life of the
    container. The secret is per-env: the dev instance holds a sandbox token,
    the prod instance a live token (Metronome is token-scoped — same base URL,
    the key decides sandbox vs prod).
    """
    global _METRONOME_TOKEN
    if _METRONOME_TOKEN is None:
        literal = os.environ.get("METRONOME_API_KEY")
        if literal:
            _METRONOME_TOKEN = literal
        else:
            arn = os.environ["METRONOME_API_KEY_SECRET_ARN"]
            _METRONOME_TOKEN = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
    return _METRONOME_TOKEN


def _emit_metric(name, **properties):
    """Emit a CloudWatch metric via EMF (one JSON log line).

    Mirrors the backend's core/observability/metrics.put_metric EMF shape so the
    meter Lambda's failure metrics land in the same Isol8 namespace. The
    dimension set is always exactly {service, env}: both function instances
    share the namespace + service name, so the env dimension is what keeps dev
    noise from tripping the prod alarms (each alarm scopes to its env). Extra
    keyword args become plain log-line properties (Logs-Insights-queryable),
    NOT dimensions — unbounded values like region/model would blow up metric
    cardinality. Never raises — observability must not be able to take down
    the fail-safe path.
    """
    try:
        dims = {
            "service": "isol8-bedrock-meter",
            "env": os.environ.get("METER_ENV", "unknown"),
        }
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
            **properties,
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
    Redriving the queue is safe: Metronome dedups on transaction_id.
    """
    queue_url = os.environ.get("DLQ_URL", "")
    if not queue_url:
        logger.error("meter: no DLQ_URL configured, cannot durably hold failed record (reason=%s)", reason)
        raise RuntimeError(f"meter transport failure with no DLQ: {reason}")
    _sqs().send_message(
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


def _resolve_owner_and_tier(record):
    """Resolve (owner_id, tier) from the record's AIP ARN, or (None, None).

    The record's ``modelId`` is the per-owner application-inference-profile
    ARN; the aip-owners reverse table (written atomically per row at provision
    by bedrock_aip.persist_owner_aips) maps it to ``owner_id`` + ``tier`` in
    ONE GetItem. The per-owner IAM role can only invoke that owner's own AIPs,
    so every record reaching this instance's subscription filter should
    resolve — a miss means the reverse row is absent (a provisioning gap) and
    must surface on the alarmed meter.unattributed metric, never be ingested.

    A DDB transport error deliberately propagates: the whole batch fails and
    CloudWatch Logs retries it, which is safe because Metronome dedups any
    already-ingested records on transaction_id.
    """
    resp = _ddb().get_item(
        TableName=os.environ["AIP_OWNERS_TABLE"],
        Key={"aip_arn": {"S": record.get("modelId", "")}},
    )
    item = resp.get("Item")
    if not item:
        return (None, None)
    owner = (item.get("owner_id") or {}).get("S")
    tier = (item.get("tier") or {}).get("S")
    return (owner or None, tier or None)


def _ingest_to_metronome(parsed):
    """POST one usage event to Metronome's ingest endpoint.

    The body is a JSON ARRAY of one event. ``transaction_id`` (the Bedrock
    requestId) gives native exactly-once over a 34-day window; ``customer_id``
    is the owner_id, matched via the Metronome customer's ingest_aliases.
    Every property value is stringified per Metronome's ingest docs.
    """
    event = {
        "transaction_id": parsed["request_id"],
        "customer_id": parsed["owner_id"],
        "timestamp": parsed["occurred_at"],
        "event_type": "bedrock_invocation",
        "properties": {
            k: str(v)
            for k, v in {
                "tier": parsed["tier"],
                "model_id": parsed["model_id"],
                "input_tokens": parsed["input_tokens"],
                "output_tokens": parsed["output_tokens"],
                "cache_read_tokens": parsed["cache_read_tokens"],
                "cache_write_tokens": parsed["cache_write_tokens"],
                "region": parsed["region"],
                "run_status": parsed["run_status"],
            }.items()
        },
    }
    req = urllib.request.Request(
        os.environ.get("METRONOME_INGEST_URL", METRONOME_INGEST_URL_DEFAULT),
        method="POST",
        data=json.dumps([event]).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_metronome_token()}",
        },
    )
    urllib.request.urlopen(req, timeout=10)


def handler(event, context):
    payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    # CloudWatch Logs delivers a non-JSON health-probe message when a
    # subscription filter is created. Without this early return that probe
    # would DLQ as parse_failed and page both envs' warn topics on every
    # deploy that touches the filters.
    if payload.get("messageType") == "CONTROL_MESSAGE":
        return {"statusCode": 200}
    for log_event in payload.get("logEvents", []):
        # Each record is handled independently and fails safe: a parse error,
        # a malformed record, an unattributable record, or a Metronome ingest
        # failure DLQs that one record + emits a metric + continues, so one
        # bad record never silently drops and never poisons the rest of the
        # batch. CloudWatch Logs retries the WHOLE batch on a raised error and
        # then drops it, so an unguarded raise on a deterministic per-record
        # failure converts one bad record into losing the entire batch
        # silently. Only TRANSPORT errors may propagate (the retry can
        # actually fix those): a DDB error in _resolve_owner_and_tier raises
        # so CW Logs retries the batch — safe because Metronome dedups on
        # transaction_id.
        try:
            record = json.loads(log_event["message"])
        except (ValueError, KeyError):
            logger.warning("meter: unparseable log event, DLQ + skip")
            _emit_metric("meter.parse_failed")
            _send_to_dlq(log_event, "unparseable_log_event")
            continue
        # Shape guard BEFORE any use of the record: valid JSON is not
        # necessarily a usable record. requestId is load-bearing — it becomes
        # the Metronome transaction_id, which is the ENTIRE replay/dedup
        # safety story (DLQ redrives, CW Logs batch retries), so an event
        # without one must never be ingested. modelId is the attribution key.
        malformed = None
        if not isinstance(record, dict):
            malformed = "not a dict"
        elif not record.get("modelId"):
            malformed = "missing modelId"
        elif not record.get("requestId"):
            malformed = "missing requestId"
        if malformed:
            logger.warning("meter: malformed record (%s), DLQ + skip", malformed)
            _emit_metric("meter.malformed_record")
            _send_to_dlq(log_event, f"malformed_record: {malformed}")
            continue
        try:
            parsed = _extract(record)
        except Exception as exc:  # noqa: BLE001 - deterministic per-record failure must never fail the batch
            logger.warning("meter: extract failed, DLQ + skip: %s", exc)
            _emit_metric("meter.parse_failed")
            _send_to_dlq(log_event, f"extract_failed: {exc}")
            continue
        owner_id, tier = _resolve_owner_and_tier(record)
        if owner_id is None:
            # NEVER ingest unattributed: a customer-less event lands in
            # Metronome's unmatched-events bin and silently vanishes from
            # revenue. model_id is a log field, not a metric dimension
            # (per-owner cardinality).
            logger.warning("meter: unattributed record model=%s req=%s", parsed["model_id"], parsed["request_id"])
            _emit_metric("meter.unattributed")
            _send_to_dlq(log_event, "unattributed")
            continue
        if tier is None:
            # Owner resolved but the row carries no tier: the event could be
            # ingested but never rated (the billable metrics group by tier).
            # Same posture as unattributed — alarm + hold, never half-bill.
            logger.warning("meter: no tier on reverse row model=%s req=%s", parsed["model_id"], parsed["request_id"])
            _emit_metric("meter.unknown_model")
            _send_to_dlq(log_event, "missing_tier")
            continue
        parsed["owner_id"] = owner_id
        parsed["tier"] = tier
        try:
            _ingest_to_metronome(parsed)
        except Exception as exc:  # noqa: BLE001 - any transport error must DLQ, not drop
            logger.error("meter: Metronome ingest failed, DLQ + skip: %s", exc)
            _emit_metric("meter.transport_error")
            _send_to_dlq(log_event, f"transport_error: {exc}")
            continue
    return {"statusCode": 200}
