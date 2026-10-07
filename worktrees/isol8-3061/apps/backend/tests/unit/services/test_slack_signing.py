import hashlib
import hmac
import time

from core.services import slack_signing as s

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
BODY = b'{"type":"event_callback","team_id":"T1"}'


def _expected(ts: str) -> str:
    return "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + BODY, hashlib.sha256).hexdigest()


def test_sign_matches_slack_basestring():
    assert s.sign(SECRET, "1531420618", BODY) == _expected("1531420618")


def test_verify_accepts_fresh_valid_signature():
    ts = str(int(time.time()))
    assert s.verify(SECRET, ts, BODY, s.sign(SECRET, ts, BODY)) is True


def test_verify_rejects_wrong_secret():
    ts = str(int(time.time()))
    assert s.verify("other", ts, BODY, s.sign(SECRET, ts, BODY)) is False


def test_verify_rejects_tampered_body():
    ts = str(int(time.time()))
    assert s.verify(SECRET, ts, BODY + b" ", s.sign(SECRET, ts, BODY)) is False


def test_verify_rejects_stale_timestamp():
    ts = str(int(time.time()) - 301)
    assert s.verify(SECRET, ts, BODY, s.sign(SECRET, ts, BODY)) is False


def test_verify_rejects_missing_or_malformed_inputs():
    ts = str(int(time.time()))
    assert s.verify(SECRET, None, BODY, s.sign(SECRET, ts, BODY)) is False
    assert s.verify(SECRET, ts, BODY, None) is False
    assert s.verify(SECRET, "not-a-number", BODY, s.sign(SECRET, ts, BODY)) is False
    assert s.verify(SECRET, ts, BODY, "sha256=abc") is False


def test_verify_honours_injected_now():
    assert s.verify(SECRET, "1531420618", BODY, _expected("1531420618"), now=1531420618 + 10) is True
