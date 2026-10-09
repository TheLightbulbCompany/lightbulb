"""What to call a person we know only by an email address.

Sign in with Apple's "Hide My Email" hands us ``<random>@privaterelay.appleid.com``
and no name, so the local part is noise, not a name. Every place that falls back
from a name to an email goes through here so none of them shows that noise.
"""

NEW_MEMBER = "New member"
_RELAY_SUFFIX = "@privaterelay.appleid.com"


def is_relay_email(email: str | None) -> bool:
    return (email or "").strip().lower().endswith(_RELAY_SUFFIX)


def email_label(email: str | None) -> str:
    """The local part of an ordinary address, ``NEW_MEMBER`` for a relay, ``""`` for none."""
    if is_relay_email(email):
        return NEW_MEMBER
    return (email or "").split("@", 1)[0].strip()
