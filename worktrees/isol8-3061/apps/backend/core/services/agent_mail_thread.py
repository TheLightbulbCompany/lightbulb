"""Thread identity for inbound agent email.

Every inbound email used to land in a brand-new OpenClaw session, so replying
to an agent was not a conversation: each message was a cold start. The agent
only ever *appeared* to follow a thread because Gmail quotes the previous
message into the body — it was re-reading its own earlier mail as input, never
as memory. Strip the quoting and continuity vanished.

This module derives a STABLE session key from the mail thread itself, so every
message in one thread reaches the same session.

Thread root, in RFC 5322 terms:

* ``References`` is the ancestry, ordered oldest-first, so ``References[0]``
  IS the thread root and is the identity we want.
* ``In-Reply-To`` is only the immediate parent — a weaker signal, used when
  ``References`` is absent (some clients omit it).
* A message with neither is itself a root (a fresh inbound, not a reply), so
  its own ``Message-ID`` becomes the identity.

Note the root is only stable because outbound replies emit the FULL chain
(``agent_mail_reply.build_references``). While replies overwrote ``References``
with just the parent, the root moved on every exchange and any key derived
from it would have split one conversation into several.

The key is hashed rather than embedding the Message-ID: session keys are lowercased upstream,
and a raw Message-ID carries ``<>``, ``@`` and arbitrary punctuation.

**What bounds this key.** Its shape is guaranteed by CONSTRUCTION, not by an allowlist: this
module is the only producer of a ``lightbulb-email:`` key, it appends 16 hex chars of a SHA-256
digest to a constant prefix, and the sender's Message-ID is the digest INPUT — never
interpolated. So an inbound mail cannot name a session; it can only pick which hash it lands
on, within its own thread.

It used to also have to sit inside the container's ``hooks.allowedSessionKeyPrefixes``. That
allowlist bounded the OpenClaw gateway's own RPC surface, where a holder of the owner's hook
token could pass a ``sessionKey`` of its choosing to ``chat.send``. There is no such surface
now: the ingress (``/plugins/lightbulb-channel/inbound/<agentId>``) is reachable only through
``InvokeAgentRuntime`` with the backend's own IAM role, and is gated on the GATEWAY token, not
the hook token. The bridge takes ``sessionKey`` verbatim (``turn._resolve_session_key``), which
is exactly why the backend must never derive one from an untrusted string.

The ONE door that still accepts a caller-supplied session key is the native Paperclip facade's
``X-Hermes-Session-Key`` (``routers/messaging.py``), and it is bounded harder than this ever
was: ``_HERMES_SESSION_RE`` pins the exact shape, and ``_verify_paperclip_hermes_identity``
re-checks the key's ``companyId`` against the authenticated owner's canonical
``paperclip-companies`` row on every call.
"""

import hashlib

#: Constant. Every key this module emits is this prefix plus a hex digest, so no caller can
#: steer the key outside the mail namespace (see the module docstring on what bounds it).
_PREFIX = "lightbulb-email:"

# 16 hex chars of SHA-256. Collisions would merge two unrelated threads into
# one session; 64 bits is far past what a single owner's mail volume can reach.
_DIGEST_CHARS = 16


def _first_message_id(value: str) -> str:
    """The first message-id token in a header value, or "".

    ``References`` is a whitespace-separated list ordered oldest-first, so the
    first token is the thread root. Tolerates folded headers (the caller may
    hand us embedded newlines) since splitting on any whitespace is enough.
    """
    for token in (value or "").split():
        if token:
            return token
    return ""


def derive_thread_root(headers: dict[str, str] | None) -> str:
    """The thread's root message-id, normalized. "" when nothing identifies it.

    Resolution order is deliberate: ``References[0]`` (true root) beats
    ``In-Reply-To`` (immediate parent only) beats this message's own
    ``Message-ID`` (it is itself a root).
    """
    h = headers or {}

    def get(name: str) -> str:
        # Mail headers are case-insensitive; SES/our Lambda have delivered
        # both spellings of Message-ID historically, so never trust one case.
        lowered = name.lower()
        for key, value in h.items():
            if isinstance(key, str) and key.lower() == lowered and isinstance(value, str):
                return value
        return ""

    root = (
        _first_message_id(get("References"))
        or _first_message_id(get("In-Reply-To"))
        or _first_message_id(get("Message-ID"))
    )
    # Strip the angle brackets RFC 5322 wraps msg-ids in, and case-fold: the
    # same thread must hash identically no matter which client sent it.
    return root.strip().strip("<>").strip().lower()


def thread_session_key(headers: dict[str, str] | None) -> str | None:
    """Stable per-thread session key, or None when no thread identity exists.

    None means "let the container generate its own throwaway session" — the
    prior behaviour, which is the right fallback for mail carrying no usable
    message-id at all rather than inventing a key that cannot be re-derived
    from the next message in the same thread.
    """
    root = derive_thread_root(headers)
    if not root:
        return None
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{_PREFIX}{digest}"
