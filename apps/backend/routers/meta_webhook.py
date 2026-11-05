"""APP-LEVEL webhook ingress for Meta (Facebook Pages + Instagram).

**Why this route has no owner in its path.** Every other trigger source gets a
per-owner signed URL (``/hooks/t/{owner_id}/{public_id}``) because every other
provider lets each owner register their own endpoint. Meta does not. A Meta app
has ONE callback URL, ONE verify token, and one field subscription per object
type, configured once in the app dashboard — so every owner's deliveries arrive
here, fanned IN rather than out. Putting an owner segment on this URL would be
a lie: Meta would never fill it in.

Routing is therefore by payload, not by path. A delivery carries ``object``
(``page`` / ``instagram``) and ``entry[].id`` — the Page id or IG account id —
and the seed (``core/services/inbound_triggers``, ``auto_meta``) recorded that
asset id against the owner and trigger it belongs to on the owner's
integrations row. Unknown asset ⇒ logged and skipped, not an error: Meta
retries endpoints that fail and disables ones that keep failing, and somebody
else's asset arriving here is not our failure.

**Auth.** Deliberately no ``Depends(get_current_user)`` — Meta holds no Clerk
token. The real authentication is ``X-Hub-Signature-256``: HMAC-SHA256 over the
RAW body keyed by the Meta APP SECRET (``client_secret`` of the platform OAuth
app in Secrets Manager). That is verified HERE, at the edge, because it is not
a per-trigger secret and Paperclip cannot check it. On success the trigger's
OWN bearer secret — recorded with the binding — is injected as the
Authorization, so Paperclip's native ``bearer`` mode still does the final
timing-safe compare. Same verify-at-edge-then-inject shape as the Gmail push
OIDC swap in ``paperclip_trigger_proxy``, minus the JWT.

**Delivery contract.** Meta expects a fast 2xx and retries otherwise. Once the
signature verifies we answer 200 regardless of what happens downstream; a
failed fire is logged, never surfaced, because surfacing it would have Meta
redeliver the whole batch (including the entries that DID fire) and eventually
disable the callback for the entire fleet.

**The gates** are the same ones the per-owner proxy applies, reusing the same
helpers: the always-on entitlement of the resolved owner, and a sliding-window
rate limit — keyed per ASSET here, since the asset is the only tenant-shaped
key a delivery carries.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from core.config import settings
from core.repositories import automations_repo, billing_repo, integrations_repo
from core.services.provision_gate import has_always_on_entitlement
from core.services.super_agent_flags import flags_for
from routers import hooks
from routers._public_ingress import _check_rate_limit, _client_factory, _extract_caller_ip

logger = logging.getLogger(__name__)
router = APIRouter()

# asset_id -> the bindings recorded for it. Rebuilt from a table scan, which is
# the only way to go from an asset id back to an owner (nothing else in the
# delivery is a key we could query by).
#
# ponytail: full-table scan behind a TTL. The integrations table is one row per
# owner per provider — a few thousand items at fleet scale, one scan a minute
# per task. The upgrade path when that stops being true is a GSI on the asset
# id, or a dedicated bindings table; neither is worth a migration today.
_BINDINGS_TTL_SECONDS = 300
# A MISS is the one case that can be a fresh binding the cache hasn't seen, so
# a miss refreshes early rather than waiting out the TTL. Bounded so a flood of
# unknown asset ids can't turn into a scan storm.
_BINDINGS_MISS_REFRESH_SECONDS = 10

_bindings_by_asset: dict[str, list[dict[str, Any]]] = {}
_bindings_loaded_at: float = 0.0


def _reset_bindings_cache() -> None:
    """Test seam — the module-level cache would otherwise leak across cases."""
    global _bindings_by_asset, _bindings_loaded_at
    _bindings_by_asset = {}
    _bindings_loaded_at = 0.0


async def _load_bindings() -> None:
    """Rebuild the asset→bindings map from every owner's integrations row."""
    global _bindings_by_asset, _bindings_loaded_at
    fresh: dict[str, list[dict[str, Any]]] = {}
    for row in await integrations_repo.scan_all():
        owner_id = row.get("owner_id")
        if not owner_id:
            continue
        for binding in row.get("meta_webhook_bindings") or []:
            asset_id = str(binding.get("asset_id") or "")
            if not asset_id or not binding.get("trigger_public_id") or not binding.get("trigger_secret"):
                continue
            fresh.setdefault(asset_id, []).append({**binding, "owner_id": owner_id})
    _bindings_by_asset = fresh
    _bindings_loaded_at = time.monotonic()


async def _bindings_for_asset(asset_id: str) -> list[dict[str, Any]]:
    """Every recorded binding for ``asset_id`` (one owner can bind the same
    asset to several automations, one per event)."""
    age = time.monotonic() - _bindings_loaded_at
    stale = not _bindings_loaded_at or age > _BINDINGS_TTL_SECONDS
    miss = asset_id not in _bindings_by_asset and age > _BINDINGS_MISS_REFRESH_SECONDS
    if stale or miss:
        await _load_bindings()
    return _bindings_by_asset.get(asset_id, [])


def _app_secret_names() -> tuple[str, ...]:
    """Names of the platform OAuth apps whose ``client_secret`` may have signed
    a delivery — the platform app of every provider wired as an ``auto_meta``
    source.

    There is more than one because Meta splits a single app into a Facebook
    client and an Instagram client with their own secrets, and signs each
    product's webhooks with the matching one. Registry-derived (same idiom as
    the trigger proxy's ``_provider_signature_headers``) so wiring a third Meta
    surface stays YAML-only. Sorted for a deterministic try order.
    """
    from core.services.credentials import registry

    return tuple(
        sorted(
            {
                p.oauth.platform_app
                for p in registry.all_providers().values()
                if p.inbound and p.inbound.webhook_registration == "auto_meta" and p.oauth and p.oauth.platform_app
            }
        )
    )


async def _signature_valid(body: bytes, header: str | None) -> bool:
    """Whether ``header`` is a ``sha256=<hex>`` HMAC of the raw body under any
    of our Meta app secrets. Missing, malformed and wrong all read the same to
    the caller — the sender is a machine. Secret reads are cached by
    ``platform_oauth_app_store``, so this is not a per-delivery Secrets Manager
    round trip."""
    if not header:
        return False
    scheme, _, provided = header.partition("=")
    if scheme != "sha256" or not provided:
        return False

    from core.services.credentials import platform_oauth_app_store

    for name in _app_secret_names():
        app = await platform_oauth_app_store.get(name)
        secret = (app or {}).get("client_secret")
        if not secret:
            continue
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8")):
            return True
    return False


def _entry_fields(entry: dict[str, Any]) -> set[str]:
    """The Meta field names an entry reports changing. Only the ``changes``
    shape declares them; ``messaging``/``standby`` entries carry none, and
    those deliberately return an empty set so the caller fans out rather than
    dropping a delivery whose shape it can't read."""
    return {c["field"] for c in entry.get("changes") or [] if isinstance(c, dict) and c.get("field")}


async def _fire(binding: dict[str, Any], body: bytes, idempotency_key: str) -> None:
    """Fire one bound trigger with the raw body. Failures are logged, never
    raised — the route owes Meta a 200 (see the module docstring)."""
    base = settings.PAPERCLIP_INTERNAL_URL.rstrip("/")
    target = f"{base}/api/routine-triggers/public/{binding['trigger_public_id']}/fire"
    headers = {
        "Authorization": f"Bearer {binding['trigger_secret']}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-Forwarded-Proto": "https",
    }
    try:
        async with _client_factory() as client:
            resp = await client.post(target, headers=headers, content=body)
        if resp.status_code >= 400:
            logger.warning(
                "meta webhook fire rejected trigger=%s status=%s",
                binding["trigger_public_id"],
                resp.status_code,
            )
    except httpx.RequestError as exc:
        logger.warning("meta webhook fire upstream error trigger=%s err=%s", binding["trigger_public_id"], exc)


@router.get("/meta")
async def verify_meta_subscription(request: Request) -> Response:
    """Meta's one-time subscribe handshake: echo ``hub.challenge`` as plain
    text when ``hub.verify_token`` matches the configured value.

    503 when no verify token is configured — a handshake that succeeded there
    would have Meta start delivering to an environment that can't verify a
    single signature."""
    if not settings.META_WEBHOOK_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="meta_webhook_not_configured")
    params = request.query_params
    # Compare bytes, not str: compare_digest raises TypeError on non-ASCII str.
    if not hmac.compare_digest(
        params.get("hub.verify_token", "").encode("utf-8"), settings.META_WEBHOOK_VERIFY_TOKEN.encode("utf-8")
    ):
        raise HTTPException(status_code=403, detail="invalid_verify_token")
    return Response(content=params.get("hub.challenge", ""), media_type="text/plain")


@router.post("/meta")
async def receive_meta_event(request: Request) -> Response:
    """Verify Meta's app-secret signature, then fan the raw body out to every
    trigger bound to an asset the payload names."""
    if not settings.META_WEBHOOK_VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="meta_webhook_not_configured")

    # Raw bytes: the signature is over the exact body, and Paperclip receives
    # that same body verbatim.
    body = await request.body()
    if not await _signature_valid(body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("meta webhook rejected unsigned delivery from %s", _extract_caller_ip(request))
        raise HTTPException(status_code=401, detail="invalid_signature")

    if not settings.PAPERCLIP_INTERNAL_URL:
        return Response(status_code=503, content=b'{"detail":"paperclip_unavailable"}', media_type="application/json")

    try:
        payload = await request.json()
    except ValueError:
        # Signed by Meta but unparseable — nothing to route. 200 anyway: a
        # retry would deliver the same bytes.
        logger.warning("meta webhook received a signed but unparseable body")
        return Response(status_code=200)

    object_type = payload.get("object") if isinstance(payload, dict) else None
    entries = payload.get("entry") if isinstance(payload, dict) else None
    for index, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            continue
        asset_id = str(entry.get("id") or "")
        if not asset_id:
            continue
        if not _check_rate_limit(f"meta:{asset_id}"):
            logger.warning("meta webhook rate limited asset=%s", asset_id)
            continue
        bindings = await _bindings_for_asset(asset_id)
        if not bindings:
            # Somebody else's asset, or one whose automation was deleted. Not
            # an error — see the module docstring.
            logger.info("meta webhook: no binding for object=%s asset=%s", object_type, asset_id)
            continue
        # Meta subscribes an asset to the UNION of the fields its automations
        # asked for, so a delivery for one automation's field reaches all of
        # them. Narrow by the field the entry reports — but only when both
        # sides declare one, so an entry shape that carries no field
        # (messaging/standby) still fans out rather than being dropped.
        fields = _entry_fields(entry)
        # Meta's retries reuse the payload, so (asset, entry time, position) is
        # stable across them and distinct per entry within one delivery.
        idempotency_key = f"{asset_id}:{entry.get('time')}:{index}"
        for binding in bindings:
            wanted = set(binding.get("fields") or [])
            if fields and wanted and not (fields & wanted):
                continue
            billing = await billing_repo.get_by_owner_id(binding["owner_id"])
            if not has_always_on_entitlement(billing):
                logger.info("meta webhook: owner %s not entitled, dropping asset=%s", binding["owner_id"], asset_id)
                continue
            if await flags_for(binding["owner_id"]) and await automations_repo.get_hook(
                binding["owner_id"], binding["trigger_public_id"]
            ):
                await hooks.fire_verified(
                    binding["owner_id"], binding["trigger_public_id"], body, dedupe_key=idempotency_key
                )
                continue
            await _fire(binding, body, idempotency_key)

    return Response(status_code=200)
