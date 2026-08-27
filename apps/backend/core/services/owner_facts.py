"""Owner-facts file: per-owner dynamic platform state on EFS.

``lightbulb-facts.json`` sits at the EFS user root (in-container:
``~/.openclaw/lightbulb-facts.json`` — the access point chroots
``/users/{user_id}`` to ``/home/node/.openclaw``) so every agent — Friday,
Atlas, scaffolded specialists, marketplace hires — can read the owner's
CURRENT plan/provider state instead of trusting seed files rendered once at
provision time. The backend is the sole writer; writes happen at provision
(``ecs_manager.write_user_configs``), on provider switches
(``billing_service.change_provider``), and on subscription-state webhooks
(``stripe_webhook_handlers.handle_subscription_state``). Best-effort by
design: a facts write must never block any of those callers.

The bundled ``lightbulb`` platform skill tells agents to read this file for
plan/provider questions.
"""

from __future__ import annotations

import json
import logging

from core.config import settings
from core.containers.workspace import get_workspace
from core.repositories import agent_email_repo, billing_repo
from core.dynamodb import utc_now_iso

logger = logging.getLogger(__name__)

FACTS_FILENAME = "lightbulb-facts.json"
# Pre-rebrand name. Written alongside the current one so an image built before
# the rename — which points agents and its own boot script at this path — keeps
# reading fresh data. Friday's USER.md pointer is repointed by
# ``friday_identity._migrate_legacy_user_md``.
LEGACY_FACTS_FILENAME = "isol8-facts.json"


def format_provider_label(provider_choice: str | None, byo_provider: str | None) -> str:
    """Map (provider_choice, byo_provider) to a human-readable label."""
    if provider_choice == "chatgpt_oauth":
        return "ChatGPT (their own OpenAI account)"
    if provider_choice == "bedrock_claude":
        return "Bedrock (Claude, Lightbulb AI-managed credits)"
    if provider_choice == "bedrock_trial":
        return "Bedrock (Claude Sonnet, free trial — $3/day)"
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return "BYO Anthropic key"
        if byo_provider == "openai":
            return "BYO OpenAI key"
        return "BYO API key (provider unspecified)"
    return f"unknown ({provider_choice})"


def build_owner_facts(owner_id: str, billing_account: dict | None, *, email_address: str | None = None) -> dict:
    """Render the facts dict from a billing row. Pure — no I/O."""
    from core.auth import is_org_owner_id

    # Single row-level mapping shared with the provision surface — handles
    # the trial_local short-circuit (trial rows carry NO provider_choice)
    # and the bedrock_claude legacy default. Lazy import: container_provision
    # pulls in repos this module doesn't otherwise need.
    from core.services.container_provision import provider_choice_from_row

    account = billing_account or {}
    status = account.get("subscription_status")
    if status == "trial_local":
        plan = "Free tier (no card): Bedrock Claude Sonnet only, $3/day cap, container sleeps when idle"
    elif status in ("active", "trialing"):
        if account.get("subscription_item_id"):
            # Teams per-seat subscription (set_team_subscription stamps the
            # item id + seat count) — a distinct pricing path, not the flat fee.
            seats = account.get("seat_count")
            plan = f"Teams (per-seat subscription, {seats} seats)" if seats else "Teams (per-seat subscription)"
        else:
            plan = "Starter ($50/mo flat fee)"
    elif status:
        # canceled / past_due / unpaid / incomplete — do NOT tell agents the
        # owner is on a paid plan when the subscription has lapsed.
        plan = f"Subscription not active (status: {status})"
    elif account.get("stripe_subscription_id"):
        # Legacy rows whose status hasn't been backfilled yet — treated as
        # subscribed by provision_gate.is_subscription_active.
        plan = "Starter ($50/mo flat fee)"
    else:
        plan = "No billing account yet"

    provider_choice, byo_provider = provider_choice_from_row(account)
    return {
        # The container builds its own /u/{owner_id} backend URLs from this,
        # so there is no second source of truth for who it is acting as.
        "owner_id": owner_id,
        "plan": plan,
        "subscription_status": status,
        "provider": format_provider_label(provider_choice, byo_provider),
        "provider_choice": provider_choice,
        "owner_kind": "org" if is_org_owner_id(owner_id) else "personal",
        # TRI-STATE with `email_address` below, and load-bearing.
        #
        # `email_address` alone cannot distinguish "email is off for this
        # owner" from "you can send; your address is created by your first
        # send" — and the address IS minted by the first send, so it is null
        # right up until the agent acts. The lightbulb skill's rule is "null =>
        # don't claim an address and don't offer email", so without this flag
        # the agent reads null, correctly declines, and first contact
        # deadlocks on its own instructions.
        #
        #   email_enabled=False                  -> email is off here
        #   email_enabled=True,  address=None    -> may send; sending mints it
        #   email_enabled=True,  address="..."   -> that is the address
        "email_enabled": bool((settings.AGENT_EMAIL_DOMAIN or "").strip()),
        "email_address": email_address,
        "generated_at": utc_now_iso(),
    }


async def _lookup_email_address(owner_id: str) -> str | None:
    """The owner's advertised agent email, or None.

    Selects the ``active`` row (aliases still route but are not advertised)
    and breaks ties on the earliest ``created_at`` — a rare re-provision can
    leave two active rows, and DynamoDB's return order is arbitrary, so an
    unordered pick would make the agent's own address nondeterministic.
    Never raises: the address is a nicety and must not stop the rest of the
    facts from being written.
    """
    try:
        rows = await agent_email_repo.query_by_owner(owner_id)
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: agent-email lookup failed for %s: %s", owner_id, e)
        return None
    active = [r for r in rows if r.get("status") == "active"]
    if not active:
        return None
    active.sort(key=lambda r: r.get("created_at") or "")
    return active[0].get("address")


async def write_owner_facts(owner_id: str, *, account_overrides: dict | None = None) -> None:
    """Render + write the facts file. Best-effort: never raises.

    The row read is STRONGLY consistent: the facts file often gets written
    right after a billing mutation (possibly from a different process — e.g.
    the webhook commits, then the reprovision's config write re-reads), and
    an eventually consistent read observing the pre-write row would roll the
    file back to stale state (e.g. a just-converted owner back to the free
    tier). ``account_overrides`` additionally lets a caller that JUST
    committed billing fields pin the written values explicitly.
    """
    try:
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        if account_overrides:
            account = {**account, **account_overrides}
        facts = build_owner_facts(owner_id, account, email_address=await _lookup_email_address(owner_id))
        blob = json.dumps(facts, indent=2) + "\n"
        ws = get_workspace()
        ws.write_file(owner_id, FACTS_FILENAME, blob)
        # The pre-rebrand filename is what an image built before the rename
        # tells agents (and its boot script) to read. Keep it in step rather
        # than deleting it — a stale snapshot is worse than a duplicate, and a
        # missing one breaks the boot script's owner-id fallback. Drop this
        # write once dev and prod are pinned to a post-rename image.
        try:
            ws.write_file(owner_id, LEGACY_FACTS_FILENAME, blob)
        except Exception:  # noqa: BLE001 — facts must never block callers
            pass
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("write_owner_facts: skipped for %s: %s", owner_id, e)
