"""The provisioning loop (spec §3.2), run entirely in the backend.

States are OURS; Stripe's are quoted in code font. The row
(``provisioning_workflow_repo``) is the memory: there are no Provisioning
webhooks and no idempotency contract, so every id is persisted before the write
that produces it and every state move is conditional.

    ask -> awaiting_approval -> connecting_provider -> awaiting_user_action
        -> provisioning -> binding -> active | failed | requires_review

``advance`` is one step and ``advance_on_read`` is what takes it: there is
nothing in the background, so a workflow moves when somebody READS it —
Friday's status route, the owner's page loads, the owner's clicks. Nothing here
sleeps.

One residual race is ACCEPTED: two truly simultaneous ``start`` calls can both
read no open workflow and create two rows. The window is the round trip between
the read and the create, the loser's card is a duplicate the owner declines,
and closing it would cost a conditional (owner, provider, service) uniqueness
row. Sequential retries — the common case, Friday re-asking after a timeout —
are covered.

Nothing here logs a credential. The only place a revealed value exists is the
local ``env`` dict handed to ``land_credentials``; the log lines carry workflow
id, state, provider, service and error code.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
import jsonschema

from core.config import settings
from core.dynamodb import utc_now_iso
from core.repositories import billing_repo
from core.repositories import provisioning_workflow_repo as repo
from core.services import owner_facts, provisioning_approvals, stripe_provisioning as api, work_products
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.provisioning_landing import land_credentials
from core.services.stripe_provisioning import ProvisioningError

logger = logging.getLogger(__name__)

DEADLINE_S = 900  # 15 minutes, then requires_review with the ids kept.
# How long a workflow may sit waiting on the OWNER before the poller gives up
# and marks it `stale`. Days, not minutes: the card is in their Inbox and they
# are allowed to think about it over a weekend.
USER_ACTION_DEADLINE_S = 7 * 24 * 3600

# How long one reader may hold a row while it takes its step. Sized to the
# WORST step, not the average: `connecting_provider` can chain six Stripe calls
# (`list_provider_connections`, `list_projects`, `create_project`,
# `create_account_request`, `list_resources`, `create_resource`), each bounded
# at `stripe_provisioning.TIMEOUT_S + 5` = 50s. Under that, a live step would
# lose its claim mid-way and a second reader would create the resource twice —
# which costs money, where an over-long claim only costs a reader its turn.
# Not renewable (see `repo.claim`), so a reader that dies mid-step blocks the
# row for five minutes at most.
READ_LEASE_S = 300

# Which process holds a claim. The pid matters: a rolling deploy can put two
# workers on one host, and `HOSTNAME` is the Fargate task id in ECS.
INSTANCE_ID = f"{os.getenv('HOSTNAME') or 'local'}:{os.getpid()}"

# Paperclip's own two approval outcomes (fork `services/approvals.ts`).
# Rejection is a real outcome, not silence: without it a declined card is read
# forever and the owner is never told it stopped.
_OUTCOMES = ("approved", "rejected")

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
    ``no_company_stripe_account``, ``no_company``, ``cannot_cancel``,
    ``needs_review_first``, ``reserved_env_name`` (raised by
    ``provisioning_landing.land_credentials``, not this module -- imported
    locally there to avoid a circular import)."""

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

    The CLIENT decides: it raises ``TransientProvisioningError`` for a 5xx, a
    429 and our own ``transport_error``. Re-deriving that from ``code`` and
    ``http_status`` here would drift from it — the 429 was the one it missed.
    """
    return isinstance(e, api.TransientProvisioningError)


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
        "provider_id",
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


async def account_id_for(owner_id: str) -> str:
    """The company Stripe account every provisioning call is made on. Public
    so the container catalog route reads it the same way ``start`` does."""
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        raise WorkflowRefused(
            "no_company_stripe_account",
            "Set up your company's Stripe account first — providers are created on it.",
        )
    return account_id


async def _read_catalog(account_id: str, provider_name: str, service_id: str) -> tuple[dict, dict]:
    # Unfiltered read + local match: the wire `provider_name` is mixed-case
    # ("Sentry", "Shopify") while our request bodies are lowercase slugs
    # (validated — the value becomes a Secrets Manager name component), and
    # Stripe's provider_name filter is case-sensitive, so filtering by the
    # slug returned nothing and every capitalized provider was unfilable
    # (found live on dev 2026-08-31). ~30 rows; one paginated read either way.
    services = await api.catalog_services(account_id)  # catalog partition in the client
    want = (provider_name or "").lower()
    service = next(
        (s for s in services if (s.get("provider_name") or "").lower() == want and s.get("service_id") == service_id),
        None,
    )
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


def schema_reason(e: jsonschema.ValidationError) -> str:
    """Why a payload failed its schema, with no SUBMITTED VALUE in it.

    `e.message` quotes the offending value ("'mars' is not one of […]"), and
    both callers relay this to somebody — Friday's transcript, the owner's
    form — where a provider field could be a credential. So: the path, except
    for `required`, whose message names a field the SCHEMA declares and
    nothing the caller sent. `$` alone would tell neither of them which field
    to fix.
    """
    return e.message if e.validator == "required" else f"{e.json_path} is not valid"


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
        enum = field.get("enum")
        if enum is not None:
            # A `select` with nothing in it is a field the owner cannot answer,
            # and an `enum` that is not a list is not a form we can draw at all.
            if not isinstance(enum, list) or not enum:
                return False
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


def _for_pair(rows: list[dict], provider_id: str, service_id: str) -> list[dict]:
    """This owner's workflows for one (provider, service), NEWEST FIRST."""
    mine = [r for r in rows if r.get("provider_id") == provider_id and r.get("service_id") == service_id]
    mine.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return mine


def _parked_for_review(for_pair: list[dict]) -> dict | None:
    """The newest row for the pair, when re-asking would risk a SECOND billable
    resource.

    ``uncertain_write`` means a state-changing POST was issued and its answer
    never arrived — the Stripe object may exist. A fresh workflow gets a fresh
    ``resource_name_for``, so the reconcile-by-name read that guards every
    create would find nothing and create a second one. Same for any parked row
    that already carries an id. A row parked for a reason with no write behind
    it (``unrenderable_schema``, say) blocks nothing.
    """
    newest = for_pair[0] if for_pair else None
    if not newest or newest.get("state") != "requires_review":
        return None
    if newest.get("error_code") == "uncertain_write" or newest.get("resource_id") or newest.get("account_request_id"):
        return newest
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
    *,
    owner_id: str,
    provider_name: str,
    service_id: str,
    configuration: dict,
    issue_id: str | None,
    reason: str = "",
) -> dict:
    """Step 1 of spec §3.2. Refuses loudly rather than opening a workflow that
    cannot finish."""
    if not settings.STRIPE_PROVISIONING_ENABLED:
        # Every state-changing /v2/provisioning call 500s until Stripe ships
        # delegated account creation + KYC; an approved workflow today lands
        # in a permanent requires_review. Refuse at the door instead.
        raise WorkflowRefused("provisioning_unavailable", "Setting up services isn't available yet")
    account_id = await account_id_for(owner_id)

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
        raise WorkflowRefused("invalid_configuration", f"{schema_reason(e)} for this service") from e

    # One open workflow per (provider, service). Two asks in the same turn —
    # or Friday retrying after a timeout — must not open a second card or, once
    # approved, provision the resource twice.
    for_pair = _for_pair(await repo.list_for_owner(owner_id), provider["id"], service["service_id"])
    existing = next((r for r in for_pair if r.get("state") not in repo.TERMINAL_STATES), None)
    if existing is not None:
        # In-memory marker, never written to the row: the caller must not tell
        # the owner a second card was filed when it reused the first.
        return {**existing, "reused": True}
    parked = _parked_for_review(for_pair)
    if parked is not None:
        raise WorkflowRefused(
            "needs_review_first",
            f"An earlier request for this ({parked['workflow_id']}) is parked for review at Lightbulb — "
            "it may already have created the resource. Tell the owner and do not ask again.",
        )

    row = await repo.create(
        owner_id=owner_id,
        action="create_resource",
        provider_id=provider["id"],
        provider_name=provider.get("name") or provider_name,
        service_ref=service["service_id"],
        service_id=service["service_id"],
        configuration=configuration or {},
        issue_id=issue_id,
        reason=reason,
    )
    snapshot = snapshot_of(service, provider, configuration or {})
    try:
        approval_id = await provisioning_approvals.request_provider_consent(
            owner_id=owner_id,
            issue_id=issue_id,
            workflow_id=row["workflow_id"],
            snapshot=snapshot,
            limit_editable=await limit_is_editable(account_id),
            reason=reason,
        )
    except Exception:
        # The row is already `ask`, and the dedupe above treats an `ask` row as
        # open — so leaving it would make Friday's retry a no-op reuse of a
        # workflow nothing is driving. Close it; the retry opens a fresh one.
        await repo.transition(
            row["workflow_id"], from_state="ask", to_state="failed", fields={"error_code": "board_unavailable"}
        )
        raise
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
    if not settings.STRIPE_PROVISIONING_ENABLED:
        # Same door as `start`: an approval must not become a Stripe write
        # while the rail is off, even for a card filed before it was.
        raise WorkflowRefused("provisioning_unavailable", "Provisioning is not available yet")
    row = await repo.get(workflow_id)
    if not row or row.get("state") != "awaiting_approval":
        return row
    account_id = await account_id_for(row["owner_id"])
    service, provider = await _read_catalog(account_id, row["provider_name"], row["service_id"])
    fresh = snapshot_of(service, provider, row.get("configuration") or {})
    if _drifted(row.get("approval") or {}, fresh):
        approval_id = await provisioning_approvals.request_provider_consent(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=workflow_id,
            snapshot=fresh,
            limit_editable=await limit_is_editable(account_id),
            reason=row.get("reason", ""),
        )
        if approval_id is None:
            return await _no_board(row)
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
            limits = usage_limit or dict(provisioning_approvals.DEFAULT_USAGE_LIMIT)
            approval_id = await provisioning_approvals.request_payment_setup(
                owner_id=row["owner_id"],
                issue_id=row.get("issue_id"),
                workflow_id=workflow_id,
                snapshot=fresh,
                usage_limit=limits,
            )
            if approval_id is None:
                return await _no_board(row)
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
                    "user_action_since": utc_now_iso(),
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
    moved = await repo.transition(workflow_id, from_state="awaiting_approval", to_state="rejected")
    if moved:
        await _write_facts(moved["owner_id"])
    return moved


async def cancel(workflow_id: str) -> dict | None:
    """Withdraw an ask before anything was created.

    Refuses the moment an ``account_request_id`` or ``resource_id`` exists:
    those are objects on the owner's own Stripe account, and closing our row
    would orphan them (spec §3.6 — a resource is removed explicitly, never by
    forgetting about it).

    The pending QUESTION is cleared with the state. A cancelled row that still
    says ``awaiting: payment_setup`` is a row every owner route keyed on
    `awaiting` still treats as live — a withdrawn ask could mint a hosted
    checkout.
    """
    row = await repo.get(workflow_id)
    if not row:
        raise WorkflowRefused("cannot_cancel", "No such request")
    if row.get("state") not in repo.CANCELLABLE_STATES or row.get("account_request_id") or row.get("resource_id"):
        raise WorkflowRefused("cannot_cancel", "This request has already started — review it instead")
    return await repo.transition(
        workflow_id,
        from_state=row["state"],
        to_state="cancelled",
        fields={
            "awaiting": None,
            "information_target": None,
            "information_schema": None,
            "user_action_since": None,
        },
    )


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
        fields={
            "error_code": "provider_reauth_required",
            "account_request_id": None,
            "awaiting": None,
            "user_action_since": None,
        },
    )
    return moved or {**row, "state": "connecting_provider", "account_request_id": None}


def _already_waiting(row: dict, awaiting: str, target: str | None = None) -> bool:
    """The row is already parked on THIS question, with a card already filed.

    Polling a provider that still says `needs_information` must not re-file the
    card in the owner's Inbox — nor restart their seven days by re-stamping
    `user_action_since`, which is exactly what a same-state re-transition did.
    """
    return (
        row.get("state") == "awaiting_user_action"
        and row.get("awaiting") == awaiting
        and (target is None or row.get("information_target") == target)
        and bool(row.get("approval_id"))
    )


async def _write_facts(owner_id: str) -> None:
    """Every TERMINAL move re-renders the owner's facts file, so Friday's next
    turn sees a stopped workflow instead of re-reading an in-flight one and
    waiting for something nothing is driving.

    Best-effort, and through the module attribute: the facts file is a nicety
    and must never turn a state move into a failure.
    """
    try:
        await owner_facts.write_owner_facts(owner_id)
    except Exception:  # noqa: BLE001 — facts must never block a transition
        logger.warning("provisioning: facts write failed for %s", owner_id, exc_info=True)


async def _requires_review(row: dict, code: str) -> dict:
    moved = await repo.transition(
        row["workflow_id"], from_state=row["state"], to_state="requires_review", fields={"error_code": code}
    )
    await _write_facts(row["owner_id"])
    logger.warning(
        "provisioning: workflow %s requires review (%s) provider=%s service=%s",
        row["workflow_id"],
        code,
        row.get("provider_name"),
        row.get("service_ref"),
    )
    return moved or {**row, "state": "requires_review"}


async def _no_board(row: dict) -> dict:
    """A later-leg card could not be filed — the owner has no Paperclip board,
    so nobody can ever answer it. Park it for review instead of returning to
    the poll loop, which would re-file the same un-fileable card forever.

    ``start`` fails its row outright for the same cause; by this point Stripe
    objects exist, so the row is kept for a human rather than closed."""
    return await _requires_review(row, "no_company")


async def _fail(row: dict, code: str, message: str = "") -> dict:
    moved = await repo.transition(
        row["workflow_id"],
        from_state=row["state"],
        to_state="failed",
        fields={"error_code": code, "error_message": message[:400]},
    )
    await _write_facts(row["owner_id"])
    return moved or {**row, "state": "failed"}


async def _read_or_park(row: dict, awaitable) -> tuple:
    """Run one Stripe READ. Returns ``(result, None)`` or ``(None, refusal_row)``.

    A read is safe to repeat, so a transient failure only bumps the attempt and
    leaves the next reader to try again; an expired provider connection re-enters
    ``connecting_provider``; anything else fails the workflow. Every read site
    goes through here so no branch can quietly forget one of the three.
    """
    try:
        return await awaitable, None
    except ProvisioningError as e:
        if e.code == "provider_reauth_required":
            return None, await _reauth(row)
        if is_transient(e):
            return None, await _bump(row, e.retry_after)
        return None, await _fail(row, e.code, e.message)


async def _bump(row: dict, retry_after: float | None = None) -> dict:
    """One more poll attempt, same state.

    ``retry_after`` is Stripe's own answer to "when": a 429 carries it, and a
    read has no other way to know. Stamped on the row as a floor
    ``advance_on_read`` will not step before — the read still answers, it just
    does not spend a Stripe call it was asked to wait for.
    """
    fields: dict = {"attempts": int(row.get("attempts") or 0) + 1}
    if retry_after and retry_after > 0:
        fields["next_poll_after"] = (datetime.now(timezone.utc) + timedelta(seconds=float(retry_after))).isoformat()
    return await repo.patch(row["workflow_id"], fields) or row


async def _company_project_id(owner_id: str) -> str | None:
    """The company's ONE Provisioning Project, or None if it has none yet."""
    return (await billing_repo.get_by_owner_id(owner_id) or {}).get("company_stripe_project_id")


async def _project_for(account_id: str, row: dict) -> tuple:
    """``(project_id, refusal_row)`` — one Project per COMPANY (spec §3.2).

    An account-scoped service is placed at account level (spec 4.4), so it
    neither needs nor mints a Project.

    The id lives on the BILLING row, not the workflow: `environment_info` is
    scoped by project, so a second Project would split one company's
    credentials across two. Order is read → adopt → create → store, because a
    crash between the create and the store leaves a Project we own but never
    recorded ("retrieve known objects and reconcile").

    READS and the CREATE are handled differently on purpose: a failed read is
    simply retried on the next tick, and only the create has an outcome we
    cannot know, which is the one case worth parking the row for.
    """
    if (row.get("approval") or {}).get("scope") != "project":
        return None, None
    stored = await _company_project_id(row["owner_id"])
    if stored:
        return stored, None
    from core.services.company_stripe import resolve_company_name

    name = await resolve_company_name(row["owner_id"])
    known, refused = await _read_or_park(row, api.list_projects(account_id))
    if refused is not None:
        return None, refused
    mine = next((p for p in known if p.get("name") == name), None)
    if mine:
        project_id = mine["id"]
    else:
        try:
            project = await api.create_project(account_id, name=name, catalog=settings.STRIPE_PROJECTS_CATALOG)
        except ProvisioningError as e:
            if is_transient(e):
                return None, await _requires_review(row, "uncertain_write")
            return None, await _fail(row, e.code, e.message)
        project_id = project["id"]
    if await billing_repo.set_company_stripe_project_id(row["owner_id"], project_id=project_id) is None:
        # Lost the conditional write: another worker stored first, so THEIRS is
        # the company's Project. Strongly consistent — this reads back a write
        # that just landed from another process.
        winner = (await billing_repo.get_by_owner_id(row["owner_id"], consistent_read=True) or {}).get(
            "company_stripe_project_id"
        )
        if not winner:
            # The write was refused and there is still no id (no billing row
            # yet, most likely). Falling back to OURS would put this company's
            # next credential in a Project the winner never sees, so retry
            # instead. A Project we created and lost with is inert metadata —
            # no resources, no cost — and the adopt-by-name read collects it.
            return None, await _bump(row)
        return winner, None
    return project_id, None


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

    project_id, refused = await _project_for(account_id, row)
    if refused is not None:
        return refused
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
    # Persist BEFORE opening any redirect (guide, Account request table). The
    # window between the POST returning and this patch landing is the one place
    # an id can be lost: account requests carry no name, so there is nothing to
    # reconcile them by. A lost one strands a `connecting_provider` row that the
    # next pass re-creates from — accepted, and the reason this patch is the
    # very next statement after the create.
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
        if _already_waiting(row, "provider_connect"):
            return row
        approval_id = await provisioning_approvals.request_provider_connect(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=row["workflow_id"],
            snapshot=row.get("approval") or {},
            # Known after a reauth: the owner sees which account they are
            # re-authorising rather than a bare provider name.
            provider_account=row.get("provider_account"),
        )
        if approval_id is None:
            return await _no_board(row)
        return await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="awaiting_user_action",
            fields={
                "awaiting": "provider_connect",
                "approval_id": approval_id,
                "user_action_since": utc_now_iso(),
            },
        ) or {**row, "state": "awaiting_user_action"}
    if status == "needs_information":
        schema = ar.get("needs_information_schema") or {}
        if schema_is_empty(schema):
            # The provider has not published the form yet — poll, don't park.
            return await _bump(row)
        if not _renderable(schema):
            return await _requires_review(row, "unrenderable_schema")
        if _already_waiting(row, "provider_information", "account_request"):
            return row
        approval_id = await provisioning_approvals.request_provider_information(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=row["workflow_id"],
            snapshot=row.get("approval") or {},
            schema=schema,
            target="account_request",
        )
        if approval_id is None:
            return await _no_board(row)
        return await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="awaiting_user_action",
            fields={
                "awaiting": "provider_information",
                "approval_id": approval_id,
                "information_schema": schema,
                "information_target": "account_request",
                "user_action_since": utc_now_iso(),
            },
        ) or {**row, "state": "awaiting_user_action"}
    if status == "error":
        err = ar.get("error") or {}
        return await _fail(row, err.get("code") or "provider_failure", err.get("message") or "")
    # `requested`: persist and keep polling.
    return await _bump(row)


async def _create_resource(account_id: str, row: dict) -> dict:
    if row.get("resource_id"):
        return await _poll_resource(account_id, row)
    project_id, refused = await _project_for(account_id, row)
    if refused is not None:
        return refused
    name = resource_name_for(row)
    # "Retrieve known objects, and reconcile before issuing another create": a
    # create whose response we never saw may have made the resource, and the
    # name is unique to this workflow, so it is what identifies ours.
    known, refused = await _read_or_park(row, api.list_resources(account_id, project=project_id))
    if refused is not None:
        return refused
    resource = next(
        (
            r
            for r in known
            if r.get("name") == name
            and r.get("service_ref") in (None, row["service_ref"])
            # `removed`/`errored` are terminal: adopting one would bind an
            # owner to a resource that no longer exists. Create a fresh one.
            and r.get("status") not in ("removed", "errored")
        ),
        None,
    )
    if resource is None:
        try:
            resource = await api.create_resource(
                account_id,
                service_ref=row["service_ref"],
                provider=row["provider_id"],
                project=project_id,
                name=name,
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
    moved = await repo.transition(
        row["workflow_id"], from_state=row["state"], to_state="provisioning", fields={"user_action_since": None}
    )
    return moved or {**row, "state": "provisioning", "resource_id": resource["id"]}


async def _poll_resource(account_id: str, row: dict) -> dict:
    resource, refused = await _read_or_park(row, api.get_resource(account_id, row.get("resource_id")))
    if refused is not None:
        return refused

    status = resource.get("status")
    if status == "complete":
        return await _bind(account_id, row, resource)
    if status in ("errored", "removed"):
        # Both terminal; `removed` must never reach a credential fetch.
        return await _fail(row, status, resource.get("error_message") or "")
    if status == "needs_information":
        schema = resource.get("needs_information_schema") or {}
        if schema_is_empty(schema):
            return await _bump(row)
        if not _renderable(schema):
            return await _requires_review(row, "unrenderable_schema")
        if _already_waiting(row, "provider_information", "resource"):
            return row
        approval_id = await provisioning_approvals.request_provider_information(
            owner_id=row["owner_id"],
            issue_id=row.get("issue_id"),
            workflow_id=row["workflow_id"],
            snapshot=row.get("approval") or {},
            schema=schema,
            target="resource",
        )
        if approval_id is None:
            return await _no_board(row)
        moved = await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="awaiting_user_action",
            fields={
                "awaiting": "provider_information",
                "approval_id": approval_id,
                "information_schema": schema,
                "information_target": "resource",
                "user_action_since": utc_now_iso(),
            },
        )
        return moved or {**row, "state": "awaiting_user_action"}
    # pending
    deadline = row.get("deadline_at")
    # Only while WE are the ones waiting. A row parked on the owner's answer can
    # sit for days; billing the provider's 15-minute budget for that time would
    # park it the moment they finally submit the form.
    if row.get("state") == "provisioning" and deadline and deadline < utc_now_iso():
        return await _requires_review(row, "poll_deadline_exceeded")
    return await _bump(row)


async def _bind(account_id: str, row: dict, resource: dict) -> dict:
    moved = await repo.transition(row["workflow_id"], from_state=row["state"], to_state="binding")
    if moved is None:
        # Someone else moved this row first (two concurrent readers, or a
        # second binder). It is not ours any more: reading credentials or
        # landing them again would be the duplicate the conditional exists to
        # stop. Report the row as it now stands.
        return await repo.get(row["workflow_id"]) or row
    # MERGED, not replaced: the transition's projection is the row as stored,
    # and `resource_id` is what selects our entry out of `environment_info`.
    row = {**row, **moved}
    # An account-scoped resource has no Project (spec 4.4), and binding NEVER
    # mints one — by here the create already resolved it.
    scope = (row.get("approval") or {}).get("scope")
    project_id = await _company_project_id(row["owner_id"]) if scope == "project" else None
    # The connection can expire between `complete` and the credential read.
    info, refused = await _read_or_park(
        row,
        api.environment_info(
            account_id, project=project_id, provider=row["provider_id"], service=row["service_ref"], reveal=True
        ),
    )
    if refused is not None:
        return refused
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
    # Best-effort receipt on the originating issue; `register` skips (debug
    # log, None) when the row carries no issue_id and never raises — a missing
    # receipt must not fail the rail.
    await work_products.register(
        row["owner_id"],
        type="artifact",
        provider=row["provider_name"],
        title=f"Provisioned {row['service_ref']}",
        issue_id=row.get("issue_id"),
        external_id=row["resource_id"],
        metadata={"workflow_id": row["workflow_id"], "level": "company"},
    )
    # Stamp the NAMES onto the workflow row too, so the facts file stays a
    # single query and can never disagree with the pointer.
    await repo.patch(row["workflow_id"], {"resource_name": pointer["resource_name"], "env_keys": pointer["env_keys"]})
    done = await repo.transition(row["workflow_id"], from_state="binding", to_state="active")
    await _write_facts(row["owner_id"])
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
    moved = await _poll_awaited_leg(account_id, row)
    # Still waiting afterwards? Then the only question left is whether they are
    # ever coming back. Asked ONCE, here, so no leg can forget it.
    return await _stale_if_expired(moved) if moved.get("state") == "awaiting_user_action" else moved


async def _poll_awaited_leg(account_id: str, row: dict) -> dict:
    """Whichever object knows the answer — chosen by what the row is WAITING
    for, never by which ids happen to be on it.

    A resource-information row still carries the `account_request_id` from its
    connect leg; polling that (already `complete`) request instead would walk
    forward into the resource poll on every tick.
    """
    awaiting = row.get("awaiting")
    if awaiting == "payment_setup":
        try:
            await api.payment_profile(account_id)
        except ProvisioningError as e:
            if e.code == "not_found":
                return row  # no card on file yet — keep waiting
            if is_transient(e):
                return await _bump(row, e.retry_after)
            return await _fail(row, e.code, e.message)
        moved = await repo.transition(
            row["workflow_id"],
            from_state=row["state"],
            to_state="connecting_provider",
            fields={"awaiting": None, "user_action_since": None},
        )
        return moved or {**row, "state": "connecting_provider"}

    if awaiting == "provider_information" and row.get("information_target") == "resource":
        return await _poll_resource(account_id, row)

    if row.get("account_request_id"):
        ar, refused = await _read_or_park(row, api.get_account_request(account_id, row["account_request_id"]))
        return refused if refused is not None else await _handle_account_request(account_id, row, ar)

    # A card with nothing to poll: the owner submits it through the owner
    # route, which takes the step itself.
    return row


async def _stale_if_expired(row: dict) -> dict:
    """A workflow nobody came back to. NOT terminal: the ids stay good and the
    next owner click re-mints the link, so this only stops the poller."""
    if row.get("state") == "stale":
        return row
    # `user_action_since`, never `updated_at`: a transient poll bumps `attempts`
    # (and with it `updated_at`) every few seconds, which would reset the
    # owner's seven days on every tick and the row would never go stale.
    since = row.get("user_action_since") or ""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=USER_ACTION_DEADLINE_S)).isoformat()
    if since and since < cutoff:
        moved = await repo.transition(row["workflow_id"], from_state=row["state"], to_state="stale")
        await _write_facts(row["owner_id"])
        return moved or {**row, "state": "stale"}
    return row


async def _reconcile_approval(row: dict) -> dict:
    """Read the consent card and resume the workflow if it has been answered.

    The answer lives in Paperclip, not here: an owner who approves on their
    phone never touches our routes. A Paperclip outage is transient by
    construction — the row is left exactly as it was and the next read looks
    again.
    """
    approval_id = row.get("approval_id")
    if not approval_id:
        return row
    try:
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        approval = await admin.get_approval(session_cookie=cookie, approval_id=approval_id)
    except (PaperclipApiError, httpx.HTTPError) as e:
        logger.warning("provisioning: approval read failed for %s: %s", row["workflow_id"], e)
        return row
    status = (approval or {}).get("status")
    if status not in _OUTCOMES:
        return row
    if status == "rejected":
        return await on_rejected(row["workflow_id"]) or row
    # An approver CANNOT edit a card's payload in Paperclip, so the only limit
    # this path can honour is the one the card was filed with. An owner who
    # changed the amount comes through the approve route, which passes theirs.
    return await on_approved(row["workflow_id"], usage_limit=row.get("usage_limit"), approved_by="paperclip") or row


def _too_soon(row: dict) -> bool:
    """Stripe asked us to wait — a 429's ``Retry-After``, stamped by ``_bump``.

    An unparseable value is no floor at all: a junk header must not be able to
    park a workflow that nothing else will come back to.
    """
    when = row.get("next_poll_after")
    if not when:
        return False
    try:
        return datetime.fromisoformat(when) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


async def advance(workflow: dict) -> dict:
    """One step. Terminal states and `ask` are no-ops; everything else moves or
    reports that it could not."""
    if not settings.STRIPE_PROVISIONING_ENABLED:
        # Rail off: freeze in place. No Stripe write, no state change — a row
        # resumes from exactly here the day the flag flips.
        return workflow
    state = workflow.get("state")
    if state in repo.TERMINAL_STATES or state == "ask":
        return workflow
    if state == "awaiting_approval":
        # Nothing moves until the owner answers, and the answer is Paperclip's,
        # not Stripe's.
        return await _reconcile_approval(workflow)
    account_id = await account_id_for(workflow["owner_id"])
    if state in ("awaiting_user_action", "stale"):
        return await _poll_user_action(account_id, workflow)
    if state == "connecting_provider":
        if workflow.get("account_request_id"):
            ar, refused = await _read_or_park(
                workflow, api.get_account_request(account_id, workflow["account_request_id"])
            )
            return refused if refused is not None else await _handle_account_request(account_id, workflow, ar)
        return await _connect_provider(account_id, workflow)
    if state == "provisioning":
        # Always the READ: `provisioning` is only ever entered after the
        # resource id is persisted, so a create here would be the second POST
        # the no-repeat rule exists to prevent.
        return await _poll_resource(account_id, workflow)
    if state == "binding":
        resource, refused = await _read_or_park(workflow, api.get_resource(account_id, workflow.get("resource_id")))
        return refused if refused is not None else await _bind(account_id, workflow, resource)
    return workflow


async def advance_on_read(workflow_id: str) -> dict:
    """Take ONE step, on somebody's read.

    There are no Provisioning webhooks, so the readers are the clock: Friday's
    status route, the owner's page loads, the owner's clicks. Each one moves a
    row by one step under a short claim, so two concurrent readers (or a
    rolling deploy's two instances) cannot file the same card or issue the same
    Stripe write twice — the loser simply reports the row as it stands.

    Never raises: a read must still answer when the step behind it could not.
    """
    claimed = await repo.claim(workflow_id, owner=INSTANCE_ID, ttl_s=READ_LEASE_S)
    if claimed is None:
        # Another reader holds it, or the row is gone. Its step is our step.
        return await repo.get(workflow_id) or {}
    if claimed.get("state") in repo.TERMINAL_STATES or _too_soon(claimed):
        await repo.release(workflow_id, owner=INSTANCE_ID)
        return claimed
    try:
        # ponytail: the caller's real bound is API Gateway's 30s integration
        # timeout (api-stack.ts), which a six-call step can exceed. A 504 there
        # is harmless — the step runs on to completion server-side and the
        # claim stops the retry re-entering it. Split the step if reads get slow.
        row = await advance(claimed)
    except WorkflowRefused as e:
        # A door the state machine will keep finding shut — the owner
        # disconnected the company Stripe account, say. Park it for the human
        # who has to fix it rather than re-reading it forever.
        row = await _requires_review(claimed, e.code)
    except Exception:
        logger.exception("provisioning: read-step failed for %s", workflow_id)
        row = claimed
    await repo.release(workflow_id, owner=INSTANCE_ID)
    return row
