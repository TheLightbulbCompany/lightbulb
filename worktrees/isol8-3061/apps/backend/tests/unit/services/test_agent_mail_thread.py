"""Thread identity for inbound agent email (agent_mail_thread).

The behaviour under test is what makes a reply a CONTINUATION rather than a
cold start: the same mail thread must always produce the same session key, and
a thread we cannot identify must fall back to the old throwaway session rather
than to a key that the next message in that thread could not re-derive.
"""

import re

import pytest

from core.services.agent_mail_thread import derive_thread_root, thread_session_key

ROOT = "<0100019fab803ac1-aaaa@email.amazonses.com>"
PARENT = "<CAGOuwMXqiVpBRrMowW6@mail.gmail.com>"
SELF = "<self-1234@mail.gmail.com>"


def test_references_root_wins_over_in_reply_to():
    """References is ordered oldest-first, so References[0] IS the thread root.
    In-Reply-To is only the immediate parent — using it would make the key
    move on every exchange, splitting one conversation into several."""
    headers = {"References": f"{ROOT} {PARENT}", "In-Reply-To": PARENT, "Message-ID": SELF}
    assert derive_thread_root(headers) == ROOT.strip("<>").lower()


def test_falls_back_to_in_reply_to_when_references_absent():
    headers = {"In-Reply-To": PARENT, "Message-ID": SELF}
    assert derive_thread_root(headers) == PARENT.strip("<>").lower()


def test_fresh_inbound_is_its_own_root():
    """Mail that is not a reply has no ancestry — it starts a thread."""
    assert derive_thread_root({"Message-ID": SELF}) == SELF.strip("<>").lower()


def test_header_lookup_is_case_insensitive():
    """Mail headers are case-insensitive and both spellings of Message-ID have
    reached us in practice."""
    assert derive_thread_root({"message-id": SELF}) == SELF.strip("<>").lower()
    assert derive_thread_root({"references": f"{ROOT} {PARENT}"}) == ROOT.strip("<>").lower()


def test_no_identifying_header_yields_no_root():
    assert derive_thread_root({}) == ""
    assert derive_thread_root(None) == ""
    assert derive_thread_root({"Subject": "hi"}) == ""


# --- session key ----------------------------------------------------------


def test_same_thread_yields_the_same_key_regardless_of_position():
    """The whole point: every message in one thread hits one session.

    A later reply carries a LONGER References chain, but the root is unchanged,
    so the key must be identical.
    """
    first = thread_session_key({"References": ROOT, "Message-ID": PARENT})
    later = thread_session_key({"References": f"{ROOT} {PARENT} {SELF}", "Message-ID": "<x@y>"})
    assert first == later


def test_different_threads_get_different_keys():
    a = thread_session_key({"Message-ID": "<a@x>"})
    b = thread_session_key({"Message-ID": "<b@x>"})
    assert a != b


def test_key_carries_the_mail_prefix():
    key = thread_session_key({"Message-ID": SELF})
    assert key is not None
    assert key.startswith("lightbulb-email:")


@pytest.mark.parametrize(
    "message_id",
    [
        "<agent:other:lightbulb:channel:room-1@x>",
        "<paperclip:company:c1:agent:a1:issue:i1@x>",
        "<lightbulb-email:deadbeefdeadbeef@x>",
        "<../../etc/passwd@x>",
        "<" + "A" * 4000 + "@x>",
    ],
    ids=["room-key", "paperclip-key", "another-thread", "traversal", "huge"],
)
def test_a_sender_cannot_steer_the_key_out_of_the_mail_namespace(message_id):
    """The bridge takes ``sessionKey`` VERBATIM (``turn._resolve_session_key``), so the shape
    of this key is the whole boundary — and it is guaranteed by construction, not by an
    allowlist: the sender's Message-ID is the SHA-256 input, never interpolated.

    This replaces the container's ``hooks.allowedSessionKeyPrefixes``, which bounded the
    OpenClaw gateway's own RPC surface (a hook-token holder naming a ``sessionKey`` on
    ``chat.send``) and has no Hermes equivalent. Nothing reachable from outside the backend can
    name a session any more; this test pins the one place a sender's bytes get near one.
    """
    key = thread_session_key({"Message-ID": message_id})
    assert key is not None
    assert re.fullmatch(r"lightbulb-email:[0-9a-f]{16}", key), key


def test_key_is_hashed_not_the_raw_message_id():
    """Session keys are lowercased upstream and a raw Message-ID carries <>, @
    and arbitrary punctuation — embedding it would be fragile."""
    key = thread_session_key({"Message-ID": SELF})
    assert key is not None
    assert "@" not in key
    assert "self-1234" not in key


def test_unidentifiable_thread_returns_none_not_a_made_up_key():
    """None means "let the container mint its own throwaway session" — the old
    behaviour. Inventing a key here would be worse than the cold start: the
    next message in the same thread could not re-derive it, so it would look
    like a session while never actually being resumed."""
    assert thread_session_key({}) is None
    assert thread_session_key(None) is None


def test_angle_brackets_and_case_do_not_fork_a_thread():
    """The same root written differently by two clients must not split."""
    a = thread_session_key({"Message-ID": "<Abc@Example.COM>"})
    b = thread_session_key({"Message-ID": "abc@example.com"})
    assert a == b
