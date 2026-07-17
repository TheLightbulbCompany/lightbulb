"""License key lifecycle: generation, validation, rate limiting, revocation.

Per design doc: license keys are the auth primitive for both CLI installs and
hosted MCP sessions. Format `iml_<32-char-base32>` (160 bits of entropy).
Stored on each marketplace_purchases row; rotated by issuing a new purchase
(reserved for genuine compromise).
"""

import base64
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from core.repositories import marketplace_repo


# 20 bytes = 160 bits, base32-encoded → 32 chars (drop padding).
_KEY_BODY_LEN = 20


def generate() -> str:
    """Generate a new license key. iml_<32-char-base32>, 160 bits of entropy."""
    raw = secrets.token_bytes(_KEY_BODY_LEN)
    body = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return f"iml_{body}"


@dataclass
class ValidationResult:
    status: Literal["valid", "revoked", "rate_limited", "not_found"]
    listing_id: str | None = None
    listing_version: int | None = None
    entitlement_version_floor: int | None = None
    reason: str | None = None


async def validate(*, license_key: str, source_ip: str) -> ValidationResult:
    """Validate a license key for an install attempt.

    Rate limit: 10 unique source IPs per 24 hours per license. Same IP
    repeated is fine (CI/dev workflows reinstall many times).
    """
    if not license_key.startswith("iml_"):
        return ValidationResult(status="not_found")

    purchase = await marketplace_repo.find_purchase_by_license_key(license_key)
    if purchase is None:
        return ValidationResult(status="not_found")

    if purchase.get("license_key_revoked"):
        return ValidationResult(
            status="revoked",
            reason=purchase.get("license_key_revoked_reason"),
        )

    # Rate-limit window = 24h.
    now = int(time.time())
    window_start = now - 24 * 60 * 60
    install_log = purchase.get("install_log", [])
    recent = [e for e in install_log if e.get("ts", 0) >= window_start]
    unique_ips = {e["ip"] for e in recent}
    if source_ip not in unique_ips and len(unique_ips) >= 10:
        return ValidationResult(status="rate_limited")

    # Record this install before returning so the rate-limit window actually
    # advances. Without this, install_log never grows and the 10-unique-IPs
    # cap is a no-op. Recorded inside validate() so callers don't have to
    # remember to invoke record_install separately. Records the (ip, ts)
    # used for THIS validate call, not a future one.
    await record_install(
        purchase_id=purchase["purchase_id"],
        buyer_id=purchase["buyer_id"],
        source_ip=source_ip,
    )

    return ValidationResult(
        status="valid",
        listing_id=purchase["listing_id"],
        listing_version=purchase["listing_version_at_purchase"],
        entitlement_version_floor=purchase.get("entitlement_version_floor", purchase["listing_version_at_purchase"]),
    )


async def revoke(*, purchase_id: str, buyer_id: str, reason: str) -> None:
    """Mark license_key_revoked + reason on a purchase row."""
    await marketplace_repo.set_license_revoked(buyer_id=buyer_id, purchase_id=purchase_id, reason=reason)


async def record_install(*, purchase_id: str, buyer_id: str, source_ip: str) -> None:
    """Append the install IP+timestamp to purchase's install_log."""
    await marketplace_repo.append_install(buyer_id=buyer_id, purchase_id=purchase_id, source_ip=source_ip)
