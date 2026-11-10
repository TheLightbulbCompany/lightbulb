"""Voice-plan side effects (OpenRouter limit, owner facts, runtime reconcile) after an entitlement change."""

from datetime import UTC, datetime
import hashlib
import json

from core.config import settings
from core.repositories import billing_repo


def utc(raw: str) -> str:
    return datetime.fromisoformat(raw).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def refresh(owner_id: str) -> None:
    """Repairable side-effect receipt, separate from payment truth; facts still have their existing sole store."""
    from core.billing.voice_billing import has_voice_plan
    from core.services import openrouter_keys, owner_facts, proactive
    from core.services.provision_dispatch import dispatch_reconcile

    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    if not account.get("metronome_voice_contract_id"):
        return
    revision = hashlib.sha256(
        json.dumps(
            {
                "active": has_voice_plan(account),
                "stripe": account.get("voice_plan_stripe_status"),
                "ends": account.get("voice_plan_ending_at"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if account.get("voice_plan_runtime_sync_revision") == revision:
        return
    if settings.OPENROUTER_WORKSPACE_ID:
        await openrouter_keys.relimit_owner_key(owner_id)
    await owner_facts.write_owner_facts(owner_id)
    if has_voice_plan(account):
        if not await dispatch_reconcile(owner_id):
            raise RuntimeError("voice runtime reconcile could not start; retry")
    else:
        await proactive.ensure(owner_id)
    await billing_repo.set_voice_runtime_sync_revision(
        owner_id, contract_id=account["metronome_voice_contract_id"], revision=revision
    )
