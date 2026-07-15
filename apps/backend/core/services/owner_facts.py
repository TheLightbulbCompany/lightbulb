"""Owner-facts file: per-owner dynamic platform state on EFS.

``isol8-facts.json`` sits at the EFS user root (in-container:
``~/.openclaw/isol8-facts.json``) so every agent — Friday, Atlas,
scaffolded specialists, marketplace hires — can read the owner's
CURRENT plan/provider state instead of trusting seed files rendered
once at provision time. The backend is the sole writer; writes happen
at provision (``ecs_manager.write_user_configs``) and on provider
switches (``billing_service.change_provider``). Best-effort by design:
a facts write must never block provisioning or a provider switch.

The bundled ``isol8`` platform skill tells agents to read this file
for plan/provider questions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.containers.workspace import get_workspace
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

FACTS_FILENAME = "isol8-facts.json"


def format_provider_label(provider_choice: str | None, byo_provider: str | None) -> str:
    """Map (provider_choice, byo_provider) to a human-readable label."""
    if provider_choice == "chatgpt_oauth":
        return "ChatGPT (their own OpenAI account)"
    if provider_choice == "bedrock_claude":
        return "Bedrock (Claude, Isol8-managed credits)"
    if provider_choice == "bedrock_trial":
        return "Bedrock (Claude Sonnet, free trial — $3/day)"
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return "BYO Anthropic key"
        if byo_provider == "openai":
            return "BYO OpenAI key"
        return "BYO API key (provider unspecified)"
    return f"unknown ({provider_choice})"


def build_owner_facts(owner_id: str, billing_account: dict | None) -> dict:
    """Render the facts dict from a billing row. Pure — no I/O."""
    from core.auth import is_org_owner_id

    account = billing_account or {}
    status = account.get("subscription_status")
    if status == "trial_local":
        plan = "Free tier (no card): Bedrock Claude Sonnet only, $3/day cap, container sleeps when idle"
    elif status:
        plan = "Starter ($50/mo flat fee)"
    else:
        plan = "No billing account yet"
    return {
        "plan": plan,
        "subscription_status": status,
        "provider": format_provider_label(account.get("provider_choice"), account.get("byo_provider")),
        "provider_choice": account.get("provider_choice"),
        "owner_kind": "org" if is_org_owner_id(owner_id) else "personal",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def write_owner_facts(owner_id: str) -> None:
    """Render + write the facts file. Best-effort: never raises."""
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
        facts = build_owner_facts(owner_id, account)
        get_workspace().write_file(owner_id, FACTS_FILENAME, json.dumps(facts, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("write_owner_facts: skipped for %s: %s", owner_id, e)
