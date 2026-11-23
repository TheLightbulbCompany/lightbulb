"""Inbound mail helpers: sender parsing, sender authentication, loop guard, the stranger counter key.

Shared by `routers/ses_inbound.py` (the SES ingest endpoint), `meeting_invites` (is this invite from who it
says it is) and `routers/photon_inbound.py` (`stranger_key`).
"""

import hashlib
import logging
from email.utils import parseaddr

logger = logging.getLogger(__name__)


def stranger_key(sender: str) -> str:
    """Stable, address-free key for one sender's counter (it becomes a DynamoDB map key)."""
    return hashlib.sha256((sender or "").strip().lower().encode()).hexdigest()[:16]


# Header-based loop guard: values that mark a message as machine-generated
# (autoresponders, mailing lists, bulk senders) rather than a human reply.
# Case-insensitive on both header name and value — mail clients/relays are
# inconsistent about casing on both.
_AUTO_SUBMITTED_HEADER = "auto-submitted"
_PRECEDENCE_HEADER = "precedence"
_PRECEDENCE_BLOCK_VALUES = {"bulk", "list", "junk", "auto_reply"}


def parse_from_address(raw_from: str) -> str:
    """Extract a bare, lowercased, trimmed email address from a `From` header.

    Handles both `"Acme <a@b.com>"` and bare `"a@b.com"` forms via
    `email.utils.parseaddr`.
    """
    _, addr = parseaddr(raw_from)
    return addr.strip().lower()


def is_auto_submitted(headers: dict[str, str]) -> bool:
    """Loop guard: True if this message looks machine-generated.

    True when the `Auto-Submitted` header is present and not `"no"`, OR the
    `Precedence` header is one of bulk/list/junk/auto_reply. Both header
    keys and values are matched case-insensitively. Non-dict input (e.g. a
    malformed fetch payload) is treated as "not auto-submitted" rather than
    raising.
    """
    if not isinstance(headers, dict):
        return False

    lowered = {k.lower(): v for k, v in headers.items()}

    auto_submitted = lowered.get(_AUTO_SUBMITTED_HEADER)
    if auto_submitted is not None and auto_submitted.strip().lower() != "no":
        return True

    precedence = lowered.get(_PRECEDENCE_HEADER)
    if precedence is not None and precedence.strip().lower() in _PRECEDENCE_BLOCK_VALUES:
        return True

    return False


def _domain_of(value: str) -> str:
    """Bare lowercased domain from an address, an `@domain`, or a bare domain."""
    v = (value or "").strip().strip("<>").strip().lower()
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    return v.strip().strip(".")


# Multi-label public suffixes under which two domains are NOT the same
# organization. Hand-picked, not the Public Suffix List — see `_aligned`.
_MULTI_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "com.au",
        "co.jp",
        "com.br",
        "co.nz",
        "co.za",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.mx",
        "co.in",
        "com.sg",
    }
)


def _aligned(candidate: str, sender_domain: str) -> bool:
    """Relaxed alignment: same domain, or one a subdomain of the other.

    A CONSERVATIVE APPROXIMATION of DMARC's organizational-domain rule, not
    the Public Suffix List (which would need a dependency and a refreshed
    suffix database). Two domains align when they are EQUAL, or when the
    shorter one has at least two labels, is not a known multi-label public
    suffix (`co.uk`, `com.au`, …), and the longer one is a subdomain of it.

    That errs toward FALSE NEGATIVES — a real sender under an unlisted
    multi-label suffix, or one signing from a sibling domain, is merely
    ``stored_not_woken`` rather than woken — and never toward a false
    POSITIVE for the listed suffixes: `attacker.co.uk` does not align with
    `victim.co.uk`, which the old last-two-labels key wrongly did.

    Fails closed on empty/invalid input. DMARC=PASS is the strong path; this
    only decides the no-DMARC-verdict fallback.
    """
    a = _domain_of(candidate)
    b = _domain_of(sender_domain)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len([label for label in shorter.split(".") if label]) < 2:
        return False
    if shorter in _MULTI_LABEL_PUBLIC_SUFFIXES:
        return False
    return longer.endswith("." + shorter)


def authenticated_sender(verdicts: dict, headers: dict, sender: str) -> bool:
    """True when the parsed `From` (``sender``) is actually authenticated.

    This gates WAKING the agent, not storing the mail. A bare `spf=PASS` is
    NOT enough on its own: SPF authenticates the ENVELOPE sender, and DKIM can
    carry a valid signature for a domain unrelated to `From` — so an attacker
    sending from their own SPF-passing domain can forge an allowlisted `From`
    and, without this check, wake somebody else's agent.

    - ``dmarc == PASS`` is sufficient: DMARC *is* the From-alignment check.
    - With no DMARC verdict, either authenticated identity is accepted only
      when it ALIGNS with the sender's domain (relaxed, see ``_aligned``):
      ``verdicts["dkim_domains"]`` (the `d=` tags of the signatures that
      each verified on their own in the Lambda) or ``verdicts["spf_domain"]`` (SES's envelope MAIL FROM).
    - Anything else (including no verdicts at all) is False.

    ``headers`` IS DELIBERATELY IGNORED and stays only so callers keep the
    payload's shape at this seam. Both identities come from ``verdicts``,
    which the SES-ingest Lambda derives from SES's own receipt and from the
    signed `DKIM-Signature` — never from a header. An attacker holding a
    valid signature for `attacker.net` can also paste a forged
    `Authentication-Results: … dkim=pass header.d=github.com` into the
    message, so no header may be an authentication source here.
    """
    if not isinstance(verdicts, dict):
        return False

    def _verdict(name: str) -> str:
        return str(verdicts.get(name) or "").strip().upper()

    if _verdict("dmarc") == "PASS":
        return True

    sender_domain = _domain_of(sender)
    if not sender_domain:
        return False

    if _verdict("dkim") == "PASS":
        dkim_domains = verdicts.get("dkim_domains")
        if isinstance(dkim_domains, (list, tuple)) and any(_aligned(str(d), sender_domain) for d in dkim_domains):
            return True

    if _verdict("spf") == "PASS" and _aligned(str(verdicts.get("spf_domain") or ""), sender_domain):
        return True

    return False
