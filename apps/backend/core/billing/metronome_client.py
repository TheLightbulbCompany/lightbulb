"""Thin httpx client for the handful of Metronome HTTP shapes we use.

No business logic, no UsageEvent knowledge, no DDB — just request marshalling
and auth. Grounded in docs/superpowers/research/2026-06-07-metronome-ingest-
contract.md. The environment (sandbox vs prod) is decided entirely by the
api_key (Metronome is token-scoped, same base URL).

Tests inject a mock AsyncClient via the `http_client` arg; prod constructs a
fresh client per logical operation via `for_settings()`.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.config import settings


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
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: Any) -> dict:
        resp = await self._http.post(path, json=payload, headers=self._headers)
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
        """POST /v1/customers/create — returns the Metronome customer UUID.
        ingest_aliases lets us send our owner_id directly as customer_id on
        every event (no translation table)."""
        body = await self._post("/customers/create", {"name": name, "ingest_aliases": ingest_aliases})
        customer_id = body.get("data", {}).get("id")
        if not customer_id:
            raise MetronomeError(f"create_customer: no data.id in Metronome response (got keys {sorted(body)})")
        return customer_id

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

    async def create_alert(self, *, customer_id: str, name: str, alert_type: str, threshold_cents: int) -> None:
        """POST /v1/alerts/create — per-customer threshold alert. The threshold
        passes through in cents: Metronome's default USD credit type is
        cents-denominated, so no conversion belongs here."""
        await self._post(
            "/alerts/create",
            {
                "customer_id": customer_id,
                "name": name,
                "alert_type": alert_type,
                "threshold": threshold_cents,
            },
        )

    async def set_stripe_billing_config(self, *, metronome_customer_id: str, stripe_customer_id: str) -> None:
        """POST /v1/customers/{id}/setCustomerBillingProviderConfigurations —
        link the EXISTING Stripe customer so Metronome pushes usage/credit
        invoices onto the same Stripe Customer as our $50/mo subscription."""
        await self._post(
            f"/customers/{metronome_customer_id}/setCustomerBillingProviderConfigurations",
            {
                "billing_provider": "stripe",
                "delivery_method": "direct_to_billing_provider",
                "configuration": {
                    "stripe_customer_id": stripe_customer_id,
                    "stripe_collection_method": "charge_automatically",
                },
            },
        )

    async def edit_contract_add_commits(
        self, *, metronome_customer_id: str, contract_id: str, add_commits: list[dict]
    ) -> None:
        """POST /v1/contracts/edit — add prepaid commits (credits). Each commit
        carries payment_gate_config STRIPE upstream so credits release only on
        a confirmed Stripe payment."""
        await self._post(
            "/contracts/edit",
            {
                "customer_id": metronome_customer_id,
                "contract_id": contract_id,
                "add_commits": add_commits,
            },
        )

    async def get_net_balance(self, *, customer_id: str) -> dict:
        """POST /v1/contracts/customerBalances/getNetBalance — aggregated
        remaining balance. Returns the `data` sub-object. NOT for the
        synchronous chat hot path (no low-latency SLA)."""
        body = await self._post("/contracts/customerBalances/getNetBalance", {"customer_id": customer_id})
        return body.get("data", {})


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
