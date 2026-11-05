"""Webhook fires verified in the backend (spec 06 §3.5, contracts-v2 §11). No route of its own.

`paperclip_trigger_proxy` (same public URL `/api/v1/hooks/t/{owner}/{public_id}`) and `meta_webhook` hand a
delivery here when the workspace is on the super-agent switch and the trigger has a `HOOK#` row; everything
else keeps going to Paperclip unchanged.

Order: entitlement 402 → `HOOK#` (the key binds the public id to this owner) 404 → rate 429 → Google `sync` /
GitHub `ping` 204 → signature 401 → event filter 200 `ignored` → dedupe key → raw body to S3 →
`TriggerFireWorkflow` with id `hook:{ws}:{public_id}:{key}` (REJECT_DUPLICATE) → 202, or 200 `duplicate`.
Temporal unreachable is 503: the provider retries, so it is the durable queue for this path.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import boto3
from fastapi import Request, Response
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.common import WorkflowIDReusePolicy

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import automations_repo, billing_repo
from core.services.credentials import integration_secret_store
from core.services.hook_signatures import dedupe_key, event_type, verify, verify_google_oidc
from core.services.provision_gate import has_always_on_entitlement
from core.services.trigger_fire_url import trigger_fire_url
from core.temporal.automations import FireInput
from core.temporal.client import get_client
from routers._public_ingress import _check_rate_limit

_SECRET_TTL_S = 60
# (owner, secret name) -> (secret dict or None, fetched at). Rotation shows up within a minute.
_SECRET_CACHE: dict[tuple[str, str], tuple[dict | None, float]] = {}


def _json(status: int, body: dict, headers: dict | None = None) -> Response:
    return Response(
        status_code=status, content=json.dumps(body).encode(), media_type="application/json", headers=headers
    )


async def _secret(owner_id: str, name: str) -> dict | None:
    hit = _SECRET_CACHE.get((owner_id, name))
    if hit and time.monotonic() - hit[1] < _SECRET_TTL_S:
        return hit[0]
    value = await integration_secret_store.get(owner_id, name)
    _SECRET_CACHE[(owner_id, name)] = (value, time.monotonic())
    return value


def _bearer(value: str | None) -> str | None:
    scheme, _, token = (value or "").partition(" ")
    return token if scheme.lower() == "bearer" and token else None


async def _check(owner_id: str, public_id: str, hook: dict, headers, body: bytes) -> str | None:
    """None when the delivery is genuine, else the 401 reason."""
    if hook["signingMode"] == "google_oidc":  # Gmail Pub/Sub push: a per-delivery Google OIDC JWT
        ok = await asyncio.to_thread(
            verify_google_oidc, _bearer(headers.get("authorization")), trigger_fire_url(owner_id, public_id)
        )
        return None if ok else "bad_signature"
    secret = await _secret(owner_id, hook["secretRef"])
    if not hook.get("armed") or not secret or not secret.get("secret"):
        return "not_armed"
    return verify(
        hook["signingMode"],
        secret["secret"],
        headers,
        body,
        replay_window_s=hook.get("replayWindowS") or 300,
        signature_header=hook.get("signatureHeader"),
    )


async def _put_body(key: str, body: bytes) -> None:
    if not settings.OWNER_STATE_BUCKET:
        raise RuntimeError("OWNER_STATE_BUCKET is not configured")
    s3 = boto3.client("s3", region_name=settings.AWS_REGION)
    await run_in_thread(
        s3.put_object, Bucket=settings.OWNER_STATE_BUCKET, Key=key, Body=body, ContentType="application/json"
    )


async def _start(owner_id: str, hook: dict, key: str, body: bytes) -> Response:
    public_id = hook["publicId"]
    s3_key = f"{owner_id}/triggers/{datetime.now(timezone.utc):%Y-%m}/{public_id}/{key}.json"
    try:
        await _put_body(s3_key, body)
        await get_client().start_workflow(
            "TriggerFireWorkflow",
            FireInput(owner_id, hook["scheduleId"], "webhook", public_id=public_id, dedupe_key=key, payload_ref=s3_key),
            id=f"hook:{owner_id}:{public_id}:{key}",
            task_queue=settings.TEMPORAL_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return _json(200, {"status": "duplicate"})
    except Exception:  # Temporal or S3 unreachable: the provider retries
        return _json(503, {"detail": "unavailable"}, {"Retry-After": "30"})
    return _json(202, {"status": "accepted"})


async def fire(owner_id: str, public_id: str, request: Request, hook: dict | None = None) -> Response:
    billing = await billing_repo.get_by_owner_id(owner_id)
    if not has_always_on_entitlement(billing):
        return _json(402, {"detail": {"blocked": True, "code": "always_on_requires_subscription"}})
    hook = hook or await automations_repo.get_hook(owner_id, public_id)
    if hook is None:
        return _json(404, {"detail": "unknown_trigger"})
    if not _check_rate_limit(f"trigger:{public_id}"):
        return _json(429, {"detail": "rate_limited"}, {"Retry-After": "60"})
    headers = request.headers
    if headers.get("X-Goog-Resource-State") == "sync" or headers.get("X-GitHub-Event") == "ping":
        return Response(status_code=204)

    body = await request.body()
    reason = await _check(owner_id, public_id, hook, headers, body)
    if reason:
        await automations_repo.note_sig_failure(owner_id, public_id, now=datetime.now(timezone.utc))
        return _json(401, {"detail": reason})

    provider = hook["provider"]
    kind = event_type(provider, headers, body)
    if hook.get("events") and kind is not None and kind not in hook["events"]:
        return _json(200, {"status": "ignored"})
    return await _start(owner_id, hook, dedupe_key(provider, headers, body, mode=hook["signingMode"]), body)


async def fire_verified(owner_id: str, public_id: str, body: bytes, *, dedupe_key: str) -> Response:
    """A delivery whose signature the caller already checked (Meta's app secret)."""
    hook = await automations_repo.get_hook(owner_id, public_id)
    if hook is None:
        return _json(404, {"detail": "unknown_trigger"})
    return await _start(owner_id, hook, dedupe_key, body)
