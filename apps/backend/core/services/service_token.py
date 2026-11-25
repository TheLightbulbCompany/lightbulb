"""The HMAC signing key for Spark's signed checkout and portal links (``os_billing``)."""

from __future__ import annotations

import os


def _signing_key() -> str:
    key = os.environ.get("PAPERCLIP_SERVICE_TOKEN_KEY")
    if not key:
        raise RuntimeError("PAPERCLIP_SERVICE_TOKEN_KEY env var not set")
    return key
