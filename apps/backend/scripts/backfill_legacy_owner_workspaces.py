"""Backfill Clerk organizations for legacy PERSONAL owners (owner_id == Clerk user_id).

The product went workspace-first on 2026-09-10: every human now belongs to a
Clerk organization, created client-side by ``<CreateOrganization>`` with the
signed-up user as its admin. Prod holds a cohort of PERSONAL owners left over
from before that cutover, who have no Clerk organization at all — the web's
``useOnboardingGate`` (apps/frontend/src/hooks/useOnboardingGate.ts) expects
exactly one membership and never gets one, so these owners are stuck outside
the normal workspace-provisioning flow.

This script mints ONE Clerk organization per legacy personal owner, with the
owner as its admin (``created_by``), so their next sign-in resolves to exactly
one membership and the normal flow takes over from there.

Runs INSIDE the backend task (needs ``PAPERCLIP_INTERNAL_URL`` and the EFS
mount, same as ``purge_legacy_personal_workspaces.py``)::

    B64=$(base64 < apps/backend/scripts/backfill_legacy_owner_workspaces.py | tr -d '\\n')
    aws ecs execute-command --cluster lightbulb-prod-container --task <backend task> \\
      --container backend --interactive \\
      --command "/bin/sh -c 'cd /app && echo $B64 | base64 -d | uv run python - [--apply] [--owner user_x ...]'"

Dry-run by default: prints the plan per owner, one line each. ``--apply``
actually creates the orgs. ``--owner`` narrows to specific owner ids
(repeatable).

Workspace naming, in this order: the owner's Paperclip company name (fetched
live from the Paperclip BFF; a lookup failure of any kind is tolerated and
falls through to the next source), else the Clerk user's first name + "'s
workspace", else their primary email's local part + "'s workspace", else the
bare owner id. The slug is a lowercase, dash-normalised, 40-char-capped
version of the name; a Clerk slug collision (``SlugTakenError``) is retried
once with a random 4-char suffix, and reported as a FAILure if that also
collides.

A still-entitled (paying) personal owner still gets an org created — the org
is what lets them past the onboarding gate at all — but the line is flagged
with a WARNING, because creating the org does NOT move their running
container, Paperclip company, or billing row onto the new workspace. That
re-keying is a separate, deliberate migration (a runbook, not this script).

Deliberately does NOT, by construction: touch any container, EFS dir,
Paperclip company/agents, billing row, or Stripe subscription — it only ever
calls Clerk's ``POST /v1/organizations``. Re-runnable: an owner who already has
a membership (from a prior run of this script, or from any other route, e.g.
they created their own workspace since the scan was taken) is skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import string
import sys
import time
from typing import Any

import httpx

from core.config import settings
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services import clerk_admin


# Piped into a deployed backend task via ECS exec, this script cannot rely on the
# image's ``clerk_admin.create_organization`` (its signature has already changed once
# underneath a run), so the one POST it needs is inlined here.
class SlugTakenError(Exception):
    pass


async def create_organization(*, name: str, created_by: str, slug: str | None = None):
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.clerk.com/v1/organizations",
            headers={
                "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "lightbulb-backend/1.0",
            },
            json={"name": name, "created_by": created_by, **({"slug": slug} if slug else {})},
        )
    if resp.status_code == 422 and any(
        e.get("code") in ("form_identifier_exists", "duplicate_record") for e in (resp.json().get("errors") or [])
    ):
        raise SlugTakenError(slug)
    if resp.status_code >= 400:
        print(f"  clerk POST /organizations -> {resp.status_code} {resp.text[:300]}", flush=True)
        return None
    return resp.json()


from core.services.entitlement_teardown import still_entitled
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie

_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 40


class _Out(list):
    """Report lines: kept for the exit code, printed as they happen."""

    def append(self, line: str) -> None:  # type: ignore[override]
        super().append(line)
        print(line, flush=True)


def slugify(text: str, *, max_len: int = _SLUG_MAX_LEN) -> str:
    """Lowercase; runs of non `[a-z0-9]` become one `-`; trimmed; capped."""
    s = _SLUG_INVALID_RE.sub("-", text.lower()).strip("-")
    s = s[:max_len].strip("-")
    return s or "workspace"


def _slug_with_suffix(slug: str, suffix: str, *, max_len: int = _SLUG_MAX_LEN) -> str:
    """`slug` shortened just enough to fit `-{suffix}` within `max_len`."""
    base = slug[: max_len - len(suffix) - 1].rstrip("-")
    return f"{base}-{suffix}" if base else suffix


def plan_workspace(
    owner_id: str,
    memberships: list[dict[str, Any]],
    company_name: str | None,
    first_name: str | None,
    email: str | None,
    *,
    billing: dict[str, Any] | None = None,
    now: float | None = None,
) -> tuple[str, str, str, bool]:
    """Pure planning step — no I/O, fully unit-testable.

    Returns ``(verdict, name, slug, warn_paid)``:
      - ``verdict`` is ``"create"``, or ``"skip: already in <org id>"`` when
        the owner already has at least one membership.
      - ``name`` / ``slug`` are ``""`` on a skip verdict.
      - ``warn_paid`` is True when `billing` is paid or inside a live trial
        (an expired ``trial_local`` row is not) — the caller should still
        create the org, but print the runtime-migration warning: this script
        never moves a paid owner's container/company/billing.
    """
    if memberships:
        return f"skip: already in {memberships[0].get('id', '?')}", "", "", False

    if company_name:
        name = company_name
    elif first_name:
        name = f"{first_name}'s workspace"
    elif email:
        name = f"{email.split('@', 1)[0]}'s workspace"
    else:
        name = owner_id

    return "create", name, slugify(name), _runtime_worth_migrating(billing, now)


def _runtime_worth_migrating(billing: dict[str, Any] | None, now: float | None) -> bool:
    """Paid, or inside a live trial. An EXPIRED ``trial_local`` row counts as
    entitled to ``still_entitled`` but has nothing worth carrying over."""
    if not still_entitled(billing):
        return False
    if (billing or {}).get("subscription_status") != "trial_local":
        return True
    trial_end = (billing or {}).get("trial_end")
    return trial_end is not None and float(trial_end) > (time.time() if now is None else now)


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


async def _legacy_personal_owner_ids() -> tuple[list[str], dict[str, dict], dict[str, PaperclipCompany]]:
    """Same union-of-tables scan `purge_legacy_personal_workspaces.py` uses."""
    containers = {r["owner_id"]: r for r in await _scan("containers")}
    billing = {r["owner_id"]: r for r in await _scan("billing-accounts")}
    pc_by_user: dict[str, PaperclipCompany] = {}
    for r in await _scan("paperclip-companies"):
        # The table also holds claim / pending-brief rows that are not company rows.
        if not (r.get("user_id") and r.get("org_id") and r.get("company_id")):
            continue
        try:
            pc_by_user[r["user_id"]] = PaperclipRepo._item_to_company(r)
        except Exception:  # noqa: BLE001 — a malformed row is simply not a naming source
            continue

    candidates = sorted({o for o in [*containers, *billing, *pc_by_user] if o.startswith("user_")})
    return candidates, billing, pc_by_user


async def _company_name(pc_row: PaperclipCompany | None) -> str | None:
    """Best-effort Paperclip company name lookup; None on any failure."""
    if pc_row is None:
        return None
    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=30.0)
    try:
        cookie = await get_admin_session_cookie(http)
        company = await PaperclipAdminClient(http_client=http).get_company(
            session_cookie=cookie, company_id=pc_row.company_id
        )
        return company.get("name") or None
    except Exception:  # noqa: BLE001 — tolerate failure, caller falls through to the next source
        return None
    finally:
        await http.aclose()


async def _first_name(owner_id: str) -> str | None:
    user = await clerk_admin.get_user(owner_id)
    return (user or {}).get("first_name") or None


async def _primary_email(owner_id: str) -> str | None:
    try:
        return await clerk_admin.get_primary_email(owner_id)
    except RuntimeError:
        return None


async def _create_with_retry(name: str, slug: str, owner_id: str, out: list[str]) -> None:
    try:
        org = await create_organization(name=name, created_by=owner_id)
    except SlugTakenError:
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        retry_slug = _slug_with_suffix(slug, suffix)
        try:
            org = await create_organization(name=name, created_by=owner_id)
        except SlugTakenError:
            out.append(f"  FAIL {owner_id}: slug taken twice ({slug}, {retry_slug})")
            return
        slug = retry_slug

    if org is None:
        out.append(f"  FAIL {owner_id}: create_organization failed")
        return
    out.append(f"  ok created {org.get('id', '?')} {slug}")


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="execute; default is a dry run")
    ap.add_argument("--owner", action="append", default=[], help="limit to these owner ids (repeatable)")
    ap.add_argument(
        "--skip-paid",
        action="store_true",
        help="do not create a workspace for owners still paying / in a live trial (their runtime must be migrated first)",
    )
    args = ap.parse_args(argv)

    candidates, billing, pc_by_user = await _legacy_personal_owner_ids()
    if args.owner:
        candidates = [o for o in candidates if o in set(args.owner)]

    out: list[str] = _Out()
    out.append(f"{'APPLY' if args.apply else 'DRY RUN'} env={settings.ENVIRONMENT} candidates={len(candidates)}")

    for owner_id in candidates:
        memberships = await clerk_admin.list_user_organizations(owner_id)

        company_name = None if memberships else await _company_name(pc_by_user.get(owner_id))
        first_name = None if memberships or company_name else await _first_name(owner_id)
        email = None if memberships or company_name or first_name else await _primary_email(owner_id)

        verdict, name, slug, warn_paid = plan_workspace(
            owner_id, memberships, company_name, first_name, email, billing=billing.get(owner_id)
        )

        if verdict != "create":
            out.append(f"  {owner_id}: {verdict}")
            continue

        if warn_paid and args.skip_paid:
            out.append(
                f"  {owner_id}: skip: paying owner — migrate their runtime first (migrate_personal_owner_to_workspace.py)"
            )
            continue
        out.append(f"  {owner_id}: create name={name!r} slug={slug}")
        if warn_paid:
            out.append("    WARN: paid personal owner — runtime migration required before they switch workspaces")

        if args.apply:
            await _create_with_retry(name, slug, owner_id, out)

    return 1 if any(line.startswith("  FAIL") for line in out) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
