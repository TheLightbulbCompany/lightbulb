"""ONE-OFF: move paperclip-companies to one workspace row per org, and
reconcile Clerk-vs-Paperclip membership drift.

Before: one row per MEMBER, each carrying the company facts AND that member's
Better Auth identity. After: ONE workspace row per org, keyed by the org id
(company facts + ``provisioner_user_id`` + ``member_user_ids``), and human rows
holding ONLY their identity (see ``core/repositories/paperclip_repo.py``).

Runs INSIDE the backend task (ECS exec), like ``refresh_owner_facts``:

    uv run python -m scripts.migrate_paperclip_org_rows            # dry run (default)
    uv run python -m scripts.migrate_paperclip_org_rows --apply

ROLLOUT: run it in dev right after the dev deploy of the PR that ships the new
shape, and in prod straight after the prod dispatch. In prod run the DRY RUN
first and review both the plan and the drift report, then ``--apply``. DELETE
this script and its test after the prod run (CLAUDE.md one-shot rule).

Per org (human rows grouped by ``org_id``; claim / pending-brief sentinels and
existing workspace rows are not grouped):

* ABORT the whole run, before any write, if an org's rows name anything but
  exactly ONE distinct non-empty ``company_id``, if an existing workspace row
  names a different company, or if any row's ``org_id`` is not a Clerk org id
  (personal owners no longer exist). Every offender is listed.
* The company facts come from the owner row (the old ``resolve_owner_row``
  rule: oldest active row carrying a starter/seeded marker, else oldest
  active, else — for a failed org — oldest marked, else oldest). Members are
  the org's ``active`` rows.
* The workspace row is a CONDITIONAL put: it lands only when no row exists
  (or only a company-less failed marker does); an existing row naming the same
  company is left alone. Then every human row is stripped to its identity
  (UpdateItem REMOVE). Idempotent: a second run finds nothing to group.

Membership drift (the owner ruling: a human is in a workspace's company IF AND
ONLY IF they are a member of the workspace's Clerk org). Per workspace, the
Clerk members (strict — a Clerk failure aborts, never reads as "no members")
are compared with the company's Paperclip members (agents excluded; Paperclip
principals mapped to Clerk users through identity rows):

* missing = in Clerk, not in the company → ``--apply`` joins them with the
  ``organizationMembership.created`` webhook's own join.
* extra = in the company, not in Clerk → ``--apply`` archives them, ONLY when
  they are a real non-member: never the bootstrap admin (``BOOTSTRAP_EMAILS``),
  never a vendor-org (``VENDOR_ORG_ID``) seat, never a principal with no
  identity row (unknown — reported, left alone), and never anyone in an org
  whose Clerk roster reads empty.

The DRIFT REPORT is printed before anything is applied.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from core.auth import is_org_owner_id
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import clerk_admin
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS

TABLE = "paperclip-companies"

#: Everything a workspace row carries that the old owner row held.
COMPANY_FIELDS = (
    "company_id",
    "status",
    "created_at",
    "updated_at",
    "last_error",
    "scheduled_purge_at",
    "seeded_agent_ids",
    "starter_agent_ids",
    "onboarding_generation",
    "funnel_state",
    "funnel_submitted_at",
    "funnel_completed_at",
    "onboarding_goal_id",
    "onboarding_project_id",
    "onboarding_brief_text",
    "onboarding_submitted_by",
    "first_contact_state",
    "first_contact_attempts",
    "first_contact_reason",
    "first_contact_claimed_at",
)
IDENTITY_FIELDS = {
    "user_id",
    "paperclip_user_id",
    "paperclip_email",
    "paperclip_password_encrypted",
    "created_at",
    "updated_at",
}
#: An unconfirmed sign-up keeps its claim state (``try_acquire_seed_claim``).
CLAIM_FIELDS = {"status", "seeding_claim_id", "seeding_claimed_at", "last_error"}


class MigrationAbort(SystemExit):
    pass


@dataclass
class OrgPlan:
    org_id: str
    company_id: str
    item: dict
    """The workspace row to write."""
    existing: bool = False
    """A workspace row naming the same company already exists: not rewritten."""


@dataclass
class Plan:
    orgs: list[OrgPlan] = field(default_factory=list)
    strips: dict[str, list[str]] = field(default_factory=dict)
    """human row key -> attributes to REMOVE."""


def _is_sentinel(key: str) -> bool:
    return key.startswith("__")


def _owner_row(rows: list[dict]) -> dict:
    """The old ``find_owner_row(include_inactive=True)`` pick."""

    def created(it: dict) -> str:
        return it.get("created_at", "")

    def marked(its: list[dict]) -> list[dict]:
        return [it for it in its if it.get("starter_agent_ids") or it.get("seeded_agent_ids")]

    active = [it for it in rows if it.get("status") == "active"]
    pool = active or rows
    return min(marked(pool) or pool, key=created)


def plan_migration(items: list[dict]) -> Plan:
    """Pure: validate everything, then plan. Raises ``MigrationAbort`` listing
    EVERY problem before a single write can happen."""
    workspaces = {it["user_id"]: it for it in items if is_org_owner_id(it["user_id"])}
    humans = [it for it in items if not _is_sentinel(it["user_id"]) and not is_org_owner_id(it["user_id"])]
    by_org: dict[str, list[dict]] = {}
    for it in humans:
        if it.get("org_id"):
            by_org.setdefault(it["org_id"], []).append(it)

    problems: list[str] = []
    personal = sorted(org for org in by_org if not is_org_owner_id(org))
    if personal:
        problems.append(f"non-org org_id(s) found (personal owners must not exist): {', '.join(personal)}")
    plan = Plan()
    for org_id in sorted(o for o in by_org if is_org_owner_id(o)):
        rows = by_org[org_id]
        companies = sorted({r["company_id"] for r in rows if r.get("company_id")})
        if len(companies) != 1:
            named = ", ".join(f"{r['user_id']}={r.get('company_id') or '-'}" for r in rows)
            problems.append(f"{org_id}: {len(companies)} distinct company ids ({named})")
            continue
        company_id = companies[0]
        existing = workspaces.get(org_id)
        if existing is not None and existing.get("company_id") and existing["company_id"] != company_id:
            problems.append(f"{org_id}: workspace row names {existing['company_id']}, member rows name {company_id}")
            continue
        owner = _owner_row(rows)
        item = {"user_id": org_id, "org_id": org_id, "provisioner_user_id": owner["user_id"]}
        item.update({k: owner[k] for k in COMPANY_FIELDS if k in owner})
        item["company_id"] = company_id
        members = {r["user_id"] for r in rows if r.get("status") == "active"}
        if members:
            item["member_user_ids"] = members
        plan.orgs.append(
            OrgPlan(org_id, company_id, item, existing=existing is not None and bool(existing.get("company_id")))
        )
    if problems:
        raise MigrationAbort("ABORT — nothing written:\n  " + "\n  ".join(problems))

    for it in humans:
        keep = IDENTITY_FIELDS | (set() if it.get("paperclip_user_id") else CLAIM_FIELDS)
        remove = sorted(k for k in it if k not in keep)
        if remove:
            plan.strips[it["user_id"]] = remove
    return plan


async def scan_items() -> list[dict]:
    table = get_table(TABLE)
    items: list[dict] = []
    kwargs: dict = {"ConsistentRead": True}
    while True:
        resp = await run_in_thread(table.scan, **kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


@dataclass
class Drift:
    org_id: str
    company_id: str
    clerk: set[str]
    missing: set[str]
    """In Clerk, not in the company: joined on --apply."""
    extra: set[str]
    """In the company, not in Clerk, and a real non-member: archived on --apply."""
    protected: set[str]
    """In the company, not in Clerk, but never archived (bootstrap admin / vendor seat)."""
    unknown: set[str]
    """Paperclip principals with no identity row: reported, never archived."""
    in_company: set[str]
    emails: dict[str, str] = field(default_factory=dict)
    """Clerk member -> their Clerk email, for the join."""
    clerk_empty: bool = False


def _members(members_resp) -> list[dict]:
    rows = (members_resp.get("members") or []) if isinstance(members_resp, dict) else (members_resp or [])
    return [m for m in rows if isinstance(m, dict) and m.get("principalType", "user") == "user"]


async def compute_drift(
    workspaces: list[tuple[str, str]],
    *,
    principal_to_user: dict[str, str],
    identity_emails: dict[str, str],
    admin,
    cookie: str,
) -> list[Drift]:
    """``workspaces`` is ``[(org_id, company_id)]``. Clerk reads are strict: a
    failure raises and the run aborts."""
    vendor = set()
    if settings.VENDOR_ORG_ID:
        vendor = {
            m["user_id"] for m in await clerk_admin.list_organization_members(settings.VENDOR_ORG_ID, strict=True)
        }
    bootstrap = {e.lower() for e in BOOTSTRAP_EMAILS}
    out: list[Drift] = []
    for org_id, company_id in workspaces:
        roster = await clerk_admin.list_organization_members(org_id, strict=True)
        clerk = {m["user_id"] for m in roster}
        in_company: set[str] = set()
        protected: set[str] = set()
        unknown: set[str] = set()
        for m in _members(await admin.list_members(session_cookie=cookie, company_id=company_id)):
            principal = m.get("principalId") or ""
            uid = principal_to_user.get(principal)
            email = ((m.get("user") or {}).get("email") or identity_emails.get(uid or "", "")).lower()
            if email in bootstrap:
                protected.add(uid or principal)
            elif uid is None:
                unknown.add(principal)
            else:
                in_company.add(uid)
        not_in_clerk = in_company - clerk
        protected |= not_in_clerk & vendor
        extra = set() if not clerk else not_in_clerk - vendor
        out.append(
            Drift(
                org_id=org_id,
                company_id=company_id,
                clerk=clerk,
                missing=clerk - in_company - protected,
                extra=extra,
                protected=protected,
                unknown=unknown,
                in_company=in_company,
                emails={m["user_id"]: m.get("email") or "" for m in roster},
                clerk_empty=not clerk,
            )
        )
    return out


def print_report(plan: Plan, drifts: list[Drift]) -> None:
    print(f"== PLAN: {len(plan.orgs)} workspace row(s), {len(plan.strips)} human row(s) to strip")
    for op in plan.orgs:
        members = sorted(op.item.get("member_user_ids") or ())
        state = "exists (kept)" if op.existing else "write"
        print(
            f"  {op.org_id}: company={op.company_id} status={op.item.get('status')} "
            f"provisioner={op.item['provisioner_user_id']} members={members} [{state}]"
        )
    for key, fields in sorted(plan.strips.items()):
        print(f"  strip {key}: {', '.join(fields)}")
    print("== DRIFT REPORT (Clerk org members vs Paperclip company members)")
    for d in drifts:
        note = " — CLERK ROSTER EMPTY: nothing will be archived" if d.clerk_empty else ""
        print(f"  {d.org_id} (company {d.company_id}){note}")
        print(f"    missing (join):    {sorted(d.missing) or '-'}")
        print(f"    extra (archive):   {sorted(d.extra) or '-'}")
        print(f"    protected (keep):  {sorted(d.protected) or '-'}")
        print(f"    unknown (keep):    {sorted(d.unknown) or '-'}")


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


async def apply_migration(plan: Plan) -> None:
    table = get_table(TABLE)
    client_errors = table.meta.client.exceptions
    for op in plan.orgs:
        if op.existing:
            continue
        try:
            await run_in_thread(
                table.put_item,
                Item=op.item,
                ConditionExpression="attribute_not_exists(user_id) OR attribute_not_exists(company_id)",
            )
        except client_errors.ConditionalCheckFailedException:
            current = (await run_in_thread(table.get_item, Key={"user_id": op.org_id}, ConsistentRead=True)).get("Item")
            if (current or {}).get("company_id") != op.company_id:
                raise MigrationAbort(f"ABORT: {op.org_id} workspace row changed underneath the run: {current}")
        print(f"wrote workspace row {op.org_id}")
    for key, fields in plan.strips.items():
        await run_in_thread(
            table.update_item,
            Key={"user_id": key},
            UpdateExpression="REMOVE " + ", ".join(f"#f{i}" for i in range(len(fields))),
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={f"#f{i}": f for i, f in enumerate(fields)},
        )
        print(f"stripped {key}")


async def apply_drift(drifts: list[Drift], *, provisioning) -> None:
    from core.services.user_provision import _ensure_paperclip_for_member

    repo: PaperclipRepo = provisioning._repo  # noqa: SLF001 — one-off script
    for d in drifts:
        for uid in sorted(d.missing):
            # The recorded membership would short-circuit the join; the join
            # re-adds it once Paperclip actually has the member.
            await repo.remove_member(d.org_id, uid)
            await _ensure_paperclip_for_member(org_id=d.org_id, member_user_id=uid, member_email=d.emails.get(uid, ""))
            print(f"{d.org_id}: join attempted for {uid}")
        for uid in sorted(d.extra):
            await provisioning.archive_member(user_id=uid, org_id=d.org_id)
            print(f"{d.org_id}: archived {uid}")
        # The DDB membership mirrors the company for everyone Clerk keeps, and
        # drops anyone recorded who is in neither (a join re-adds itself).
        keep = d.in_company & d.clerk
        for uid in sorted(keep):
            await repo.add_member(d.org_id, uid)
        ws = await repo.get_workspace(d.org_id, consistent_read=True)
        for uid in sorted((ws.member_user_ids if ws else set()) - keep - d.missing):
            await repo.remove_member(d.org_id, uid)
            print(f"{d.org_id}: dropped stale DDB membership {uid}")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run, print only)")
    args = parser.parse_args(argv)

    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_provisioning import build_paperclip_provisioning, close_paperclip_provisioning

    items = await scan_items()
    plan = plan_migration(items)

    humans = [it for it in items if not _is_sentinel(it["user_id"]) and not is_org_owner_id(it["user_id"])]
    principal_to_user = {it["paperclip_user_id"]: it["user_id"] for it in humans if it.get("paperclip_user_id")}
    identity_emails = {it["user_id"]: it.get("paperclip_email", "") for it in humans}
    workspaces = {op.org_id: op.company_id for op in plan.orgs}
    for it in items:
        if is_org_owner_id(it["user_id"]) and it.get("company_id"):
            workspaces.setdefault(it["user_id"], it["company_id"])

    provisioning = await build_paperclip_provisioning()
    try:
        cookie = await get_admin_session_cookie(provisioning._admin._http)  # noqa: SLF001
        drifts = await compute_drift(
            sorted(workspaces.items()),
            principal_to_user=principal_to_user,
            identity_emails=identity_emails,
            admin=provisioning._admin,  # noqa: SLF001
            cookie=cookie,
        )
        print_report(plan, drifts)
        if not args.apply:
            print("DRY RUN — nothing written. Re-run with --apply.")
            return 0
        await apply_migration(plan)
        await apply_drift(drifts, provisioning=provisioning)
    finally:
        await close_paperclip_provisioning(provisioning)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
