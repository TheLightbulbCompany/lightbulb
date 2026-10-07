"""agent_mail_inbound: sender parsing, sender authentication, loop guard, message format."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_inbound as inbound  # noqa: E402
from core.services.agent_mail_inbound import build_delivered_message  # noqa: E402


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


@pytest.mark.parametrize("headers", [["not", "a", "dict"], None])
def test_is_auto_submitted_non_dict_returns_false(headers):
    """A malformed (non-dict) `headers` value must not raise."""
    assert inbound.is_auto_submitted(headers) is False


def test_build_delivered_message_inlines_header_and_token():
    msg = inbound.build_delivered_message(
        from_addr="alice@example.com",
        subject="Need a hand",
        body_text="Can you draft a reply?",
        reply_key="TOKEN123",
    )
    assert "alice@example.com" in msg
    assert "Need a hand" in msg
    assert "TOKEN123" in msg
    assert "Can you draft a reply?" in msg
    # The body must come after the header block.
    assert msg.index("TOKEN123") < msg.index("Can you draft a reply?")


def test_delivered_message_names_the_reply_command():
    msg = build_delivered_message(from_addr="a@b.c", subject="S", body_text="B", reply_key="a@b.c/2026#m1")
    assert "Reply-Key: a@b.c/2026#m1" in msg
    assert "lightbulb-mail.sh send --reply" in msg


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


def test_build_attachment_manifest_mentions_link_expiry():
    """`download_url` is a time-limited SES S3 presigned link — the manifest
    must warn the agent to fetch promptly rather than treating it as durable."""
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
    assert "time-limited" in manifest.lower()
    assert "promptly" in manifest.lower()


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
        reply_key="TOKEN123",
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
        reply_key="TOKEN123",
        attachments_manifest=manifest_text,
    )
    assert manifest_text in msg
    assert msg.index("Can you draft a reply?") < msg.index(manifest_text)


def test_build_delivered_message_truncates_oversized_body():
    """A body over OpenClaw's ~1 MB /hooks/agent limit must be capped rather
    than inlined whole — an over-limit request 413s and silently drops the
    entire delivery."""
    huge_body = "x" * 600_000
    msg = inbound.build_delivered_message(
        from_addr="alice@example.com",
        subject="Huge",
        body_text=huge_body,
        reply_key="TOKEN123",
    )
    assert len(msg) < 520_000
    assert "truncated" in msg


def test_build_delivered_message_short_body_is_unchanged():
    msg = inbound.build_delivered_message(
        from_addr="alice@example.com",
        subject="Short",
        body_text="a normal short body",
        reply_key="TOKEN123",
    )
    assert "truncated" not in msg
    assert "a normal short body" in msg


# ---------------------------------------------------------------------------
# authenticated_sender — the WAKE gate. SPF authenticates the ENVELOPE sender
# and DKIM can sign an unrelated domain, so neither PASS counts unless the
# identity it authenticated ALIGNS with the `From` domain. Both identities come
# from `verdicts` (the SES-ingest Lambda derives them from SES's receipt and
# from the signed `DKIM-Signature`); `headers` is never an identity source.
# ---------------------------------------------------------------------------

_FORGED_AUTHRES = {"Authentication-Results": "amazonses.com; dkim=pass header.d=github.com;"}


@pytest.mark.parametrize(
    "verdicts,headers,sender,expected",
    [
        # DMARC PASS is the From-alignment check itself — sufficient alone.
        ({"dmarc": "PASS"}, {}, "anyone@example.com", True),
        ({"dmarc": "PASS", "spf": "FAIL"}, {}, "anyone@example.com", True),
        # No DMARC: an aligned DKIM signing domain authenticates the From.
        ({"dkim": "PASS", "dkim_domains": ["github.com"]}, {}, "noreply@github.com", True),
        # Relaxed alignment — a signing SUBdomain still aligns.
        ({"dkim": "PASS", "dkim_domains": ["mail.github.com"]}, {}, "x@github.com", True),
        # SES's PASS says only that ONE signature verified — any of them aligning is enough.
        ({"dkim": "PASS", "dkim_domains": ["other.test", "github.com"]}, {}, "x@github.com", True),
        # THE ATTACK: the attacker's own envelope passes SPF, `From` is forged.
        ({"spf": "PASS", "spf_domain": "attacker.net"}, {}, "owner@example.com", False),
        # An ALIGNED envelope (a bounce subdomain) is fine.
        ({"spf": "PASS", "spf_domain": "bounce.example.com"}, {}, "owner@example.com", True),
        # A DKIM PASS for an unrelated signing domain proves nothing about From.
        ({"dkim": "PASS", "dkim_domains": ["github.com"]}, {}, "owner@example.com", False),
        # A PASS with no identity to align against is not an authentication.
        ({"spf": "PASS", "dkim": "PASS"}, {}, "owner@example.com", False),
        # An identity present but the verdict absent proves nothing either.
        ({"dkim_domains": ["example.com"], "spf_domain": "example.com"}, {}, "owner@example.com", False),
        # No signal at all.
        ({}, {}, "owner@example.com", False),
        ({"dmarc": "GRAY"}, {}, "owner@example.com", False),
        # Junk input must be False, never an exception.
        (None, None, "owner@example.com", False),
        ({"dkim": "PASS", "dkim_domains": "github.com"}, {}, "x@github.com", False),
        ({"dmarc": "PASS"}, {}, "", True),
        ({"dkim": "PASS", "dkim_domains": ["github.com"]}, {}, "", False),
    ],
)
def test_authenticated_sender(verdicts, headers, sender, expected):
    assert inbound.authenticated_sender(verdicts, headers, sender) is expected


def test_forged_authentication_results_header_cannot_authenticate():
    """REGRESSION: an attacker with a valid DKIM signature for their OWN domain
    can also paste `Authentication-Results: … header.d=github.com` into the
    message. The gate must read only the Lambda-derived signer domains."""
    assert (
        inbound.authenticated_sender(
            {"dkim": "PASS", "dkim_domains": ["attacker.net"]},
            _FORGED_AUTHRES,
            "x@github.com",
        )
        is False
    )
    # ...and the same header alongside the REAL signer still wakes.
    assert (
        inbound.authenticated_sender(
            {"dkim": "PASS", "dkim_domains": ["github.com"]},
            _FORGED_AUTHRES,
            "x@github.com",
        )
        is True
    )


def test_forged_return_path_header_cannot_authenticate():
    """Same for the envelope: `Return-Path` in the MIME is whatever the sender
    typed. Only `verdicts["spf_domain"]` — SES's receipt — counts."""
    assert (
        inbound.authenticated_sender(
            {"spf": "PASS", "spf_domain": "attacker.net"},
            {"Return-Path": "<bounce@example.com>"},
            "owner@example.com",
        )
        is False
    )


@pytest.mark.parametrize(
    "candidate,sender_domain,expected",
    [
        # THE FINDING: last-two-labels aligned these; a public suffix does not.
        ("attacker.co.uk", "victim.co.uk", False),
        ("a.attacker.co.uk", "victim.co.uk", False),
        ("mail.github.com", "github.com", True),
        ("github.com", "mail.github.com", True),
        ("github.com", "github.com", True),
        ("evil.com", "github.com", False),
        # A bare public suffix is never an organization.
        ("a.co.uk", "co.uk", False),
        ("x.com", "com", False),
        # Fail closed on empty/invalid.
        ("", "github.com", False),
        ("github.com", "", False),
        ("", "", False),
    ],
)
def test_relaxed_alignment(candidate, sender_domain, expected):
    assert inbound._aligned(candidate, sender_domain) is expected
