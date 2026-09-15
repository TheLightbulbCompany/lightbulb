"""Purge legacy PERSONAL workspaces (owner_id == Clerk user_id) from an environment.

The product went workspace-first on 2026-09-10; every human now belongs to a Clerk
organization. Personal workspaces created before that are legacy: a stopped
per-user container, a Paperclip company with Friday/Atlas and a nightly routine,
a ``trial_local`` billing row, gate tokens and a Bedrock profile. Left alone they
cost money (Bedrock profiles, EFS, replicas) and, worse, their nightly routines
still wake the personal box — which is how a cofounder's org Paperclip row got
clobbered (see ``_ensure_paperclip_for_owner``'s org-member guard).

Runs INSIDE the backend task (needs ``PAPERCLIP_INTERNAL_URL`` and the EFS mount)::

    B64=$(base64 < apps/backend/scripts/purge_legacy_personal_workspaces.py | tr -d '\\n')
    aws ecs execute-command --cluster lightbulb-prod-container --task <backend task> \\
      --container backend --interactive \\
      --command "/bin/sh -c 'cd /app && echo $B64 | base64 -d | uv run python - [--apply] [--owner user_x ...] [--delete-company <id> ...]'"

Dry-run by default: prints the plan per owner and the Paperclip companies no
surviving row references. ``--apply`` executes. ``--delete-company`` names the
orphan companies to delete explicitly — never inferred, because a company nobody
references is not proof it is junk.

NEVER touched, by construction: Clerk users and org memberships, ``users`` rows,
``paperclip-companies`` rows that point at an org (a dual-tenant member keeps
their org membership and their shared Better Auth user), OAuth tokens, usage
counters, Stripe. Owners who are still entitled (``still_entitled``), whose
``trial_end`` has not passed, or whose container is running are SKIPPED and
listed — migrating a paying personal owner to a workspace is a product decision,
not a purge.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

import httpx

from core.auth import is_org_owner_id
from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.repositories import billing_repo, container_repo, messaging_repo, update_repo
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services.entitlement_teardown import still_entitled
from core.services.litestream_replicas import delete_replica_prefix
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS, delete_paperclip_user

PURGEABLE_BILLING = frozenset({None, "", "trial_local"})


class _Out(list):
    """Report lines: kept for the exit code, printed as they happen."""

    def append(self, line: str) -> None:  # type: ignore[override]
        super().append(line)
        print(line, flush=True)

    def extend(self, lines) -> None:  # type: ignore[override]
        for line in lines:
            self.append(line)


def classify(
    owner_id: str,
    billing: dict[str, Any] | None,
    container: dict[str, Any] | None,
    pc_row: PaperclipCompany | None,
    *,
    now: float,
) -> tuple[str, str]:
    """``("skip" | "purge", reason)`` for one owner id. Pure — unit-tested."""
    if is_org_owner_id(owner_id):
        return "skip", "org owner"
    status = (billing or {}).get("subscription_status")
    if status == "trial_local":
        # ``still_entitled`` counts every trial_local row; only an EXPIRED one is legacy junk.
        trial_end = (billing or {}).get("trial_end")
        if trial_end is None or float(trial_end) > now:
            return "skip", "trial not expired" if trial_end is not None else "trial_local with no trial_end"
    elif still_entitled(billing):
        return "skip", f"still entitled ({status})"
    if container is not None and container.get("status") == "running":
        return "skip", "container running"
    if pc_row is not None and is_org_owner_id(pc_row.org_id):
        return "purge", f"dual-tenant: personal side only, org row {pc_row.org_id} kept"
    return "purge", f"personal-only (billing={(billing or {}).get('subscription_status') or 'none'})"


async def _scan(short_name: str) -> list[dict[str, Any]]:
    from core.dynamodb import get_table

    tbl = get_table(short_name)
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    while True:
        page = await asyncio.to_thread(tbl.scan, **kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def _purge_owner(
    owner_id: str, pc_row: PaperclipCompany | None, billing: dict[str, Any] | None, out: list[str]
) -> None:
    async def step(name: str, fn):
        try:
            detail = await fn()
            out.append(f"  ok   {name}: {detail}")
        except Exception as exc:  # noqa: BLE001 — continue with the next step, report at the end
            out.append(f"  FAIL {name}: {exc!r}")

    if pc_row is not None and not is_org_owner_id(pc_row.org_id):

        async def _company() -> str:
            http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=30.0)
            try:
                cookie = await get_admin_session_cookie(http)
                await PaperclipAdminClient(http_client=http).delete_company(
                    session_cookie=cookie, company_id=pc_row.company_id
                )
            finally:
                await http.aclose()
            return pc_row.company_id

        await step("paperclip company", _company)

        async def _aurora_user() -> str:
            if pc_row.paperclip_email in BOOTSTRAP_EMAILS:
                return "bootstrap admin — refused"
            await delete_paperclip_user(
                paperclip_user_id=pc_row.paperclip_user_id,
                email=pc_row.paperclip_email,
                env=settings.ENVIRONMENT or "dev",
            )
            return pc_row.paperclip_user_id

        await step("paperclip aurora user", _aurora_user)

        async def _row() -> str:
            await PaperclipRepo(table_name="paperclip-companies").delete(owner_id)
            return "deleted"

        await step("paperclip-companies row", _row)

    async def _tokens() -> str:
        row = await container_repo.get_by_owner_id(owner_id, consistent=True)
        hashes = owner_gate_token_hashes(row)
        return f"{await delete_gate_token_rows(hashes) if hashes else 0} of {len(hashes)}"

    await step("bedrock gate tokens", _tokens)

    async def _ecs() -> str:
        dead = await get_ecs_manager().delete_user_service(owner_id)
        late = owner_gate_token_hashes(dead) if dead else []
        swept = await delete_gate_token_rows(late) if late else 0
        return f"service/access point/row deleted, bedrock reaped, late tokens {swept}"

    await step("ecs + bedrock", _ecs)

    async def _efs() -> str:
        await asyncio.to_thread(get_workspace().delete_user_dir, owner_id)
        return "deleted"

    await step("efs dir", _efs)

    async def _replicas() -> str:
        return str(await delete_replica_prefix(owner_id))

    await step("litestream replicas", _replicas)

    async def _messaging() -> str:
        room_ids = await messaging_repo.list_owner_room_ids(owner_id)
        deleted = 0
        for room_id in room_ids:
            meta = await messaging_repo.get_room(room_id)
            if meta is not None and meta.get("ownerId") != owner_id:
                raise RuntimeError(f"room {room_id} owned elsewhere")
            deleted += await messaging_repo.delete_room_partition(room_id)
        deleted += await messaging_repo.delete_owner_partition(owner_id)
        return f"{len(room_ids)} room(s), {deleted} rows"

    await step("messaging", _messaging)

    async def _updates() -> str:
        return str(await update_repo.delete_all_for_owner(owner_id))

    await step("pending updates", _updates)

    status = (billing or {}).get("subscription_status")
    if billing is not None and status in PURGEABLE_BILLING:

        async def _billing() -> str:
            await billing_repo.delete(owner_id)
            return f"deleted ({status or 'no status'})"

        await step("billing row", _billing)
    elif billing is not None:
        out.append(f"  keep billing row (status={status}; Stripe-bearing rows are not purged here)")


async def _admin_client() -> tuple[httpx.AsyncClient, PaperclipAdminClient, str]:
    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=30.0)
    cookie = await get_admin_session_cookie(http)
    return http, PaperclipAdminClient(http_client=http), cookie


async def _list_companies() -> list[dict[str, Any]]:
    http, admin, cookie = await _admin_client()
    try:
        body = await admin._get("/api/companies", session_cookie=cookie)
    finally:
        await http.aclose()
    rows = body.get("companies", body.get("data", body)) if isinstance(body, dict) else body
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def _delete_companies(ids: list[str], out: list[str]) -> None:
    http, admin, cookie = await _admin_client()
    try:
        for cid in ids:
            try:
                await admin.delete_company(session_cookie=cookie, company_id=cid)
                out.append(f"  ok   orphan company {cid} deleted")
            except Exception as exc:  # noqa: BLE001
                out.append(f"  FAIL orphan company {cid}: {exc!r}")
    finally:
        await http.aclose()


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="execute; default is a dry run")
    ap.add_argument("--owner", action="append", default=[], help="limit to these owner ids (repeatable)")
    ap.add_argument(
        "--delete-company", action="append", default=[], help="orphan Paperclip company ids to delete (repeatable)"
    )
    args = ap.parse_args(argv)

    containers = {r["owner_id"]: r for r in await _scan("containers")}
    billing = {r["owner_id"]: r for r in await _scan("billing-accounts")}
    pc_by_user: dict[str, PaperclipCompany] = {}
    unparsed = 0
    for r in await _scan("paperclip-companies"):
        # The table also holds claim / pending-brief rows that are not company rows.
        if not (r.get("user_id") and r.get("org_id") and r.get("company_id")):
            unparsed += 1
            continue
        try:
            pc_by_user[r["user_id"]] = PaperclipRepo._item_to_company(r)
        except Exception:  # noqa: BLE001 — a malformed row is reported, never purged
            unparsed += 1

    candidates = sorted({o for o in [*containers, *billing, *pc_by_user] if o.startswith("user_")})
    if args.owner:
        candidates = [o for o in candidates if o in set(args.owner)]
    now = time.time()
    out: list[str] = _Out()
    out.append(
        f"{'APPLY' if args.apply else 'DRY RUN'} env={settings.ENVIRONMENT} "
        f"candidates={len(candidates)} non-company paperclip rows skipped={unparsed}"
    )
    purge, skip = [], []
    for owner in candidates:
        verdict, reason = classify(owner, billing.get(owner), containers.get(owner), pc_by_user.get(owner), now=now)
        (purge if verdict == "purge" else skip).append((owner, reason))
    out.append(f"skip {len(skip)}:")
    out.extend(f"  {o}: {r}" for o, r in skip)
    out.append(f"purge {len(purge)}:")
    for owner, reason in purge:
        ctr = (containers.get(owner) or {}).get("status") or "none"
        bil = (billing.get(owner) or {}).get("subscription_status") or "none"
        row = pc_by_user.get(owner)
        pc = "none" if row is None else ("org-kept" if is_org_owner_id(row.org_id) else row.company_id[:8])
        out.append(f"  {owner}: {reason} [ctr={ctr} billing={bil} pc={pc}]")
        if args.apply:
            await _purge_owner(owner, pc_by_user.get(owner), billing.get(owner), out)

    purged = {o for o, _ in purge}
    surviving = {row.company_id for uid, row in pc_by_user.items() if uid not in purged or is_org_owner_id(row.org_id)}
    try:
        companies = await _list_companies()
        orphans = [c for c in companies if c.get("id") not in surviving]
        out.append(f"paperclip companies: {len(companies)} total, {len(orphans)} referenced by no surviving row:")
        out.extend(f"  {c.get('id')}  {str(c.get('name', ''))[:40]!r}" for c in orphans)
    except Exception as exc:  # noqa: BLE001
        out.append(f"paperclip company listing failed: {exc!r}")
    if args.delete_company:
        if args.apply:
            await _delete_companies(args.delete_company, out)
        else:
            out.extend(f"  would delete orphan company {cid}" for cid in args.delete_company)
    return 1 if any(line.startswith("  FAIL") for line in out) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
