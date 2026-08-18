"""Meta ACCOUNT callbacks — deauthorize + data-deletion.

**Not to be confused with ``routers/meta_webhook.py`` (singular).** That module
is the EVENT ingress: Meta's delivery of Page/Instagram field changes, mounted
at ``/api/v1/hooks/meta``, authenticated by ``X-Hub-Signature-256`` over the
raw delivery body. THIS module is the ACCOUNT-lifecycle pair Meta configures
under App settings → Basic, mounted at ``/api/v1/webhooks/meta``, authenticated
by a form-encoded ``signed_request``. Complementary surfaces, different auth,
different dashboard fields — only the names are close.

Meta requires a business app to register both before it will grant Advanced
Access. They fire when a user removes the app from their Facebook/Instagram
settings (deauthorize) or requests deletion of their data (data-deletion).

**These routes are unauthenticated by design — the signature IS the auth.**
Meta POSTs form-encoded ``signed_request`` with no bearer token, so
:func:`meta_signed_request.verify` is the only gate: a bad or absent signature
is a 400 that tears down nothing. Never add a code path that acts on the
payload before verification returns.

Resolving the owner: Meta identifies the user only by its app-scoped
``user_id``, which we record on the integration row at connect time
(``integrations_repo.put_status(provider_user_id=…)``). An unknown id is a 200
with nothing done — the user may have connected before we captured the id, or
never connected at all, and either way there is nothing to revoke. It is NOT an
error to Meta, whose only requirement is that we answer.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Form, HTTPException

from core.config import settings
from core.repositories import integrations_repo
from core.services import meta_signed_request
from core.services.credentials import connection_lock, registry
from core.services.meta_signed_request import SignatureError

logger = logging.getLogger(__name__)
router = APIRouter()

# Only providers backed by a Meta platform OAuth app may be addressed here. The
# path segment is attacker-controlled, so it is matched against this map rather
# than passed to the registry directly — otherwise any provider key would reach
# the teardown, and a provider with no Meta app would fail open on the
# "unprovisioned secret" path instead of 404ing.
_META_PROVIDERS = ("instagram", "meta_ads")


def _platform_app_for(provider: str) -> str:
    if provider not in _META_PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    try:
        p = registry.get_provider(provider)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown provider") from None
    app = p.oauth.platform_app if p.oauth else None
    if not app:
        raise HTTPException(status_code=404, detail="provider has no platform app")
    return app


async def _verify(provider: str, signed_request: str) -> dict:
    platform_app = _platform_app_for(provider)
    try:
        return await meta_signed_request.verify(signed_request, platform_app)
    except SignatureError as exc:
        # Deliberately terse to the caller: a verification oracle would help an
        # attacker probe. The reason goes to our logs, not the response.
        logger.warning("meta webhook signature rejected for %s: %s", provider, exc)
        raise HTTPException(status_code=400, detail="invalid signed_request") from exc


async def _teardown(provider: str, meta_user_id: str) -> str | None:
    """Revoke the connection this Meta user id maps to. Returns the owner id, or
    None when no connection matches."""
    row = await integrations_repo.find_by_provider_user_id(provider, meta_user_id)
    if not row:
        logger.info("meta webhook: no %s connection for the given user id", provider)
        return None
    owner_id = row["owner_id"]
    # Imported here rather than at module scope: routers/connections.py imports a
    # large slice of the credentials stack, and a top-level import would make this
    # module's import order depend on it.
    from routers.connections import _disconnect_teardown, _get_provider_for_teardown

    p = _get_provider_for_teardown(provider)
    # Same lock the user-initiated disconnect takes, so a teardown racing
    # Instagram's background token refresh cannot be undone by it.
    async with connection_lock.connection_lock(owner_id, provider):
        await _disconnect_teardown(owner_id, provider, p)
    logger.info("meta webhook: revoked %s for owner %s", provider, owner_id)
    return owner_id


@router.post("/{provider}/deauthorize")
async def deauthorize(provider: str, signed_request: str = Form(default="")) -> dict:
    """The user removed Isol8 from their Meta account. Revoke our side."""
    payload = await _verify(provider, signed_request)
    user_id = str(payload.get("user_id") or "")
    if not user_id:
        # A verified payload with no user_id identifies nobody. Answer 200 (Meta
        # only needs the endpoint to respond) but do not guess at a target.
        logger.warning("meta deauthorize for %s carried no user_id", provider)
        return {"status": "ok"}
    await _teardown(provider, user_id)
    return {"status": "ok"}


@router.post("/{provider}/data-deletion")
async def data_deletion(provider: str, signed_request: str = Form(default="")) -> dict:
    """The user asked Meta to have their data deleted. Same teardown, plus the
    status URL + confirmation code Meta requires in the response body."""
    payload = await _verify(provider, signed_request)
    user_id = str(payload.get("user_id") or "")
    if user_id:
        await _teardown(provider, user_id)
    else:
        logger.warning("meta data-deletion for %s carried no user_id", provider)
    # Deletion completes synchronously above, so the code is an acknowledgement
    # rather than a ticket to poll — we deliberately persist no record of the
    # request (storing one would outlive the data it reports on).
    #
    # The status URL is the SAME page already declared as the app's data-deletion
    # INSTRUCTIONS URL. Meta accepts either an instructions URL or a callback;
    # we serve both, and pointing them at one page means there is a single
    # canonical answer to "what happened to my data" rather than two that drift.
    confirmation_code = secrets.token_hex(8)
    base = (settings.FRONTEND_URL or "").rstrip("/")
    return {"url": f"{base}/support#data-deletion", "confirmation_code": confirmation_code}
