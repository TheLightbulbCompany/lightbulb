"""Thin httpx client for the handful of Metronome HTTP shapes we use.

No business logic, no DDB — just request marshalling
and auth. Grounded in docs/superpowers/research/2026-06-07-metronome-ingest-
contract.md. The environment (sandbox vs prod) is decided entirely by the
api_key (Metronome is token-scoped, same base URL).

Tests inject a mock AsyncClient via the `http_client` arg; prod constructs a
fresh client per logical operation via `for_settings()`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote
from typing import Any

import httpx

from core.config import settings


def _stripe_delivery() -> dict[str, str]:
    """Metronome takes exactly one of delivery_method / delivery_method_id, and rejects the bare
    method once an env has two Stripe integrations (sandbox-verified 2026-09-22)."""
    if settings.METRONOME_STRIPE_DELIVERY_METHOD_ID:
        return {"delivery_method_id": settings.METRONOME_STRIPE_DELIVERY_METHOD_ID}
    return {"delivery_method": "direct_to_billing_provider"}


# Prepaid credits never expire on our side; commit schedule items still need
# an end, so they get one far past any plausible product lifetime.
_NO_EXPIRY = "9999-12-31T00:00:00.000Z"

# Relative ranking among commits when several could burn down at once. We
# issue exactly one commit class (Stripe-confirmed top-ups), so any fixed
# value works; Metronome's create validator requires the field regardless
# (sandbox-verified required fields: access_schedule, priority, product_id,
# type).
_COMMIT_PRIORITY = 100


class MetronomeError(Exception):
    """A Metronome response was well-formed HTTP but semantically unusable
    (e.g. a 2xx body missing the expected envelope). Distinct from
    httpx.HTTPStatusError, which the caller raises on non-2xx."""


class MetronomeClient:
    def __init__(self, *, api_key: str, base_url: str, http_client: httpx.AsyncClient):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http_client

    @property
    def _v2_base_url(self) -> str:
        # The configured base is the v1 API root; the v2 surface (gated
        # commits via contract edit) lives beside it at the same host, so
        # derive it instead of carrying a second configured URL that could
        # drift to a different environment.
        return self._base_url.removesuffix("/v1") + "/v2"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: Any, *, extra_headers: dict[str, str] | None = None) -> dict:
        headers = {**self._headers, **extra_headers} if extra_headers else self._headers
        resp = await self._http.post(path, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json() or {}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        resp = await self._http.get(path, params=params, headers=self._headers)
        resp.raise_for_status()
        return resp.json() or {}

    async def ingest(self, events: list[dict]) -> None:
        """POST /v1/ingest — body is a JSON array of event objects. The
        transaction_id on each event is the 34-day idempotency key, so
        retrying until 200 is safe."""
        await self._post("/ingest", events)

    async def create_customer(self, *, name: str, ingest_aliases: list[str]) -> str:
        """POST /v1/customers — returns the Metronome customer UUID.
        Sandbox-verified: customer create is a plain REST POST on the
        collection path (a "/customers/create" RPC-style path 404s).
        ingest_aliases lets us send our owner_id directly as customer_id on
        every event (no translation table)."""
        body = await self._post("/customers", {"name": name, "ingest_aliases": ingest_aliases})
        customer_id = body.get("data", {}).get("id")
        if not customer_id:
            raise MetronomeError(f"create_customer: no data.id in Metronome response (got keys {sorted(body)})")
        return customer_id

    async def archive_customer(self, *, customer_id: str) -> None:
        """POST /v1/customers/archive — Admin Account-Wipe teardown step 9
        (docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md).
        Metronome has no hard customer-delete; archive is the terminal
        state (see the design spec's "Out of scope: Metronome hard-delete").
        Body shape mirrors create_customer's plain-REST posture: {"id":
        customer_id}.

        Treats a 404 (customer already gone — e.g. a re-run after a partial
        wipe failure, or an owner who never had a Metronome customer) as
        success. Any other HTTP error re-raises so the wipe orchestrator can
        report the step as failed."""
        try:
            await self._post("/customers/archive", {"id": customer_id})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return
            raise

    async def get_customer_id_by_ingest_alias(self, ingest_alias: str) -> str | None:
        """GET /v1/customers?ingest_alias=… — adoption lookup for when
        create_customer conflicts on an alias already claimed (e.g. a prior
        provision attempt that crashed before persisting the UUID). Returns
        the first match's customer UUID, or None when nothing matches."""
        body = await self._get("/customers", params={"ingest_alias": ingest_alias})
        matches = body.get("data") or []
        if not matches:
            return None
        return matches[0].get("id")

    async def create_contract(
        self,
        *,
        customer_id: str,
        rate_card_id: str | None = None,
        with_stripe_billing: bool = False,
        uniqueness_key: str,
    ) -> str:
        """POST /v1/contracts/create — per-customer contract. Returns the
        contract id; prepaid commits and balances hang off it. Sandbox-
        verified: this path is correct as-is, the minimal body
        {customer_id, starting_at} is accepted, and rate_card_id is
        OPTIONAL — so it is sent only when the env actually has one
        configured (an empty-string rate card must not reach the API).
        ``starting_at`` must be a UTC-midnight date (Metronome rejects
        mid-day contract starts); today's midnight makes the contract live
        immediately.

        ``with_stripe_billing`` attaches a contract-level
        billing_provider_configuration pointing at the customer's Stripe
        config. Payment-gated commits on the contract collect through this
        object, so a contract created without it cannot fund gated top-ups.
        Live-verified (sandbox, 2026-06-11): the object is only valid AFTER
        the customer's root-level billing-provider configuration exists
        (set_stripe_billing_config) — sent before that, contract create
        404s with "Billing provider configuration not found for STRIPE",
        which is why callers gate this flag on that call having succeeded.

        ``uniqueness_key`` (1-128 chars) makes Metronome 409 a second create
        with the same key instead of minting another contract — overlapping
        contracts on one rate card each price the same usage."""
        starting_at = time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime())
        payload: dict[str, Any] = {
            "customer_id": customer_id,
            "starting_at": starting_at,
            "uniqueness_key": uniqueness_key,
        }
        if rate_card_id:
            payload["rate_card_id"] = rate_card_id
        if with_stripe_billing:
            payload["billing_provider_configuration"] = {"billing_provider": "stripe", **_stripe_delivery()}
        body = await self._post("/contracts/create", payload)
        contract_id = body.get("data", {}).get("id")
        if not contract_id:
            raise MetronomeError(f"create_contract: no data.id in Metronome response (got keys {sorted(body)})")
        return contract_id

    async def create_voice_contract(self, *, customer_id: str, rate_card_id: str, uniqueness_key: str) -> str:
        """An enrolled voice plan; distinct from the owner's prepaid-credit contract.

        Usage only: the flat fee is a Stripe subscription. usage_filter's key must be a group key
        on the voice metric. The dedicated card prices only voice_minutes (1,500 free, then $0.35).
        """
        body = await self._post(
            "/contracts/create",
            {
                "customer_id": customer_id,
                "rate_card_id": rate_card_id,
                "starting_at": time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime()),
                "uniqueness_key": uniqueness_key,
                "billing_provider_configuration": {"billing_provider": "stripe", **_stripe_delivery()},
                "usage_filter": {"group_key": "voice_plan", "group_values": [uniqueness_key]},
            },
        )
        contract_id = (body.get("data") or {}).get("id")
        if not contract_id:
            raise MetronomeError("create_voice_contract: no data.id")
        return contract_id

    async def end_voice_contract(self, *, customer_id: str, contract_id: str, ending_before: str) -> None:
        """End only the voice contract; leave the customer's credits contract intact."""
        # Metronome rejects Python's six-digit fractions; it accepts at most milliseconds.
        end = (
            datetime.fromisoformat(ending_before)
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        await self._post(
            "/contracts/updateEndDate",
            {
                "customer_id": customer_id,
                "contract_id": contract_id,
                "ending_before": end,
            },
        )

    async def list_contracts(self, *, customer_id: str) -> list[dict]:
        """POST /v2/contracts/list — all contracts for one customer. Adoption
        lookup for when create_contract conflicts. v1 is disabled: it answers
        "This endpoint has been disabled. Please use the v2 endpoint"
        (live-verified 2026-09-23). Follows ``cursor`` to the last page."""
        contracts: list[dict] = []
        cursor = None
        while True:
            payload = {"customer_id": customer_id, **({"cursor": cursor} if cursor else {})}
            body = await self._post(f"{self._v2_base_url}/contracts/list", payload)
            contracts += body.get("data") or []
            cursor = body.get("cursor")
            if not cursor:
                return contracts

    async def create_alert(
        self,
        *,
        customer_id: str,
        name: str,
        alert_type: str,
        threshold_cents: int,
        credit_type_id: str,
        uniqueness_key: str,
    ) -> None:
        """POST /v1/alerts/create — per-customer threshold alert. The threshold
        passes through in cents: Metronome's USD credit type is
        cents-denominated (see core/billing/metronome_units.py), so no
        conversion belongs here. credit_type_id is REQUIRED for the
        balance-style alert types we use (sandbox-verified: the create
        validator rejects the body without it); the exact body below —
        {alert_type, name, threshold, customer_id, credit_type_id} —
        returned 200 in the sandbox. ``uniqueness_key`` makes a duplicate
        create 409 instead of arming a second copy of the alert."""
        await self._post(
            "/alerts/create",
            {
                "customer_id": customer_id,
                "name": name,
                "alert_type": alert_type,
                "threshold": threshold_cents,
                "credit_type_id": credit_type_id,
                "uniqueness_key": uniqueness_key,
            },
        )

    async def set_stripe_billing_config(self, *, metronome_customer_id: str, stripe_customer_id: str) -> None:
        """POST /v1/setCustomerBillingProviderConfigurations (ROOT level) —
        write the customer's NEW-STYLE billing-provider configuration linking
        the EXISTING Stripe customer, so Metronome pushes usage/credit
        invoices onto the same Stripe Customer as our $50/mo subscription.
        charge_automatically is what lets payment-gated commits collect
        without a checkout redirect.

        Live-verified (sandbox, 2026-06-11): the per-customer legacy path
        (/customers/{id}/billing-config/stripe) also 200s, but it writes a
        DIFFERENT legacy record that gated commits IGNORE — top-ups through
        a customer linked only that way fail at the payment gate. This
        root-level call writes the record that contract-level
        billing_provider_configuration objects + payment gates actually
        consult; the exact body below returned 200 with the config record."""
        await self._post(
            "/setCustomerBillingProviderConfigurations",
            {
                "data": [
                    {
                        "customer_id": metronome_customer_id,
                        "billing_provider": "stripe",
                        **_stripe_delivery(),
                        "configuration": {
                            "stripe_customer_id": stripe_customer_id,
                            "stripe_collection_method": "charge_automatically",
                        },
                    }
                ]
            },
        )

    async def get_contract(self, *, customer_id: str, contract_id: str) -> dict:
        """Read the contract and its effective Stripe billing-provider link."""
        body = await self._post(
            f"{self._v2_base_url}/contracts/get",
            {"customer_id": customer_id, "contract_id": contract_id},
        )
        return body.get("data") or {}

    async def get_net_balance(self, *, customer_id: str) -> dict:
        """POST /v1/contracts/customerBalances/getNetBalance — aggregated
        remaining balance. Returns the `data` sub-object, whose live-verified
        shape (sandbox, 2026-06-10) is {"balance": <number-in-CENTS>,
        "credit_type_id": …} — the number is under "balance", NOT
        "net_balance". NOT for the synchronous chat hot path (no low-latency
        SLA)."""
        body = await self._post("/contracts/customerBalances/getNetBalance", {"customer_id": customer_id})
        return body.get("data", {})

    async def scoped_voice_seconds(
        self, *, customer_id: str, metric_id: str, usage_key: str, starting_on: str, ending_before: str
    ) -> int:
        """The same scoped seconds that the voice contract prices, without local rounding or timezone drift."""
        total, cursor = Decimal(0), None
        while True:
            path = "/usage/groups" + (f"?next_page={quote(cursor, safe='')}" if cursor else "")
            body = await self._post(
                path,
                {
                    "customer_id": customer_id,
                    "billable_metric_id": metric_id,
                    "window_size": "none",
                    "starting_on": starting_on,
                    "ending_before": ending_before,
                    "group_key": ["voice_plan"],
                    "group_filters": {"voice_plan": [usage_key]},
                },
            )
            total += sum((Decimal(str(row.get("value") or 0)) for row in body.get("data") or []), Decimal(0))
            cursor = body.get("next_page")
            if not cursor:
                return int(total)

    async def list_customer_invoices(self, *, customer_id: str, status: str | None = "DRAFT") -> list[dict]:
        """GET /v1/customers/{id}/invoices — invoice list for one customer.

        With status=DRAFT this returns the continuously-updated draft usage
        invoice for the current billing period; its line_items carry
        pricing_group_values (our ``tier`` key), which is what the
        user-facing spend surface renders. Read-only display path — the
        gates never call this.

        Metronome's docs describe this list response as an invoice SUMMARY
        and direct callers to the single-invoice Get Invoice endpoint for
        detailed line items — callers of this method should treat
        ``line_items`` here as unreliable and re-fetch via
        ``get_customer_invoice`` before summing.
        """
        invoices, cursor = [], None
        while True:
            params = {**({"status": status} if status else {}), **({"next_page": cursor} if cursor else {})}
            body = await self._get(f"/customers/{customer_id}/invoices", params=params)
            invoices += body.get("data") or []
            cursor = body.get("next_page")
            if not cursor:
                return invoices

    async def get_customer_invoice(self, *, customer_id: str, invoice_id: str) -> dict:
        """GET /v1/customers/{customer_id}/invoices/{invoice_id} — full detail
        for one invoice. This is Metronome's documented "Get Invoice"
        endpoint, which — unlike ``list_customer_invoices`` — is guaranteed
        to carry full ``line_items``. Returns the ``data`` sub-object (empty
        dict when absent)."""
        body = await self._get(f"/customers/{customer_id}/invoices/{invoice_id}")
        return body.get("data") or {}


def for_settings(http_client: httpx.AsyncClient) -> MetronomeClient:
    """Build a client from settings, given a caller-owned AsyncClient.

    The caller owns the AsyncClient lifecycle (so it can reuse one connection
    pool across multiple Metronome calls in a single request). Base URL is the
    same for sandbox + prod; the api_key decides the environment.
    """
    return MetronomeClient(
        api_key=settings.METRONOME_API_KEY,
        base_url=settings.METRONOME_BASE_URL,
        http_client=http_client,
    )
