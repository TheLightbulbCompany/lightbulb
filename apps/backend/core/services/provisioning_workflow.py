"""The provisioning loop (spec §3.2), run entirely in the backend.

States are OURS; Stripe's are quoted in code font. The row
(``provisioning_workflow_repo``) is the memory: there are no Provisioning
webhooks and no idempotency contract, so every id is persisted before the write
that produces it and every state move is conditional.

    ask -> awaiting_approval -> connecting_provider -> awaiting_user_action
        -> provisioning -> binding -> active | failed | requires_review

``advance`` is one step. The driver (``provisioning_driver``) decides when to
call it; the sweep re-enters it after a restart. Nothing here sleeps.

Nothing here logs a credential. The only place a revealed value exists is the
local ``env`` dict handed to ``land_credentials``; the log lines carry workflow
id, state, provider, service and error code.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

import jsonschema

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import billing_repo
from core.repositories import provisioning_workflow_repo as repo
from core.services import provisioning_approvals, stripe_provisioning as api
from core.services.owner_facts import write_owner_facts
from core.services.provisioning_landing import land_credentials
from core.services.stripe_provisioning import ProvisioningError

logger = logging.getLogger(__name__)

POLL_BASE_S = 2.0
POLL_MAX_S = 30.0
DEADLINE_S = 900  # 15 minutes, then requires_review with the ids kept.
# How long a workflow may sit waiting on the OWNER before the poller gives up
# and marks it `stale`. Days, not minutes: the card is in their Inbox and they
# are allowed to think about it over a weekend.
USER_ACTION_DEADLINE_S = 7 * 24 * 3600

# spec.txt: the enum values "have no external_name annotation in the proto…
# use the value returned by the endpoint and do not hard-code a lower-case wire
# spelling". Both spellings are accepted for exactly that reason.
_AVAILABLE = frozenset({"PROVIDER_SERVICE_AVAILABILITY_AVAILABLE", "available"})

# The constrained renderer's supported shapes (spec §3.2). Anything else is a
# form we cannot honestly draw, so the workflow stops instead.
_RENDERABLE_TYPES = frozenset({"string", "number", "integer", "boolean"})

# Providers we will show even though their catalog row is thin. The live
# catalog carries what look like Stripe's own test providers (`herenow`,
# `Perplexity`, `Shopify`) with empty categories AND description, so a thin row
# is the signal for "not a real offering" unless we have vetted it.
_THIN_ROW_ALLOWLIST = frozenset({"posthog", "vercel", "neon", "supabase", "resend", "sentry", "cloudflare"})


class WorkflowRefused(RuntimeError):
    """A refusal the caller renders: ``not_eligible``, ``requirements_pending``,
    ``service_unavailable``, ``invalid_configuration``,
    ``no_company_stripe_account``, ``no_company``, ``cannot_cancel``."""

    def __init__(self, code: str, message: str, *, requirements: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.requirements = requirements or []


def is_offerable(service: dict, provider: dict | None) -> bool:
    """The deterministic policy filter. Runs BEFORE anything a model sees:
    "Catalog and Forge policy → determine available Services → apply country,
    scope, Provider, pricing, and security rules → pass only eligible options
    to a model for explanation or ranking."

    Shared by the workflow and the container catalog route so the two can never
    disagree about what exists.
    """
    if service.get("availability") not in _AVAILABLE:
        return False
    if service.get("development") or (provider or {}).get("development"):
        return False
    name = ((provider or {}).get("name") or service.get("provider_name") or "").lower()
    thin = not (provider or {}).get("categories") and not (provider or {}).get("description")
    return not thin or name in _THIN_ROW_ALLOWLIST


def is_transient(e: ProvisioningError) -> bool:
    """A failure worth RE-READING. Never worth re-POSTing.

    `transport_error` is our own timeout/unreachable class; a Stripe 5xx is the
    preview's own flakiness (features/identity returned 500 on 2026-08-29 while
    eligibility worked).
    """
    return e.code == "transport_error" or (e.http_status or 0) >= 500


def poll_delay(attempts: int) -> float:
    """Bounded exponential backoff with jitter (guide: "Poll with bounded
    exponential backoff and jitter").

    Equal jitter, not full: half the window is the backoff itself, so a later
    attempt always waits longer than an earlier one and a poller cannot spin at
    the floor forever. The exponent is capped before the multiply — ``2 **
    attempts`` on a long-lived row overflows the float otherwise.
    """
    ceiling = min(POLL_MAX_S, POLL_BASE_S * 2 ** min(attempts, 10))
    return random.uniform(ceiling / 2, ceiling)


def snapshot_of(service: dict, provider: dict, configuration: dict) -> dict:
    """What the owner approved. Compared field-for-field before every write.

    Deliberately NOT a structured cost: "Don't derive a structured cost from
    freeform price text." The price is display text, kept verbatim.
    """
    pricing = service.get("pricing") or {}
    return {
        "provider_id": provider.get("id"),
        "provider_name": provider.get("name"),
        "service_ref": service.get("service_id"),
        "configuration": configuration,
        "configuration_schema": service.get("configuration_schema") or {},
        "availability": service.get("availability"),
        "scope": service.get("scope"),
        "kind": service.get("kind"),
        "pricing_type": pricing.get("type"),
        "paid_pricing": pricing.get("paid_pricing") or [],
        # The guide's "Collect informed approval" list, in full: provider name,
        # description, website, PRIVACY POLICY and terms; service description,
        # CATEGORIES, availability, scope, required configuration and
        # CONSTRAINTS; the environment; who owns the payment method; and
        # whether this creates new infrastructure or links existing.
        "terms_url": provider.get("tos_url"),
        "website_url": provider.get("website_url"),
        "privacy_policy_url": provider.get("privacy_policy_url"),
        "categories": service.get("categories") or [],
        "constraints": service.get("constraints") or [],
        "description": service.get("description"),
        # One company, one production Project (spec §3.2) — stated so the owner
        # knows which deployment the credential lands in.
        "environment": "Company — production",
        # `resources/link` is not offered in P0b, so every approval is for NEW
        # infrastructure and the card must say so rather than leave it implied.
        "action": "creates new",
        "snapshot_at": utc_now_iso(),
    }


def _drifted(before: dict, after: dict) -> bool:
    keys = (
        "service_ref",
        "configuration_schema",
        "availability",
        "scope",
        "kind",
        "pricing_type",
        "paid_pricing",
        "terms_url",
        "privacy_policy_url",
        "constraints",
    )
    return any(before.get(k) != after.get(k) for k in keys)


async def _account_id(owner_id: str) -> str:
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        raise WorkflowRefused(
            "no_company_stripe_account",
            "Set up your company's Stripe account first — providers are created on it.",
        )
    return account_id


async def _read_catalog(account_id: str, provider_name: str, service_id: str) -> tuple[dict, dict]:
    services = await api.catalog_services(account_id, provider_name=provider_name)  # catalog partition in the client
    service = next((s for s in services if s.get("service_id") == service_id), None)
    if service is None:
        raise WorkflowRefused("service_unavailable", f"{provider_name}/{service_id} is not in your catalog")
    if service.get("availability") not in _AVAILABLE:
        # Answered by the service row itself — no reason to spend a second
        # round trip on the provider list to say no.
        raise WorkflowRefused("service_unavailable", f"{provider_name}/{service_id} is not available to you")
    providers = await api.catalog_providers(account_id)
    provider = next((p for p in providers if p.get("id") == service.get("provider_id")), None)
    if provider is None:
        raise WorkflowRefused("service_unavailable", "That provider is not in your catalog")
    if not is_offerable(service, provider):
        raise WorkflowRefused("service_unavailable", f"{provider_name}/{service_id} is not available to you")
    return service, provider


def schema_is_empty(schema: dict) -> bool:
    """A `needs_information` payload with no fields yet. The provider has not
    published the form — that is a RE-POLL, not a dead end. Treating it as
    unrenderable parked healthy workflows in requires_review."""
    props = (schema or {}).get("properties")
    return not isinstance(props, dict) or not props


def _renderable(schema: dict) -> bool:
    props = (schema or {}).get("properties") or {}
    if not isinstance(props, dict) or not props:
        return False
    for field in props.values():
        if not isinstance(field, dict):
            return False
        if field.get("enum") is not None:
            continue
        if field.get("type") not in _RENDERABLE_TYPES:
            return False
    return True


async def limit_is_editable(account_id: str) -> bool:
    """The owner may set the spending limit only while there is no Payment
    profile yet; afterwards it is a live billing setting and belongs on the
    account, not on a card. Never raises — a probe failure defaults to
    read-only, which is the safe direction."""
    try:
        await api.payment_profile(account_id)
    except ProvisioningError as e:
        return e.code == "not_found"
    except Exception:  # noqa: BLE001
        return False
    return False


async def _open_workflow_for(owner_id: str, provider_id: str, service_id: str) -> dict | None:
    """An existing non-terminal workflow for the same pair, or None."""
    for row in await repo.list_for_owner(owner_id):
        if (
            row.get("provider_id") == provider_id
            and row.get("service_id") == service_id
            and row.get("state") not in repo.TERMINAL_STATES
        ):
            return row
    return None


def resource_name_for(row: dict) -> str:
    """A name unique per workflow.

    NEVER the bare `service_ref`: two resources of the same service on one
    provider account would be indistinguishable in the owner's own provider
    dashboard, and `remove`'s typed confirmation would be ambiguous.
    """
    return f"{row['service_id']}-{row['workflow_id'][-6:]}"


def safe_account_details(details: dict | None) -> dict | None:
    """What the owner is shown about the provider account that now exists:
    "Show returned account details so Ada knows which Provider account will
    receive authentication, billing, security, and operational
    communications." Names and an e-mail — never a provider id or a token."""
    if not details:
        return None
    return {
        "display_name": (details.get("display_name") or "")[:200] or None,
        "primary_email": (details.get("primary_email") or "")[:320] or None,
        # `created` vs `linked_existing`: whether we made them a new account or
        # attached to one they already had. The consent card promised one of
        # these, so the owner must be able to see which happened.
        "link_action": details.get("link_action"),
        "active_services": [
            {"service_id": svc.get("service_id"), "display_name": svc.get("display_name"), "status": svc.get("status")}
            for svc in (details.get("active_services") or [])[:20]
        ],
    }


async def start(
    *, owner_id: str, provider_name: str, service_id: str, configuration: dict, issue_id: str | None
) -> dict:
    """Step 1 of spec §3.2. Refuses loudly rather than opening a workflow that
    cannot finish."""
    account_id = await _account_id(owner_id)

    elig = await api.eligibility(account_id)
    if not elig.get("is_eligible"):
        raise WorkflowRefused("not_eligible", "This Stripe account cannot provision providers yet")
    requirements = list(elig.get("requirements") or [])
    if requirements:
        # "is_eligible=true with requirements […] onboarding isn't complete."
        raise WorkflowRefused("requirements_pending", "Finish your Stripe setup first", requirements=requirements)

    service, provider = await _read_catalog(account_id, provider_name, service_id)
    schema = service.get("configuration_schema") or {}
    try:
        jsonschema.validate(configuration or {}, schema)
    except jsonschema.ValidationError as e:
        raise WorkflowRefused("invalid_configuration", e.message) from e

    # One open workflow per (provider, service). Two asks in the same turn —
    # or Friday retrying after a timeout — must not open a second card or, once
    # approved, provision the resource twice.
    existing = await _open_workflow_for(owner_id, provider["id"], service["service_id"])
    if existing is not None:
        return existing

    row = await repo.create(
        owner_id=owner_id,
        action="create_resource",
        provider_id=provider["id"],
        provider_name=provider.get("name") or provider_name,
        service_ref=service["service_id"],
        service_id=service["service_id"],
        configuration=configuration or {},
        issue_id=issue_id,
    )
    snapshot = snapshot_of(service, provider, configuration or {})
    approval_id = await provisioning_approvals.request_provider_consent(
        owner_id=owner_id,
        issue_id=issue_id,
        workflow_id=row["workflow_id"],
        snapshot=snapshot,
        limit_editable=await limit_is_editable(account_id),
    )
    if approval_id is None:
        # No Paperclip company ⇒ no Inbox ⇒ nobody can ever approve this. Fail
        # the row rather than leaving an un-approvable workflow open forever.
        await repo.transition(
            row["workflow_id"], from_state="ask", to_state="failed", fields={"error_code": "no_company"}
        )
        raise WorkflowRefused("no_company", "This owner has no company board to approve the request")
    moved = await repo.transition(
        row["workflow_id"],
        from_state="ask",
        to_state="awaiting_approval",
        fields={"approval": snapshot, "approval_id": approval_id},
    )
    return moved or row


async def on_approved(workflow_id: str, *, usage_limit: dict | None, approved_by: str) -> dict | None:
    """The owner approved the consent card. Re-read the catalog before moving."""
    row = await repo.get(workflow_id)
    if not row or row.get("state") != "awaiting_approval":
        return row
    account_id = await _account_id(row["owner_id"])
    service, provider = await _read_catalog(account_id, row["provider_name"], row["service_id"])
    fresh = snapshot_of(service, provider, row.get("configuration") or {})
    if _drifted(row.get("approval") or {}, fresh):
        approval_id = await provisioning_approvals.request_provider_consent(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=workflow_id,
            snapshot=fresh,
            limit_editable=await limit_is_editable(account_id),
        )
        return await repo.transition(
            workflow_id,
            from_state="awaiting_approval",
            to_state="awaiting_approval",
            fields={"approval": fresh, "approval_id": approval_id, "error_code": "catalog_changed"},
        )

    if fresh.get("pricing_type") == "paid":
        try:
            await api.payment_profile(account_id)
        except ProvisioningError as e:
            if e.code != "not_found":
                raise
            # `max_amount` stays an int here: the client stringifies it (Stripe
            # rejects large numbers that are not strings), so doing it twice
            # would send a quoted quote.
            limits = usage_limit or {"max_amount": 5000, "currency": "usd", "recurring_interval": "month"}
            approval_id = await provisioning_approvals.request_payment_setup(
                owner_id=row["owner_id"],
                issue_id=row.get("issue_id"),
                workflow_id=workflow_id,
                snapshot=fresh,
                usage_limit=limits,
            )
            return await repo.transition(
                workflow_id,
                from_state="awaiting_approval",
                to_state="awaiting_user_action",
                fields={
                    "awaiting": "payment_setup",
                    "approval_id": approval_id,
                    "usage_limit": limits,
                    "approved_by": approved_by,
                    "approved_at": utc_now_iso(),
                },
            )

    approved = {"approval": fresh, "approved_by": approved_by, "approved_at": utc_now_iso()}
    if usage_limit:
        approved["usage_limit"] = usage_limit
    return await repo.transition(
        workflow_id, from_state="awaiting_approval", to_state="connecting_provider", fields=approved
    )


async def on_rejected(workflow_id: str) -> dict | None:
    """The owner declined the consent card. Terminal, and a clean stop: nothing
    exists on their Stripe account yet, so there is nothing to unwind."""
    return await repo.transition(workflow_id, from_state="awaiting_approval", to_state="rejected")


async def cancel(workflow_id: str) -> dict | None:
    """Withdraw an ask before anything was created.

    Refuses the moment an ``account_request_id`` or ``resource_id`` exists:
    those are objects on the owner's own Stripe account, and closing our row
    would orphan them (spec §3.6 — a resource is removed explicitly, never by
    forgetting about it).
    """
    row = await repo.get(workflow_id)
    if not row:
        raise WorkflowRefused("cannot_cancel", "No such request")
    if row.get("state") not in repo.CANCELLABLE_STATES or row.get("account_request_id") or row.get("resource_id"):
        raise WorkflowRefused("cannot_cancel", "This request has already started — review it instead")
    return await repo.transition(workflow_id, from_state=row["state"], to_state="cancelled")


async def _reauth(row: dict) -> dict:
    """The provider connection expired or vanished: "Create a new Account
    request and ask the user to reconnect."

    ``account_request_id`` is CLEARED, or ``advance`` would re-read the old
    (already `complete`) request forever and never create the new one.
    """
    moved = await repo.transition(
        row["workflow_id"],
        from_state=row["state"],
        to_state="connecting_provider",
        fields={"error_code": "provider_reauth_required", "account_request_id": None, "awaiting": None},
    )
    return moved or {**row, "state": "connecting_provider", "account_request_id": None}


async def _requires_review(row: dict, code: str) -> dict:
    moved = await repo.transition(
        row["workflow_id"], from_state=row["state"], to_state="requires_review", fields={"error_code": code}
    )
    logger.warning(
        "provisioning: workflow %s requires review (%s) provider=%s service=%s",
        row["workflow_id"],
        code,
        row.get("provider_name"),
        row.get("service_ref"),
    )
    return moved or {**row, "state": "requires_review"}


async def _fail(row: dict, code: str, message: str = "") -> dict:
    moved = await repo.transition(
        row["workflow_id"],
        from_state=row["state"],
        to_state="failed",
        fields={"error_code": code, "error_message": message[:400]},
    )
    return moved or {**row, "state": "failed"}


async def _ensure_project(account_id: str, row: dict) -> str:
    """One Project per company (spec §3.2), named for the company. Persisted on
    the row before it is used for a write."""
    if row.get("project_id"):
        return row["project_id"]
    from core.services.company_stripe import resolve_company_name

    name = await resolve_company_name(row["owner_id"])
    project = await api.create_project(account_id, name=name, catalog=settings.STRIPE_PROJECTS_CATALOG)
    await repo.patch(row["workflow_id"], {"project_id": project["id"]})
    return project["id"]


async def _connect_provider(account_id: str, row: dict) -> dict:
    connections = await api.list_provider_connections(account_id)
    mine = [c for c in connections if c.get("provider") == row["provider_id"] and c.get("status") == "active"]
    if len(mine) > 1:
        # "The current API doesn't let Forge select among several same-Provider
        # connections. Don't show a connection picker that the write API can't
        # honor; if the returned state is ambiguous, stop."
        return await _requires_review(row, "ambiguous_provider_connection")
    if mine:
        details = safe_account_details(mine[0].get("provider_account_details"))
        if details:
            await repo.patch(row["workflow_id"], {"provider_account": details})
            row = {**row, "provider_account": details}
        return await _create_resource(account_id, row)

    project_id = await _ensure_project(account_id, row)
    try:
        ar = await api.create_account_request(account_id, provider=row["provider_id"], project_id=project_id)
    except ProvisioningError as e:
        # A POST that timed out or 5xx'd has an UNKNOWN outcome: it may have
        # created the account request. Never repeat it.
        if is_transient(e):
            return await _requires_review(row, "uncertain_write")
        if e.code == "provider_reauth_required":
            return await _reauth(row)
        return await _fail(row, e.code, e.message)
    # Persist BEFORE opening any redirect (guide, Account request table).
    await repo.patch(row["workflow_id"], {"account_request_id": ar["id"]})
    return await _handle_account_request(account_id, {**row, "account_request_id": ar["id"]}, ar)


async def _handle_account_request(account_id: str, row: dict, ar: dict) -> dict:
    status = ar.get("status")
    if status == "complete":
        details = safe_account_details(ar.get("provider_account_details"))
        if details:
            await repo.patch(row["workflow_id"], {"provider_account": details})
            row = {**row, "provider_account": details}
        return await _create_resource(account_id, row)
    if status == "pending_auth":
        approval_id = await provisioning_approvals.request_provider_connect(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=row["workflow_id"],
            snapshot=row.get("approval") or {},
        )
        return await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="awaiting_user_action",
            fields={"awaiting": "provider_connect", "approval_id": approval_id},
        ) or {**row, "state": "awaiting_user_action"}
    if status == "needs_information":
        schema = ar.get("needs_information_schema") or {}
        if schema_is_empty(schema):
            # The provider has not published the form yet — poll, don't park.
            return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row
        if not _renderable(schema):
            return await _requires_review(row, "unrenderable_schema")
        approval_id = await provisioning_approvals.request_provider_information(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=row["workflow_id"],
            snapshot=row.get("approval") or {},
            schema=schema,
            target="account_request",
        )
        return await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="awaiting_user_action",
            fields={
                "awaiting": "provider_information",
                "approval_id": approval_id,
                "information_schema": schema,
                "information_target": "account_request",
            },
        ) or {**row, "state": "awaiting_user_action"}
    if status == "error":
        err = ar.get("error") or {}
        return await _fail(row, err.get("code") or "provider_failure", err.get("message") or "")
    # `requested`: persist and keep polling.
    return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row


async def _create_resource(account_id: str, row: dict) -> dict:
    if row.get("resource_id"):
        return await _poll_resource(account_id, row)
    snapshot = row.get("approval") or {}
    project_id = await _ensure_project(account_id, row) if snapshot.get("scope") == "project" else None
    try:
        resource = await api.create_resource(
            account_id,
            service_ref=row["service_ref"],
            provider=row["provider_id"],
            project=project_id,
            name=resource_name_for(row),
            configuration=row.get("configuration") or {},
            catalog=settings.STRIPE_PROJECTS_CATALOG,
        )
    except ProvisioningError as e:
        if is_transient(e):
            return await _requires_review(row, "uncertain_write")
        if e.code == "provider_reauth_required":
            return await _reauth(row)
        if e.code == "payment_method_required":
            return await _requires_review(row, "payment_method_required")
        return await _fail(row, e.code, e.message)
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=DEADLINE_S)).isoformat()
    await repo.patch(row["workflow_id"], {"resource_id": resource["id"], "deadline_at": deadline})
    moved = await repo.transition(row["workflow_id"], from_state=row["state"], to_state="provisioning")
    return moved or {**row, "state": "provisioning", "resource_id": resource["id"]}


async def _poll_resource(account_id: str, row: dict) -> dict:
    try:
        resource = await api.get_resource(account_id, row.get("resource_id"))
    except ProvisioningError as e:
        if e.code == "provider_reauth_required":
            return await _reauth(row)
        if is_transient(e):
            # A READ is safe to repeat: keep the state, bump the attempt, let
            # the driver back off.
            return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row
        return await _fail(row, e.code, e.message)

    status = resource.get("status")
    if status == "complete":
        return await _bind(account_id, row, resource)
    if status in ("errored", "removed"):
        # Both terminal; `removed` must never reach a credential fetch.
        return await _fail(row, status, resource.get("error_message") or "")
    if status == "needs_information":
        schema = resource.get("needs_information_schema") or {}
        if schema_is_empty(schema):
            return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row
        if not _renderable(schema):
            return await _requires_review(row, "unrenderable_schema")
        approval_id = await provisioning_approvals.request_provider_information(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=row["workflow_id"],
            snapshot=row.get("approval") or {},
            schema=schema,
            target="resource",
        )
        moved = await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="awaiting_user_action",
            fields={
                "awaiting": "provider_information",
                "approval_id": approval_id,
                "information_schema": schema,
                "information_target": "resource",
            },
        )
        return moved or {**row, "state": "awaiting_user_action"}
    # pending
    deadline = row.get("deadline_at")
    if deadline and deadline < utc_now_iso():
        return await _requires_review(row, "poll_deadline_exceeded")
    return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row


async def _bind(account_id: str, row: dict, resource: dict) -> dict:
    moved = await repo.transition(row["workflow_id"], from_state=row["state"], to_state="binding")
    # MERGED, not replaced: the transition's projection is the row as stored,
    # and `resource_id` is what selects our entry out of `environment_info`.
    row = {**row, **(moved or {"state": "binding"})}
    project_id = row.get("project_id") or await _ensure_project(account_id, row)
    try:
        info = await api.environment_info(
            account_id, project=project_id, provider=row["provider_id"], service=row["service_ref"], reveal=True
        )
    except ProvisioningError as e:
        # The connection can expire between `complete` and the credential read.
        if e.code == "provider_reauth_required":
            return await _reauth(row)
        if is_transient(e):
            return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row
        return await _fail(row, e.code, e.message)
    entry = next(
        (e for e in info.get("resource_access_configurations") or [] if e.get("resource_id") == row["resource_id"]),
        None,
    )
    if entry is None:
        return await _requires_review(row, "credentials_not_found")
    env = {k: v for k, v in (entry.get("access_configuration") or {}).items() if isinstance(v, str) and v}
    pointer = {
        "resource_id": row["resource_id"],
        "provider": row["provider_name"],
        "service": row["service_ref"],
        "resource_name": resource.get("name") or resource_name_for(row),
        "status": "active",
        "env_keys": sorted(env),
        "dashboard_url": None,  # deep links are minted on click, never stored
        "level": "company",  # spec §3.4
        "workflow_id": row["workflow_id"],
        "project_id": project_id,
    }
    await land_credentials(owner_id=row["owner_id"], resource=pointer, env=env)
    # Stamp the NAMES onto the workflow row too, so the facts file stays a
    # single query and can never disagree with the pointer.
    await repo.patch(row["workflow_id"], {"resource_name": pointer["resource_name"], "env_keys": pointer["env_keys"]})
    done = await repo.transition(row["workflow_id"], from_state="binding", to_state="active")
    await write_owner_facts(row["owner_id"])
    logger.info(
        "provisioning: workflow %s active provider=%s service=%s keys=%d",
        row["workflow_id"],
        row.get("provider_name"),
        row.get("service_ref"),
        len(env),
    )
    return done or {**row, "state": "active"}


async def _poll_user_action(account_id: str, row: dict) -> dict:
    """The owner is off doing something on a provider's or Stripe's page.

    We still poll from the server — "Keep polling the Account request from the
    server and restore the UI from the stored ID", and for the payment leg
    "poll GET /v2/provisioning/payment_profile until the profile exists". A
    browser that never comes back must not strand the workflow.
    """
    if row.get("awaiting") == "payment_setup":
        try:
            await api.payment_profile(account_id)
        except ProvisioningError as e:
            if e.code == "not_found":
                return await _stale_if_expired(row)
            if is_transient(e):
                return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row
            return await _fail(row, e.code, e.message)
        moved = await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="connecting_provider",
            fields={"awaiting": None},
        )
        return moved or {**row, "state": "connecting_provider"}

    if row.get("account_request_id"):
        try:
            ar = await api.get_account_request(account_id, row["account_request_id"])
        except ProvisioningError as e:
            if e.code == "provider_reauth_required":
                return await _reauth(row)
            if is_transient(e):
                return await repo.patch(row["workflow_id"], {"attempts": int(row.get("attempts") or 0) + 1}) or row
            return await _fail(row, e.code, e.message)
        if ar.get("status") in ("pending_auth", "requested", "needs_information"):
            return await _stale_if_expired(row)
        return await _handle_account_request(account_id, row, ar)

    # `provider_information` with nothing to poll: the owner submits it through
    # the owner route, which kicks the driver.
    return await _stale_if_expired(row)


async def _stale_if_expired(row: dict) -> dict:
    """A workflow nobody came back to. NOT terminal: the ids stay good and the
    next owner click re-mints the link, so this only stops the poller."""
    if row.get("state") == "stale":
        return row
    updated = row.get("updated_at") or ""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=USER_ACTION_DEADLINE_S)).isoformat()
    if updated and updated < cutoff:
        moved = await repo.transition(row["workflow_id"], from_state=row["state"], to_state="stale")
        return moved or {**row, "state": "stale"}
    return row


async def advance(workflow: dict) -> dict:
    """One step. Terminal states and `ask` are no-ops; everything else moves or
    reports that it could not."""
    state = workflow.get("state")
    if state in repo.TERMINAL_STATES or state in ("ask", "awaiting_approval"):
        return workflow
    account_id = await _account_id(workflow["owner_id"])
    if state in ("awaiting_user_action", "stale"):
        return await _poll_user_action(account_id, workflow)
    if state == "connecting_provider":
        if workflow.get("account_request_id"):
            ar = await api.get_account_request(account_id, workflow["account_request_id"])
            return await _handle_account_request(account_id, workflow, ar)
        return await _connect_provider(account_id, workflow)
    if state == "provisioning":
        # Always the READ: `provisioning` is only ever entered after the
        # resource id is persisted, so a create here would be the second POST
        # the no-repeat rule exists to prevent.
        return await _poll_resource(account_id, workflow)
    if state == "binding":
        resource = await api.get_resource(account_id, workflow["resource_id"])
        return await _bind(account_id, workflow, resource)
    return workflow
