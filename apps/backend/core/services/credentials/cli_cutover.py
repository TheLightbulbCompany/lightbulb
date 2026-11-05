"""Move an X / QuickBooks / Instagram connection to backend custody and back (spec 13 §6.2).

Fences, the ChatGPT cutover's (``token_custody.session_idle`` outside ``owner_lifecycle_guard``,
then ``fenced_session`` inside it): backend writers wait on the guard; the VM writer is fenced by
an idle session, stopped, with its cold-wake lease held, so no session can start and rotate the
grant while we move it. The VM's newest copy of the CLI file is the newest grant (the bridge
writes one rotation to every profile), so it is what moves into Secrets Manager. Then the row
flips and the projection rewrites every profile in the access-token-free form.

Results: ``done`` | ``already`` | ``busy_session`` | ``raced`` | ``no_grant``. Logs carry ids and
results only, never a value from the file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import yaml
from botocore.exceptions import ClientError

from core.config import settings
from core.repositories import integrations_repo
from core.services import owner_lifecycle, token_custody
from core.services.credentials import hermes_cli, integration_secret_store, registry
from core.dynamodb import run_in_thread

logger = logging.getLogger(__name__)

#: The CLI file per provider, relative to a profile root.
_FILE = {
    "x": f"{hermes_cli.HOME_DIR}/.xurl",
    "quickbooks": f"{hermes_cli.HOME_DIR}/quickbooks/.env",
    "instagram": "skills/instagram/credentials.json",
}


def _s3():
    import boto3

    return boto3.client("s3")


def _newest_copy(owner_id: str, rel: str) -> bytes | None:
    """The newest ``<owner>/hermes/*/<rel>`` by LastModified. Sync."""
    s3, prefix = _s3(), f"{owner_id}/hermes/"
    newest: dict | None = None
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.OWNER_STATE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/" + rel) and (newest is None or obj["LastModified"] > newest["LastModified"]):
                newest = obj
    if newest is None:
        return None
    return s3.get_object(Bucket=settings.OWNER_STATE_BUCKET, Key=newest["Key"])["Body"].read()


def _credential(provider: str, raw: bytes) -> dict[str, Any] | None:
    """The VM file as a credential. QuickBooks keeps no client secret: refresh reads the
    platform app's own store (``oauth_dance._oauth_client``)."""
    if provider == "x":
        app = ((yaml.safe_load(raw) or {}).get("apps") or {}).get("lightbulb-x") or {}
        user = app.get("default_user") or ""
        tok = (((app.get("oauth2_tokens") or {}).get(user) or {}).get("oauth2")) or {}
        if not tok.get("refresh_token"):
            return None
        exp = int(tok.get("expiration_time") or 0)
        return {
            "client_id": app.get("client_id") or "",
            "client_secret": app.get("client_secret") or "",
            "redirect_uri": app.get("redirect_uri") or "",
            "username": user,
            "access_token": tok.get("access_token") or "",
            "refresh_token": tok["refresh_token"],
            "token_expiry": datetime.fromtimestamp(exp, timezone.utc).isoformat() if exp else "",
        }
    if provider == "quickbooks":
        env = dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line)
        if not env.get("QUICKBOOKS_REFRESH_TOKEN"):
            return None
        return {
            "refresh_token": env["QUICKBOOKS_REFRESH_TOKEN"],
            "realm_id": env.get("QUICKBOOKS_REALM_ID", ""),
            "access_token": "",
        }
    data = json.loads(raw)
    return data if data.get("access_token") else None


async def cli_cutover(owner_id: str, provider: str) -> str:
    p = registry.get_provider(provider)
    if not p.custody_migratable:
        raise ValueError(f"{provider} is not custody-migratable")
    row = await integrations_repo.get(owner_id, provider, consistent=True)
    if not row or row.get("status", "connected") != "connected":
        return "no_grant"
    if row.get("custody") == "backend":
        return "already"
    if not await token_custody.session_idle(owner_id):
        return "busy_session"
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        async with token_custody.fenced_session(owner_id) as fenced:
            if not fenced:
                return "busy_session"
            raw = await run_in_thread(_newest_copy, owner_id, _FILE[provider])
            credential = _credential(provider, raw) if raw else None
            if not credential:
                return "no_grant"
            credential["custody"] = "backend"
            await integration_secret_store.put(owner_id, provider, credential)
            try:
                await integrations_repo.set_custody(owner_id, provider, "backend", expected=None)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                return "raced"
            await hermes_cli.project(owner_id, provider, p.wiring.materializer, credential)
    logger.info("cli cutover owner=%s provider=%s result=done", owner_id, provider)
    return "done"


async def cli_rollback(owner_id: str, provider: str) -> str:
    """Back to VM custody: materialize the backend's newest grant in the legacy form (with the
    refresh token), then flip the row. The secret stays; disconnect deletes it as always."""
    p = registry.get_provider(provider)
    row = await integrations_repo.get(owner_id, provider, consistent=True)
    if not row or row.get("custody") != "backend":
        return "already"
    if not await token_custody.session_idle(owner_id):
        return "busy_session"
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        async with token_custody.fenced_session(owner_id) as fenced:
            if not fenced:
                return "busy_session"
            credential = await integration_secret_store.get(owner_id, provider)
            if not credential:
                return "no_grant"
            legacy = {k: v for k, v in credential.items() if k != "custody"}
            await hermes_cli.project(owner_id, provider, p.wiring.materializer, legacy)
            try:
                await integrations_repo.set_custody(owner_id, provider, "actor", expected="backend")
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                return "raced"
    logger.info("cli rollback owner=%s provider=%s result=done", owner_id, provider)
    return "done"
