"""BREAK-GLASS: restore direct container→Bedrock invocation when the gate is down.

The bedrock-gate is the standard (and only) Bedrock path: owner roles carry NO
``bedrock:Invoke*`` and containers authenticate to the gate with bearer tokens.
If the gate hard-fails, Bedrock inference fails with it (deliberate fail-closed).
This script is the escape hatch. Full procedure — BOTH steps are required — in
``docs/runbooks/bedrock-gate-break-glass.md``:

  1. Flip the SSM parameter to direct mode (config side)::

         aws ssm put-parameter --name /lightbulb/{env}/bedrock-gate-url \\
             --value direct --overwrite

  2. Re-grant direct invoke + re-render configs (this script)::

         cd apps/backend && uv run python -m scripts.bedrock_gate_break_glass \\
             --env prod --apply

     For each Bedrock-path owner it PUTS the pre-gateway direct-invoke inline
     policy (``build_owner_bedrock_direct_policy``) on the owner role, then
     reprovisions so the owner's agent profiles + boot secret re-render in direct
     mode (they read the SSM parameter via ``bedrock_gate_url.gate_url``).

REVERT (gate healthy again): put the real URL back into the SSM parameter and
re-run this script with ``--revert`` — it re-puts the fence policy
(``build_owner_bedrock_policy``, no invoke) and reprovisions back through the
gate.

Dry-run by default; ``--apply`` mutates. Only Bedrock-path owners
(``trial_local`` / ``bedrock_claude``) are touched.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _classify_plan(billing: dict | None) -> str | None:
    """ "trial" / "paid" for Bedrock-path owners, None for everyone else."""
    if not billing:
        return None
    if billing.get("subscription_status") == "trial_local":
        return "trial"
    if billing.get("provider_choice") == "bedrock_claude":
        return "paid"
    return None


async def main(*, env: str, apply: bool, revert: bool, owner: str | None) -> int:
    os.environ["ENVIRONMENT"] = env

    from core.billing import bedrock_aip
    from core.repositories import billing_repo, container_repo
    from core.services.provision_dispatch import dispatch_provision

    # Resolve targets: one owner, or every Bedrock-path owner with a container.
    if owner:
        rows = [await container_repo.get_by_owner_id(owner)]
        if not rows[0]:
            print(f"ERR no container row for {owner}")
            return 1
    else:
        rows = await container_repo.scan_all()

    targets: list[tuple[str, str]] = []
    for row in rows:
        if not row:
            continue
        owner_id = row.get("owner_id")
        aip_arns = row.get("bedrock_aip_arns") or {}
        if not owner_id or not aip_arns:
            continue
        billing = await billing_repo.get_by_owner_id(owner_id)
        plan = _classify_plan(billing)
        if plan is None:
            continue  # BYOK / OAuth — no Bedrock, nothing to grant
        targets.append((owner_id, plan))
        mode = "FENCE (gate-only)" if revert else "DIRECT (break-glass)"
        print(f"  PLAN owner={owner_id} plan={plan} -> put {mode} policy + reprovision")

    print(f"Counts: total={len(targets)} mode={'revert-to-gate' if revert else 'grant-direct'}")
    if not targets:
        print("No Bedrock-path owners in scope — nothing to do.")
        return 0
    if not apply:
        print("DRY RUN — pass --apply to mutate IAM and reprovision")
        return 0

    failures: list[str] = []
    for owner_id, _plan in targets:
        try:
            row = await container_repo.get_by_owner_id(owner_id)
            aip_arns = (row or {}).get("bedrock_aip_arns") or {}
            builder = (
                bedrock_aip.build_owner_bedrock_policy if revert else bedrock_aip.build_owner_bedrock_direct_policy
            )
            policy = builder(
                owner_id=owner_id,
                aip_arns=aip_arns,
                regions=bedrock_aip.aip_fan_out_regions(),
            )
            await bedrock_aip.put_owner_bedrock_policy(owner_id, policy)
            # Re-render the owner's agent profiles + boot secret for the current SSM mode.
            await dispatch_provision(owner_id)
            print(f"  OK   {owner_id}")
        except Exception as e:  # noqa: BLE001 — one owner's failure must not abort the fleet
            print(f"  ERR  {owner_id}: {e}")
            failures.append(owner_id)

    if failures:
        print(f"FAILED for {len(failures)} owner(s): {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--owner", help="Single owner id (default: every Bedrock-path owner)")
    parser.add_argument("--apply", action="store_true", help="Mutate (default: dry run)")
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Re-put the gate-only FENCE policy (after the gate is healthy again)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(env=args.env, apply=args.apply, revert=args.revert, owner=args.owner)))
