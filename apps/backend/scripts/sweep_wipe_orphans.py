"""ONE-SHOT: delete credential/identity rows whose owner no longer exists.

DELETE THIS SCRIPT ONCE IT HAS BEEN RUN (CLAUDE.md: one-shot backend scripts
are not kept — git history is the operational record).

Why one-shot and not an admin endpoint: the account wipe no longer PRODUCES
these orphans. It now deletes the owner's integrations, OAuth tokens, agent
email addresses, company slug and Secrets Manager material as steps 9f-9j, so
from that change onward a completed wipe leaves none of them behind. What
remains is a finite historical backlog — rows stranded by wipes that ran
before those steps existed — and once that is swept there is nothing for a
standing endpoint to do but sit there as a loaded gun.

The one case that can still strand a row is a wipe step that FAILS. The wipe
is partial-failure tolerant by design (a Paperclip timeout must not block the
Clerk delete), and it reports every failed step. The fix for that is re-running
the wipe, which is idempotent — not a periodic sweep.

SAFETY — read before running with --apply:

Liveness comes from the ``users`` table, which the Clerk ``user.created``
webhook populates with PERSONAL ``user_...`` ids only. Org-keyed rows are
therefore skipped outright rather than judged against it: ``company-slugs``,
``agent-email-addresses`` and ``integrations`` are keyed by the fleet-wide
``owner_id``, which for a Teams account is the Clerk ORG id. Diffing those
against a personal-only liveness set marks every LIVE org as an orphan, which
would release a paying org's slug for someone else to claim, delete Friday's
still-in-use mailbox, and drop its integrations.

Does NOT touch Secrets Manager. Owner-scoped secret names embed the owner id,
so sweeping there means parsing ids back out of names and diffing them against
the users table — same shape as this, except a mis-parse deletes a LIVE
owner's credentials. Per-owner deletion (wipe step 9f) covers everything from
here on.

Requires AWS SSO (``aws sso login --profile isol8-admin``) and the table
prefix for the environment you mean — NOT ``ENVIRONMENT``; see
CLAUDE.md/scripts convention. Setting the wrong prefix silently scans dev and
reports a confident zero for prod.

    DYNAMODB_TABLE_PREFIX=isol8-dev-  uv run python -m scripts.sweep_wipe_orphans
    DYNAMODB_TABLE_PREFIX=isol8-prod- uv run python -m scripts.sweep_wipe_orphans --apply

Dry-run by default: it deletes across several tables off a single liveness
read, so the list gets eyeballed first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("sweep_wipe_orphans")

WipeStatus = Literal["ok", "skipped", "failed", "would_delete"]


@dataclass
class WipeStep:
    system: str
    action: str
    status: WipeStatus
    detail: str = ""
    error: str | None = None


def _require_table_prefix() -> str:
    """Assert the prefix BEFORE any ``core.*`` import.

    core.config binds table names at import time off DYNAMODB_TABLE_PREFIX, so
    importing first and checking after means the check is decorative: the
    module already resolved to whatever the ambient env said, and a run
    intended for prod scans dev and reports a confident zero. Asserting here,
    above the imports, is the only ordering that actually gates it.
    """
    prefix = os.environ.get("DYNAMODB_TABLE_PREFIX")
    if not prefix:
        print("Refusing to run: set DYNAMODB_TABLE_PREFIX (e.g. isol8-dev- / isol8-prod-).", file=sys.stderr)
        print("ENVIRONMENT is NOT the switch these scripts read.", file=sys.stderr)
        raise SystemExit(2)
    return prefix


def _skip(steps: list[WipeStep], system: str, action: str, detail: str) -> None:
    steps.append(WipeStep(system=system, action=action, status="skipped", detail=detail))


async def _step(steps: list[WipeStep], *, system: str, action: str, fn) -> None:
    try:
        detail = await fn()
        steps.append(WipeStep(system=system, action=action, status="ok", detail=detail))
    except Exception as exc:  # noqa: BLE001 — one table failing must not abort the rest
        logger.exception("%s failed", system)
        steps.append(WipeStep(system=system, action=action, status="failed", error=str(exc)))


@dataclass
class OrphanReport:
    """What a sweep found (and, when not a dry run, removed)."""

    dry_run: bool
    steps: list[WipeStep]

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "steps": [
                {"system": s.system, "action": s.action, "status": s.status, "detail": s.detail, "error": s.error}
                for s in self.steps
            ],
        }


async def _live_owner_ids() -> set[str]:
    """Owners that still exist, per the users table.

    The users row is the right liveness signal FOR PERSONAL OWNERS:
    ``wipe_account`` deletes it (step 9d) before deleting the Clerk user, and
    every provisioned personal owner has one.

    It says NOTHING about orgs. The users table is written only by the Clerk
    ``user.created`` webhook, so it holds ``user_...`` ids exclusively — no
    path ever writes an ``org_...`` id into it. Callers must therefore exclude
    org-keyed rows BEFORE diffing against this set; see ``_is_sweepable``.
    """
    from core.repositories import user_repo

    return {r["user_id"] for r in await user_repo.list_all() if r.get("user_id")}


def _is_sweepable(owner_key: str) -> bool:
    """Whether this owner id may be judged against the personal liveness set.

    Org ids must never be: ``company-slugs``, ``agent-email-addresses`` and
    ``integrations`` are all keyed by the fleet-wide ``owner_id`` convention,
    which for a Teams account is the Clerk ORG id (``resolve_owner_id``
    returns ``org_id`` in org context). Those ids can never appear in the
    users table, so diffing them against it marks every LIVE org as orphaned —
    which would release a paying org's slug for someone else to claim, delete
    Friday's still-in-use mailbox, and drop its integration rows.

    ``wipe_account`` refuses org owners outright for the same reason (v1 is
    personal-only). This is that refusal, applied to the sweep.
    """
    from core.auth import is_org_owner_id

    return bool(owner_key) and not is_org_owner_id(owner_key)


async def sweep_orphans(*, dry_run: bool = True) -> OrphanReport:  # noqa: C901
    """Find (and optionally delete) rows whose owner no longer exists.

    This is the one-shot companion to steps 9f-9j. Those steps stop NEW
    orphans; they cannot reach rows stranded by wipes that ran before they
    existed, because the owner id is gone from every table that would let you
    enumerate it. So this walks the credential/identity tables directly and
    keys off "is this owner still in the users table".

    Defaults to ``dry_run=True``: it deletes across several tables at once
    off a single liveness read, and an admin should see the list before it
    happens.

    Deliberately does NOT sweep Secrets Manager. Owner-scoped secret names
    embed the owner id, so a sweep there would need to parse ids back out of
    names and diff them against the users table — the same shape as this, but
    a mis-parse deletes a LIVE owner's credentials. Per-owner deletion (9f)
    covers everything from here on; anything older is a deliberate manual job.
    """
    from core.repositories import agent_email_repo, company_slug_repo, integrations_repo

    steps: list[WipeStep] = []
    live = await _live_owner_ids()

    async def _sweep(system: str, action: str, rows: list[dict], owner_key: str, delete_one) -> None:
        # Org-keyed rows are out of scope, not orphans — see _is_sweepable.
        candidates = [r for r in rows if _is_sweepable(r.get(owner_key, ""))]
        skipped_orgs = len(rows) - len(candidates)
        orphans = [r for r in candidates if r[owner_key] not in live]
        org_note = f" ({skipped_orgs} org-keyed row(s) skipped as out of scope)" if skipped_orgs else ""
        if not orphans:
            _skip(steps, system, action, f"no orphans{org_note}")
            return
        detail = f"{len(orphans)} orphan(s): " + ", ".join(sorted({r[owner_key] for r in orphans})) + org_note
        if dry_run:
            steps.append(WipeStep(system=system, action=action, status="would_delete", detail=detail))
            return

        # Per-row try/except, matching owner_secret_sweep: one bad row must
        # not abort the loop and strand every orphan after it behind a single
        # "failed" with no record of what did or did not go.
        async def _fn() -> str:
            done, failed = 0, 0
            for row in orphans:
                try:
                    await delete_one(row)
                    done += 1
                except Exception:
                    failed += 1
                    logger.exception("sweep_orphans: %s failed for %r", system, row.get(owner_key))
            suffix = f", {failed} failed" if failed else ""
            # Name the owners, not just a count: this string is the audit
            # row's record of what the sweep actually removed.
            ids = ", ".join(sorted({r[owner_key] for r in orphans}))
            return f"deleted {done}/{len(orphans)}{suffix}: {ids}{org_note}"

        await _step(steps, system=system, action=action, fn=_fn)

    await _sweep(
        "integrations",
        "delete",
        await integrations_repo.scan_all(),
        "owner_id",
        lambda r: integrations_repo.delete(r["owner_id"], r["provider"]),
    )
    await _sweep(
        "agent_email_addresses",
        "delete",
        await agent_email_repo.scan_all(),
        "owner_id",
        lambda r: agent_email_repo.delete_by_address(r["address"]),
    )
    await _sweep(
        "company_slug",
        "delete",
        await company_slug_repo.scan_all(),
        "owner_id",
        lambda r: company_slug_repo.delete_for_owner(r["owner_id"]),
    )

    logger.info(
        "account_wipe.sweep_orphans: dry_run=%s live_owners=%d results=%s",
        dry_run,
        len(live),
        [(s.system, s.status, s.detail) for s in steps],
    )
    return OrphanReport(dry_run=dry_run, steps=steps)


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()

    prefix = _require_table_prefix()
    print(f"Table prefix: {prefix}   mode: {'APPLY' if args.apply else 'DRY RUN'}\n")

    report = asyncio.run(sweep_orphans(dry_run=not args.apply))
    for s in report.steps:
        print(f"  [{s.status:>13}] {s.system:<24} {s.detail}{(' ERROR=' + s.error) if s.error else ''}")
    print()
    if not args.apply:
        print("Dry run. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(_main())
