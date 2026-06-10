"""Tests for the Bedrock usage-meter Lambda — extraction, attribution, ingest.

Direct-Metronome design: the Lambda parses each CloudWatch invocation-log
record, resolves (owner_id, tier) with one GetItem on the per-env aip-owners
reverse table, and POSTs a single-event JSON array to Metronome /v1/ingest.
ALL billable counts are read from the TOP LEVEL of the record
(`input.inputTokenCount` / `output.outputTokenCount` /
`input.cacheReadInputTokenCount` / `input.cacheWriteInputTokenCount`); the
nested `output.outputBodyJson.usage` block is a defensive fallback only.

Failure posture under test: every per-record failure (unparseable record,
malformed record shape, unattributed owner, missing tier, Metronome transport
error) DLQs the raw log event + emits a metric + continues the batch — never
a silent drop, and an unattributed or transaction-id-less record is NEVER
ingested.
"""

import base64
import gzip
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

OWNER_ITEM = {
    "Item": {
        "aip_arn": {"S": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus-org_abc"},
        "owner_id": {"S": "org_abc"},
        "tier": {"S": "opus"},
    }
}


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def _awslogs_event(*records):
    payload = {"logEvents": [{"message": json.dumps(r)} for r in records]}
    raw = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(raw).decode()}}


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    monkeypatch.setenv("METRONOME_API_KEY", "metronome-token-abc")
    monkeypatch.setenv("AIP_OWNERS_TABLE", "isol8-dev-bedrock-aip-owners")
    monkeypatch.setenv("DLQ_URL", "https://sqs.test.local/dlq")
    monkeypatch.setenv("METER_ENV", "dev")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    # Re-import fresh per test so the module-global cold-start caches (token,
    # boto3 clients) never leak state between tests.
    sys.modules.pop("index", None)
    import index  # noqa: F401

    yield
    sys.modules.pop("index", None)


def _stub_resolver(owner="org_abc", tier="opus"):
    return patch("index._resolve_owner_and_tier", return_value=(owner, tier))


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
    import index

    record = _load("inline_record.json")
    fake_ddb = MagicMock()
    fake_ddb.get_item.return_value = OWNER_ITEM

    with patch("index._ddb", return_value=fake_ddb):
        owner, tier = index._resolve_owner_and_tier(record)

    assert (owner, tier) == ("org_abc", "opus")
    fake_ddb.get_item.assert_called_once_with(
        TableName="isol8-dev-bedrock-aip-owners",
        Key={"aip_arn": {"S": record["modelId"]}},
    )


def test_resolve_owner_miss_returns_none_pair():
    import index

    fake_ddb = MagicMock()
    fake_ddb.get_item.return_value = {}
    with patch("index._ddb", return_value=fake_ddb):
        assert index._resolve_owner_and_tier(_load("inline_record.json")) == (None, None)


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
