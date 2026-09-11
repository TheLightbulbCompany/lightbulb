"""sessions.list ownership filter for the authenticated runtime RPC caller."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.runtime_rpc import _filter_sessions_for_user  # noqa: E402

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_keeps_own_general_and_named_drops_others():
    result = {
        "sessions": [
            {"key": "agent:luna:user_1"},  # own General (3-part)
            {"key": f"agent:luna:user_1:{_UUID}"},  # own named (4-part)
            {"key": "agent:luna:user_2"},  # another org member
            {"key": "agent:luna:paperclip"},  # paperclip
            {"key": "agent:luna:cron:job1"},  # cron
            {"key": "agent:luna:user_10"},  # prefix-collision guard: must NOT match user_1
        ]
    }
    out = _filter_sessions_for_user(result, "user_1")
    assert [s["key"] for s in out["sessions"]] == [
        "agent:luna:user_1",
        f"agent:luna:user_1:{_UUID}",
    ]


def test_case_insensitive_keeps_own_rows():
    # OpenClaw lowercases the whole sessionKey, so the rows carry a lowercased
    # user_id while the caller's Clerk id retains its original mixed case. Both
    # the caller's own General (3-part) and named (4-part) rows must be kept.
    result = {
        "sessions": [
            {"key": "agent:luna:user_abc"},  # own General, lowercased by OpenClaw
            {"key": f"agent:luna:user_abc:{_UUID}"},  # own named, lowercased
            {"key": "agent:luna:user_xyz"},  # another member
        ]
    }
    out = _filter_sessions_for_user(result, "user_AbC")
    assert [s["key"] for s in out["sessions"]] == [
        "agent:luna:user_abc",
        f"agent:luna:user_abc:{_UUID}",
    ]


def test_non_dict_and_missing_sessions_pass_through():
    assert _filter_sessions_for_user({"other": 1}, "user_1") == {"other": 1}
    assert _filter_sessions_for_user("nope", "user_1") == "nope"  # type: ignore[arg-type]
