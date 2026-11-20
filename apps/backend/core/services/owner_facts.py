"""Owner-facts file: per-owner dynamic platform state in S3 Hermes profiles.

``lightbulb-facts.json`` is written to each agent's S3 profile and restored at
``$HERMES_HOME/lightbulb-facts.json`` so every agent can read the owner's
CURRENT plan/provider state instead of trusting seed files rendered once at
provision time. The backend is the sole writer; writes happen at provision
(``hermes_provisioning._stage_owner_facts``), on provider switches
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
from core.repositories import billing_repo
from core.services.agent_email import public_address
from core.dynamodb import run_in_thread, utc_now_iso

logger = logging.getLogger(__name__)

FACTS_FILENAME = "lightbulb-facts.json"


def _paid_plan_label() -> str:
    """Base, Ultra and Team are all retired; only the Phone plan is sold."""
    return "An older plan that is no longer sold (not the Lightbulb Phone plan)"


def format_provider_label(provider_choice: str | None, byo_provider: str | None) -> str:
    """Map (provider_choice, byo_provider) to a human-readable label."""
    if provider_choice == "chatgpt_oauth":
        return "ChatGPT (their own OpenAI account)"
    if provider_choice == "openrouter_managed":
        return "Lightbulb-managed credits"
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return "BYO Anthropic key"
        if byo_provider == "openai":
            return "BYO OpenAI key"
        return "BYO API key (provider unspecified)"
    return f"unknown ({provider_choice})"


def build_owner_facts(
    owner_id: str,
    billing_account: dict | None,
    *,
    email_address: str | None = None,
    owner_timezone: str = "UTC",
    teammates: list[dict] | None = None,
    connected_integrations: list[dict] | None = None,
) -> dict:
    """Render the facts dict from a billing row. Pure — no I/O."""
    from core.auth import is_org_owner_id

    # Single row-level mapping shared with the provision surface — handles
    # the trial_local short-circuit (trial rows carry NO provider_choice)
    # and the managed default. Lazy import: container_provision
    # pulls in repos this module doesn't otherwise need.
    from core.services.container_provision import provider_choice_from_row

    account = billing_account or {}
    from core.billing.voice_billing import INCLUDED_MINUTES, MONTHLY_USD, OVERAGE_USD_PER_MINUTE, has_voice_plan

    voice_paid = has_voice_plan(account)
    status = account.get("subscription_status")
    if voice_paid:
        voice_label = (
            f"Lightbulb Phone plan (${MONTHLY_USD}/mo, {INCLUDED_MINUTES:,} minutes for calls and meetings, "
            f"then ${OVERAGE_USD_PER_MINUTE}/min; UTC periods)"
        )
        plan = voice_label
        if account.get("voice_plan_ending_at"):
            plan += f"; cancellation confirmed at {account['voice_plan_ending_at']}"
    elif status == "trial_local":
        plan = "Free trial (no card, 30 days): Lightbulb-managed DeepSeek within a daily allowance"
        if account.get("metronome_voice_contract_id"):
            from core.services.trial_cap import is_trial_expired

            if is_trial_expired(account):
                plan = "Trial ended (reading-only)"
    elif status in ("active", "trialing"):
        plan = _paid_plan_label()
    elif status:
        # canceled / past_due / unpaid / incomplete — do NOT tell agents the
        # owner is on a paid plan when the subscription has lapsed.
        plan = f"Subscription not active (status: {status})"
    elif account.get("stripe_subscription_id"):
        # Legacy rows whose status hasn't been backfilled yet — treated as
        # subscribed by provision_gate.is_subscription_active.
        plan = _paid_plan_label()
    else:
        plan = "No billing account yet"
    if account.get("metronome_voice_contract_id") and not voice_paid:
        plan += "; Lightbulb Phone plan coverage is not active; check payment in Settings"

    provider_choice, byo_provider = provider_choice_from_row(account)
    from core.services.trial_cap import is_managed_capped

    included_voice_model = voice_paid and is_managed_capped(account)
    if account.get("company_stripe_account_id"):
        company_stripe = "connected" if account.get("company_stripe_charges_enabled") else "pending"
    else:
        company_stripe = "none"
    return {
        **(
            {
                "voice_plan": {
                    "collected_coverage_active": voice_paid,
                    "ends_at": account.get("voice_plan_ending_at"),
                    "payment_required": account.get("voice_plan_stripe_status") in ("past_due", "unpaid"),
                    "managed_daily_allowance_usd": settings.OPENROUTER_BASE_KEY_DAILY_LIMIT_USD
                    if included_voice_model
                    else None,
                    "billing_timezone": "UTC",
                }
            }
            if account.get("metronome_voice_contract_id")
            else {}
        ),
        # The container builds its own /u/{owner_id} backend URLs from this,
        # so there is no second source of truth for who it is acting as.
        "owner_id": owner_id,
        "plan": plan,
        "subscription_status": status,
        "provider": "Included managed model (daily allowance)"
        if included_voice_model
        else format_provider_label(provider_choice, byo_provider),
        "provider_choice": provider_choice,
        "company_stripe": company_stripe,
        "owner_kind": "org" if is_org_owner_id(owner_id) else "personal",
        # This environment's app host. One image serves dev and prod, so the bundled skill's
        # links say thelightbulb.company and agents swap this in when they hand the owner a link.
        "app_url": (settings.FRONTEND_URL or "").rstrip("/"),
        # TRI-STATE with `email_address` below, and load-bearing.
        #
        # `email_address` alone cannot distinguish "email is off for this
        # owner" from "you can send; your address is created by your first
        # send" — and the address IS minted by the first send, so it is null
        # right up until the agent acts. The isol8 skill's rule is "null =>
        # don't claim an address and don't offer email", so without this flag
        # the agent reads null, correctly declines, and first contact
        # deadlocks on its own instructions.
        #
        #   email_enabled=False                  -> email is off here
        #   email_enabled=True,  address=None    -> may send; sending mints it
        #   email_enabled=True,  address="..."   -> that is the address
        "email_enabled": bool((settings.AGENT_EMAIL_DOMAIN or "").strip()),
        "email_address": email_address,
        # IANA zone of the workspace OWNER (the org creator), so the CEO's daily brief
        # lands on the owner's morning. Refreshed on every facts write (provision/reconcile).
        "owner_timezone": owner_timezone,
        # Declared manifest of the workspace, so agents read it instead of inferring:
        # every live agent (ids are what assignments use) and the connected
        # integrations (provider + rail, NAMES only).
        "teammates": list(teammates or []),
        "connected_integrations": list(connected_integrations or []),
        "generated_at": utc_now_iso(),
    }


async def _lookup_email_address(owner_id: str) -> str | None:
    """The workspace's public team address (every agent sends from it), or None.
    Never raises: the address is a nicety and must not stop the rest of the
    facts from being written."""
    try:
        return await public_address(owner_id)
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: team-address lookup failed for %s: %s", owner_id, e)
        return None


async def _lookup_owner_timezone(owner_id: str) -> str:
    """The workspace owner's timezone, else ``"UTC"``. Never raises.

    The owner is the Clerk org's CREATOR -- the same stable choice
    ``provision_runner``'s step 0 makes for the company's bootstrap identity;
    members are never consulted. ``user_repo.get_timezone`` applies its own
    default for a user with none set; UTC is only for an unresolvable owner.
    """
    try:
        from core.auth import is_org_owner_id
        from core.repositories import user_repo
        from core.services import clerk_admin

        if not is_org_owner_id(owner_id):
            return "UTC"
        creator = ((await clerk_admin.get_organization(owner_id)) or {}).get("created_by")
        if not creator:
            return "UTC"
        return await user_repo.get_timezone(creator)
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: owner timezone lookup failed for %s: %s", owner_id, e)
        return "UTC"


def _teammates_from_roster(roster: list[dict], starter_ids: dict[str, str] | None) -> list[dict]:
    """``[{id, name, role, is_ceo}]``. ``role`` is the starter role key where the agent is a
    starter, else its own role string. The CEO is the starter ``ceo`` id, else a legacy
    root agent (role ``ceo`` reporting to nobody)."""
    by_id = {v: k for k, v in (starter_ids or {}).items()}
    ceo_id = (starter_ids or {}).get("ceo")
    out = []
    for a in roster:
        role = by_id.get(a["id"]) or a.get("role")
        is_ceo = a["id"] == ceo_id if ceo_id else (a.get("role") == "ceo" and not a.get("reportsTo"))
        out.append({"id": a["id"], "name": a.get("name"), "role": role, "is_ceo": bool(is_ceo)})
    return out


async def _lookup_teammates(owner_id: str, roster: list[dict] | None = None) -> list[dict]:
    """Never raises; a failed lookup is an empty list."""
    try:
        from core.repositories.workspace_repo import WorkspaceRepo
        from core.services.messaging import load_agent_roster

        if roster is None:
            roster = await load_agent_roster(owner_id)
        company = await WorkspaceRepo().resolve_owner_row(owner_id, consistent_read=True)
        return _teammates_from_roster(roster, getattr(company, "starter_agent_ids", None))
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: teammates lookup failed for %s: %s", owner_id, e)
        return []


async def _lookup_connected_integrations(owner_id: str) -> list[dict]:
    """``[{provider, rail}]`` for every CONNECTED integrations row. NAMES only: nothing from the
    row but its key and status is read. A ``projects/<p>`` row is the Stripe Projects rail; any
    other row takes its registry actor (``mcp``/``cli``/``skill``), else ``oauth``. Rows outside the catalog, and a ``projects/`` row with no resources, are skipped. Never raises."""
    try:
        from core.repositories import integrations_repo
        from core.services.credentials import registry

        rows = await integrations_repo.list_for_owner(owner_id)
        providers = registry.all_providers()
        out = []
        for row in rows:
            key = row.get("provider") or ""
            if not key or row.get("status") != "connected":
                continue
            if key.startswith(integrations_repo.PROJECTS_KEY_PREFIX):
                if not row.get("provider_resources"):
                    continue  # last resource removed: the row stays "connected" but nothing is bound
                out.append({"provider": key[len(integrations_repo.PROJECTS_KEY_PREFIX) :], "rail": "projects"})
                continue
            provider = providers.get(key)
            if provider is None:
                continue  # not a catalog integration (e.g. the Slack channel's bookkeeping row)
            actor = provider.actor
            out.append({"provider": key, "rail": actor if actor in ("mcp", "cli", "skill") else "oauth"})
        return sorted(out, key=lambda e: (e["provider"], e["rail"]))
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: integrations lookup failed for %s: %s", owner_id, e)
        return []


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
        from core.services.messaging import load_agent_roster

        roster = await load_agent_roster(owner_id)
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        if account_overrides:
            account = {**account, **account_overrides}
        facts = build_owner_facts(
            owner_id,
            account,
            email_address=await _lookup_email_address(owner_id),
            owner_timezone=await _lookup_owner_timezone(owner_id),
            teammates=await _lookup_teammates(owner_id, roster),
            connected_integrations=await _lookup_connected_integrations(owner_id),
        )
        facts_json = json.dumps(facts, indent=2) + "\n"
        # Ruling M6-R30: stage the facts file into every roster profile in S3
        # (hermes_profiles.write_facts_to_profiles) -- one shared boto3 client, bumping each
        # profile's .revision last so the bridge's ensure_profile() re-pull picks it up before
        # the agent's next turn.
        from core.services import hermes_profiles

        agent_ids = [agent["id"] for agent in roster]
        await run_in_thread(hermes_profiles.write_facts_to_profiles, owner_id, agent_ids, facts_json)
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("write_owner_facts: skipped for %s: %s", owner_id, e)
