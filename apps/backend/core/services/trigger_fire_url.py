"""The ONE definition of a Paperclip webhook trigger's public fire URL.

This is the address an owner pastes into a provider's dashboard (Stripe,
PostHog, Sentry, Linear, Attio) to arm a trigger. Upstream Paperclip mints
its own ``secretMaterial.webhookUrl`` from ``PAPERCLIP_API_URL``, which CDK
sets to the in-VPC Cloud Map host (``paperclip-stack.ts``) — unreachable
from the public internet, so it must never be handed to an owner. The
reachable address is this platform's hook route:
``POST /api/v1/hooks/t/{owner_id}/{public_id}`` (``routers/hooks.py``, mounted in ``main.py``).

A subtly wrong URL pasted into Stripe fails silently at delivery time —
worse than showing nothing — so every producer reads the shape from here
rather than re-spelling it: the guided seed (``inbound_triggers``) and the
Gmail push OIDC audience check (``routers/hooks.py``).
"""

from __future__ import annotations

import re
from typing import Any

from core.config import settings


def trigger_fire_url(owner_id: str, public_id: str) -> str:
    """Public URL that fires the webhook trigger ``public_id`` for ``owner_id``."""
    return f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/{owner_id}/{public_id}"


def stamp_fire_urls(payload: Any, owner_id: str) -> Any:
    """Add the Isol8-facing fire URL to an upstream routine/trigger response.

    Two upstream shapes, both handled in place:

    - a routine detail (``{..., "triggers": [...]}``) — every webhook trigger
      gains ``fireUrl``, which is what makes an EXISTING trigger armable from
      the detail panel instead of only at guided-create time;
    - a trigger create/rotate response (``{"trigger": ..., "secretMaterial":
      ...}``) — the in-VPC ``secretMaterial.webhookUrl`` is REPLACED with the
      public one, because the UI shows that field as the value to paste into
      the provider;
    - a revision-restore response (``{..., "secretMaterials": [...]}``) — an
      ARRAY, minted when the restored snapshot names a webhook trigger that no
      longer exists, so upstream recreates it with a fresh secret. Same
      one-time "shown once, never again" material, same in-VPC URL, same swap.

    Schedule triggers have no fire URL and are left untouched. Tolerant of
    upstream shape drift: anything unexpected passes straight through.
    """
    if not isinstance(payload, dict):
        return payload

    triggers = payload.get("triggers")
    if isinstance(triggers, list):
        for trigger in triggers:
            _stamp_trigger(trigger, owner_id)

    trigger = payload.get("trigger")
    _stamp_trigger(trigger, owner_id)
    material = payload.get("secretMaterial")
    public_id = trigger.get("publicId") if isinstance(trigger, dict) else None
    if public_id and isinstance(material, dict):
        material["webhookUrl"] = trigger_fire_url(owner_id, public_id)

    materials = payload.get("secretMaterials")
    if isinstance(materials, list):
        for restored in materials:
            _restamp_material(restored, owner_id)

    return payload


def _stamp_trigger(trigger: Any, owner_id: str) -> None:
    public_id = trigger.get("publicId") if isinstance(trigger, dict) else None
    if public_id and trigger.get("kind") == "webhook":
        trigger["fireUrl"] = trigger_fire_url(owner_id, public_id)


# Upstream's own (in-VPC, unusable) webhook URL, whose only job here is to carry
# the public id back out — see ``_restamp_material``.
_UPSTREAM_PUBLIC_ID = re.compile(r"/api/routine-triggers/public/([^/]+)/fire/?$")


def _restamp_material(material: Any, owner_id: str) -> None:
    """Rewrite one restore-response ``secretMaterial``'s in-VPC ``webhookUrl``.

    A restore entry names its ``triggerId``, never its ``publicId`` — the
    freshly minted public id appears nowhere else in the response, so it is
    read back out of upstream's own URL. A non-match means that format moved:
    leave the value untouched rather than publish a guessed URL, which is the
    exact failure this module exists to prevent (a subtly wrong URL pasted into
    a provider fails silently at delivery time).
    """
    url = material.get("webhookUrl") if isinstance(material, dict) else None
    found = _UPSTREAM_PUBLIC_ID.search(url) if isinstance(url, str) else None
    if found:
        material["webhookUrl"] = trigger_fire_url(owner_id, found.group(1))
