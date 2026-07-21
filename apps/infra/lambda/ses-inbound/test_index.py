"""Tests for the SES-inbound agent-email ingest Lambda.

Covers: MIME parsing (from/to/subject/plain-text body/headers), SES verdict
extraction, attachment storage + presigned URLs (with count/size caps and
per-attachment failure isolation), the shared-secret header, and the
FAILURE POSTURE described in index.py's module docstring — a non-2xx
backend response never raises, but an S3/parse failure or a backend
transport failure (no response at all) always does, so SES/Lambda's retry
policy re-invokes.

boto3 clients (S3, Secrets Manager) are stubbed with plain MagicMocks —
unlike the bedrock-usage-meter Lambda's tests, nothing here exercises a
DynamoDB expression DynamoDB itself would reject, so a hand mock is
sufficient (no moto needed).

Running:
    apps/backend/.venv/bin/python -m pytest apps/infra/lambda/ses-inbound -q
"""

import email.message
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

RAW_MAIL_BUCKET = "isol8-dev-ses-raw-mail"
ATTACHMENTS_BUCKET = "isol8-dev-agent-mail-attachments"
BACKEND_INGEST_URL = "https://api-dev.isol8.co/api/v1/webhooks/ses-inbound"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:877352799272:secret:isol8-dev-ses-inbound-secret-abc123"
SHARED_SECRET = "test-shared-secret-value"

MESSAGE_ID = "abcdef0123456789-abcdef01-2345-6789-abcd-ef0123456789-000000"


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    monkeypatch.setenv("RAW_MAIL_BUCKET", RAW_MAIL_BUCKET)
    monkeypatch.setenv("RAW_MAIL_PREFIX", "")
    monkeypatch.setenv("ATTACHMENTS_BUCKET", ATTACHMENTS_BUCKET)
    monkeypatch.setenv("BACKEND_INGEST_URL", BACKEND_INGEST_URL)
    monkeypatch.setenv("SES_INBOUND_SECRET_ARN", SECRET_ARN)
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    # Re-import fresh per test so the module-global cold-start caches (the
    # secret, the boto3 clients) never leak state between tests.
    sys.modules.pop("index", None)
    import index  # noqa: F401

    yield
    sys.modules.pop("index", None)


# ---------------------------------------------------------------------------
# Fixture builders — a raw MIME message (stdlib EmailMessage) and the SES
# receipt-rule Lambda-action event shape that references it by messageId.
# ---------------------------------------------------------------------------


def _build_raw_mime(*, attachment=True, extra_headers=None, body="Hello, this is the plain text body.\n"):
    msg = email.message.EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "agent@agents.isol8.co"
    msg["Subject"] = "Hello agent"
    msg["Message-ID"] = "<abc123@example.com>"
    if extra_headers:
        for k, v in extra_headers.items():
            msg[k] = v
    msg.set_content(body)
    if attachment:
        msg.add_attachment(b"hello world", maintype="text", subtype="plain", filename="note.txt")
    return msg.as_bytes()


def _ses_event(*, message_id=MESSAGE_ID, destination=None, verdict_overrides=None):
    verdicts = {
        "spfVerdict": {"status": "PASS"},
        "dkimVerdict": {"status": "PASS"},
        "dmarcVerdict": {"status": "PASS"},
        "spamVerdict": {"status": "PASS"},
        "virusVerdict": {"status": "PASS"},
    }
    if verdict_overrides:
        verdicts.update(verdict_overrides)
    return {
        "Records": [
            {
                "eventSource": "aws:ses",
                "eventVersion": "1.0",
                "ses": {
                    "mail": {
                        "timestamp": "2026-07-19T12:00:00.000Z",
                        "messageId": message_id,
                        "source": "jane@example.com",
                        "destination": destination if destination is not None else ["agent@agents.isol8.co"],
                        "commonHeaders": {
                            "from": ["Jane Doe <jane@example.com>"],
                            "to": ["agent@agents.isol8.co"],
                            "subject": "Hello agent",
                        },
                        "headers": [
                            {"name": "From", "value": "Jane Doe <jane@example.com>"},
                            {"name": "Subject", "value": "Hello agent"},
                        ],
                    },
                    "receipt": {
                        **verdicts,
                        "action": {"type": "Lambda", "functionArn": "arn:aws:lambda:us-east-1:877352799272:fn"},
                    },
                },
            }
        ]
    }


def _stub_clients(raw_mime):
    """A MagicMock S3 client wired for the happy path + a stubbed Secrets
    Manager client returning the shared secret."""
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=raw_mime))}
    s3.generate_presigned_url.return_value = "https://s3.example.com/presigned-url"

    secretsmanager = MagicMock()
    secretsmanager.get_secret_value.return_value = {"SecretString": SHARED_SECRET}
    return s3, secretsmanager


def _fake_urlopen_capturing(captured, *, status=200):
    def _urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        resp = MagicMock()
        resp.status = status
        resp.getcode.return_value = status
        return resp

    return _urlopen


# ---------------------------------------------------------------------------
# Happy path: clean payload shape, verdicts, one attachment, secret header.
# ---------------------------------------------------------------------------


def test_handler_posts_clean_payload_with_verdicts_and_attachment():
    import index

    raw = _build_raw_mime()
    s3, secretsmanager = _stub_clients(raw)
    captured = {}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        result = index.handler(_ses_event(), None)

    assert result == {"processed": 1, "records": [{"message_id": MESSAGE_ID, "backend_status": 200}]}

    body = captured["body"]
    assert body["message_id"] == MESSAGE_ID
    # `to` comes from SES's own envelope destination, not the To header.
    assert body["to"] == ["agent@agents.isol8.co"]
    assert body["from"] == "Jane Doe <jane@example.com>"
    assert body["subject"] == "Hello agent"
    assert body["text"].strip() == "Hello, this is the plain text body."
    assert body["headers"]["Message-ID"] == "<abc123@example.com>"
    assert isinstance(body["headers"]["Message-ID"], str)
    assert body["verdicts"] == {
        "spf": "PASS",
        "dkim": "PASS",
        "dmarc": "PASS",
        "spam": "PASS",
        "virus": "PASS",
    }

    assert len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["filename"] == "note.txt"
    assert att["content_type"] == "text/plain"
    assert att["size"] == len(b"hello world")
    assert att["download_url"] == "https://s3.example.com/presigned-url"

    # The shared secret is forwarded as the auth header, exactly as fetched.
    assert captured["headers"]["x-isol8-ses-secret"] == SHARED_SECRET
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["url"] == BACKEND_INGEST_URL

    s3.put_object.assert_called_once()
    put_kwargs = s3.put_object.call_args.kwargs
    assert put_kwargs["Bucket"] == ATTACHMENTS_BUCKET
    assert put_kwargs["Key"] == f"inbound/{MESSAGE_ID}/note.txt"
    assert put_kwargs["Body"] == b"hello world"


def test_destination_with_multiple_recipients_all_forwarded():
    """`to` is the full SES envelope destination list, not just one address."""
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)
    captured = {}
    event = _ses_event(destination=["agent@agents.isol8.co", "cc@example.com"])

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(event, None)

    assert captured["body"]["to"] == ["agent@agents.isol8.co", "cc@example.com"]


# ---------------------------------------------------------------------------
# The backend decides drop policy, not this Lambda — a DMARC-fail event
# still gets POSTed, just with the failing verdict visible in the payload.
# ---------------------------------------------------------------------------


def test_dmarc_fail_still_posts():
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)
    captured = {}
    event = _ses_event(verdict_overrides={"dmarcVerdict": {"status": "FAIL"}})

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        result = index.handler(event, None)

    assert result["processed"] == 1
    assert captured["body"]["verdicts"]["dmarc"] == "FAIL"


def test_missing_verdict_key_defaults_to_empty_string():
    """Every verdict lookup is `.get`-guarded — a receipt missing a key must
    not raise, it must default to ""."""
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)
    captured = {}
    event = _ses_event()
    del event["Records"][0]["ses"]["receipt"]["spamVerdict"]

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(event, None)

    assert captured["body"]["verdicts"]["spam"] == ""


# ---------------------------------------------------------------------------
# Attachments: per-attachment failure isolation + count/size caps.
# ---------------------------------------------------------------------------


def test_bad_attachment_does_not_sink_the_message():
    """One attachment failing to store must not prevent the rest of the
    message (and its other, good attachment) from being delivered."""
    import index

    msg = email.message.EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "agent@agents.isol8.co"
    msg["Subject"] = "Two attachments, one bad"
    msg.set_content("body text\n")
    msg.add_attachment(b"bad data", maintype="application", subtype="octet-stream", filename="bad.bin")
    msg.add_attachment(b"good data", maintype="text", subtype="plain", filename="good.txt")
    raw = msg.as_bytes()

    s3, secretsmanager = _stub_clients(raw)
    # First put_object call (bad.bin) blows up; the second (good.txt) succeeds.
    s3.put_object.side_effect = [RuntimeError("s3 down"), None]
    captured = {}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        result = index.handler(_ses_event(), None)

    assert result["processed"] == 1  # the message itself still delivered
    attachments = captured["body"]["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "good.txt"


def test_attachment_count_capped_at_ten():
    import index

    msg = email.message.EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "agent@agents.isol8.co"
    msg["Subject"] = "Many attachments"
    msg.set_content("body\n")
    for i in range(12):
        msg.add_attachment(f"data-{i}".encode(), maintype="text", subtype="plain", filename=f"f{i}.txt")
    raw = msg.as_bytes()

    s3, secretsmanager = _stub_clients(raw)
    captured = {}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(_ses_event(), None)

    assert len(captured["body"]["attachments"]) == 10
    assert s3.put_object.call_count == 10


def test_attachment_total_bytes_capped(monkeypatch):
    import index

    # Shrink the cap so the test doesn't need to construct real megabytes.
    monkeypatch.setattr(index, "_MAX_ATTACHMENTS_TOTAL_BYTES", 10)

    msg = email.message.EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "agent@agents.isol8.co"
    msg["Subject"] = "Big attachments"
    msg.set_content("body\n")
    msg.add_attachment(b"0123456789", maintype="text", subtype="plain", filename="a.txt")  # exactly 10 bytes
    msg.add_attachment(b"this-pushes-us-over", maintype="text", subtype="plain", filename="b.txt")
    raw = msg.as_bytes()

    s3, secretsmanager = _stub_clients(raw)
    captured = {}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(_ses_event(), None)

    attachments = captured["body"]["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "a.txt"


# ---------------------------------------------------------------------------
# FAILURE POSTURE. A non-2xx backend response never raises (the backend
# already answered); an S3/parse failure or a transport-level POST failure
# (no response at all) always does, so SES/Lambda's retry policy fires.
# ---------------------------------------------------------------------------


def test_backend_non_2xx_response_does_not_raise():
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)

    def _urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_urlopen),
    ):
        result = index.handler(_ses_event(), None)  # must not raise

    assert result["processed"] == 1
    assert result["records"][0]["backend_status"] == 500


def test_backend_transport_failure_raises():
    """No response came back at all (connection refused / DNS / timeout) —
    this is "the whole thing failed to POST" and must propagate so
    SES/Lambda retries the invocation."""
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)

    def _urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_urlopen),
        pytest.raises(urllib.error.URLError),
    ):
        index.handler(_ses_event(), None)


def test_s3_get_object_failure_raises():
    """Cannot even read the raw mail — must surface as a Lambda error, not
    be silently swallowed, so SES retries the whole invocation."""
    import index

    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("s3 unavailable")
    secretsmanager = MagicMock()
    secretsmanager.get_secret_value.return_value = {"SecretString": SHARED_SECRET}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        pytest.raises(RuntimeError),
    ):
        index.handler(_ses_event(), None)


def test_mime_parse_failure_raises():
    import index

    s3, secretsmanager = _stub_clients(b"irrelevant raw bytes")

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch.object(index, "message_from_bytes", side_effect=ValueError("bad mime")),
        pytest.raises(ValueError),
    ):
        index.handler(_ses_event(), None)


# ---------------------------------------------------------------------------
# The shared secret is fetched once and cached across warm invocations.
# ---------------------------------------------------------------------------


def test_secret_is_fetched_once_and_cached_across_invocations():
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)
    captured = {}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(_ses_event(), None)
        index.handler(_ses_event(message_id="second-message-id"), None)

    secretsmanager.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN)


# ---------------------------------------------------------------------------
# RAW_MAIL_PREFIX is honored when reading from S3.
# ---------------------------------------------------------------------------


def test_raw_mail_prefix_is_applied_to_the_s3_key(monkeypatch):
    import index

    monkeypatch.setenv("RAW_MAIL_PREFIX", "inbound-raw/")
    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)
    captured = {}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(_ses_event(), None)

    s3.get_object.assert_called_once_with(Bucket=RAW_MAIL_BUCKET, Key=f"inbound-raw/{MESSAGE_ID}")
