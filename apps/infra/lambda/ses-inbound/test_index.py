"""Tests for the SES-inbound agent-email ingest Lambda.

Covers: MIME parsing (from/to/subject/plain-text body/headers), SES verdict
extraction, attachment storage + presigned URLs (with count/size caps,
pre-decode size estimation, per-attachment failure isolation, and
collision-safe S3 keys), the shared-secret header, and the FAILURE POSTURE
described in index.py's module docstring — a 2xx/4xx backend response never
raises, but a 5xx response, an S3/parse failure, or a backend transport
failure (no response at all) always does, so SES/Lambda's retry policy
re-invokes.

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
        # No DKIM-Signature on this fixture; the envelope domain comes from
        # SES's receipt (mail.source), never from a header.
        "dkim_domains": [],
        "spf_domain": "example.com",
    }

    assert len(body["attachments"]) == 1
    att = body["attachments"][0]
    assert att["filename"] == "note.txt"
    assert att["content_type"] == "text/plain"
    assert att["size"] == len(b"hello world")
    assert att["download_url"] == "https://s3.example.com/presigned-url"

    # The shared secret is forwarded as the auth header, exactly as fetched.
    assert captured["headers"]["x-lightbulb-ses-secret"] == SHARED_SECRET
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["url"] == BACKEND_INGEST_URL

    s3.put_object.assert_called_once()
    put_kwargs = s3.put_object.call_args.kwargs
    assert put_kwargs["Bucket"] == ATTACHMENTS_BUCKET
    # The key is index-prefixed (see the duplicate-filename collision test
    # below); the manifest's `filename` field keeps the plain original name.
    assert put_kwargs["Key"] == f"inbound/{MESSAGE_ID}/0-note.txt"
    assert put_kwargs["Body"] == b"hello world"


def _calendar_mime(ics):
    msg = email.message.EmailMessage()
    msg["From"] = "jane@example.com"
    msg["Subject"] = "Invitation"
    msg.set_content("plain body")
    msg.add_alternative(ics, subtype="calendar", params={"method": "REQUEST"})
    return msg.as_bytes()


def test_calendar_part_is_carried_as_text():
    import index

    raw = _calendar_mime("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n")
    payload = index._build_payload(_ses_event()["Records"][0]["ses"], raw)
    assert payload["calendar"].startswith("BEGIN:VCALENDAR")
    assert payload["text"].strip() == "plain body"


def test_calendar_is_none_without_a_calendar_part():
    import index

    payload = index._build_payload(_ses_event()["Records"][0]["ses"], _build_raw_mime())
    assert payload["calendar"] is None


def test_calendar_part_is_capped_at_65536_chars():
    import index

    raw = _calendar_mime("BEGIN:VCALENDAR\r\n" + "X" * 100_000 + "\r\nEND:VCALENDAR\r\n")
    payload = index._build_payload(_ses_event()["Records"][0]["ses"], raw)
    assert len(payload["calendar"]) == 65536


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
    # The cap is now checked against the pre-decode (base64) estimate, not
    # the exact decoded size, so this has to clear "a.txt"'s ~12-byte
    # estimate (its 10 decoded bytes base64-encode to 17 chars, incl.
    # padding/newline -> int(17*3/4) == 12) while still excluding "b.txt"'s
    # ~21-byte estimate (10 + 21 == 31 > 15).
    monkeypatch.setattr(index, "_MAX_ATTACHMENTS_TOTAL_BYTES", 15)

    msg = email.message.EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "agent@agents.isol8.co"
    msg["Subject"] = "Big attachments"
    msg.set_content("body\n")
    msg.add_attachment(b"0123456789", maintype="text", subtype="plain", filename="a.txt")  # 10 decoded bytes
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


def test_oversized_attachment_skipped_without_decoding(monkeypatch):
    """The size cap must be enforced from the still-encoded payload length
    BEFORE `get_content()` decodes the part into memory — a crafted huge
    attachment is never decoded just to find out it should be dropped. The
    message (and any other, small attachment) still POSTs."""
    import index

    monkeypatch.setattr(index, "_MAX_ATTACHMENTS_TOTAL_BYTES", 100)

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)
    captured = {}

    small_att = MagicMock()
    small_att.get_filename.return_value = "small.txt"
    small_att.get_content_type.return_value = "text/plain"
    small_att.get_payload.return_value = "aGVsbG8="  # tiny base64 payload
    small_att.get_content.return_value = "hello"

    huge_att = MagicMock()
    huge_att.get_filename.return_value = "huge.bin"
    huge_att.get_content_type.return_value = "application/octet-stream"
    # A raw (still-encoded) payload whose estimated decoded size alone blows
    # the (monkeypatched, tiny) cap.
    huge_att.get_payload.return_value = "A" * 1000
    huge_att.get_content.return_value = b"x" * 750  # would never be reached

    fake_msg = MagicMock()
    fake_msg.iter_attachments.return_value = [small_att, huge_att]
    fake_msg.get.side_effect = lambda key, default=None: {
        "From": "Jane Doe <jane@example.com>",
        "Subject": "Oversized attachment",
    }.get(key, default)
    fake_msg.get_body.return_value = None
    fake_msg.items.return_value = []

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch.object(index, "message_from_bytes", return_value=fake_msg),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        result = index.handler(_ses_event(), None)

    # The oversized part's get_content() (the full base64 decode) must never
    # be called — that is the whole point of the pre-decode size estimate.
    huge_att.get_content.assert_not_called()

    assert result["processed"] == 1
    attachments = captured["body"]["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "small.txt"


def test_duplicate_filenames_get_distinct_s3_keys():
    """Two parts sharing a filename must not collide on the same S3 key —
    the part index is folded into the key; the manifest keeps the plain
    original filename."""
    import index

    msg = email.message.EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "agent@agents.isol8.co"
    msg["Subject"] = "Duplicate filenames"
    msg.set_content("body\n")
    msg.add_attachment(b"first", maintype="text", subtype="plain", filename="dup.txt")
    msg.add_attachment(b"second", maintype="text", subtype="plain", filename="dup.txt")
    raw = msg.as_bytes()

    s3, secretsmanager = _stub_clients(raw)
    captured = {}

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(_ses_event(), None)

    keys = [call.kwargs["Key"] for call in s3.put_object.call_args_list]
    assert keys == [f"inbound/{MESSAGE_ID}/0-dup.txt", f"inbound/{MESSAGE_ID}/1-dup.txt"]
    assert len(set(keys)) == 2  # distinct S3 objects, no overwrite

    attachments = captured["body"]["attachments"]
    assert [a["filename"] for a in attachments] == ["dup.txt", "dup.txt"]


# ---------------------------------------------------------------------------
# Presigned attachment URL TTL — must be realistic (bounded by the Lambda
# execution role's temporary credentials), not the illusory 24h it used to be.
# ---------------------------------------------------------------------------


def test_presigned_url_ttl_is_one_hour():
    import index

    assert index._PRESIGNED_URL_EXPIRY_SECONDS == 3600


# ---------------------------------------------------------------------------
# FAILURE POSTURE. A 2xx/4xx backend response never raises (the backend
# already answered, terminally); a 5xx response, an S3/parse failure, or a
# transport-level POST failure (no response at all) always raises, so
# SES/Lambda's retry policy fires.
# ---------------------------------------------------------------------------


def test_backend_5xx_response_raises():
    """A 5xx means the backend is signalling a transient failure — it must
    propagate so SES retries the delivery (the backend dedups on
    message_id, so a retry that eventually succeeds is not a double
    delivery)."""
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)

    def _urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_urlopen),
        pytest.raises(urllib.error.HTTPError),
    ):
        index.handler(_ses_event(), None)


def test_backend_4xx_response_does_not_raise():
    """A 4xx is terminal — the backend already answered, and retrying a
    deterministic 4xx (e.g. bad secret) would only retry-storm the Lambda."""
    import index

    raw = _build_raw_mime(attachment=False)
    s3, secretsmanager = _stub_clients(raw)

    def _urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_urlopen),
    ):
        result = index.handler(_ses_event(), None)  # must not raise

    assert result["processed"] == 1
    assert result["records"][0]["backend_status"] == 403


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


# --- Environment routing ---------------------------------------------------
#
# One receipt rule serves both environments: SES matches a recipient condition
# of "isol8.co" against its SUBDOMAINS too, so mail for
# friday@acme.dev.isol8.co lands on the same prod-owned rule as
# friday@acme.isol8.co. This Lambda is therefore what separates them.


def test_routes_dev_recipients_to_the_dev_backend(monkeypatch):
    import index

    monkeypatch.setenv("DEV_MAIL_SUFFIX", "dev.isol8.co")
    monkeypatch.setenv("DEV_BACKEND_INGEST_URL", "https://api-dev.example/ingest")
    monkeypatch.setenv("DEV_SES_INBOUND_SECRET_ARN", "arn:dev-secret")

    url, arn = index._route_for(["friday@acme.dev.isol8.co"])

    assert url == "https://api-dev.example/ingest"
    assert arn == "arn:dev-secret"


def test_routes_prod_recipients_to_the_prod_backend(monkeypatch):
    import index

    monkeypatch.setenv("DEV_MAIL_SUFFIX", "dev.isol8.co")
    monkeypatch.setenv("DEV_BACKEND_INGEST_URL", "https://api-dev.example/ingest")
    monkeypatch.setenv("DEV_SES_INBOUND_SECRET_ARN", "arn:dev-secret")

    url, arn = index._route_for(["friday@acme.isol8.co"])

    assert url == BACKEND_INGEST_URL
    assert arn == SECRET_ARN


def test_dev_routing_is_inert_when_unconfigured(monkeypatch):
    """No DEV_* env (the posture before dev is enabled) -> always prod."""
    import index

    monkeypatch.delenv("DEV_MAIL_SUFFIX", raising=False)
    monkeypatch.delenv("DEV_BACKEND_INGEST_URL", raising=False)

    url, arn = index._route_for(["friday@acme.dev.isol8.co"])

    assert url == BACKEND_INGEST_URL
    assert arn == SECRET_ARN


def test_routes_every_configured_dev_suffix(monkeypatch):
    """A mail-domain move leaves TWO live dev suffixes at once.

    Addresses already minted into DynamoDB stay on the old domain, so both the
    new and the legacy dev suffix must reach the DEV backend — a single-value
    DEV_MAIL_SUFFIX would hand every legacy dev address to PROD.
    """
    import index

    monkeypatch.setenv("DEV_MAIL_SUFFIX", "dev.thelightbulb.company,dev.isol8.co")
    monkeypatch.setenv("DEV_BACKEND_INGEST_URL", "https://api-dev.example/ingest")
    monkeypatch.setenv("DEV_SES_INBOUND_SECRET_ARN", "arn:dev-secret")

    for addr in ("friday@acme.dev.thelightbulb.company", "friday@acme.dev.isol8.co"):
        assert index._route_for([addr])[0] == "https://api-dev.example/ingest", addr

    # A prod address under either domain still goes to prod.
    for addr in ("friday@acme.thelightbulb.company", "friday@acme.isol8.co"):
        assert index._route_for([addr])[0] == BACKEND_INGEST_URL, addr


def test_lookalike_domain_is_not_routed_to_dev(monkeypatch):
    """`notdev.isol8.co` must not match a `dev.isol8.co` suffix."""
    import index

    monkeypatch.setenv("DEV_MAIL_SUFFIX", "dev.isol8.co")
    monkeypatch.setenv("DEV_BACKEND_INGEST_URL", "https://api-dev.example/ingest")
    monkeypatch.setenv("DEV_SES_INBOUND_SECRET_ARN", "arn:dev-secret")

    url, _ = index._route_for(["friday@acme.notdev.isol8.co"])

    assert url == BACKEND_INGEST_URL


# ---------------------------------------------------------------------------
# Sender-identity forwarding: the two authenticated identities SES's flat
# verdicts do not carry, forwarded as FIRST-CLASS `verdicts` fields. Neither is
# read out of a header — see index.py's module docstring.
# ---------------------------------------------------------------------------

_DKIM_SIG = "v=1; a=rsa-sha256; c=relaxed/relaxed; d=example.com; s=sel; h=from:to; bh=Zm8=; b=c2ln"


def _post_and_capture(raw, event=None):
    import index

    s3, secretsmanager = _stub_clients(raw)
    captured = {}
    with (
        patch.object(index, "_s3", return_value=s3),
        patch.object(index, "_secretsmanager", return_value=secretsmanager),
        patch("index.urllib.request.urlopen", side_effect=_fake_urlopen_capturing(captured)),
    ):
        index.handler(event or _ses_event(), None)
    return captured["body"]


def test_dkim_signer_domain_is_forwarded_when_ses_verified_the_signature():
    raw = _build_raw_mime(extra_headers={"DKIM-Signature": _DKIM_SIG})

    verdicts = _post_and_capture(raw)["verdicts"]

    assert verdicts["dkim_domains"] == ["example.com"]
    # From SES's receipt envelope (mail.source), not the MIME's Return-Path.
    assert verdicts["spf_domain"] == "example.com"


def test_dkim_signer_domain_is_empty_when_ses_did_not_pass_dkim():
    """SES's PASS is the only thing that makes a `d=` tag mean anything: without
    it the signature is unverified and its domain proves nothing about `From`."""
    raw = _build_raw_mime(extra_headers={"DKIM-Signature": _DKIM_SIG})
    event = _ses_event(verdict_overrides={"dkimVerdict": {"status": "FAIL"}})

    assert _post_and_capture(raw, event)["verdicts"]["dkim_domains"] == []


def test_all_dkim_signature_domains_are_forwarded():
    """SES's flat PASS says at least one signature verified, not which — so
    every `d=` is forwarded and the backend aligns against any of them."""
    msg = email.message.EmailMessage()
    msg["From"] = "Jane Doe <jane@example.com>"
    msg["To"] = "agent@agents.isol8.co"
    msg["Subject"] = "Hello agent"
    msg["DKIM-Signature"] = _DKIM_SIG
    msg["DKIM-Signature"] = _DKIM_SIG.replace("d=example.com", "d=Mail.Example.NET")
    msg.set_content("body\n")

    assert _post_and_capture(msg.as_bytes())["verdicts"]["dkim_domains"] == ["example.com", "mail.example.net"]


def test_forged_authentication_results_is_not_an_identity_source():
    """The whole point of the first-class fields: an attacker can paste any
    `Authentication-Results` into the message, and none of it may be forwarded
    as an authenticated identity."""
    raw = _build_raw_mime(
        extra_headers={
            "Authentication-Results": "amazonses.com; dkim=pass header.d=github.com;",
            "Return-Path": "<spoofed@attacker.test>",
        }
    )

    body = _post_and_capture(raw)

    assert body["verdicts"]["dkim_domains"] == []
    assert body["verdicts"]["spf_domain"] == "example.com"
    # Headers are still forwarded verbatim for the inbox — they are simply not
    # an authentication source any more.
    assert body["headers"]["Return-Path"] == "<spoofed@attacker.test>"
