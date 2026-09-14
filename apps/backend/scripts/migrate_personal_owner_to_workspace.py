"""Move ONE paying legacy personal owner onto a workspace (a Clerk org they admin).

Workspace-first (2026-09-10) refuses every product API for a caller with no org
(``resolve_owner_id`` -> 409 ``workspace_required``), so a legacy PERSONAL owner
(owner_id == Clerk user_id) who still pays is locked out of their own workspace
until they have one. This script gives them one and carries their state across,
so the normal org provisioning builds the org container on top of it instead of
starting them over on a fresh trial next to the box they pay for.

Runs INSIDE the backend task via ECS exec (needs the Paperclip internal URL and
the EFS mount)::

    B64=$(base64 < apps/backend/scripts/migrate_personal_owner_to_workspace.py | tr -d '\\n')
    aws ecs execute-command --cluster lightbulb-prod-container --task <backend task> \\
      --container backend --interactive \\
      --command "/bin/sh -c 'cd /app && echo $B64 | base64 -d | uv run python - --owner user_x [--apply] [--org org_y] [--org-name NAME] [--provision]'"

Exactly one owner per run; dry-run by default. Order, and why:

1. Clerk org (``created_by`` = the owner, so they are admin). ``--org`` resumes a
   partial run onto an org already created for them.
2. Billing row copied to the org id (same Stripe customer, subscription, provider,
   credits, Metronome ids), personal row deleted. Stripe webhooks resolve the
   owner through the ``stripe-customer-index`` GSI, so the copied row is what
   they find. Billing moves FIRST so the org is entitled the moment it first polls.
3. Personal box scaled to 0 and drained; for a ChatGPT-OAuth owner the rotated
   OAuth seed is harvested off the stopped box so the connection survives.
4. EFS ``users/<user>`` renamed to ``users/<org>`` (same filesystem) BEFORE any
   provisioning, so the org's access point roots on the existing agent
   workspaces, memory and skills.
5. Litestream replicas copied ``<user>/`` -> ``<org>/`` (the source is left for
   the purge), so sessions and transcripts restore on first boot.
6. ``paperclip-companies`` row re-pointed at the org with a service token whose
   ``sub`` is the org (the container owner). The agents' adapter configs are NOT
   patched here: ``register_paperclip_agent_devices`` re-binds every agent during
   the org provisioning.
7. Messaging: the whole ``OWNER#<user>`` partition (workspace id, room catalog,
   user streams, idempotency receipts) moves to ``OWNER#<org>`` and each room's
   META ``ownerId`` follows, so chat history, rooms and the AppSync channel
   segment carry over unchanged.
8. ``--provision`` dispatches the org provisioning immediately (the canary can be
   verified without the user signing in); otherwise their first sign-in does it.

Left for ``purge_legacy_personal_workspaces.py --owner <user>`` afterwards: the
personal ECS service, access point, containers row, Bedrock profile and gate
token, the old replica prefix. Never touched: the Clerk user, the ``users`` row,
OAuth tokens (keyed by user id), usage counters, Stripe.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import random
import re
import string
import sys
from datetime import datetime, timezone
from typing import Any

import boto3
import httpx

from core.auth import is_org_owner_id
from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.dynamodb import run_in_thread
from core.encryption import encrypt
from core.repositories import billing_repo, container_repo, messaging_repo
from core.repositories.messaging_repo import owner_pk, room_pk
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services import clerk_admin, service_token
from core.services.entitlement_teardown import still_entitled
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

# Piped into a deployed backend task via ECS exec, this script may run against an
# image that predates ``clerk_admin.create_organization`` — fall back to an inline
# copy of that one POST so the migration does not have to wait for a promotion.
try:
    from core.services.clerk_admin import SlugTakenError, create_organization
except ImportError:  # pragma: no cover — only the deployed-image path

    class SlugTakenError(Exception):
        pass

    async def create_organization(*, name: str, slug: str, created_by: str):
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.clerk.com/v1/organizations",
                headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}", "Content-Type": "application/json"},
                json={"name": name, "slug": slug, "created_by": created_by},
            )
        if resp.status_code == 422 and any(
            e.get("code") in ("form_identifier_exists", "duplicate_record") for e in (resp.json().get("errors") or [])
        ):
            raise SlugTakenError(slug)
        return resp.json() if resp.status_code < 400 else None


_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")


class _Out(list):
    """Report lines: kept for the exit code, printed as they happen."""

    def append(self, line: str) -> None:  # type: ignore[override]
        super().append(line)
        print(line, flush=True)


# ---------------------------------------------------------------------------
# pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def slugify(text: str, *, max_len: int = 40) -> str:
    s = _SLUG_INVALID_RE.sub("-", text.lower()).strip("-")[:max_len].strip("-")
    return s or "workspace"


def check_preconditions(
    owner_id: str,
    billing: dict[str, Any] | None,
    container: dict[str, Any] | None,
    pc_row: PaperclipCompany | None,
    memberships: list[dict[str, Any]],
    *,
    org_override: str | None = None,
) -> str | None:
    """``None`` when the owner is migratable, else the refusal reason."""
    if is_org_owner_id(owner_id) or not owner_id.startswith("user_"):
        return "owner must be a Clerk user id"
    if org_override is not None and not is_org_owner_id(org_override):
        return "--org must be a Clerk org id"
    if not still_entitled(billing):
        return "not entitled — this is a purge candidate, not a migration"
    if container is None:
        return "no containers row — nothing to carry over"
    if pc_row is None:
        return "no paperclip-companies row"
    if is_org_owner_id(pc_row.org_id) and pc_row.org_id != org_override:
        return f"paperclip row already points at {pc_row.org_id}"
    member_ids = [m.get("id") for m in memberships]
    if member_ids and org_override is None:
        return f"already a member of {member_ids} — pass --org <id> to resume onto one of them"
    if org_override is not None and org_override not in member_ids:
        return f"--org {org_override} is not one of the owner's memberships {member_ids}"
    return None


def billing_for_org(row: dict[str, Any], org_id: str, *, now_iso: str) -> dict[str, Any]:
    """The same billing row keyed by the org: every entitlement field intact."""
    new = dict(row)
    new["owner_id"] = org_id
    new["owner_type"] = "org"
    new["migrated_from"] = row["owner_id"]
    new["updated_at"] = now_iso
    return new


def paperclip_row_for_org(
    row: PaperclipCompany, org_id: str, token_encrypted: str, *, now: datetime
) -> PaperclipCompany:
    """The same Paperclip row pointed at the org, with a token whose ``sub`` is the org."""
    return dataclasses.replace(row, org_id=org_id, service_token_encrypted=token_encrypted, updated_at=now)


def rekeyed_owner_item(item: dict[str, Any], owner_id: str, org_id: str) -> dict[str, Any]:
    """An ``OWNER#<user>`` messaging row moved under ``OWNER#<org>``."""
    new = dict(item)
    new["pk"] = owner_pk(org_id)
    if new.get("ownerId") == owner_id:
        new["ownerId"] = org_id
    return new


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


async def _paperclip_company_name(pc_row: PaperclipCompany) -> str | None:
    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=30.0)
    try:
        cookie = await get_admin_session_cookie(http)
        company = await PaperclipAdminClient(http_client=http).get_company(
            session_cookie=cookie, company_id=pc_row.company_id
        )
        name = (company or {}).get("name") or (company or {}).get("company", {}).get("name")
        return str(name) if name else None
    except Exception:  # noqa: BLE001 — a name is nice to have, never load-bearing
        return None
    finally:
        await http.aclose()


async def _create_org(name: str, owner_id: str, out: list[str]) -> str | None:
    slug = slugify(name)
    try:
        org = await create_organization(name=name, slug=slug, created_by=owner_id)
    except SlugTakenError:
        slug = f"{slug[:35].rstrip('-')}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
        org = await create_organization(name=name, slug=slug, created_by=owner_id)
    if not org or not org.get("id"):
        out.append("  FAIL clerk org: create_organization returned nothing")
        return None
    out.append(f"  ok   clerk org {org['id']} slug={slug} admin={owner_id}")
    return org["id"]


async def _move_billing(owner_id: str, org_id: str, billing: dict[str, Any]) -> str:
    table = billing_repo._get_table()
    new = billing_for_org(billing, org_id, now_iso=datetime.now(timezone.utc).isoformat())
    if await billing_repo.get_by_owner_id(org_id, consistent_read=True) is None:
        await run_in_thread(table.put_item, Item=new, ConditionExpression="attribute_not_exists(owner_id)")
    await billing_repo.delete(owner_id)
    customer = billing.get("stripe_customer_id")
    if customer:
        rows = await billing_repo.list_by_stripe_customer_id(customer)
        owners = sorted(r.get("owner_id") for r in rows)
        if owners != [org_id]:
            raise RuntimeError(f"stripe-customer-index now resolves {customer} to {owners}, expected [{org_id}]")
    return f"row moved {owner_id} -> {org_id} (status={billing.get('subscription_status')}, stripe={customer or '-'})"


async def _stop_personal_box(owner_id: str, billing: dict[str, Any]) -> str:
    ecs = get_ecs_manager()
    await ecs.stop_user_service(owner_id, reason="migrate_to_workspace")
    drained = await ecs.wait_until_drained(owner_id, timeout_s=300.0)
    note = "drained" if drained else "DRAIN TIMED OUT — proceeding"
    if billing.get("provider_choice") == "chatgpt_oauth":
        from core.services.oauth_service import harvest_rotated_oauth_seed

        wrote = await harvest_rotated_oauth_seed(owner_id=owner_id)
        note += f"; oauth seed harvested={wrote}"
    return note


def _rename_efs(owner_id: str, org_id: str) -> str:
    ws = get_workspace()
    src, dst = ws.user_path(owner_id), ws.user_path(org_id)
    if dst.exists():
        return f"{dst} already exists — left as is"
    if not src.exists():
        return f"{src} missing — nothing to move"
    os.rename(src, dst)
    return f"{src} -> {dst}"


def _copy_replicas(owner_id: str, org_id: str) -> str:
    bucket = settings.LITESTREAM_BUCKET
    if not bucket:
        return "LITESTREAM_BUCKET unset — skipped"
    s3 = boto3.client("s3", region_name=settings.AWS_REGION)
    src_prefix, dst_prefix = f"{owner_id}/", f"{org_id}/"
    existing = {
        o["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=dst_prefix)
        for o in page.get("Contents", [])
    }
    copied = skipped = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=src_prefix):
        for obj in page.get("Contents", []):
            dst_key = dst_prefix + obj["Key"][len(src_prefix) :]
            if dst_key in existing:
                skipped += 1
                continue
            s3.copy({"Bucket": bucket, "Key": obj["Key"]}, bucket, dst_key)
            copied += 1
    return f"{copied} object(s) copied, {skipped} already present"


async def _repoint_paperclip(pc_row: PaperclipCompany, org_id: str) -> str:
    token = encrypt(service_token.mint(org_id))
    await PaperclipRepo(table_name="paperclip-companies").put(
        paperclip_row_for_org(pc_row, org_id, token, now=datetime.now(timezone.utc))
    )
    return f"row org_id -> {org_id}, service token re-minted with sub={org_id}; adapters re-bind on provisioning"


async def _move_messaging(owner_id: str, org_id: str) -> str:
    table = messaging_repo._table()
    rows = await messaging_repo._query(owner_pk(owner_id))
    moved = rooms = 0
    for item in rows:
        await run_in_thread(table.put_item, Item=rekeyed_owner_item(item, owner_id, org_id))
        room_id = item.get("roomId") if str(item.get("sk", "")).startswith("ROOM#") else None
        if room_id:
            await run_in_thread(
                table.update_item,
                Key={"pk": room_pk(room_id), "sk": "META"},
                UpdateExpression="SET ownerId = :org",
                ConditionExpression="ownerId = :old",
                ExpressionAttributeValues={":org": org_id, ":old": owner_id},
            )
            rooms += 1
        moved += 1
    for item in rows:
        await run_in_thread(table.delete_item, Key={"pk": item["pk"], "sk": item["sk"]})
    return f"{moved} owner row(s) moved, {rooms} room META ownerId updated; org now lists {len(await messaging_repo.list_owner_room_ids(org_id))} room(s)"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--owner", required=True, help="the legacy personal owner (Clerk user id)")
    ap.add_argument("--apply", action="store_true", help="execute; default is a dry run")
    ap.add_argument("--org", default=None, help="resume onto an org already created for this owner")
    ap.add_argument("--org-name", default=None, help="workspace name (default: the Paperclip company name)")
    ap.add_argument("--provision", action="store_true", help="dispatch the org provisioning at the end")
    args = ap.parse_args(argv)
    owner_id = args.owner

    billing = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    container = await container_repo.get_by_owner_id(owner_id, consistent=True)
    pc_row = await PaperclipRepo(table_name="paperclip-companies").get(owner_id, consistent_read=True)
    memberships = await clerk_admin.list_user_organizations(owner_id, limit=10)

    out: list[str] = _Out()
    reason = check_preconditions(owner_id, billing, container, pc_row, memberships, org_override=args.org)
    if reason:
        out.append(f"REFUSED {owner_id}: {reason}")
        return 2

    name = args.org_name or await _paperclip_company_name(pc_row) or f"{owner_id}'s workspace"
    out.append(
        f"{'APPLY' if args.apply else 'DRY RUN'} env={settings.ENVIRONMENT} owner={owner_id} "
        f"billing={billing.get('subscription_status')} provider={billing.get('provider_choice')} "
        f"container={container.get('status')} company={pc_row.company_id} "
        f"org={'(create ' + name + ')' if args.org is None else args.org}"
    )
    if not args.apply:
        out.append("  plan: clerk org -> billing row -> stop+drain box (+oauth seed) -> EFS rename -> replicas copy")
        out.append(
            "        -> paperclip row -> messaging partition" + (" -> dispatch provisioning" if args.provision else "")
        )
        return 0

    async def step(label: str, coro) -> bool:
        try:
            out.append(f"  ok   {label}: {await coro}")
            return True
        except Exception as exc:  # noqa: BLE001 — report, then stop: later steps depend on earlier ones
            out.append(f"  FAIL {label}: {exc!r}")
            return False

    org_id = args.org or await _create_org(name, owner_id, out)
    if not org_id:
        return 1
    steps = [
        ("billing", _move_billing(owner_id, org_id, billing)),
        ("container", _stop_personal_box(owner_id, billing)),
        ("efs", asyncio.to_thread(_rename_efs, owner_id, org_id)),
        ("replicas", asyncio.to_thread(_copy_replicas, owner_id, org_id)),
        ("paperclip", _repoint_paperclip(pc_row, org_id)),
        ("messaging", _move_messaging(owner_id, org_id)),
    ]
    for label, coro in steps:
        if not await step(label, coro):
            out.append(f"  stopped after '{label}' — fix and re-run with --org {org_id}")
            return 1
    if args.provision:
        from core.services.provision_dispatch import dispatch_provision

        await step("provision", _dispatch(dispatch_provision, org_id))
    out.append(f"next: verify chat/Teams/billing as the user on org {org_id}; then")
    out.append(f"      purge_legacy_personal_workspaces.py --owner {owner_id} --apply")
    return 0


async def _dispatch(dispatch_provision, org_id: str) -> str:
    await dispatch_provision(org_id)
    return f"org provisioning dispatched for {org_id}"


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
