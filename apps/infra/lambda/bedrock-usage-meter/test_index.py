"""Tests for the Bedrock usage-meter Lambda — extraction, attribution, ingest,
the free-trial daily counter, and the cap kill.

Two-plan design: the Lambda parses each CloudWatch invocation-log record,
resolves (owner_id, tier, plan) with ONE GetItem on the per-env aip-owners
reverse table, then splits:

  paid  -> POST a single-event JSON array to Metronome /v1/ingest. Byte-
           identical to the pre-cap Lambda: no dedup row, no counter, no kill.
  trial -> price the record LOCALLY, ADD the microcents to
           billing-accounts.trial_daily_spend_microcents.{UTC date}, and at
           $3/day revoke the owner's Bedrock IAM policy AND stop their ECS
           service. NEVER ingested (a trial owner has no Metronome contract).

ALL billable counts are read from the TOP LEVEL of the record
(`input.inputTokenCount` / `output.outputTokenCount` /
`input.cacheReadInputTokenCount` / `input.cacheWriteInputTokenCount`); the
nested `output.outputBodyJson.usage` block is a defensive fallback only.

Failure posture under test: every per-record failure (unparseable record,
malformed record shape, unattributed owner, missing tier, unpriceable tier,
counter failure, Metronome transport error) DLQs the raw log event + emits a
metric + continues the batch — never a silent drop, and an unattributed or
transaction-id-less record is NEVER ingested.

The DynamoDB paths (the dedup guard and the guarded two-step counter) are
exercised against a REAL DynamoDB engine (moto), not a hand mock: the counter
is a nested-map ADD that DynamoDB rejects if written naively, so a mock would
happily pass code that fails in production. IAM and ECS are stubbed — the
assertions are on the exact calls, which is the whole contract.

The rate-card/CAP_MICROCENTS lockstep tests below exist because the gate/
meter/backend copies are forced by process boundaries (the gate has no
Python import path, the meter has no VPC) and must NOT be consolidated —
see `docs/superpowers/plans/2026-09-07-audit-report.md` §6, "Deliberately
NOT recommended".

Running: these tests need `boto3`, `moto` and the backend importable (the
lockstep rate-table test imports core.billing.bedrock_pricing) — run them with
the backend's env, e.g.
    apps/backend/.venv/bin/python -m pytest apps/infra/lambda/bedrock-usage-meter -q
"""

import base64
import gzip
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
BACKEND = os.path.abspath(os.path.join(HERE, "..", "..", "..", "backend"))
INFRA_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
GATE_STACK_TS = os.path.join(INFRA_ROOT, "lib", "stacks", "bedrock-gate-stack.ts")

BILLING_TABLE = "isol8-dev-billing-accounts"
CONTAINERS_TABLE = "isol8-dev-containers"
DEDUP_TABLE = "isol8-dev-bedrock-invocation-dedup"
GATE_TOKENS_TABLE = "isol8-dev-bedrock-gate-tokens"
CLUSTER_ARN = "arn:aws:ecs:us-east-1:877352799272:cluster/isol8-dev-container-ClusterEB0386A7-L9MzqPCGDupk"

# A record signed by the per-env GATEWAY role (lightbulb-dev-bedrock-gate) rather
# than the per-owner role — the marker _is_gateway_identity keys on.
GATEWAY_IDENTITY_ARN = "arn:aws:sts::877352799272:assumed-role/lightbulb-dev-bedrock-gate/gate-session"

OWNER_ITEM = {
    "Item": {
        "aip_arn": {"S": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus-org_abc"},
        "owner_id": {"S": "org_abc"},
        "tier": {"S": "opus"},
        "plan": {"S": "paid"},
    }
}

# The fixture's four non-zero token counts (1200 in / 350 out / 142000
# cache-read / 9800 cache-write) priced on the SONNET rate card — the trial
# tier. Cache tokens are ~92% of it: the no-cache price of the same record is
# 9,735 microcents, so a meter that ignored the cache counts would undercount
# this record ~12x and the $3 cap would fire days late.
SONNET_PRICE_MICROCENTS = 121_275
SONNET_PRICE_NO_CACHE = 9_735


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def _awslogs_event(*records):
    payload = {"logEvents": [{"message": json.dumps(r)} for r in records]}
    raw = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(raw).decode()}}


def _utc_today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    monkeypatch.setenv("METRONOME_API_KEY", "metronome-token-abc")
    monkeypatch.setenv("AIP_OWNERS_TABLE", "isol8-dev-bedrock-aip-owners")
    monkeypatch.setenv("INVOCATION_DEDUP_TABLE", DEDUP_TABLE)
    monkeypatch.setenv("BILLING_TABLE", BILLING_TABLE)
    monkeypatch.setenv("CONTAINERS_TABLE", CONTAINERS_TABLE)
    monkeypatch.setenv("GATE_TOKENS_TABLE", GATE_TOKENS_TABLE)
    monkeypatch.setenv("ECS_CLUSTER_PREFIX", "isol8-dev-container-")
    monkeypatch.setenv("DLQ_URL", "https://sqs.test.local/dlq")
    monkeypatch.setenv("METER_ENV", "dev")
    # moto refuses to run without credentials + a region.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    # Re-import fresh per test so the module-global cold-start caches (token,
    # boto3 clients, resolved cluster ARN) never leak state between tests.
    sys.modules.pop("index", None)
    import index  # noqa: F401

    yield
    sys.modules.pop("index", None)


def _stub_resolver(owner="org_abc", tier="opus", plan="paid"):
    return patch("index._resolve_owner_and_tier", return_value=(owner, tier, plan))


@contextmanager
def _aws_world(
    *, owner="org_abc", starting_spend=None, billing_row=True, container_row=True, gate_token_hash=None
):
    """A real (moto) DynamoDB with the four tables the counter + kill path touch,
    plus stubbed IAM/ECS clients. Yields (ddb, iam, ecs).

    The counter is a nested-map ADD guarded by a conditional map init — a hand
    mock would accept the naive single-expression form DynamoDB rejects, so
    the money path is tested against the real engine.

    ``gate_token_hash`` (when set) writes the rotation pointer
    (bedrock_gate_token_hash) on the containers row AND seeds a gate-tokens row
    keyed by that hash with denied=false, so the deny-flag kill step has
    something to flip.
    """
    import index

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        for table, key in (
            (BILLING_TABLE, "owner_id"),
            (CONTAINERS_TABLE, "owner_id"),
            (DEDUP_TABLE, "request_id"),
            (GATE_TOKENS_TABLE, "token_hash"),
        ):
            ddb.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        if billing_row:
            item = {"owner_id": {"S": owner}, "subscription_status": {"S": "trial_local"}}
            if starting_spend is not None:
                item["trial_daily_spend_microcents"] = {"M": {_utc_today(): {"N": str(starting_spend)}}}
            ddb.put_item(TableName=BILLING_TABLE, Item=item)
        if container_row:
            container = {"owner_id": {"S": owner}, "status": {"S": "running"}}
            if gate_token_hash is not None:
                container["bedrock_gate_token_hash"] = {"S": gate_token_hash}
            ddb.put_item(TableName=CONTAINERS_TABLE, Item=container)
        if gate_token_hash is not None:
            ddb.put_item(
                TableName=GATE_TOKENS_TABLE,
                Item={
                    "token_hash": {"S": gate_token_hash},
                    "owner_id": {"S": owner},
                    "denied": {"BOOL": False},
                },
            )

        iam, ecs = MagicMock(), MagicMock()
        ecs.list_clusters.return_value = {"clusterArns": [CLUSTER_ARN]}
        with patch.object(index, "_iam", return_value=iam), patch.object(index, "_ecs", return_value=ecs):
            yield ddb, iam, ecs


def _daily_spend(ddb, owner="org_abc"):
    """The owner's whole trial_daily_spend_microcents map, as {date: int}."""
    item = ddb.get_item(TableName=BILLING_TABLE, Key={"owner_id": {"S": owner}}).get("Item", {})
    day_map = item.get("trial_daily_spend_microcents", {}).get("M", {})
    return {date: int(v["N"]) for date, v in day_map.items()}


# ---------------------------------------------------------------------------
# Extraction: every billable count comes from the TOP LEVEL of the record.
# ---------------------------------------------------------------------------


def test_extract_reads_top_level_tokens():
    import index

    record = _load("inline_record.json")
    parsed = index._extract(record)
    assert parsed["request_id"] == record["requestId"]
    assert parsed["model_id"] == record["modelId"]
    assert parsed["identity_arn"] == record["identity"]["arn"]
    assert parsed["input_tokens"] == record["input"]["inputTokenCount"]
    assert parsed["output_tokens"] == record["output"]["outputTokenCount"]


def test_extract_reads_cache_tokens_from_top_level():
    """The cache counts come from the TOP-LEVEL input block. The nested usage
    block is DELETED first so a wrong (nested-path-first) implementation cannot
    pass by accident, and the expected values are read straight from the
    fixture with non-zero guards — a zeroed fixture would make this vacuous
    (the ~30x cache undercount is the landmine this meter exists to prevent).
    """
    import index

    record = _load("inline_record.json")
    expected_read = int(record["input"]["cacheReadInputTokenCount"])
    expected_write = int(record["input"]["cacheWriteInputTokenCount"])
    assert expected_read > 0, "fixture must keep a non-zero top-level cacheRead count"
    assert expected_write > 0, "fixture must keep a non-zero top-level cacheWrite count"
    del record["output"]["outputBodyJson"]

    parsed = index._extract(record)
    assert parsed["cache_read_tokens"] == expected_read
    assert parsed["cache_write_tokens"] == expected_write


def test_extract_falls_back_to_nested_usage_when_top_level_absent():
    """If a delivery variant omits the top-level counts, fall back to the
    output body's usage block (documented resilience path)."""
    import index

    record = _load("inline_record.json")
    usage = record["output"]["outputBodyJson"]["usage"]
    del record["input"]["inputTokenCount"]
    del record["input"]["cacheReadInputTokenCount"]
    del record["input"]["cacheWriteInputTokenCount"]
    del record["output"]["outputTokenCount"]

    parsed = index._extract(record)
    assert parsed["input_tokens"] == int(usage["inputTokens"])
    assert parsed["output_tokens"] == int(usage["outputTokens"])
    assert parsed["cache_read_tokens"] == int(usage["cacheReadInputTokens"])
    assert parsed["cache_write_tokens"] == int(usage["cacheWriteInputTokens"])


# ---------------------------------------------------------------------------
# Attribution: one GetItem on the per-env aip-owners reverse table.
# ---------------------------------------------------------------------------


def test_resolve_owner_and_tier_reads_reverse_table():
    """owner + tier + plan all come out of ONE GetItem — `plan` rides along in
    the read that was already happening, so the trial branch costs zero reads."""
    import index

    record = _load("inline_record.json")
    fake_ddb = MagicMock()
    fake_ddb.get_item.return_value = OWNER_ITEM

    with patch("index._ddb", return_value=fake_ddb):
        owner, tier, plan = index._resolve_owner_and_tier(record)

    assert (owner, tier, plan) == ("org_abc", "opus", "paid")
    fake_ddb.get_item.assert_called_once_with(
        TableName="isol8-dev-bedrock-aip-owners",
        Key={"aip_arn": {"S": record["modelId"]}},
    )


def test_resolve_reads_trial_plan_from_reverse_row():
    import index

    item = {"Item": dict(OWNER_ITEM["Item"], plan={"S": "trial"}, tier={"S": "sonnet"})}
    fake_ddb = MagicMock()
    fake_ddb.get_item.return_value = item
    with patch("index._ddb", return_value=fake_ddb):
        assert index._resolve_owner_and_tier(_load("inline_record.json")) == ("org_abc", "sonnet", "trial")


def test_resolve_defaults_missing_plan_to_paid():
    """Rows written before the `plan` attribute existed carry none. The money
    path must fail towards "bill it", never towards "silently drop it"."""
    import index

    legacy = {"Item": {k: v for k, v in OWNER_ITEM["Item"].items() if k != "plan"}}
    fake_ddb = MagicMock()
    fake_ddb.get_item.return_value = legacy
    with patch("index._ddb", return_value=fake_ddb):
        assert index._resolve_owner_and_tier(_load("inline_record.json")) == ("org_abc", "opus", "paid")


def test_resolve_owner_miss_returns_none_triple():
    import index

    fake_ddb = MagicMock()
    fake_ddb.get_item.return_value = {}
    with patch("index._ddb", return_value=fake_ddb):
        assert index._resolve_owner_and_tier(_load("inline_record.json")) == (None, None, None)


def test_handler_unattributed_dlqs_and_never_ingests():
    """A reverse-table miss is a provisioning gap (alarmed) — the record is
    held on the DLQ and NEVER ingested (a customer-less event would vanish
    into Metronome's unmatched-events bin: a silent revenue leak)."""
    import index

    record = _load("inline_record.json")
    ingested, dlqd, metrics = [], [], []

    with (
        _stub_resolver(owner=None, tier=None),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(record), None)

    assert result == {"statusCode": 200}
    assert ingested == []
    assert len(dlqd) == 1
    assert dlqd[0][1] == "unattributed"
    assert "meter.unattributed" in metrics


def test_handler_drops_foreign_env_gateway_without_dlq():
    """The other env's gateway traffic is dropped, not DLQ'd.

    The subscription filter cannot split the two envs: the dev gate Lambda
    lives in the prod-named stack, so its generated role name contains the
    literal `lightbulb-prod-bedrock-gate` and the PROD filter matches DEV records.
    Such a record can never resolve against this env's reverse table, so
    without the guard it DLQs as `unattributed` and looks like a revenue
    miss. METER_ENV is "dev" here, so a prod-gate session is the foreign one.
    """
    import index

    record = _load("inline_record.json")
    record["identity"]["arn"] = (
        "arn:aws:sts::877352799272:assumed-role/"
        "lightbulb-prod-bedrock-gate-GateFnDevServiceRoleE10163F-t7q3kmHZY2Pl/"
        "lightbulb-prod-bedrock-gate"
    )
    ingested, dlqd, metrics = [], [], []

    with (
        _stub_resolver(owner=None, tier=None),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(record), None)

    assert result == {"statusCode": 200}
    assert ingested == []
    assert dlqd == []
    assert "meter.foreign_env_skipped" in metrics
    assert "meter.unattributed" not in metrics


def test_handler_own_env_gateway_still_dlqs_when_unattributed():
    """The foreign-env guard must not swallow a REAL provisioning gap.

    A gateway record from THIS env that fails attribution is still a genuine
    miss and must keep its DLQ + alarm posture.
    """
    import index

    record = _load("inline_record.json")
    record["identity"]["arn"] = (
        "arn:aws:sts::877352799272:assumed-role/"
        "lightbulb-prod-bedrock-gate-GateFnDevServiceRoleE10163F-t7q3kmHZY2Pl/"
        "lightbulb-dev-bedrock-gate"
    )
    dlqd, metrics = [], []

    with (
        _stub_resolver(owner=None, tier=None),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: None),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index.handler(_awslogs_event(record), None)

    assert len(dlqd) == 1
    assert dlqd[0][1] == "unattributed"
    assert "meter.foreign_env_skipped" not in metrics


def test_handler_missing_tier_dlqs_and_never_ingests():
    """Owner resolved but no tier on the row: the event could ingest but never
    rate (billable metrics group by tier) — same hold-and-alarm posture."""
    import index

    record = _load("inline_record.json")
    ingested, dlqd, metrics = [], [], []

    with (
        _stub_resolver(owner="org_abc", tier=None),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index.handler(_awslogs_event(record), None)

    assert ingested == []
    assert dlqd[0][1] == "missing_tier"
    assert "meter.unknown_model" in metrics


# ---------------------------------------------------------------------------
# Ingest: array-of-one event, everything stringified, Bearer auth.
# ---------------------------------------------------------------------------


def test_ingest_payload_shape_and_auth():
    import index

    record = _load("inline_record.json")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        return MagicMock()

    with (
        _stub_resolver(),
        patch("index.urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        index.handler(_awslogs_event(record), None)

    assert captured["url"] == "https://api.metronome.com/v1/ingest"
    assert captured["headers"]["authorization"] == "Bearer metronome-token-abc"
    assert captured["headers"]["content-type"] == "application/json"

    # The body is a JSON ARRAY of exactly one event.
    assert isinstance(captured["body"], list)
    assert len(captured["body"]) == 1
    event = captured["body"][0]
    assert event["transaction_id"] == record["requestId"]
    assert event["customer_id"] == "org_abc"
    assert event["event_type"] == "bedrock_invocation"
    assert event["timestamp"] == record["timestamp"]

    # EVERY property value is stringified (Metronome ingest docs) and the
    # numeric values are the exact top-level fixture counts.
    props = event["properties"]
    assert all(isinstance(v, str) for v in props.values())
    assert props == {
        "tier": "opus",
        "model_id": record["modelId"],
        "input_tokens": str(record["input"]["inputTokenCount"]),
        "output_tokens": str(record["output"]["outputTokenCount"]),
        "cache_read_tokens": str(record["input"]["cacheReadInputTokenCount"]),
        "cache_write_tokens": str(record["input"]["cacheWriteInputTokenCount"]),
        "region": record["region"],
        "run_status": "end",
    }


def test_ingest_url_env_override(monkeypatch):
    import index

    monkeypatch.setenv("METRONOME_INGEST_URL", "https://sandbox.test.local/v1/ingest")
    record = _load("inline_record.json")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return MagicMock()

    with (
        _stub_resolver(),
        patch("index.urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        index.handler(_awslogs_event(record), None)

    assert captured["url"] == "https://sandbox.test.local/v1/ingest"


# ---------------------------------------------------------------------------
# FAIL-SAFE TRANSPORT. A CloudWatch Logs subscription invokes the Lambda
# SYNCHRONOUSLY, so the function's async DLQ never fires on a per-record
# failure. Every per-record failure path must durably route to the SQS DLQ +
# emit a metric and continue the batch — retry-safe because Metronome dedups
# on transaction_id (34-day window).
# ---------------------------------------------------------------------------


def test_dlq_copy_is_redacted_but_still_meterable():
    """The DLQ retains messages for 14 days — far past the log group's 3-day
    retention — so the stored copy must NOT carry the invocation bodies (live
    records were observed containing a tenant's Paperclip API key + gateway
    JWT in plaintext). It MUST still carry everything a redrive needs: the
    top-level token/cache counts and the output usage block."""
    import index

    record = _load("inline_record.json")
    record["input"]["inputBodyJson"] = {"messages": [{"content": "SECRET pcp_12345 TOKEN"}]}
    log_event = {"id": "e1", "timestamp": 1, "message": json.dumps(record)}
    sent = []
    sqs = MagicMock()
    sqs.send_message.side_effect = lambda **kw: sent.append(kw)

    with patch.object(index, "_sqs", return_value=sqs), patch.dict(
        os.environ, {"DLQ_URL": "https://sqs.test.local/dlq"}
    ):
        index._send_to_dlq(log_event, "counter_error: boom")

    body = json.loads(sent[0]["MessageBody"])
    stored = json.loads(body["log_event"]["message"])
    # Bodies gone — no secret survives the DLQ write.
    assert "pcp_12345" not in sent[0]["MessageBody"]
    assert stored["input"]["inputBodyJson"] == "<redacted>"
    assert set(stored["output"]["outputBodyJson"].keys()) == {"usage"}
    # ...but the record still meters on redrive: counts + usage intact.
    parsed = index._extract(stored)
    original = index._extract(record)
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        assert parsed[field] == original[field]
    # The original event object was not mutated (callers may reuse it).
    assert "pcp_12345" in log_event["message"]


def test_redact_covers_non_dict_output_bodies():
    """outputBodyJson is redacted by KEY PRESENCE, like the input side: a
    string- or array-encoded response body is still a body and must not
    persist in the 14-day queue just because it isn't a dict."""
    import index

    for body in ("raw string response with pcp_67890", ["array", "of", "chunks"]):
        record = _load("inline_record.json")
        record["output"]["outputBodyJson"] = body
        ev = {"id": "e3", "timestamp": 3, "message": json.dumps(record)}
        stored = json.loads(index._redact_log_event(ev)["message"])
        assert stored["output"]["outputBodyJson"] == "<redacted>"
        assert "pcp_67890" not in json.dumps(stored)


def test_redact_replaces_unparseable_messages_with_placeholder():
    """A message that does not parse to a dict may be a TRUNCATED invocation
    record still carrying body fragments — it must not persist in the 14-day
    queue. The placeholder keeps length + digest for identification; the log
    group holds the original for its 3-day debug window, and a redrive could
    never process the raw copy anyway (it would just re-fail parsing)."""
    import index

    secret_fragment = '{"input": {"inputBodyJson": {"content": "pcp_TRUNCATED_SECRET'
    for message in (secret_fragment, "not json at all", json.dumps(["a", "list"]), json.dumps("str")):
        ev = {"id": "e2", "timestamp": 2, "message": message}
        redacted = index._redact_log_event(ev)
        assert "pcp_TRUNCATED_SECRET" not in redacted["message"]
        assert redacted["message"].startswith("<unparseable message redacted:")
        assert "sha256:" in redacted["message"]
        # Event identity survives for correlation; the original is untouched.
        assert redacted["id"] == "e2" and redacted["timestamp"] == 2
        assert ev["message"] == message


def test_handler_transport_error_dlqs_and_continues():
    """A Metronome ingest failure DLQs that record + emits
    meter.transport_error + continues the batch (does not raise / drop)."""
    import index

    first = _load("inline_record.json")
    second = _load("inline_record.json")
    second["requestId"] = "second-record-id"
    dlqd, metrics = [], []

    def fail_ingest(_parsed):
        raise RuntimeError("metronome 503")

    with (
        _stub_resolver(),
        patch.object(index, "_ingest_to_metronome", side_effect=fail_ingest),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(first, second), None)

    assert result == {"statusCode": 200}
    # BOTH records' transport failures were DLQ'd (batch continued past the first).
    assert len(dlqd) == 2
    assert all("transport_error" in r for _e, r in dlqd)
    assert metrics.count("meter.transport_error") == 2


def test_handler_unparseable_record_dlqs_and_emits_metric():
    import index

    bad_event = {
        "awslogs": {
            "data": base64.b64encode(
                gzip.compress(json.dumps({"logEvents": [{"message": "{not json"}]}).encode())
            ).decode()
        }
    }
    dlqd, metrics = [], []
    with (
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(bad_event, None)

    assert result == {"statusCode": 200}
    assert len(dlqd) == 1
    assert dlqd[0][1] == "unparseable_log_event"
    assert "meter.parse_failed" in metrics


def test_handler_malformed_non_dict_record_dlqs_and_continues():
    """Valid JSON that is not a dict (a poison record) must DLQ as
    malformed_record and never poison the rest of the batch: an unguarded
    raise would make CW Logs retry the WHOLE batch and then drop it."""
    import index

    good = _load("inline_record.json")
    ingested, dlqd, metrics = [], [], []

    payload = {"logEvents": [{"message": json.dumps("just a string")}, {"message": json.dumps(good)}]}
    event = {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}

    with (
        _stub_resolver(),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(event, None)

    assert result == {"statusCode": 200}
    assert len(dlqd) == 1
    assert dlqd[0][1] == "malformed_record: not a dict"
    assert "meter.malformed_record" in metrics
    # The batch continued: the good record was still ingested.
    assert len(ingested) == 1
    assert ingested[0]["request_id"] == good["requestId"]


def test_handler_missing_model_id_dlqs_and_never_ingests():
    """No modelId means no attribution key — hold on the DLQ, never ingest."""
    import index

    record = _load("inline_record.json")
    del record["modelId"]
    ingested, dlqd, metrics = [], [], []

    with (
        _stub_resolver(),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(record), None)

    assert result == {"statusCode": 200}
    assert ingested == []
    assert len(dlqd) == 1
    assert dlqd[0][1] == "malformed_record: missing modelId"
    assert "meter.malformed_record" in metrics


def test_handler_missing_request_id_dlqs_and_never_ingests():
    """requestId becomes the Metronome transaction_id — the ENTIRE replay/dedup
    safety story. An event without one must never be ingested (a redrive or
    batch retry would double-bill it)."""
    import index

    record = _load("inline_record.json")
    record["requestId"] = ""
    ingested, dlqd, metrics = [], [], []

    with (
        _stub_resolver(),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(record), None)

    assert result == {"statusCode": 200}
    assert ingested == []
    assert len(dlqd) == 1
    assert dlqd[0][1] == "malformed_record: missing requestId"
    assert "meter.malformed_record" in metrics


def test_handler_control_message_returns_200_without_dlq_or_metrics():
    """CloudWatch Logs sends a non-JSON health probe on filter creation; it
    must short-circuit cleanly instead of DLQing as parse_failed (which would
    page both envs' warn topics on every deploy)."""
    import index

    payload = {
        "messageType": "CONTROL_MESSAGE",
        "logEvents": [{"message": "CWL CONTROL MESSAGE: Checking health of destination Lambda."}],
    }
    event = {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}
    dlqd, metrics, ingested = [], [], []

    with (
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(event, None)

    assert result == {"statusCode": 200}
    assert dlqd == []
    assert metrics == []
    assert ingested == []


def test_handler_extract_exception_dlqs_as_parse_failed_and_continues():
    """Any unexpected exception out of _extract is a deterministic per-record
    failure: it must DLQ + continue, never raise (a raise would fail the whole
    batch, and CW Logs retries then silently drops it)."""
    import index

    first = _load("inline_record.json")
    second = _load("inline_record.json")
    second["requestId"] = "second-record-id"
    ingested, dlqd, metrics = [], [], []
    calls = {"n": 0}
    real_extract = index._extract

    def flaky_extract(record):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("unexpected shape")
        return real_extract(record)

    with (
        _stub_resolver(),
        patch.object(index, "_extract", side_effect=flaky_extract),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(first, second), None)

    assert result == {"statusCode": 200}
    assert len(dlqd) == 1
    assert dlqd[0][1].startswith("extract_failed:")
    assert "meter.parse_failed" in metrics
    # The batch continued: the second record was still ingested.
    assert len(ingested) == 1
    assert ingested[0]["request_id"] == "second-record-id"


def test_send_to_dlq_posts_message_to_queue():
    """The DLQ helper sends the raw log event + reason to the configured queue."""
    import index

    fake_sqs = MagicMock()
    log_event = {"message": "{}", "id": "abc"}
    with patch("index._sqs", return_value=fake_sqs):
        index._send_to_dlq(log_event, "transport_error: boom")

    fake_sqs.send_message.assert_called_once()
    kwargs = fake_sqs.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs.test.local/dlq"
    body = json.loads(kwargs["MessageBody"])
    assert body["reason"] == "transport_error: boom"
    assert body["log_event"] == log_event


def test_send_to_dlq_raises_when_no_queue_configured(monkeypatch):
    """With no DLQ_URL the helper re-raises so the batch fails (CW Logs retries)
    rather than silently losing a record."""
    import index

    monkeypatch.delenv("DLQ_URL", raising=False)
    with pytest.raises(RuntimeError):
        index._send_to_dlq({"message": "{}"}, "transport_error: boom")


# ---------------------------------------------------------------------------
# LOCAL PRICING. Third copy of the rate table (backend _RATES + the config.py
# catalog cost blocks are the other two) — the Lambda has no VPC and no backend
# import path, so it cannot share the module. The lockstep test is what keeps
# the three from drifting; without it the cap would silently enforce the wrong
# dollar amount.
# ---------------------------------------------------------------------------


def _backend_pricing():
    """Import the backend's pricing module (the source of truth for the rates).

    Deliberately a HARD import, not an importorskip: a skipped lockstep test is
    a drift detector that never fires.
    """
    os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    from core.billing import bedrock_aip, bedrock_pricing

    return bedrock_pricing, bedrock_aip


def test_lambda_rate_table_matches_backend_bedrock_pricing():
    """The Lambda's rate table and tier map must equal the backend's, exactly."""
    import index

    bedrock_pricing, bedrock_aip = _backend_pricing()

    assert index._RATES == bedrock_pricing._RATES
    assert index.TIER_TO_FM == bedrock_aip.TIER_TO_FM


def test_lambda_pricing_matches_backend_price_from_tier():
    """Same table AND same arithmetic (per-component int() truncation) — a
    matching table with a different rounding rule would still drift the cap."""
    import index

    bedrock_pricing, _ = _backend_pricing()
    record = _load("inline_record.json")
    counts = {
        "input_tokens": record["input"]["inputTokenCount"],
        "output_tokens": record["output"]["outputTokenCount"],
        "cache_read_tokens": record["input"]["cacheReadInputTokenCount"],
        "cache_write_tokens": record["input"]["cacheWriteInputTokenCount"],
    }
    for tier in index.TIER_TO_FM:
        assert index._price_from_tier(tier, **counts) == bedrock_pricing.price_from_tier(tier, **counts)


def test_cap_microcents_matches_backend_trial_cap():
    """The meter's async-backstop threshold must equal the gate's primary
    reservation cap (core.services.trial_cap.CAP_MICROCENTS) — a silent drift
    here means the backstop kill fires at the wrong dollar amount."""
    import index

    os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    from core.services import trial_cap

    assert index.CAP_MICROCENTS == trial_cap.CAP_MICROCENTS


def _gate_stack_env_int(name):
    """Regex a `NAME: "<digits>"` CDK environment literal out of
    bedrock-gate-stack.ts. Raises if the name doesn't appear exactly once —
    a silent 0-match would make this test vacuously pass."""
    with open(GATE_STACK_TS) as f:
        src = f.read()
    matches = re.findall(rf'{re.escape(name)}:\s*"(\d+)"', src)
    assert len(matches) == 1, f"expected exactly one {name} literal in {GATE_STACK_TS}, found {matches}"
    return int(matches[0])


def test_gate_stack_cap_and_reserve_env_literals_match_backend():
    """CAP_MICROCENTS and RESERVE_ESTIMATE_MICROCENTS are hardcoded CDK env
    string literals (the Lambda has no import path to the backend) — this is
    the only thing keeping them from drifting off core.services.trial_cap."""
    os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
    if BACKEND not in sys.path:
        sys.path.insert(0, BACKEND)
    from core.services import trial_cap

    assert _gate_stack_env_int("CAP_MICROCENTS") == trial_cap.CAP_MICROCENTS
    assert _gate_stack_env_int("RESERVE_ESTIMATE_MICROCENTS") == trial_cap.RESERVE_ESTIMATE_MICROCENTS


def test_price_includes_cache_read_and_cache_write():
    """All four token counts are billed. Cache tokens are ~92% of this record's
    price — a meter that dropped them would undercount ~12x."""
    import index

    record = _load("inline_record.json")
    counts = {
        "input_tokens": record["input"]["inputTokenCount"],
        "output_tokens": record["output"]["outputTokenCount"],
        "cache_read_tokens": record["input"]["cacheReadInputTokenCount"],
        "cache_write_tokens": record["input"]["cacheWriteInputTokenCount"],
    }
    assert all(v > 0 for v in counts.values()), "fixture must keep all four counts non-zero"

    assert index._price_from_tier("sonnet", **counts) == SONNET_PRICE_MICROCENTS
    assert index._price_from_tier("sonnet", **dict(counts, cache_read_tokens=0, cache_write_tokens=0)) == (
        SONNET_PRICE_NO_CACHE
    )


def test_unknown_tier_is_unpriceable():
    import index

    with pytest.raises(index.UnknownTierError):
        index._price_from_tier("gpt-9", input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0)


# ---------------------------------------------------------------------------
# TRIAL COUNTER. Against a REAL DynamoDB engine (moto): the counter is a
# nested-map ADD, which DynamoDB rejects if combined with the map init in one
# expression — a hand mock would pass code that fails in production.
# ---------------------------------------------------------------------------


def test_trial_record_adds_priced_spend_to_todays_key_and_never_ingests():
    import index

    record = _load("inline_record.json")
    ingested = []

    with (
        _aws_world() as (ddb, iam, ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
    ):
        result = index.handler(_awslogs_event(record), None)
        # Read back INSIDE the moto context — outside it, boto3 talks to real AWS.
        spend = _daily_spend(ddb)

    assert result == {"statusCode": 200}
    # Trial owners have no Stripe customer and therefore no Metronome contract.
    assert ingested == []
    assert spend == {_utc_today(): SONNET_PRICE_MICROCENTS}
    # Far under the cap: nothing is killed.
    iam.delete_role_policy.assert_not_called()
    ecs.update_service.assert_not_called()


def test_trial_counter_accumulates_across_records():
    """The guarded two-step must ADD (not overwrite) on an existing day key."""
    import index

    first = _load("inline_record.json")
    second = _load("inline_record.json")
    second["requestId"] = "second-record-id"

    with _aws_world() as (ddb, _iam, _ecs), _stub_resolver(tier="sonnet", plan="trial"):
        index.handler(_awslogs_event(first, second), None)
        spend = _daily_spend(ddb)

    assert spend == {_utc_today(): 2 * SONNET_PRICE_MICROCENTS}


def test_trial_counter_initializes_map_on_a_row_that_has_none():
    """A fresh billing row has no trial_daily_spend_microcents map at all. The
    conditional map-init step is what makes the nested-path SET legal."""
    import index

    with _aws_world(starting_spend=None) as (ddb, _iam, _ecs):
        assert _daily_spend(ddb) == {}
        total = index._add_daily_spend("org_abc", _utc_today(), 500)
        assert _daily_spend(ddb) == {_utc_today(): 500}

    assert total == 500


def test_trial_counter_raises_on_a_missing_billing_row():
    """No phantom rows: a trial owner with no billing row is a provisioning gap
    that must alarm, not a row the meter invents."""
    import index

    with _aws_world(billing_row=False), pytest.raises(ClientError):
        index._add_daily_spend("org_abc", _utc_today(), 500)


def test_missing_billing_row_dlqs_as_counter_error_and_continues():
    import index

    record = _load("inline_record.json")
    dlqd, metrics = [], []

    with (
        _aws_world(billing_row=False),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(record), None)

    assert result == {"statusCode": 200}
    assert len(dlqd) == 1
    assert dlqd[0][1].startswith("counter_error:")
    assert "meter.counter_error" in metrics


def test_unpriceable_tier_dlqs_and_never_counts():
    import index

    record = _load("inline_record.json")
    dlqd, metrics = [], []

    with (
        _aws_world() as (ddb, _iam, _ecs),
        _stub_resolver(tier="mystery", plan="trial"),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)

    assert spend == {}
    assert dlqd[0][1].startswith("unpriceable_tier:")
    assert "meter.unknown_model" in metrics


# ---------------------------------------------------------------------------
# REPLAY SAFETY. CloudWatch Logs retries whole batches and DLQ redrives replay
# them; a DynamoDB ADD has no dedup. The per-requestId conditional put guards
# the COUNTER — and ONLY the counter: the Metronome ingest keeps its own 34-day
# dedup and must still run on a replay.
# ---------------------------------------------------------------------------


def test_same_request_id_delivered_twice_counts_once():
    import index

    record = _load("inline_record.json")
    metrics = []

    with (
        _aws_world() as (ddb, _iam, _ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index.handler(_awslogs_event(record), None)
        index.handler(_awslogs_event(record), None)  # the CW-Logs batch retry
        spend = _daily_spend(ddb)

    assert spend == {_utc_today(): SONNET_PRICE_MICROCENTS}
    assert metrics.count("meter.replay_skipped") == 1
    # A replay is not a failure: it must not DLQ or trip a failure alarm.
    assert not any(m.endswith("_error") for m in metrics)


def test_replay_of_a_paid_record_still_ingests_both_times():
    """The dedup guard protects the counter, NOT the ingest. Metronome dedups on
    transaction_id (34 days) and that is the paid path's correctness story —
    suppressing a replayed ingest would regress it."""
    import index

    record = _load("inline_record.json")
    ingested = []

    with (
        _aws_world(),
        _stub_resolver(plan="paid"),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
    ):
        index.handler(_awslogs_event(record), None)
        index.handler(_awslogs_event(record), None)

    assert len(ingested) == 2
    assert {p["request_id"] for p in ingested} == {record["requestId"]}


def test_claim_request_is_first_write_wins():
    import index

    with _aws_world() as (ddb, _iam, _ecs):
        assert index._claim_request("req-1") is True
        assert index._claim_request("req-1") is False
        assert index._claim_request("req-2") is True

        row = ddb.get_item(TableName=DEDUP_TABLE, Key={"request_id": {"S": "req-1"}})["Item"]
        # 48h TTL — past CloudWatch's retry + redrive horizon.
        assert int(row["ttl"]["N"]) - int(time.time()) > 47 * 3600
        # Claims are attributable: the meter stamps source="meter" so a
        # gateway-vs-meter reconciliation can tell who counted a record.
        assert row["source"]["S"] == "meter"


# ---------------------------------------------------------------------------
# THE PAID PATH IS BYTE-IDENTICAL. A paid record must POST to Metronome exactly
# as before and touch NOTHING else — no dedup row, no counter, no kill, ever.
# ---------------------------------------------------------------------------


def test_paid_record_ingests_and_touches_no_counter():
    import index

    record = _load("inline_record.json")
    ingested = []

    with (
        _aws_world() as (ddb, iam, ecs),
        _stub_resolver(plan="paid"),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
    ):
        index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)
        dedup_rows = ddb.scan(TableName=DEDUP_TABLE)["Count"]

    assert len(ingested) == 1
    assert spend == {}
    assert dedup_rows == 0
    iam.delete_role_policy.assert_not_called()
    ecs.update_service.assert_not_called()


def test_paid_owner_crossing_three_dollars_is_untouched():
    """The cap is a TRIAL construct. A paid owner is billed through Metronome
    and must never be revoked, stopped, or counted — no matter the spend."""
    import index

    record = _load("inline_record.json")
    record["input"]["inputTokenCount"] = 100_000_000  # ~$330 in one record
    ingested = []

    with (
        _aws_world(starting_spend=2_999_999) as (ddb, iam, ecs),
        _stub_resolver(plan="paid"),
        patch.object(index, "_ingest_to_metronome", side_effect=lambda p: ingested.append(p)),
    ):
        index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)
        dedup_rows = ddb.scan(TableName=DEDUP_TABLE)["Count"]

    assert len(ingested) == 1
    # The pre-existing counter value is untouched (the meter did not ADD to it).
    assert spend == {_utc_today(): 2_999_999}
    assert dedup_rows == 0
    iam.delete_role_policy.assert_not_called()
    ecs.update_service.assert_not_called()


# ---------------------------------------------------------------------------
# THE KILL. The container holds its own AWS credentials and calls Bedrock
# directly, so revoking the per-owner IAM policy + stopping the ECS service is
# the ONLY thing that provably stops a free owner's spend.
# ---------------------------------------------------------------------------


def test_crossing_the_cap_revokes_iam_and_stops_ecs_exactly_once():
    import index

    record = _load("inline_record.json")  # 121,275 microcents on sonnet
    under = index.CAP_MICROCENTS - 1_000  # one record takes us over

    with (
        _aws_world(starting_spend=under) as (ddb, iam, ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
    ):
        index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)
        container = ddb.get_item(TableName=CONTAINERS_TABLE, Key={"owner_id": {"S": "org_abc"}})["Item"]

    assert spend == {_utc_today(): under + SONNET_PRICE_MICROCENTS}

    iam.delete_role_policy.assert_called_once_with(
        RoleName="lightbulb-dev-owner-bedrock-org_abc",
        PolicyName="owner-bedrock",
    )
    ecs.update_service.assert_called_once_with(
        cluster=CLUSTER_ARN,
        service=index._service_name("org_abc"),
        desiredCount=0,
    )
    # The stamp is the whole downstream contract, not just the flag:
    # bedrock_revoked drives the daily restore; status="stopped" is what the
    # status poll's retryable branch keys the 402 limit card on AND what the
    # rollover auto-restart fires on. A row left "running" shows an indefinite
    # cold-start stepper instead of the limit card and never self-heals.
    assert container["bedrock_revoked"]["BOOL"] is True
    assert container["status"]["S"] == "stopped"
    assert container["substatus"]["S"] == "trial_daily_cap"
    assert container["bedrock_revoked_at"]["S"]
    assert container["updated_at"]["S"]


def test_staying_under_the_cap_kills_nothing():
    import index

    record = _load("inline_record.json")

    with (
        _aws_world(starting_spend=1_000) as (ddb, iam, ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
    ):
        index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)
        container = ddb.get_item(TableName=CONTAINERS_TABLE, Key={"owner_id": {"S": "org_abc"}})["Item"]

    assert spend == {_utc_today(): 1_000 + SONNET_PRICE_MICROCENTS}
    iam.delete_role_policy.assert_not_called()
    ecs.update_service.assert_not_called()
    assert "bedrock_revoked" not in container


def test_every_over_cap_record_re_asserts_the_kill():
    """The kill is RE-FIRED on every over-cap record, not just the crossing one.

    This looks redundant and is the only retry the fence has.
    ``_kill_owner_bedrock`` never raises — it swallows each step's failure so one
    transient IAM error cannot fail a batch of already-counted records — so if
    the kill were fired only on the crossing, a blip there would never be
    retried: every later record also satisfies ``total - microcents >= CAP``, the
    crossing test is false forever, and the owner keeps invoking Bedrock. Each
    step is idempotent, so re-asserting costs nothing."""
    import index

    crossing = _load("inline_record.json")
    trailing = _load("inline_record.json")
    trailing["requestId"] = "trailing-record-id"

    with (
        _aws_world(starting_spend=index.CAP_MICROCENTS - 1_000) as (_ddb, iam, ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
    ):
        index.handler(_awslogs_event(crossing, trailing), None)

    assert iam.delete_role_policy.call_count == 2
    assert ecs.update_service.call_count == 2


def test_a_transient_kill_failure_on_the_crossing_is_retried_by_the_next_record():
    """THE regression this retry exists for. IAM blips on the crossing record —
    the failure is swallowed (by design: it must not fail already-counted
    records), so the owner is still able to invoke Bedrock. The next over-cap
    record must land the kill. Under a crossing-only kill this owner would spend
    unbounded until a human read the alarm."""
    import index

    crossing = _load("inline_record.json")
    trailing = _load("inline_record.json")
    trailing["requestId"] = "trailing-record-id"

    with (
        _aws_world(starting_spend=index.CAP_MICROCENTS - 1_000) as (_ddb, iam, ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
    ):
        # Transient failure on the crossing record only; the retry succeeds.
        iam.delete_role_policy.side_effect = [
            ClientError({"Error": {"Code": "Throttling", "Message": "rate exceeded"}}, "DeleteRolePolicy"),
            None,
        ]
        index.handler(_awslogs_event(crossing, trailing), None)

    # The revoke was attempted twice and SUCCEEDED on the retry — the owner is
    # actually cut off, not merely alarmed about.
    assert iam.delete_role_policy.call_count == 2
    assert ecs.update_service.call_count == 2


def test_kill_is_independent_iam_failure_still_stops_ecs():
    """A revoke failure must NOT skip the ECS stop — coupling them would let one
    transient IAM error leave a runaway container invoking Bedrock."""
    import index

    stopped, stamped, denied, metrics = [], [], [], []

    with (
        patch.object(index, "_revoke_owner_iam", side_effect=RuntimeError("iam 503")),
        patch.object(index, "_stop_owner_service", side_effect=lambda o: stopped.append(o)),
        patch.object(index, "_deny_gate_token", side_effect=lambda o: denied.append(o)),
        patch.object(index, "_stamp_bedrock_revoked", side_effect=lambda o: stamped.append(o)),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index._kill_owner_bedrock("org_abc")  # must not raise

    assert stopped == ["org_abc"]
    assert denied == ["org_abc"]
    assert stamped == ["org_abc"]
    assert "meter.kill_iam_error" in metrics


def test_kill_is_independent_ecs_failure_still_revokes_and_stamps():
    import index

    revoked, stamped, denied, metrics = [], [], [], []

    with (
        patch.object(index, "_revoke_owner_iam", side_effect=lambda o: revoked.append(o)),
        patch.object(index, "_stop_owner_service", side_effect=RuntimeError("ecs 503")),
        patch.object(index, "_deny_gate_token", side_effect=lambda o: denied.append(o)),
        patch.object(index, "_stamp_bedrock_revoked", side_effect=lambda o: stamped.append(o)),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index._kill_owner_bedrock("org_abc")

    assert revoked == ["org_abc"]
    assert denied == ["org_abc"]
    assert stamped == ["org_abc"]
    assert "meter.kill_ecs_error" in metrics


def test_kill_is_independent_deny_token_failure_still_revokes_stops_stamps():
    """A deny-flag failure must NOT skip the IAM/ECS/stamp steps — the deny step
    is a THIRD independent kill action, not a precondition for the others."""
    import index

    revoked, stopped, stamped, metrics = [], [], [], []

    with (
        patch.object(index, "_revoke_owner_iam", side_effect=lambda o: revoked.append(o)),
        patch.object(index, "_stop_owner_service", side_effect=lambda o: stopped.append(o)),
        patch.object(index, "_deny_gate_token", side_effect=RuntimeError("ddb 503")),
        patch.object(index, "_stamp_bedrock_revoked", side_effect=lambda o: stamped.append(o)),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index._kill_owner_bedrock("org_abc")

    assert revoked == ["org_abc"]
    assert stopped == ["org_abc"]
    assert stamped == ["org_abc"]
    assert "meter.kill_deny_token_error" in metrics


def test_kill_failure_never_fails_the_batch():
    """Every kill step failing must still leave the record counted and the batch
    green — the failure pages via its metric instead."""
    import index

    record = _load("inline_record.json")
    dlqd, metrics = [], []

    with (
        _aws_world(starting_spend=index.CAP_MICROCENTS - 1_000) as (ddb, iam, ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        iam.delete_role_policy.side_effect = RuntimeError("iam down")
        ecs.update_service.side_effect = RuntimeError("ecs down")
        result = index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)

    assert result == {"statusCode": 200}
    assert dlqd == []
    assert spend == {_utc_today(): index.CAP_MICROCENTS - 1_000 + SONNET_PRICE_MICROCENTS}
    assert "meter.kill_iam_error" in metrics
    assert "meter.kill_ecs_error" in metrics


def test_revoke_is_idempotent_a_missing_policy_is_already_revoked():
    import index

    iam = MagicMock()
    iam.delete_role_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntity", "Message": "policy not found"}}, "DeleteRolePolicy"
    )
    with patch.object(index, "_iam", return_value=iam):
        index._revoke_owner_iam("org_abc")  # must not raise


def test_revoke_surfaces_a_real_iam_error():
    import index

    iam = MagicMock()
    iam.delete_role_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "DeleteRolePolicy"
    )
    with patch.object(index, "_iam", return_value=iam), pytest.raises(ClientError):
        index._revoke_owner_iam("org_abc")


@pytest.mark.parametrize("code", ["ServiceNotFoundException", "ServiceNotActiveException"])
def test_stop_is_idempotent_a_stopped_service_is_already_stopped(code):
    import index

    ecs = MagicMock()
    ecs.list_clusters.return_value = {"clusterArns": [CLUSTER_ARN]}
    ecs.update_service.side_effect = ClientError({"Error": {"Code": code, "Message": "gone"}}, "UpdateService")
    with patch.object(index, "_ecs", return_value=ecs):
        index._stop_owner_service("org_abc")  # must not raise


def test_stop_surfaces_a_real_ecs_error():
    import index

    ecs = MagicMock()
    ecs.list_clusters.return_value = {"clusterArns": [CLUSTER_ARN]}
    ecs.update_service.side_effect = ClientError(
        {"Error": {"Code": "InvalidParameterException", "Message": "nope"}}, "UpdateService"
    )
    with patch.object(index, "_ecs", return_value=ecs), pytest.raises(ClientError):
        index._stop_owner_service("org_abc")


def test_cluster_is_resolved_by_prefix_and_cached():
    """The cluster's physical name is CFN-generated (renaming it would REPLACE
    it and destroy every per-user service), and this account-scoped stack cannot
    hold a cross-stage ref to the dev cluster — so it is resolved at runtime by
    prefix, once per container."""
    import index

    ecs = MagicMock()
    ecs.list_clusters.return_value = {
        "clusterArns": [
            "arn:aws:ecs:us-east-1:877352799272:cluster/goosetown-dev-shared",
            CLUSTER_ARN,
            "arn:aws:ecs:us-east-1:877352799272:cluster/isol8-prod-container-ClusterEB0386A7-Uc0EwgAC9DcQ",
        ]
    }
    with patch.object(index, "_ecs", return_value=ecs):
        assert index._cluster_arn() == CLUSTER_ARN
        assert index._cluster_arn() == CLUSTER_ARN

    assert ecs.list_clusters.call_count == 1


def test_service_name_matches_the_backend_derivation():
    """Mirrors ecs_manager._service_name — the backend's own stop path derives
    the name this way, so a mismatch here would UpdateService a service that
    does not exist and the kill's instant half would silently no-op."""
    import hashlib

    import index

    owner = "org_2abcDEF"
    expected = f"openclaw-{owner}-{hashlib.sha256(owner.encode()).hexdigest()[:12]}"
    assert index._service_name(owner) == expected


# ---------------------------------------------------------------------------
# GATEWAY AUDITOR. Once an owner is cut over to the bedrock-gate, the gateway
# claims + debits each invocation synchronously; the meter is the AUDITOR that
# backstops anything the gateway missed (crash / client-abort / lost usage
# event), deduped on the shared per-requestId claim. A trial record the meter
# CLAIMS (gateway did NOT claim first) that was gateway-SIGNED is a reconciled
# gateway miss — made visible via meter.reconciled_gateway_miss. A direct
# owner-role record is a non-gatewayed owner the meter always counts — no miss.
# ---------------------------------------------------------------------------


def test_gateway_signed_trial_record_counts_and_emits_reconciled_gateway_miss():
    """A gateway-signed record reaching the meter's claim means the gateway
    missed its own synchronous debit — the meter backstops the count AND emits
    meter.reconciled_gateway_miss so the miss is visible to the burn-in gate."""
    import index

    record = _load("inline_record.json")
    record["identity"]["arn"] = GATEWAY_IDENTITY_ARN
    metrics = []

    with (
        _aws_world() as (ddb, _iam, _ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)

    assert result == {"statusCode": 200}
    # The meter still backstopped the count ...
    assert spend == {_utc_today(): SONNET_PRICE_MICROCENTS}
    # ... AND surfaced the gateway miss exactly once.
    assert metrics.count("meter.reconciled_gateway_miss") == 1


def test_gateway_signed_record_already_claimed_by_gateway_skips_without_reconcile():
    """If the gateway already CLAIMED the record (source=gateway), the meter's
    conditional claim FAILS → it skips: the gateway debited it, so there is no
    miss to backstop and NO reconcile metric. Only the normal replay-skip fires,
    and the counter is never double-applied."""
    import index

    record = _load("inline_record.json")
    record["identity"]["arn"] = GATEWAY_IDENTITY_ARN
    metrics = []

    with (
        _aws_world() as (ddb, _iam, _ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        # The gateway got there first: a claim for this requestId already exists,
        # stamped with its own source.
        ddb.put_item(
            TableName=DEDUP_TABLE,
            Item={
                "request_id": {"S": record["requestId"]},
                "source": {"S": "gateway"},
                "ttl": {"N": str(int(time.time()) + 15 * 24 * 3600)},
            },
        )
        result = index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)

    assert result == {"statusCode": 200}
    # No backstop count — the gateway already debited it.
    assert spend == {}
    # A gateway that DID claim is not a miss: no reconcile metric.
    assert "meter.reconciled_gateway_miss" not in metrics
    assert metrics.count("meter.replay_skipped") == 1


def test_owner_role_trial_record_counts_without_reconcile_metric():
    """The default fixture identity is the per-owner role (lightbulb-dev-owner-bedrock-*)
    — a non-gatewayed owner the meter legitimately counts. It counts, and it
    must NOT emit the reconcile metric (reserved for gateway misses)."""
    import index

    record = _load("inline_record.json")
    # Guard: the fixture is genuinely an owner-role identity, not the gateway.
    assert "lightbulb-dev-owner-bedrock" in record["identity"]["arn"]
    assert "lightbulb-dev-bedrock-gate" not in record["identity"]["arn"]
    metrics = []

    with (
        _aws_world() as (ddb, _iam, _ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index.handler(_awslogs_event(record), None)
        spend = _daily_spend(ddb)

    assert spend == {_utc_today(): SONNET_PRICE_MICROCENTS}
    assert "meter.reconciled_gateway_miss" not in metrics


def test_kill_denies_the_gate_token_row():
    """The deny-flag kill step flips denied=true on the owner's gate-token row
    (the gateway's kill switch), keyed by the containers row's rotation pointer
    (bedrock_gate_token_hash)."""
    import index

    record = _load("inline_record.json")
    token_hash = "hash-of-owners-bearer-token"

    with (
        _aws_world(starting_spend=index.CAP_MICROCENTS - 1_000, gate_token_hash=token_hash) as (ddb, _iam, _ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
    ):
        index.handler(_awslogs_event(record), None)
        token_row = ddb.get_item(TableName=GATE_TOKENS_TABLE, Key={"token_hash": {"S": token_hash}})["Item"]

    assert token_row["denied"]["BOOL"] is True


def test_kill_without_gate_token_hash_is_a_noop_and_other_steps_still_run():
    """An owner never cut over to the gateway has no bedrock_gate_token_hash on
    the containers row — the deny step is a clean no-op (NOT an error), and the
    IAM revoke / ECS stop / revoked stamp still complete."""
    import index

    record = _load("inline_record.json")
    metrics = []

    with (
        _aws_world(starting_spend=index.CAP_MICROCENTS - 1_000) as (ddb, iam, ecs),
        _stub_resolver(tier="sonnet", plan="trial"),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        index.handler(_awslogs_event(record), None)
        container = ddb.get_item(TableName=CONTAINERS_TABLE, Key={"owner_id": {"S": "org_abc"}})["Item"]
        gate_rows = ddb.scan(TableName=GATE_TOKENS_TABLE)["Count"]

    # The other kill steps still ran.
    iam.delete_role_policy.assert_called_once()
    ecs.update_service.assert_called_once()
    assert container["bedrock_revoked"]["BOOL"] is True
    # The no-op deny neither errored nor wrote a phantom gate-token row.
    assert "meter.kill_deny_token_error" not in metrics
    assert gate_rows == 0


def test_deny_gate_token_also_denies_the_superseded_rows():
    """The backstop kill must reach the token the container is ACTUALLY using.

    Backend-side, a gate-token row now survives until neither the owner's ECS
    service task definition nor any unstopped task can present it — so when a
    rotation's redeploy never lands, the container keeps invoking with the
    SUPERSEDED token while the rotation pointer names the undelivered new one.
    Denying only the pointer would leave a capped trial owner invoking Bedrock
    past the cap indefinitely, which is the runaway this kill exists to stop."""
    import index

    pointer = "hash-of-the-undelivered-new-token"
    live = "hash-the-container-is-actually-presenting"
    ghost = "hash-whose-row-already-expired"

    with _aws_world(gate_token_hash=pointer) as (ddb, _iam, _ecs):
        ddb.update_item(
            TableName=CONTAINERS_TABLE,
            Key={"owner_id": {"S": "org_abc"}},
            UpdateExpression="SET bedrock_gate_superseded_hashes = :h",
            ExpressionAttributeValues={":h": {"L": [{"S": live}, {"S": ghost}]}},
        )
        ddb.put_item(
            TableName=GATE_TOKENS_TABLE,
            Item={"token_hash": {"S": live}, "owner_id": {"S": "org_abc"}, "denied": {"BOOL": False}},
        )

        index._deny_gate_token("org_abc")

        rows = {r["token_hash"]["S"]: r["denied"]["BOOL"] for r in ddb.scan(TableName=GATE_TOKENS_TABLE)["Items"]}

    assert rows == {pointer: True, live: True}
    # A tracked hash whose row already expired is not resurrected as a partial row.
    assert ghost not in rows


def test_deny_gate_token_boto_core_error_on_first_hash_still_tries_the_rest():
    """A non-ClientError boto failure (e.g. a network timeout) on the FIRST
    hash must not abort the loop before the superseded hash is attempted --
    that's the hash the container is actually presenting in the incident
    shape this whole branch exists for. Regression: an `except ClientError`
    alone lets a BotoCoreError (ReadTimeout, EndpointConnectionError -- never
    a ClientError) on the pointer skip every hash after it."""
    import index

    pointer = "hash-of-the-undelivered-new-token"
    live = "hash-the-container-is-actually-presenting"

    with _aws_world(gate_token_hash=pointer) as (ddb, _iam, _ecs):
        ddb.update_item(
            TableName=CONTAINERS_TABLE,
            Key={"owner_id": {"S": "org_abc"}},
            UpdateExpression="SET bedrock_gate_superseded_hashes = :h",
            ExpressionAttributeValues={":h": {"L": [{"S": live}]}},
        )
        ddb.put_item(
            TableName=GATE_TOKENS_TABLE,
            Item={"token_hash": {"S": live}, "owner_id": {"S": "org_abc"}, "denied": {"BOOL": False}},
        )

        class _FlakyDDB:
            """Forwards everything to the real (moto) client except
            update_item on the pointer hash, which raises a non-ClientError
            boto failure -- e.g. a network timeout mid-call."""

            def __getattr__(self, name):
                return getattr(ddb, name)

            def update_item(self, **kwargs):
                if kwargs["Key"]["token_hash"]["S"] == pointer:
                    raise EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com/")
                return ddb.update_item(**kwargs)

        with patch.object(index, "_ddb", return_value=_FlakyDDB()):
            with pytest.raises(EndpointConnectionError):
                index._deny_gate_token("org_abc")

        live_row = ddb.get_item(TableName=GATE_TOKENS_TABLE, Key={"token_hash": {"S": live}})["Item"]

    # The pointer's failure was re-raised, but only after the superseded hash
    # -- the one actually presentable in this incident -- was still denied.
    assert live_row["denied"]["BOOL"] is True


def test_deny_gate_token_noop_when_containers_row_missing():
    """No containers row at all (a provisioning gap) → the deny step reads no
    hash and no-ops, never raising and never writing a gate-token row."""
    import index

    with _aws_world(container_row=False) as (ddb, _iam, _ecs):
        index._deny_gate_token("org_abc")  # must not raise
        gate_rows = ddb.scan(TableName=GATE_TOKENS_TABLE)["Count"]

    assert gate_rows == 0
