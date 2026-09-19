"""Repair one owner's Hermes migration residue; dry-run unless --apply is given.

    python -m scripts.repair_hermes_migration --owner-id ORG --kind trial
    python -m scripts.repair_hermes_migration --owner-id ORG --kind credentials --apply

Run only through the approved backend operations path after code deployment,
in an owner maintenance window with no concurrent subscription/provider changes.
Billing is rechecked at each write boundary, but DynamoDB and S3 are not one
transaction. On failure inspect the printed progress and retry; do not assume
an all-or-nothing repair.
Trial mode patches primary/auxiliary model pins before reconciling AIP metadata
and every tracked bearer allowlist. A failed profile write still reconciles the
fence (retry repairs the profile); no token rotation or runtime stop is needed.
Credential mode replaces only the exact legacy example, preserving the rest of
AGENTS.md. Conditional S3 writes reject concurrent edits; .revision makes warm
runtimes re-pull. No SOUL, env, secrets, or scanner settings are rewritten.
Delete this one-shot repair and tests after the affected fleet has converged.
"""

from __future__ import annotations

import argparse
import copy
import asyncio
import uuid

import yaml

from core.dynamodb import run_in_thread
from core.repositories import billing_repo, container_repo
from core.services import hermes_profiles as profiles
from core.services.hermes_provisioning import reconcile_hermes_aips
from core.services.messaging import load_agent_roster
from core.services.paperclip_credential_rule import CREDENTIAL_REFERENCE, LEGACY_EXAMPLE


def _profile_repair(owner_id: str, agent_id: str, kind: str, arns: dict, apply: bool) -> str:
    bucket = profiles._require_owner_bucket()
    prefix = profiles.profile_prefix(owner_id, agent_id)
    key = prefix + ("config.yaml" if kind == "trial" else "AGENTS.md")
    original, etag = profiles._s3_get_versioned(bucket, key)
    if original is None:
        if kind == "credentials":
            return "absent"
        raise FileNotFoundError(key)
    if kind == "credentials":
        changed = original.replace(LEGACY_EXAMPLE, CREDENTIAL_REFERENCE)
    else:
        config = yaml.safe_load(original)
        if not isinstance(config, dict):
            raise ValueError(f"Invalid config for agent {agent_id}")
        model, aux_model, aux_provider = profiles.model_block("bedrock_trial", None, arns, False)
        updated = profiles._deep_merge(
            copy.deepcopy(config),
            {
                "model": model,
                "auxiliary": profiles.auxiliary_tasks_block(aux_model, aux_provider),
            },
        )
        changed = yaml.safe_dump(updated, sort_keys=False) if updated != config else original
    if changed == original:
        if apply and (kind == "trial" or CREDENTIAL_REFERENCE in original):
            # A prior write may have landed while its revision put failed.
            # Re-publish only recognized repair targets, without rewriting content.
            profiles._s3_put(bucket, prefix + ".revision", uuid.uuid4().hex)
        return "current"
    if not apply:
        return "would-update"
    profiles._s3_put_conditionally(bucket, key, changed, etag)
    profiles._s3_put(bucket, prefix + ".revision", uuid.uuid4().hex)
    return "updated"


async def _assert_trial_unchanged(owner_id: str, initial: dict) -> None:
    current = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    fields = ("subscription_status", "provider_choice", "byo_provider")
    if any(current.get(field) != initial.get(field) for field in fields):
        raise ValueError("Billing changed during trial repair; stop and inspect partial progress before retrying")


async def repair_owner(owner_id: str, kind: str, *, apply: bool = False) -> None:
    if kind not in ("trial", "credentials"):
        raise ValueError("Unknown repair kind")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    if not row.get("agentcore_runtime_arn"):
        raise ValueError("Owner has no Hermes runtime")
    arns = row.get("bedrock_aip_arns") or {}
    if kind == "trial":
        billing = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        if billing.get("subscription_status") != "trial_local" or not arns.get("sonnet"):
            raise ValueError("Trial repair requires trial_local billing and an existing Sonnet AIP")
    agents = await load_agent_roster(owner_id)
    try:
        for agent in agents:
            if kind == "trial":
                await _assert_trial_unchanged(owner_id, billing)
            result = await run_in_thread(_profile_repair, owner_id, agent["id"], kind, arns, apply)
            print(f"{owner_id} {agent['id']} {result}")
    finally:
        # Fail closed on a partial profile repair: stale premium pins may fail a
        # turn until retry, but must not leave a trial bearer over-permitted.
        if kind == "trial" and apply:
            await _assert_trial_unchanged(owner_id, billing)
            await reconcile_hermes_aips(owner_id)
    if kind == "trial":
        print(f"{owner_id} {'reconciled' if apply else 'would-reconcile'} AIP plans and bearer tiers")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--kind", required=True, choices=("trial", "credentials"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    await repair_owner(args.owner_id, args.kind, apply=args.apply)


if __name__ == "__main__":
    asyncio.run(main())
