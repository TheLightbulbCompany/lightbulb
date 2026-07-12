"""Bedrock invocation-log usage meter — Metronome ingest (paid) + trial cap (trial).

A CloudWatch Logs subscription delivers gzipped+base64 records of Bedrock
model-invocation logs. This Lambda parses each record, attributes it to an
owner via the per-env aip-owners reverse table, and then splits on the
owner's ``plan`` (an attribute of that SAME reverse row — zero extra reads):

  plan == "paid"  -> POST one event per record straight to Metronome's
                     /v1/ingest. Metronome rates the event (rate card),
                     maintains the prepaid balance, and dedups exactly-once on
                     ``transaction_id`` (the Bedrock requestId, 34-day window)
                     — so DLQ redrives and CW-Logs batch retries are always
                     safe to replay. Untouched by everything below: a paid
                     record never touches the dedup table, never touches the
                     daily counter, and can never be killed by the cap.
  plan == "trial" -> price the record LOCALLY (the rate table below) and ADD
                     the microcents to the owner's free-tier daily-spend
                     counter, then REVOKE Bedrock at the $3/day cap. A trial
                     owner has no Stripe customer and therefore no Metronome
                     contract, so a trial record is NEVER ingested — an ingest
                     would either fail or accrue an uncollectable invoice.

Anything that is not exactly "trial" (including a legacy row written before
the attribute existed) is treated as paid: the money path must fail towards
"bill it", never towards "silently drop it".

THE CAP IS THE ONLY REAL FENCE. The container holds its own AWS credentials
and calls Bedrock directly — the backend is not on the inference path. So the
only thing that provably stops a free owner's spend is removing the
container's ability to invoke: delete the per-owner scoped inline IAM policy
(every subsequent invoke is AccessDenied within ~5-15s of IAM propagation)
AND set the owner's ECS service to desiredCount=0 (the instant stop for an
already-admitted streaming call, which the IAM revoke does not kill). Those
two actions are fired INDEPENDENTLY — a failure of one must still attempt the
other — and both are idempotent (a missing policy is already-revoked; a
stopped service is already-stopped). This mirrors ecs_manager.kill_owner_bedrock.

REPLAY SAFETY. Metronome's transaction_id dedup is what makes the paid path
replay-safe; a DynamoDB counter increment has NO such dedup, so a CW-Logs batch
retry or a DLQ redrive would double-count the trial counter and false-lock-out a
real user. Every counter write is therefore guarded by a conditional PutItem on
the per-env invocation-dedup table (PK requestId), whose TTL is deliberately set
LONGER than the DLQ's 14-day retention — see DEDUP_TTL_SECONDS. The guard
protects the COUNTER ONLY: a replayed paid record still ingests (Metronome
dedups it), which is the paid path's correctness story and must not be
regressed.

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
  - INVOCATION_DEDUP_TABLE       : isol8-{env}-bedrock-invocation-dedup
  - BILLING_TABLE                : isol8-{env}-billing-accounts
  - CONTAINERS_TABLE             : isol8-{env}-containers
  - ECS_CLUSTER_PREFIX           : isol8-{env}-container-  (cluster discovery)
  - DLQ_URL                      : that instance's own SQS DLQ
  - METER_ENV                    : the env dimension on emitted metrics, AND
                                   the env segment of the per-owner IAM role
                                   name (isol8-{env}-owner-bedrock-{owner})

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
import hashlib
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRONOME_INGEST_URL_DEFAULT = "https://api.metronome.com/v1/ingest"

# Free-tier daily cost cap. Lockstep with core/services/trial_cap.CAP_MICROCENTS
# (the READ side — gates chat with a friendly message). This module is the
# WRITE side and the only enforcement that actually stops spend.
# 1 dollar = 100 cents = 1,000,000 microcents, so $3.00/day = 3,000,000.
CAP_MICROCENTS = 3_000_000

# Dedup rows MUST outlive the longest window in which a record can be
# redelivered, which is the DLQ's own retention: bedrock-meter-stack sets
# retentionPeriod = 14 days. Anything shorter is not "safely under-counting" —
# it is the opposite. Once the claim row TTLs out, the conditional PutItem
# SUCCEEDS again, so a redrive past the TTL re-applies the counter and
# DOUBLE-counts, falsely locking a real trial user out early. 15 days gives a
# day of margin over the DLQ. The rows are a requestId plus a timestamp and
# TTL deletes are free, so the storage is noise.
DEDUP_TTL_SECONDS = 15 * 24 * 3600

# The inline policy name that build_owner_bedrock_policy attaches to the
# per-owner role — deleting it is the IAM half of the kill.
OWNER_BEDROCK_POLICY_NAME = "owner-bedrock"

# --------------------------------------------------------------------------
# Local rate table — the THIRD copy of these numbers (backend
# core/billing/bedrock_pricing._RATES and the containers/config.py catalog
# cost blocks are the other two). The Lambda has no VPC and no backend import
# path, so it cannot share the module; a lockstep test in test_index.py
# asserts this table is byte-equal to the backend's, because without it these
# WILL drift and the cap will enforce the wrong dollar amount.
#
# USD per MILLION tokens, AWS-verified `us.` Regional (CRIS) rate card.
# cache_read is the 0.1x input rate; cache_write is the 1-HOUR tier (2x input)
# because agents.defaults.params.cacheRetention is pinned to "long". Cache
# tokens dominate a real agent turn — omitting them undercounts ~30x, which is
# precisely the runaway this cap exists to stop.
# --------------------------------------------------------------------------
_RATES = {
    "anthropic.claude-sonnet-4-6": {
        "input": 3.3,
        "output": 16.5,
        "cache_write": 6.6,
        "cache_read": 0.33,
    },
    "anthropic.claude-opus-4-6-v1": {
        "input": 5.5,
        "output": 27.5,
        "cache_write": 11.0,
        "cache_read": 0.55,
    },
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 1.1,
        "output": 5.5,
        "cache_write": 2.2,
        "cache_read": 0.11,
    },
}

# Tier (the AIP "lane" the owner's reverse row carries) -> rate-table key. The
# record's modelId is the per-owner AIP ARN, which carries no foundation-model
# id, so the tier is the ONLY bridge from a record to a price. Mirrors
# core/billing/bedrock_aip.TIER_TO_FM (also lockstep-tested).
TIER_TO_FM = {
    "opus": "anthropic.claude-opus-4-6-v1",
    "sonnet": "anthropic.claude-sonnet-4-6",
    "haiku": "anthropic.claude-haiku-4-5-20251001-v1:0",
}

# Cold-start caches: the Metronome bearer token, the boto3 clients and the
# resolved ECS cluster ARN are fetched/built once per Lambda container and
# reused across invocations.
_METRONOME_TOKEN = None
_DDB = None
_SQS = None
_IAM = None
_ECS = None
_CLUSTER_ARN = None


class UnknownTierError(Exception):
    """The reverse row's tier maps to no known foundation model — unpriceable.

    Deliberately not a KeyError subclass, for the same reason as the backend's
    UnknownModelError: a pricing failure must never be swallowed by an
    unrelated `except KeyError:`.
    """


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


def _iam():
    global _IAM
    if _IAM is None:
        _IAM = boto3.client("iam")
    return _IAM


def _ecs():
    global _ECS
    if _ECS is None:
        _ECS = boto3.client("ecs")
    return _ECS


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
    """Resolve (owner_id, tier, plan) from the record's AIP ARN, or (None, None, None).

    The record's ``modelId`` is the per-owner application-inference-profile
    ARN; the aip-owners reverse table (written atomically per row at provision
    by bedrock_aip.persist_owner_aips) maps it to ``owner_id`` + ``tier`` +
    ``plan`` in ONE GetItem. The per-owner IAM role can only invoke that
    owner's own AIPs, so every record reaching this instance's subscription
    filter should resolve — a miss means the reverse row is absent (a
    provisioning gap) and must surface on the alarmed meter.unattributed
    metric, never be ingested.

    ``plan`` rides along in the read that was happening anyway (zero extra
    reads). It defaults to "paid" when absent — rows written before the
    attribute existed carry no plan, and the money path must fail towards
    "bill it" rather than towards "silently drop it".

    A DDB transport error deliberately propagates: the whole batch fails and
    CloudWatch Logs retries it, which is safe because Metronome dedups any
    already-ingested records on transaction_id and the trial counter is
    guarded by its own per-requestId dedup row.
    """
    resp = _ddb().get_item(
        TableName=os.environ["AIP_OWNERS_TABLE"],
        Key={"aip_arn": {"S": record.get("modelId", "")}},
    )
    item = resp.get("Item")
    if not item:
        return (None, None, None)
    owner = (item.get("owner_id") or {}).get("S")
    tier = (item.get("tier") or {}).get("S")
    plan = (item.get("plan") or {}).get("S") or "paid"
    return (owner or None, tier or None, plan)


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


# ===========================================================================
# TRIAL PATH — price locally, count, and kill at the cap.
# Nothing below this line runs for a paid owner.
# ===========================================================================


def _price_from_tier(tier, *, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens):
    """Price one invocation in microcents from the owner's tier. PURE.

    Byte-for-byte the backend's bedrock_pricing.price_from_tier: tier ->
    TIER_TO_FM -> rate -> per-component int() truncation, summed. The
    per-component truncation (rather than truncating the sum) is deliberate —
    it is what the backend does, and the lockstep test asserts both tables and
    this arithmetic agree with it.

    Raises:
        UnknownTierError: the tier maps to no known foundation model.
    """
    fm_id = TIER_TO_FM.get(tier) if tier else None
    rate = _RATES.get(fm_id) if fm_id else None
    if rate is None:
        raise UnknownTierError(tier)
    return (
        int(input_tokens * rate["input"])
        + int(output_tokens * rate["output"])
        + int(cache_read_tokens * rate["cache_read"])
        + int(cache_write_tokens * rate["cache_write"])
    )


def _utc_today():
    """Today's UTC date key (YYYY-MM-DD) — the counter's day bucket.

    Deliberately "now", not the record's own timestamp. The counter is a rate
    limiter, not a ledger (nobody is ever invoiced from it — a trial owner has
    no Stripe customer), and the read side (trial_cap.is_over_daily_cap) asks
    "how much has this owner spent TODAY". Bucketing a late-delivered record
    into a past day would both under-defend today and let a DLQ redrive kill an
    owner for a day that already rolled over. Records arrive seconds-to-minutes
    after the invocation, so the two clocks agree in every real case.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _claim_request(request_id):
    """Claim ``request_id`` for the counter. True = first delivery, count it.

    THE REPLAY GUARD. CloudWatch Logs retries a whole batch on any raised
    error and DLQ redrives are declared always-safe-to-replay — both are only
    true because Metronome dedups on transaction_id. A DynamoDB ADD has no
    such dedup, so an unguarded counter would double-count on every retry and
    false-lock-out a real user at half the real spend.

    Conditional PutItem on the per-env dedup table; a ConditionalCheckFailed
    means we already counted this record. Claim-then-ADD is deliberately
    at-most-once: if the ADD fails after the claim lands, the record DLQs
    (alarmed, redrivable by an operator) and the redrive SKIPS the counter
    rather than risking a double count on an ADD whose response was merely
    lost. Under-counting one record is recoverable; false-locking a user out
    of a product they are evaluating is not.

    Guards the COUNTER only — the paid path never calls this, so a replayed
    paid record still ingests (Metronome dedups it).
    """
    try:
        _ddb().put_item(
            TableName=os.environ["INVOCATION_DEDUP_TABLE"],
            Item={
                "request_id": {"S": request_id},
                "ttl": {"N": str(int(time.time()) + DEDUP_TTL_SECONDS)},
            },
            ConditionExpression="attribute_not_exists(request_id)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _add_daily_spend(owner_id, date_key, delta_microcents):
    """ADD ``delta_microcents`` to billing-accounts.meta_daily_spend_microcents.{date}
    and return the NEW total for that day.

    Mirrors billing_repo.add_meta_daily_spend in raw boto3. It is a GUARDED
    TWO-STEP because DynamoDB rejects a SET on a map and an ADD on a nested key
    of that same map in one UpdateExpression ("Two document paths overlap"):
      1. initialize the map to {} only if the row exists and the map is absent
         (a swallowed ConditionalCheckFailed if another writer won the race or
         the map already exists), then
      2. the real atomic ADD on the nested date leaf, still guarded by
         attribute_exists(owner_id) so a missing billing row raises instead of
         creating a phantom.

    The attribute name is a historical holdover and is kept deliberately: live
    rows carry it. It is simply the trial daily-spend counter now, and this
    Lambda is its SOLE writer (a second writer would double-count).
    """
    table = os.environ["BILLING_TABLE"]
    try:
        _ddb().update_item(
            TableName=table,
            Key={"owner_id": {"S": owner_id}},
            UpdateExpression="SET meta_daily_spend_microcents = :empty",
            ExpressionAttributeValues={":empty": {"M": {}}},
            ConditionExpression=("attribute_exists(owner_id) AND attribute_not_exists(meta_daily_spend_microcents)"),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Map already exists, or the owner row doesn't — the increment below
        # re-checks attribute_exists(owner_id) and raises in the no-row case.

    # Nested-path SET + if_not_exists, not ADD. Both are legal on a nested
    # leaf against a real DynamoDB engine, but AWS recommends SET over ADD and
    # this write IS the spend fence — an increment that silently fails to
    # apply is an unbounded bill. Mirrors billing_repo.add_meta_daily_spend.
    resp = _ddb().update_item(
        TableName=table,
        Key={"owner_id": {"S": owner_id}},
        UpdateExpression=(
            "SET meta_daily_spend_microcents.#d = if_not_exists(meta_daily_spend_microcents.#d, :zero) + :delta"
        ),
        ExpressionAttributeNames={"#d": date_key},
        ExpressionAttributeValues={
            ":delta": {"N": str(int(delta_microcents))},
            ":zero": {"N": "0"},
        },
        ConditionExpression="attribute_exists(owner_id)",
        ReturnValues="ALL_NEW",
    )
    day = resp.get("Attributes", {}).get("meta_daily_spend_microcents", {}).get("M", {}).get(date_key, {})
    return int(day.get("N", 0))


def _owner_role_name(owner_id):
    """The per-owner ECS task role. Mirrors bedrock_aip._owner_role_name — the
    CDK scopes iam:DeleteRolePolicy to exactly this 'isol8-{env}-owner-bedrock-*'
    prefix."""
    return f"isol8-{os.environ['METER_ENV']}-owner-bedrock-{owner_id}"


def _service_name(owner_id):
    """The owner's ECS service name. Mirrors ecs_manager._service_name — the
    backend's own stop path DERIVES this name the same way rather than reading
    the containers row, so deriving it here needs no DynamoDB read and cannot
    disagree with the service that actually exists."""
    uid_hash = hashlib.sha256(owner_id.encode()).hexdigest()[:12]
    safe_uid = re.sub(r"[^a-zA-Z0-9_-]", "", owner_id)[:40]
    return f"openclaw-{safe_uid}-{uid_hash}"


def _cluster_arn():
    """The env's ECS cluster ARN, discovered once per container and cached.

    The cluster is created by each env's container stack with a CFN-generated
    physical name (isol8-{env}-container-Cluster<hash>-<random>) — renaming it
    would REPLACE it and destroy every per-user service, so the name cannot be
    made deterministic. This account-scoped stack (instantiated in the prod
    stage, holding BOTH env instances) also cannot hold a cross-stage object
    ref to the dev cluster. So the name is resolved at runtime by prefix, from
    an env var, keeping the "instances differ only in env vars" rule intact.
    """
    global _CLUSTER_ARN
    if _CLUSTER_ARN is None:
        prefix = os.environ["ECS_CLUSTER_PREFIX"]
        matches = []
        token = None
        while True:
            kwargs = {"nextToken": token} if token else {}
            page = _ecs().list_clusters(**kwargs)
            matches += [arn for arn in page.get("clusterArns", []) if arn.split("/")[-1].startswith(prefix)]
            token = page.get("nextToken")
            if not token:
                break
        if not matches:
            raise RuntimeError(f"no ECS cluster with prefix {prefix!r}")
        if len(matches) > 1:
            logger.warning("meter: %d clusters match prefix %s, using %s", len(matches), prefix, matches[0])
        _CLUSTER_ARN = matches[0]
    return _CLUSTER_ARN


def _revoke_owner_iam(owner_id):
    """Delete the owner's scoped inline Bedrock policy. Idempotent: NoSuchEntity
    means it is already revoked.

    Eventually consistent (~5-15s of IAM propagation), so it is fired FIRST to
    start that clock — but it does NOT kill an already-admitted streaming call,
    which is why it is paired with the ECS stop.
    """
    try:
        _iam().delete_role_policy(
            RoleName=_owner_role_name(owner_id),
            PolicyName=OWNER_BEDROCK_POLICY_NAME,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise


def _stop_owner_service(owner_id):
    """Scale the owner's ECS service to 0 — the guaranteed instant stop.
    Idempotent: a missing or inactive service is already stopped."""
    try:
        _ecs().update_service(
            cluster=_cluster_arn(),
            service=_service_name(owner_id),
            desiredCount=0,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ServiceNotFoundException", "ServiceNotActiveException"):
            raise


def _stamp_bedrock_revoked(owner_id):
    """Mark the containers row so the backend can see the revoke — the daily
    restore reads this flag to re-attach the policy when the day rolls over."""
    _ddb().update_item(
        TableName=os.environ["CONTAINERS_TABLE"],
        Key={"owner_id": {"S": owner_id}},
        UpdateExpression="SET bedrock_revoked = :t",
        ExpressionAttributeValues={":t": {"BOOL": True}},
        ConditionExpression="attribute_exists(owner_id)",
    )


def _kill_owner_bedrock(owner_id):
    """Cut the owner off from Bedrock. Never raises.

    The two kill actions are INDEPENDENT: a revoke failure must still attempt
    the ECS stop, and vice versa — coupling them would let one transient IAM
    error leave a runaway container invoking Bedrock indefinitely. Each is
    idempotent, so a redundant call is harmless. Every failure emits its own
    alarmed metric: an un-executed kill is real money, so it must page rather
    than fail the batch (raising here would fail records that are already
    counted, and CW Logs would retry them into the dedup guard for nothing).
    """
    _emit_metric("meter.cap_kill", owner_id=owner_id)
    # Looked up on the module at call time (not bound at def time) so each step
    # stays independently patchable/testable.
    steps = (
        ("iam_revoke", _revoke_owner_iam, "meter.kill_iam_error"),
        ("ecs_stop", _stop_owner_service, "meter.kill_ecs_error"),
        ("revoked_stamp", _stamp_bedrock_revoked, "meter.kill_stamp_error"),
    )
    for label, step, metric in steps:
        try:
            step(owner_id)
        except Exception as exc:  # noqa: BLE001 - each kill step is independent
            _emit_metric(metric, owner_id=owner_id)
            logger.exception("meter: kill step %s failed for owner %s: %s", label, owner_id, exc)


def _meter_trial_spend(parsed):
    """Price a trial record, count it once, and kill at the cap.

    Raises:
        UnknownTierError: the tier is unpriceable (caller DLQs + alarms).
        ClientError: a DynamoDB transport failure (caller DLQs + alarms).
    """
    owner_id = parsed["owner_id"]
    microcents = _price_from_tier(
        parsed["tier"],
        input_tokens=parsed["input_tokens"],
        output_tokens=parsed["output_tokens"],
        cache_read_tokens=parsed["cache_read_tokens"],
        cache_write_tokens=parsed["cache_write_tokens"],
    )
    if not _claim_request(parsed["request_id"]):
        logger.info("meter: replay of req=%s, counter already applied", parsed["request_id"])
        _emit_metric("meter.replay_skipped", owner_id=owner_id)
        return

    total = _add_daily_spend(owner_id, _utc_today(), microcents)
    # Kill on the CROSSING, not on every over-cap record: the records already
    # in flight when the cap is crossed each land afterwards, and re-killing on
    # each of them would be pure noise. The pre-add total is the new total
    # minus this record's own price — no extra read.
    if total >= CAP_MICROCENTS > total - microcents:
        logger.warning(
            "meter: owner %s crossed the daily cap (%d >= %d microcents) — revoking Bedrock",
            owner_id,
            total,
            CAP_MICROCENTS,
        )
        _kill_owner_bedrock(owner_id)


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
        # necessarily a usable record. requestId is load-bearing — it is BOTH
        # the Metronome transaction_id AND the trial counter's dedup key, i.e.
        # the ENTIRE replay/dedup safety story (DLQ redrives, CW Logs batch
        # retries). An event without one must never be ingested and must never
        # be counted (an empty key would collapse every such record onto one
        # dedup row and count only the first). modelId is the attribution key.
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
        owner_id, tier, plan = _resolve_owner_and_tier(record)
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
        parsed["plan"] = plan
        if plan == "trial":
            # Trial owners have no Stripe customer and therefore no Metronome
            # contract — an ingest would fail, or worse, accrue an
            # uncollectable invoice. They are metered LOCALLY instead, and the
            # cap is the only thing that actually stops their spend. Anything
            # that is not exactly "trial" falls through to the paid path
            # untouched (byte-identical to the pre-cap Lambda).
            try:
                _meter_trial_spend(parsed)
            except UnknownTierError as exc:
                logger.error("meter: unpriceable tier %r req=%s, DLQ + skip", tier, parsed["request_id"])
                _emit_metric("meter.unknown_model")
                _send_to_dlq(log_event, f"unpriceable_tier: {exc}")
            except Exception as exc:  # noqa: BLE001 - a counter failure must DLQ, not drop
                # An uncounted record is spend we cannot see, so it is held on
                # the DLQ + alarmed. Redriving is safe: the dedup guard makes
                # the counter at-most-once.
                logger.error("meter: trial counter failed, DLQ + skip: %s", exc)
                _emit_metric("meter.counter_error")
                _send_to_dlq(log_event, f"counter_error: {exc}")
            continue
        try:
            _ingest_to_metronome(parsed)
        except Exception as exc:  # noqa: BLE001 - any transport error must DLQ, not drop
            logger.error("meter: Metronome ingest failed, DLQ + skip: %s", exc)
            _emit_metric("meter.transport_error")
            _send_to_dlq(log_event, f"transport_error: {exc}")
            continue
    return {"statusCode": 200}
