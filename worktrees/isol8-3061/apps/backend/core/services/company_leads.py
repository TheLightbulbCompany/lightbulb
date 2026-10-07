"""People search for the Leads tab, on the owner's own Apollo key.

The tier-2 credential path in action: ``apollo_api`` is a ``paste_token``
provider with ``refresh_owner: none`` and ``backend_only`` wiring, so its key
lives ONLY in Secrets Manager (``integration_secret_store``) and this is the
first backend consumer of it. The agent's ``apollo`` MCP connection is a
different, MCP-scoped grant that a backend route cannot use.

── WHAT THIS CALLS, AND WHY IT IS FREE ────────────────────────────────────
``POST /api/v1/mixed_people/api_search`` — Apollo's "net-new people" search.
Verified against Apollo's reference 2026-09-01:

* parameters ride the QUERY STRING, not the body;
* it consumes **0 credits** per call;
* it returns ``first_name`` and a privacy-masked ``last_name_obfuscated``,
  ``title``, ``organization.name`` and a ``has_email`` boolean — **no email,
  no LinkedIn URL**. Those come from People Enrichment (``people/match``),
  which DOES spend credits and is a separate, agent-driven step.

So this route is honest about what a free search returns: a list of who
exists, not their contact details. The Leads tab says so. Anything that
spends the owner's Apollo credits stays behind the agent + an approval, per
the policy model — this route never enriches.

``/mixed_people/search`` (without ``api_``) 403s on Basic plans; the
``api_search`` form is the one every plan can call.

── ERROR SHAPE ───────────────────────────────────────────────────────────
Mirrors ``company_stripe_reads``: no key ⇒ ``connected: False`` and an empty
list (a 200, never a 409 — the tab renders a connect notice). A provider
failure raises so the router can 502 — a list that renders "no people found"
during an Apollo outage is a lie the owner would act on.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

PROVIDER = "apollo_api"
_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
_TIMEOUT_S = 15.0
MAX_PER_PAGE = 25
# Apollo's search never returns more than 50 pages; the router refuses page 51
# for the same reason, so `has_more` must stop advertising it.
MAX_PAGE = 50


class ApolloError(RuntimeError):
    """Apollo answered, and the answer was not a result set."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"apollo {status}: {detail}")
        self.status = status


def _project(person: dict[str, Any]) -> dict[str, Any]:
    """Only what the tab renders. The full record is one enrichment away."""
    org = person.get("organization") or {}
    return {
        "id": person.get("id"),
        "first_name": person.get("first_name"),
        # Privacy-masked by Apollo on the free search (e.g. "S."). Rendered as
        # given; un-masking is what enrichment is for.
        "last_name": person.get("last_name_obfuscated") or person.get("last_name"),
        "title": person.get("title"),
        "organization": org.get("name"),
        "has_email": bool(person.get("has_email")),
        "city": person.get("city"),
        "country": person.get("country"),
    }


async def search_people(
    owner_id: str,
    *,
    query: str,
    page: int = 1,
    per_page: int = MAX_PER_PAGE,
) -> dict[str, Any]:
    cred = await integration_secret_store.get(owner_id, PROVIDER)
    token = (cred or {}).get("token")
    if not token:
        return {"connected": False, "data": [], "total": 0, "page": page, "has_more": False}

    params: dict[str, Any] = {
        "q_keywords": query,
        "page": page,
        "per_page": max(1, min(per_page, MAX_PER_PAGE)),
    }
    headers = {
        "X-Api-Key": token,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(_URL, params=params, headers=headers)

    if resp.status_code != 200:
        # 401/403 here means the pasted key is dead or under-scoped — the
        # validate block at connect time catches the common cases, but a key
        # revoked later still reaches this. Surface it; do not render "nobody".
        logger.warning("apollo api_search non-200 owner=%s status=%s", owner_id, resp.status_code)
        raise ApolloError(resp.status_code, resp.text[:200])

    body = resp.json() if resp.content else {}
    people = [_project(p) for p in body.get("people") or []]
    # The count lives in the `pagination` envelope, not at the root — reading
    # the root reported "0 people" over a full first page and never paged.
    pagination = body.get("pagination") or {}
    total = int(pagination.get("total_entries") or 0)
    return {
        "connected": True,
        "data": people,
        "total": total,
        "page": page,
        "has_more": page < MAX_PAGE and page * params["per_page"] < total,
    }
