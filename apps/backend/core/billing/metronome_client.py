"""Thin httpx client for the four Metronome HTTP shapes we use.

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

    async def set_credit_balance_threshold(
        self,
        *,
        metronome_customer_id: str,
        contract_id: str,
        is_enabled: bool,
        threshold_amount: float | None = None,
        recharge_to_amount: float | None = None,
    ) -> None:
        """POST /v1/contracts/edit — configure auto-recharge via
        credit_balance_threshold_configuration (research §2). When enabling,
        Metronome auto-funds a commit up to recharge_to_amount once the balance
        crosses below threshold_amount (it runs the recharge asynchronously
        based on usage). When disabling, the amounts are omitted."""
        config: dict[str, object] = {"is_enabled": is_enabled}
        if is_enabled:
            config["threshold_amount"] = threshold_amount
            config["recharge_to_amount"] = recharge_to_amount
        await self._post(
            "/contracts/edit",
            {
                "customer_id": metronome_customer_id,
                "contract_id": contract_id,
                "credit_balance_threshold_configuration": config,
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
