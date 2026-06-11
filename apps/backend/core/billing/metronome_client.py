"""Thin httpx client for the handful of Metronome HTTP shapes we use.

No business logic, no UsageEvent knowledge, no DDB — just request marshalling
and auth. Grounded in docs/superpowers/research/2026-06-07-metronome-ingest-
contract.md. The environment (sandbox vs prod) is decided entirely by the
api_key (Metronome is token-scoped, same base URL).

Tests inject a mock AsyncClient via the `http_client` arg; prod constructs a
fresh client per logical operation via `for_settings()`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from core.config import settings

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

    async def create_contract(self, *, customer_id: str, rate_card_id: str | None = None) -> str:
        """POST /v1/contracts/create — per-customer contract. Returns the
        contract id; prepaid commits and balances hang off it. Sandbox-
        verified: this path is correct as-is, the minimal body
        {customer_id, starting_at} is accepted, and rate_card_id is
        OPTIONAL — so it is sent only when the env actually has one
        configured (an empty-string rate card must not reach the API).
        ``starting_at`` must be a UTC-midnight date (Metronome rejects
        mid-day contract starts); today's midnight makes the contract live
        immediately."""
        starting_at = time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime())
        payload: dict[str, Any] = {"customer_id": customer_id, "starting_at": starting_at}
        if rate_card_id:
            payload["rate_card_id"] = rate_card_id
        body = await self._post("/contracts/create", payload)
        contract_id = body.get("data", {}).get("id")
        if not contract_id:
            raise MetronomeError(f"create_contract: no data.id in Metronome response (got keys {sorted(body)})")
        return contract_id

    async def list_contracts(self, *, customer_id: str) -> list[dict]:
        """POST /v1/contracts/list — all contracts for one customer. Adoption
        lookup for when create_contract conflicts (a prior provision attempt
        created the contract but crashed before persisting its id)."""
        body = await self._post("/contracts/list", {"customer_id": customer_id})
        return body.get("data") or []

    async def create_alert(
        self, *, customer_id: str, name: str, alert_type: str, threshold_cents: int, credit_type_id: str
    ) -> None:
        """POST /v1/alerts/create — per-customer threshold alert. The threshold
        passes through in cents: Metronome's USD credit type is
        cents-denominated (see core/billing/metronome_units.py), so no
        conversion belongs here. credit_type_id is REQUIRED for the
        balance-style alert types we use (sandbox-verified: the create
        validator rejects the body without it); the exact body below —
        {alert_type, name, threshold, customer_id, credit_type_id} —
        returned 200 in the sandbox."""
        await self._post(
            "/alerts/create",
            {
                "customer_id": customer_id,
                "name": name,
                "alert_type": alert_type,
                "threshold": threshold_cents,
                "credit_type_id": credit_type_id,
            },
        )

    async def set_stripe_billing_config(self, *, metronome_customer_id: str, stripe_customer_id: str) -> None:
        """POST /v1/customers/{customer_id}/billing-config/stripe — link the
        EXISTING Stripe customer so Metronome pushes usage/credit invoices
        onto the same Stripe Customer as our $50/mo subscription.
        charge_automatically is what lets payment-gated commits collect
        without a checkout redirect.

        Live-verified (sandbox, 2026-06-11): this exact path + body returned
        200, and a GET on the same path reads back
        {"data": {"billing_provider_customer_id": …,
        "stripe_collection_method": …}} (404 NotFound while unset). The
        docs-styled plural path
        (/customers/{id}/setCustomerBillingProviderConfigurations) does NOT
        exist in reality — it 404s."""
        await self._post(
            f"/customers/{metronome_customer_id}/billing-config/stripe",
            {
                "billing_provider_customer_id": stripe_customer_id,
                "stripe_collection_method": "charge_automatically",
            },
        )

    async def create_prepaid_commit(
        self,
        *,
        customer_id: str,
        contract_id: str,
        product_id: str,
        credit_type_id: str,
        amount_cents: int,
        description: str,
        idempotency_key: str,
        payment_gate_config: dict | None = None,
    ) -> None:
        """POST /v1/contracts/customerCommits/create — add a prepaid commit
        (credits). Sandbox-verified: a "/contracts/edit" endpoint does not
        exist (404), and this endpoint takes a FLAT body (commit fields at
        the top level, contract linkage via applicable_contract_ids +
        invoice_contract_id) — this exact shape returned 200. The amount is
        in CENTS: Metronome's USD credit type is cents-denominated (see
        core/billing/metronome_units.py).

        ``idempotency_key`` rides as Metronome's native ``Idempotency-Key``
        request header — the exactly-once guarantee at the money source.
        Live-verified (sandbox, 2026-06-10): same key + same body → 200
        replaying the SAME commit id (one commit, one charge); same key +
        different body → 409 {"message": "The provided idempotency key has
        been used on a request with different parameters."}. Keys are
        retained ~24h per the Metronome docs. Required (not optional)
        because every commit we create charges or grants money — a caller
        without a key is a caller with a double-charge bug.

        ``payment_gate_config`` (e.g. {"payment_gate_type": "STRIPE"}) makes
        Metronome collect the money itself through the customer's linked
        billing provider before releasing the credits — the funding path for
        top-ups now that the backend runs no Checkout of its own. Omitted
        (None) the commit is ungated and grants immediately."""
        payload: dict[str, Any] = {
            "customer_id": customer_id,
            "type": "PREPAID",
            "product_id": product_id,
            "priority": _COMMIT_PRIORITY,
            "applicable_contract_ids": [contract_id],
            "invoice_contract_id": contract_id,
            "description": description,
            "access_schedule": {
                "credit_type_id": credit_type_id,
                "schedule_items": [
                    {
                        # Today's UTC midnight, not now(): the schedule start
                        # must be deterministic within the idempotency-key
                        # retention window, or a retried request carries a
                        # different body and 409s instead of replaying.
                        # Midnight is already in the past, so the credits are
                        # live immediately either way.
                        "amount": amount_cents,
                        "starting_at": time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime()),
                        "ending_before": _NO_EXPIRY,
                    }
                ],
            },
        }
        if payment_gate_config is not None:
            payload["payment_gate_config"] = payment_gate_config
        await self._post(
            "/contracts/customerCommits/create",
            payload,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    async def get_net_balance(self, *, customer_id: str) -> dict:
        """POST /v1/contracts/customerBalances/getNetBalance — aggregated
        remaining balance. Returns the `data` sub-object, whose live-verified
        shape (sandbox, 2026-06-10) is {"balance": <number-in-CENTS>,
        "credit_type_id": …} — the number is under "balance", NOT
        "net_balance". NOT for the synchronous chat hot path (no low-latency
        SLA)."""
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
