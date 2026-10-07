"""Tests for parse_session_key pure function."""

import os

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.session_keys import parse_session_key  # noqa: E402


@pytest.mark.parametrize(
    "session_key,expected",
    [
        # Personal webchat
        (
            "agent:main:main",
            {"agent_id": "main", "source": "webchat"},
        ),
        # Personal webchat — per-user session (all users now use userId)
        (
            "agent:luna:user_3CEahsu949Q9",
            {"agent_id": "luna", "source": "webchat", "member_id": "user_3CEahsu949Q9"},
        ),
        # Org webchat — parts[2] is a Clerk user_id
        (
            "agent:main:user_2abc123",
            {"agent_id": "main", "source": "webchat", "member_id": "user_2abc123"},
        ),
        # Channel DM (per-account-channel-peer)
        (
            "agent:sales:telegram:sales:direct:99999",
            {
                "agent_id": "sales",
                "source": "dm",
                "channel": "telegram",
                "peer_id": "99999",
            },
        ),
        # Channel group
        (
            "agent:main:telegram:group:-1001234567890",
            {
                "agent_id": "main",
                "source": "group",
                "channel": "telegram",
                "group_id": "-1001234567890",
            },
        ),
        # Group with topic (Telegram forum)
        (
            "agent:main:telegram:group:-1001234567890:topic:42",
            {
                "agent_id": "main",
                "source": "group",
                "channel": "telegram",
                "group_id": "-1001234567890",
            },
        ),
        # Slack channel
        (
            "agent:main:slack:channel:C123ABC",
            {
                "agent_id": "main",
                "source": "channel",
                "channel": "slack",
                "channel_id": "C123ABC",
            },
        ),
        # Slack thread
        (
            "agent:main:slack:channel:C123ABC:thread:1234.5678",
            {
                "agent_id": "main",
                "source": "channel",
                "channel": "slack",
                "channel_id": "C123ABC",
            },
        ),
        # Cron chat session
        (
            "agent:main:cron:dfb80362-1cdb-4089-b778-27a5f51d765a",
            {"agent_id": "main", "source": "cron", "cron_id": "dfb80362-1cdb-4089-b778-27a5f51d765a"},
        ),
        # Cron run event (longer key)
        (
            "agent:main:cron:dfb80362-1cdb-4089-b778-27a5f51d765a:run:d89",
            {"agent_id": "main", "source": "cron", "cron_id": "dfb80362-1cdb-4089-b778-27a5f51d765a"},
        ),
        # Cron with no ID (just "cron" at position 2)
        (
            "agent:sales:cron",
            {"agent_id": "sales", "source": "cron"},
        ),
        # Paperclip issue session (openclaw_gateway adapter, strategy="issue").
        # Shape from paperclip/.../openclaw-gateway/src/server/execute.ts
        # resolveSessionKey. A real member id is always a Clerk "user_..." id,
        # never the literal "paperclip", so this marker is unambiguous.
        (
            "agent:friday:paperclip:issue:ADM-455",
            {"agent_id": "friday", "source": "paperclip", "issue_id": "ADM-455"},
        ),
        # Paperclip run session (strategy="run")
        (
            "agent:friday:paperclip:run:run-789",
            {"agent_id": "friday", "source": "paperclip", "run_id": "run-789"},
        ),
        # Paperclip bare fallback (strategy="fixed", default sessionKey="paperclip")
        (
            "agent:friday:paperclip",
            {"agent_id": "friday", "source": "paperclip"},
        ),
        # 4-part webchat named session (server-built by build_chat_session_key).
        # parts[2] = userId (member routing target), parts[3] = opaque UUID.
        (
            "agent:luna:user_1:3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            {"agent_id": "luna", "source": "webchat", "member_id": "user_1"},
        ),
        # Malformed
        ("garbage", {}),
        ("", {}),
        # Sub-agent webchat (3 parts, parts[2] == main)
        ("agent:research_subagent:main", {"agent_id": "research_subagent", "source": "webchat"}),
    ],
)
def test_parse_session_key(session_key, expected):
    result = parse_session_key(session_key)
    assert result == expected


def test_cron_session_is_not_webchat():
    """Cron sessions must not be classified as webchat — their events should
    not be forwarded to frontends (a cron chat.final would terminate an
    active user's streaming session)."""
    result = parse_session_key("agent:main:cron:dfb80362-1cdb-4089-b778-27a5f51d765a")
    assert result["source"] == "cron"
    assert result["source"] not in ("webchat", "dm", "group", "channel")
    assert "member_id" not in result


def test_paperclip_issue_session_is_not_webchat():
    """A Paperclip issue session (agent:<id>:paperclip:issue:<issueId>) must be
    classified as 'paperclip', NOT 'unknown'/'webchat'. The previous parser
    returned source='unknown' with no member_id, which the gateway fan-out
    treats as a personal session and BROADCASTS to every member of the org —
    leaking one issue's stream into every member's /chat."""
    result = parse_session_key("agent:friday:paperclip:issue:ADM-455")
    assert result["source"] == "paperclip"
    assert result["source"] not in ("webchat", "dm", "group", "channel")
    assert "member_id" not in result
    assert result["issue_id"] == "ADM-455"


def test_four_part_webchat_is_not_isolated_source():
    """The 4-part key must classify as webchat, not 'unknown' — otherwise
    _handle_message treats it as isolated and silently drops streamed frames."""
    parsed = parse_session_key("agent:luna:user_1:3f2504e0-4f89-41d3-9a0c-0305e82c3301")
    assert parsed["source"] == "webchat"
    assert parsed["source"] not in ("paperclip", "unknown")


def test_group_key_does_not_return_literal_channel_as_member_id():
    """Regression test for the pre-existing parser bug where group session
    keys wrote member:telegram:{period}. The new parser must NOT expose
    'telegram' (or any channel name) as a member_id anywhere."""
    result = parse_session_key("agent:main:telegram:group:-100123")
    assert result.get("source") == "group"
    assert "member_id" not in result


def test_hermes_native_paperclip_session_parses_as_paperclip():
    """The Hermes bridge reports Paperclip-woken runs (nightly CEO, issue runs) with the
    hermes_gateway session id, not the retired openclaw-gateway ``agent:<id>:paperclip:…`` shape.
    Unparsed, every such run lands as source=unknown: run.completed{source=paperclip} and
    nightly_ceo.run were never emitted after the Hermes cutover."""
    from core.services.session_keys import parse_session_key

    assert parse_session_key("paperclip:company:c-1:agent:a-2:issue:i-3") == {
        "agent_id": "a-2",
        "source": "paperclip",
        "issue_id": "i-3",
    }
    assert parse_session_key("paperclip:company:c-1:agent:a-2:run:r-4") == {
        "agent_id": "a-2",
        "source": "paperclip",
        "run_id": "r-4",
    }
    assert parse_session_key("paperclip:company:c-1:agent:a-2:bogus:x") == {}
