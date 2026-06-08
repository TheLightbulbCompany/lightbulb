"""Tests for the Bedrock usage-meter Lambda — parse + S3 fallback + transport.

Uses the captured fixtures from Task 1 as ground truth.

NOTE: the Task-1 fixtures are SYNTHETIC (hand-built from the AWS-documented
Bedrock model-invocation-logging schema — see fixtures/ + FIELDS.md), because
this plan was executed under an autonomous run with no live AWS/SSO access. The
parser is written DEFENSIVELY (multi-path token/cache extraction + S3-offload
fallback) so a shape mismatch degrades rather than silently zeroing the cache
tokens — but the EXACT field paths MUST be confirmed against a REAL dev record
(FIELDS.md's build-gate checklist) before METER_IS_LIVE is flipped to true.
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
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    sys.modules.pop("index", None)
    import index  # noqa: F401

    yield
    sys.modules.pop("index", None)


def test_extract_inline_record_reads_tokens_and_cache():
    import index

    record = _load("inline_record.json")
    parsed = index._extract(record)
    assert parsed["request_id"] == record["requestId"]
    assert parsed["model_id"] == record["modelId"]
    # cache tokens are present and >= 0 (the inline record carries them)
    assert parsed["cache_read_tokens"] >= 0
    assert parsed["cache_write_tokens"] >= 0
    assert parsed["body_offloaded"] is False


def test_extract_offloaded_record_flags_offload():
    import index

    record = _load("s3_offloaded_record.json")
    parsed = index._extract(record)
    assert parsed["body_offloaded"] is True
    assert parsed["s3_ref"]  # a non-empty S3 reference was found


def _s3_usage(s3_body):
    """Pull the usage block from the committed S3 body fixture the same way
    _merge_s3_body does, so the test asserts against the FIXTURE's real values
    (not a hardcoded literal that could drift from the captured fixture)."""
    return s3_body.get("usage", s3_body)


def test_merge_s3_body_pulls_cache_tokens_from_s3():
    import index

    record = _load("s3_offloaded_record.json")
    s3_body = _load("s3_body.json")
    parsed = index._extract(record)

    # The committed fixture MUST carry NON-ZERO cache counts — that is the
    # entire point of the S3 path (Task 1 Step 5/6 guarantees this). Read the
    # expected values straight out of the fixture so the assertion fails if the
    # merge silently yields 0 (the #1 correctness landmine this plan exists to
    # prevent — a `>= 0` assertion would pass even on a wrong S3 path).
    usage = _s3_usage(s3_body)
    expected_read = int(usage.get("cacheReadInputTokens", usage.get("cacheReadInputTokenCount", 0)))
    expected_write = int(usage.get("cacheWriteInputTokens", usage.get("cacheWriteInputTokenCount", 0)))
    assert expected_read > 0, (
        "s3_body.json fixture must retain a non-zero cacheRead count "
        "(Task 1 Step 5/6) — a zero fixture makes this test vacuous."
    )

    fake_s3 = MagicMock()
    fake_s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(s3_body).encode())}
    with patch("index.boto3.client", return_value=fake_s3):
        merged = index._merge_s3_body(parsed, record)
    # The cache tokens come from the S3 body and EQUAL the fixture's real
    # values — not 0, not the inline (offloaded ⇒ inline-zero) count.
    assert merged["cache_read_tokens"] == expected_read
    assert merged["cache_write_tokens"] == expected_write
    assert fake_s3.get_object.called


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
    # Internal-only keys are stripped from the wire body.
    assert "body_offloaded" not in captured["body"]
    assert "s3_ref" not in captured["body"]
    # The contract fields the backend BedrockLogRecord requires are present.
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
