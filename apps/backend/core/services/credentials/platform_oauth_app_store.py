"""Platform-owned OAuth app clients (client_id/client_secret/signing_secret?) in Secrets Manager.

Unlike ``oauth_app_store`` (per-owner BYO/DCR clients), these are ISOL8-owned
clients shared by every owner — e.g. the reviewed Meta app that Instagram
connects run through. Stored at::

    lightbulb/<env>/integrations/platform-oauth/<name>

deliberately under the ``integrations/`` prefix so the existing task-role grant
(``lightbulb/<env>/integrations/*`` — see service-stack.ts) covers it with no IAM
change. No collision with per-owner secrets is possible: owner segments are
Clerk ids (``user_…``/``org_…``), never the literal ``platform-oauth``.

Secrets are seeded out-of-band by an operator (``scripts/set_platform_oauth_app.py``)
— the backend only ever reads. Reads are cached in-process for a few minutes:
the client of a platform app changes ~never, and the OAuth dance reads it twice
per connect (start + exchange).
"""

from __future__ import annotations

import json
import time

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread

_CACHE_TTL_SECONDS = 300
_cache: dict[str, tuple[float, dict]] = {}


def _secret_name(name: str) -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"lightbulb/{env}/integrations/platform-oauth/{name}"


def _client():
    return boto3.client("secretsmanager", region_name=settings.AWS_REGION)


def _get_sync(name: str) -> dict | None:
    try:
        resp = _client().get_secret_value(SecretId=_secret_name(name))
    except ClientError:
        # Missing or unreadable both mean "not provisioned" to the caller; the
        # dance turns None into a clear operator-facing error.
        return None
    try:
        data = json.loads(resp["SecretString"])
    except (KeyError, json.JSONDecodeError):
        return None
    if not data.get("client_id"):
        return None
    return {
        "client_id": data["client_id"],
        "client_secret": data.get("client_secret") or "",
        # Slack signs webhooks with a separate "signing secret" (Meta reuses
        # the client secret). Empty when the seeded app has none.
        "signing_secret": data.get("signing_secret") or "",
    }


async def get(name: str) -> dict | None:
    """The platform client ``{client_id, client_secret, signing_secret}``, or None if unseeded."""
    now = time.monotonic()
    hit = _cache.get(name)
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    value = await run_in_thread(_get_sync, name)
    # Only cache successful reads: a None (secret not yet seeded) must not stick
    # for 5 minutes right after an operator provisions it.
    if value is not None:
        _cache[name] = (now, value)
    return value
