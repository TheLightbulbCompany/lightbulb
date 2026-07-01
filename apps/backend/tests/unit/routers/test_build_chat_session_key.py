"""build_chat_session_key: server-built key + trailing-UUID security guard."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from routers.websocket_chat import _SESSION_ID_RE, build_chat_session_key  # noqa: E402

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_legacy_key_when_no_session_id():
    # None -> legacy 3-part "General" session so pre-sessions history is preserved.
    assert build_chat_session_key("luna", "user_1", None) == "agent:luna:user_1"


def test_four_part_key_for_valid_lowercase_uuid():
    assert build_chat_session_key("luna", "user_1", _UUID) == f"agent:luna:user_1:{_UUID}"


@pytest.mark.parametrize(
    "bad",
    [
        "3F2504E0-4F89-41D3-9A0C-0305E82C3301",  # uppercase — OpenClaw case-folds keys -> alias merge
        "not-a-uuid",
        "heartbeat",  # reserved reap token
        "group",  # reserved classify token
        "dashboard",
        f"{_UUID}:thread:x",  # colon child-key fork injection
        "user_1:subagent:evil",
        "",
    ],
)
def test_rejects_non_uuid_trailing_segment(bad):
    with pytest.raises(ValueError):
        build_chat_session_key("luna", "user_1", bad)


def test_regex_is_anchored():
    assert _SESSION_ID_RE.match(_UUID)
    assert not _SESSION_ID_RE.match(f" {_UUID}")  # leading space
    assert not _SESSION_ID_RE.match(f"{_UUID}x")  # trailing junk
