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


@pytest.mark.asyncio
async def test_list_received_attachments_ok(monkeypatch):
    def handler(request):
        assert request.url.path == "/emails/receiving/eml_1/attachments"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "has_more": False,
                "data": [
                    {
                        "filename": "a.pdf",
                        "content_type": "application/pdf",
                        "size": 10,
                        "download_url": "https://cdn/a",
                    }
                ],
            },
        )

    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    got = await inbound.list_received_attachments("eml_1")
    assert got == [
        {
            "filename": "a.pdf",
            "content_type": "application/pdf",
            "size": 10,
            "download_url": "https://cdn/a",
        }
    ]


@pytest.mark.asyncio
async def test_list_received_attachments_non_200_returns_empty(monkeypatch):
    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "nope"}))
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))
    assert await inbound.list_received_attachments("eml_missing") == []


@pytest.mark.asyncio
async def test_list_received_attachments_never_raises(monkeypatch):
    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")

    def boom(r):
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(boom)
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))
    assert await inbound.list_received_attachments("eml_x") == []


def test_build_attachment_manifest_empty_list():
    assert inbound.build_attachment_manifest([]) == ""


def test_build_attachment_manifest_single_attachment():
    manifest = inbound.build_attachment_manifest(
        [
            {
                "filename": "contract.pdf",
                "content_type": "application/pdf",
                "size": 1234,
                "download_url": "https://cdn/x",
            }
        ]
    )
    assert "contract.pdf" in manifest
    assert "application/pdf" in manifest
    assert "1234" in manifest
    assert "https://cdn/x" in manifest
    assert "unverified" in manifest.lower()


def test_build_attachment_manifest_truncates_at_ten():
    attachments = [
        {
            "filename": f"file{i}.txt",
            "content_type": "text/plain",
            "size": i,
            "download_url": f"https://cdn/{i}",
        }
        for i in range(12)
    ]
    manifest = inbound.build_attachment_manifest(attachments)
    for i in range(10):
        assert f"file{i}.txt" in manifest
    assert "file10.txt" not in manifest
    assert "file11.txt" not in manifest
    assert "2 more" in manifest


def test_build_delivered_message_empty_manifest_is_byte_identical():
    kwargs = dict(
        from_addr="alice@example.com",
        subject="Need a hand",
        body_text="Can you draft a reply?",
        reply_token="TOKEN123",
    )
    assert inbound.build_delivered_message(**kwargs) == inbound.build_delivered_message(
        **kwargs, attachments_manifest=""
    )


def test_build_delivered_message_with_manifest_appends_after_body():
    manifest_text = "Attachments (1):\n- a.pdf (application/pdf, 10 bytes)\n  https://cdn/a"
    msg = inbound.build_delivered_message(
        from_addr="alice@example.com",
        subject="Need a hand",
        body_text="Can you draft a reply?",
        reply_token="TOKEN123",
        attachments_manifest=manifest_text,
    )
    assert manifest_text in msg
    assert msg.index("Can you draft a reply?") < msg.index(manifest_text)
