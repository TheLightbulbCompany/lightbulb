"""Tests for the Bedrock usage-meter Lambda — parse + cache extraction + transport.

CloudWatch-only: the meter never touches S3. Token counts are top-level inline
(`input.inputTokenCount` / `output.outputTokenCount`); cache counts live in the
inline output body (`output.outputBodyJson.usage.{cacheReadInputTokens,
cacheWriteInputTokens}`). The huge thing on cache-heavy turns is the cached
INPUT prompt, which we never need, so the OUTPUT body is <100KB and inline in
practice. The one rare edge — a >100KB completion offloaded to S3 so the output
body (and its usage block) is absent — is handled by failing safe to the DLQ
rather than POSTing a zeroed-cache (~30×-under-billed) record.

NOTE: confirm the exact field paths against ONE real cached-Converse
invocation-log record (FIELDS.md's pre-flip gate) before METER_IS_LIVE is
flipped to true.
"""

import base64
import gzip
import importlib  # noqa: F401  (kept to mirror the sibling ws-* lambda test shape)
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def _awslogs_event(*records):
    payload = {"logEvents": [{"message": json.dumps(r)} for r in records]}
    raw = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(raw).decode()}}


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    monkeypatch.setenv("ALB_DNS_NAME", "alb.test.local")
    monkeypatch.setenv("METER_TOKEN", "meter-token-abc")
    monkeypatch.setenv("DLQ_URL", "https://sqs.test.local/dlq")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    sys.modules.pop("index", None)
    import index  # noqa: F401

    yield
    sys.modules.pop("index", None)


def test_extract_reads_top_level_tokens():
    import index

    record = _load("inline_record.json")
    parsed = index._extract(record)
    assert parsed["request_id"] == record["requestId"]
    assert parsed["model_id"] == record["modelId"]
    # Token counts come from the top-level inline counts.
    assert parsed["input_tokens"] == record["input"]["inputTokenCount"]
    assert parsed["output_tokens"] == record["output"]["outputTokenCount"]


def test_extract_reads_cache_tokens_from_output_body_usage():
    """The cache counts come from output.outputBodyJson.usage — NOT from the
    top-level input block. Read the expected values straight out of the fixture
    so the assertion fails if the path silently yields 0 (the #1 correctness
    landmine this meter exists to prevent — a `>= 0` assertion would pass even
    on a wrong path)."""
    import index

    record = _load("inline_record.json")
    usage = record["output"]["outputBodyJson"]["usage"]
    expected_read = int(usage["cacheReadInputTokens"])
    expected_write = int(usage["cacheWriteInputTokens"])
    assert expected_read > 0, (
        "inline_record.json fixture must retain a non-zero cacheRead count — "
        "a zero fixture makes this test vacuous (it is the ~30× undercount the "
        "meter exists to fix)."
    )

    parsed = index._extract(record)
    assert parsed["cache_read_tokens"] == expected_read
    assert parsed["cache_write_tokens"] == expected_write


def test_extract_falls_back_to_usage_tokens_when_top_level_absent():
    """If a delivery omits the top-level token counts, fall back to the output
    body's usage block (documented resilience path)."""
    import index

    record = _load("inline_record.json")
    del record["input"]["inputTokenCount"]
    del record["output"]["outputTokenCount"]
    usage = record["output"]["outputBodyJson"]["usage"]

    parsed = index._extract(record)
    assert parsed["input_tokens"] == int(usage["inputTokens"])
    assert parsed["output_tokens"] == int(usage["outputTokens"])


def test_extract_parses_output_body_when_serialized_as_string():
    """Some deliveries may serialize outputBodyJson as a JSON string; the
    extractor parses it once so the usage block is still reachable."""
    import index

    record = _load("inline_record.json")
    usage = record["output"]["outputBodyJson"]["usage"]
    record["output"]["outputBodyJson"] = json.dumps(record["output"]["outputBodyJson"])

    parsed = index._extract(record)
    assert parsed["cache_read_tokens"] == int(usage["cacheReadInputTokens"])
    assert parsed["cache_write_tokens"] == int(usage["cacheWriteInputTokens"])


def test_handler_posts_each_record(monkeypatch):
    import index

    inline = _load("inline_record.json")
    posted = []

    def fake_post(parsed):
        posted.append(parsed)

    with patch.object(index, "_post_to_backend", side_effect=fake_post):
        result = index.handler(_awslogs_event(inline), None)
    assert result == {"statusCode": 200}
    assert len(posted) == 1
    assert posted[0]["request_id"] == inline["requestId"]


def test_post_to_backend_url_headers_and_body(monkeypatch):
    import index

    inline = _load("inline_record.json")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        return MagicMock()

    with patch("index.urllib.request.urlopen", side_effect=fake_urlopen):
        index.handler(_awslogs_event(inline), None)

    assert captured["url"] == "http://alb.test.local/api/v1/internal/usage/from-bedrock-log"
    assert captured["headers"]["authorization"] == "Bearer meter-token-abc"
    assert captured["headers"]["content-type"] == "application/json"
    # The wire body carries ONLY the contract fields the backend BedrockLogRecord
    # requires — no internal-only S3 plumbing keys (removed in the CW-only cutover).
    assert "body_offloaded" not in captured["body"]
    assert "s3_ref" not in captured["body"]
    for key in (
        "request_id",
        "model_id",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "run_status",
        "region",
        "occurred_at",
    ):
        assert key in captured["body"]


# ---------------------------------------------------------------------------
# FAIL-SAFE TRANSPORT (review C1 / I3 / M2)
#
# A CloudWatch Logs subscription invokes the Lambda SYNCHRONOUSLY, so the
# function's async DLQ never fires on a per-record failure. These tests prove
# every per-record failure path durably routes to the SQS DLQ + emits a metric
# instead of silently dropping the record (or POSTing a zeroed-cache record and
# under-billing ~30×) — the precise "never silently bill 0 on a parse miss"
# guarantee the meter exists to uphold.
# ---------------------------------------------------------------------------


def test_extract_raises_when_output_body_truncated():
    """A >100KB completion offloaded to S3 leaves output.outputBodyJson absent —
    the cache counts can't be read, so _extract raises rather than returning
    inline-zero cache counts."""
    import index

    record = _load("truncated_output_record.json")
    assert "outputBodyJson" not in record["output"]  # fixture sanity
    with pytest.raises(index._OutputBodyTruncatedError):
        index._extract(record)


def test_handler_truncated_output_dlqs_and_does_not_post():
    """When the output body is offloaded (cache counts unreadable), the handler
    DLQs the raw log event + emits meter.output_body_truncated and NEVER POSTs a
    zeroed-cache record."""
    import index

    truncated = _load("truncated_output_record.json")
    posted = []
    dlqd = []
    metrics = []

    with (
        patch.object(index, "_post_to_backend", side_effect=lambda p: posted.append(p)),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(truncated), None)

    assert result == {"statusCode": 200}
    # The zeroed-cache record was NOT POSTed (the whole point of the fail-safe).
    assert posted == []
    # It WAS durably held on the DLQ with a descriptive reason.
    assert len(dlqd) == 1
    assert "output_body_truncated" in dlqd[0][1]
    assert "meter.output_body_truncated" in metrics


def test_handler_transport_error_dlqs_and_continues():
    """C1: a backend POST failure DLQs that record + emits meter.transport_error
    + continues the batch (does not raise / drop)."""
    import index

    inline = _load("inline_record.json")
    second = _load("inline_record.json")
    second["requestId"] = "second-record-id"
    dlqd = []
    metrics = []

    def fail_post(_parsed):
        raise RuntimeError("backend 503")

    with (
        patch.object(index, "_post_to_backend", side_effect=fail_post),
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(_awslogs_event(inline, second), None)

    assert result == {"statusCode": 200}
    # BOTH records' transport failures were DLQ'd (batch continued past the first).
    assert len(dlqd) == 2
    assert all("transport_error" in r for _e, r in dlqd)
    assert metrics.count("meter.transport_error") == 2


def test_handler_unparseable_record_dlqs_and_emits_metric():
    """M2: a per-record JSON-parse failure DLQs the line + emits a metric."""
    import index

    bad_event = {
        "awslogs": {
            "data": base64.b64encode(
                gzip.compress(json.dumps({"logEvents": [{"message": "{not json"}]}).encode())
            ).decode()
        }
    }
    dlqd = []
    metrics = []
    with (
        patch.object(index, "_send_to_dlq", side_effect=lambda e, r: dlqd.append((e, r))),
        patch.object(index, "_emit_metric", side_effect=lambda n, **k: metrics.append(n)),
    ):
        result = index.handler(bad_event, None)

    assert result == {"statusCode": 200}
    assert len(dlqd) == 1
    assert dlqd[0][1] == "unparseable_log_event"
    assert "meter.parse_failed" in metrics


def test_send_to_dlq_posts_message_to_queue():
    """The DLQ helper sends the raw log event + reason to the configured queue."""
    import index

    fake_sqs = MagicMock()
    log_event = {"message": "{}", "id": "abc"}
    with patch("index.boto3.client", return_value=fake_sqs):
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
