"""Spark (the Mac app) product events for PostHog, through the server-side bridge in `product_analytics`.

`APP_EVENTS` is the one allow-list for what the app may send to `POST /os/event`: event name, then each
property key with its validator. No validator accepts free text: a string is an enum or a strict pattern, so
nothing a person spoke or typed, no screen content, file name or URL can reach PostHog. `server_event` is the
same bridge for events our own code emits; it never raises."""

import logging
import re
import uuid
from collections.abc import Callable

from core.observability import product_analytics

logger = logging.getLogger(__name__)

PRODUCT = "spark"
VERSION = re.compile(r"[0-9]{1,4}(\.[0-9]{1,4}){0,3}([-+][0-9A-Za-z.]{1,16})?")
# An OS version is dotted numbers, each up to five digits: macOS 26.2, Windows 10.0.26100.
OS_VERSION = re.compile(r"[0-9]{1,5}(\.[0-9]{1,5}){0,3}")
DL_ID = re.compile(r"[0-9a-f]{16}")  # the website's download id, claimed by the app on first launch
NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:spark")


def _enum(*values: str) -> Callable[[object], bool]:
    return lambda v: isinstance(v, str) and v in values


def _match(rx: re.Pattern) -> Callable[[object], bool]:
    return lambda v: isinstance(v, str) and rx.fullmatch(v) is not None


def _bool(v: object) -> bool:
    return type(v) is bool


def _seconds(v: object) -> bool:
    return type(v) is int and 0 <= v <= 86400


_GRANT = _enum("ok", "denied", "unknown")
_METHOD = _enum("apple", "google", "email")
_PROVIDER = _enum("chatgpt", "claude")
_PROBLEM_KIND = _enum(
    "permission.accessibility",
    "permission.screenRecording",
    "permission.microphone",
    "permission.fullDiskAccess",
    "permission.other",
    "brain.unavailable",
    "voice.limit",
    "voice.connection",
    "hands.helper",
    "disk.low",
    "dock",
    "daemon",
    "crash",
    "other",
    "automation.missed",
    "automation.blocked",
    "automation.budget",
    "automation.notifications",
    "automation.watch",
    "automation.failed",
    "app.version",
    "account",
)  # keep equal to ProblemKind in the Spark engine's protocol package

APP_EVENTS: dict[str, dict[str, Callable[[object], bool]]] = {
    "app_first_launch": {"dl_id": _match(DL_ID)},
    "app_opened": {},
    "signin_started": {"method": _METHOD},
    "signin_completed": {"method": _METHOD, "is_new_account": _bool},
    "brain_signin_started": {"provider": _PROVIDER, "needed_download": _bool},
    "brain_chosen": {"provider": _PROVIDER},
    "permission_granted": {"permission": _enum("microphone", "accessibility", "screen_recording")},
    "full_access_set": {"on": _bool},
    "onboarding_completed": {"skipped_sign_in": _bool, "seconds_total": _seconds},
    "first_conversation": {},
    "first_tidy": {"outcome": _enum("done", "already_tidy", "declined", "stopped", "failed")},
    "task_completed": {
        "source": _enum("voice", "typed", "automation"),
        "seconds_bucket": _enum("lt10", "lt30", "lt120", "lt600", "gte600"),
    },
    "health": {
        "brain": _enum("ok", "missing", "starting"),
        "mic": _enum("ok", "denied", "none"),
        "accessibility": _GRANT,
        "screen": _GRANT,
        "last_success": _enum("none", "lt1d", "lt7d", "gte7d"),
    },
    "talk_started": {"trigger": _enum("hold", "hands_free", "wake")},
    "task_failed": {"reason": _enum("auth", "quota", "rate", "unreachable", "signed_out", "other")},
    "problem_raised": {"kind": _PROBLEM_KIND},
    "upgrade_clicked": {},
    "manage_subscription_clicked": {},
    "referral_link_copied": {},
    "update_installed": {"from_version": _match(VERSION), "to_version": _match(VERSION)},
}


# The fixed fields every app event body may carry: body key, then the property it becomes and its validator.
COMMON: dict[str, tuple[str, Callable[[object], bool]]] = {
    "appVersion": ("app_version", _match(VERSION)),
    "osVersion": ("os_version", _match(OS_VERSION)),
    "platform": ("platform", _enum("mac", "windows")),
}


def common_properties(body: dict) -> dict:
    """`app_version`, `os_version` and `platform` from the body, each only if it passes."""
    return {prop: body[key] for key, (prop, ok) in COMMON.items() if ok(body.get(key))}


def allowed_properties(event: str, properties: object) -> dict:
    """The properties of an allow-listed event that pass their validators; everything else is dropped."""
    rules = APP_EVENTS[event]
    if not isinstance(properties, dict):
        return {}
    return {k: v for k, v in properties.items() if k in rules and rules[k](v)}


def facts(row: dict, now: int) -> dict:
    """The person's current Spark facts for PostHog's `$set`: never an email or a name."""
    from core.services import os_billing

    return {
        "spark_status": os_billing.status(row, now),
        "spark_trial_ends_at": int(row["os_trial_ends_at"]),
        "spark_referrals": int(row.get("os_referrals", 0)),
    }


def server_event(
    user_id: str, event: str, key: str, properties: dict | None = None, row: dict | None = None, now: int = 0
) -> None:
    """One server-side Spark event on the person, with `facts(row, now)` set on them when a row is given.
    `key` makes the event uuid deterministic, so a retried request or a redelivered webhook is dropped by
    PostHog. Never raises."""
    try:
        product_analytics.capture_product_event(
            distinct_id=user_id,
            event=event,
            properties={**(properties or {}), "product": PRODUCT},
            set_person=facts(row, now) if row is not None else None,
            event_uuid=str(uuid.uuid5(NS, f"{event}:{key}")),
        )
    except Exception:  # noqa: BLE001  analytics never fails the caller
        logger.warning("spark event %s not captured", event, exc_info=True)
