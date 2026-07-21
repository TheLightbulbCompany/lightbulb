"""agent_mail_inbound: sender parsing, loop guard, body fetch, message format."""

import os

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_inbound as inbound  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme <onboarding@resend.dev>", "onboarding@resend.dev"),
        ("plain@example.com", "plain@example.com"),
        ("  Mixed <A@B.com> ", "a@b.com"),
        ('"Doe, Jane" <jane@x.io>', "jane@x.io"),
    ],
)
def test_parse_from_address(raw, expected):
    assert inbound.parse_from_address(raw) == expected


@pytest.mark.parametrize(
    "headers,blocked",
    [
        ({}, False),
        ({"Auto-Submitted": "no"}, False),
        ({"Auto-Submitted": "auto-replied"}, True),
        ({"Precedence": "bulk"}, True),
        ({"Precedence": "list"}, True),
        ({"precedence": "Junk"}, True),  # case-insensitive
        ({"Precedence": "auto_reply"}, True),
    ],
)
def test_is_auto_submitted(headers, blocked):
    assert inbound.is_auto_submitted(headers) is blocked


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Authentication-Results": "mx.google.com; dmarc=pass header.from=x.com"}, "pass"),
        ({"Authentication-Results": "spf=pass smtp.mailfrom=a@x.com; dkim=fail"}, "pass"),
        ({"Authentication-Results": "spf=fail; dkim=fail; dmarc=fail"}, "fail"),
        # dmarc=fail is authoritative — must NOT be rescued by spf=pass.
        ({"Authentication-Results": "dmarc=fail; spf=pass"}, "fail"),
        ({"Authentication-Results": "spf=fail; dkim=fail"}, "fail"),
        ({}, "unknown"),
        (None, "unknown"),
        ({"Authentication-Results": 12345}, "unknown"),
        # case-insensitive key
        ({"authentication-results": "dmarc=pass"}, "pass"),
    ],
)
def test_sender_authentication_verdict(headers, expected):
    assert inbound.sender_authentication_verdict(headers) == expected


def test_build_delivered_message_inlines_header_and_token():
    msg = inbound.build_delivered_message(
        from_addr="alice@example.com",
        subject="Need a hand",
        body_text="Can you draft a reply?",
        reply_token="TOKEN123",
    )
    assert "alice@example.com" in msg
    assert "Need a hand" in msg
    assert "TOKEN123" in msg
    assert "Can you draft a reply?" in msg
    # The body must come after the header block.
    assert msg.index("TOKEN123") < msg.index("Can you draft a reply?")


@pytest.mark.asyncio
async def test_fetch_received_email_ok(monkeypatch):
    def handler(request):
        assert request.url.path == "/emails/receiving/eml_1"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"text": "hi", "message_id": "<m@x>", "headers": {}})

    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    got = await inbound.fetch_received_email("eml_1")
    assert got["text"] == "hi"


@pytest.mark.asyncio
async def test_fetch_received_email_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "nope"}))
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))
    assert await inbound.fetch_received_email("eml_missing") is None


@pytest.mark.asyncio
async def test_fetch_received_email_never_raises(monkeypatch):
    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")

    def boom(r):
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(boom)
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))
    assert await inbound.fetch_received_email("eml_x") is None


@pytest.mark.asyncio
async def test_fetch_received_email_undecodable_body_returns_none(monkeypatch):
    """A 200 response whose body is not valid JSON must return None, not raise
    — `resp.json()` raising a decode error is caught by the same broad
    `except Exception` as network failures.
    """
    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"this is not json"))
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))
    assert await inbound.fetch_received_email("eml_bad_json") is None
