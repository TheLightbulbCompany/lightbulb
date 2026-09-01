"""One-shot: seed ``paperclip_email`` on every Paperclip company row from Aurora.

WHY THIS EXISTS
---------------
Better Auth's ``user.email`` is written once at provision time and NEVER
updated. Every per-user Paperclip sign-in door used to pass the user's LIVE
Clerk address, so the moment a human's Clerk primary email changes the sign-in
401s, falls through to sign-UP, and mints a DUPLICATE Better Auth account —
and Better Auth's sign-in rate limit is per-IP, so Teams degrades fleet-wide,
not for one user.

The doors now prefer ``PaperclipCompany.paperclip_email`` and fall back to the
live Clerk address only when it is empty. This backfill fills it for rows
written before the field existed.

SEED FROM AURORA, NOT CLERK
---------------------------
The sign-in truth is the Better Auth row itself (``SELECT email FROM "user"``),
not Clerk. Seeding from Clerk AFTER a mailbox move would write an address
Aurora does not have and CEMENT the 401 the field exists to prevent — the row
would then look backfilled while being permanently wrong.

Which means this has a ONE-WAY DOOR: run it while Clerk and Aurora still agree,
i.e. BEFORE anybody's Clerk primary email changes. Afterwards, recovery is a
manual per-owner ``SELECT email FROM "user" WHERE id = '<paperclip_user_id>'``.

ORDERING — get this wrong and the backfill is silently undone:

    1. MERGE + DEPLOY the backend that knows about ``paperclip_email``.
    2. WAIT for the ECS rollover to finish (no old tasks left).
    3. Run this script.
    4. Verify (exit code 0).
    5. ONLY THEN move anybody's Clerk primary email.

Step 2 is not optional. ``PaperclipRepo.put`` is a raw ``PutItem`` and the
PRE-DEPLOY version of it has no ``paperclip_email`` key, so any task still on
old code that runs ``provision_org`` / ``provision_member`` (a member join, a
reconcile-adopt, a failed-row retry) SILENTLY BLANKS a backfilled row. Deploys
run at ``maxHealthyPercent: 200``, so there is always a window with one old task
serving traffic. For the same reason: DO NOT ROLL THE BACKEND BACK after
backfilling without re-running this script.

Usage::

    cd apps/backend
    DYNAMODB_TABLE_PREFIX=lightbulb-prod- uv run python -m scripts.backfill_paperclip_email --env prod --dry-run
    DYNAMODB_TABLE_PREFIX=lightbulb-prod- uv run python -m scripts.backfill_paperclip_email --env prod

(``--env`` sets the prefix itself; the explicit env var above is belt-and-braces
and is overwritten, not merged.) Requires AWS SSO
(``aws sso login --profile isol8-admin``) — it reads the ``paperclip-companies``
DDB table and runs one read-only RDS Data API statement.

``--dry-run`` (default off) reports what would be written and writes nothing.
Idempotent: rows that already carry a non-empty ``paperclip_email`` are skipped,
and a stored value that DISAGREES with Aurora is reported, never overwritten.

Every run — dry or not — ends with the verification count the rollout gate
wants: how many ``status=active`` rows carry a non-empty value, and which ones
do not. Exit code 0 means every active row is covered; anything else means NOT
READY, so nobody should touch a mailbox yet.

ORPHANS pin the exit code to non-zero on purpose. A row whose Better Auth user
no longer exists in Aurora (logged ``NO AURORA ROW``) cannot be backfilled —
this script refuses to guess an address, because a wrong guess cements the very
401 it exists to prevent. There is no ``--force``: resolve each orphan by hand
(reprovision the owner, or archive the dead row) and re-run. An orphan is a real
"not ready" signal, not a nuisance to be flagged past.

DELETE THIS SCRIPT once it has run against dev + prod — per CLAUDE.md, a
one-shot backfill whose data is produced at creation time afterwards is git
history, not a file to keep.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("backfill_paperclip_email")


async def _scan_company_rows() -> list[dict]:
    from core.dynamodb import get_table, run_in_thread

    table = get_table("paperclip-companies")
    items: list[dict] = []
    last_evaluated_key: dict | None = None
    while True:
        # ConsistentRead: this scan produces the rollout GATE number, and the
        # verification pass runs immediately after a write burst. An eventually
        # consistent read there reports rows as STILL MISSING that were in fact
        # written, exits 1, and tells the operator the fleet is not ready when it is.
        kwargs: dict[str, Any] = {"ConsistentRead": True}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        items.extend(resp.get("Items", []))
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


async def _better_auth_emails(env: str) -> dict[str, str]:
    """``{better_auth_user_id: email}`` for the whole instance.

    ONE statement rather than a parameterized lookup per row: the Better Auth
    ``user`` table holds one row per human across all tenants (a fleet of tens,
    not millions), so the whole id->email map is a rounding error against the
    Data API's response cap and costs a single round trip instead of N.
    """
    from core.services.paperclip_sql import select_rows

    rows = await select_rows('SELECT id AS id, email AS email FROM "user"', env=env)
    return {r["id"]: r["email"] for r in rows if r.get("id") and r.get("email")}


async def _write_email(user_id: str, email: str) -> bool:
    """Set ``paperclip_email`` on one row. False when a concurrent writer got
    there first (the condition is the race fence, not the idempotency check —
    that already happened in ``_run``)."""
    from botocore.exceptions import ClientError

    from core.dynamodb import get_table, run_in_thread

    table = get_table("paperclip-companies")
    try:
        # Targeted UpdateItem, never PaperclipRepo.put: put is a raw PutItem
        # that would wipe every onboarding field this script does not know
        # about (see the WRITER-SAFETY note on PaperclipRepo.put).
        await run_in_thread(
            table.update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET paperclip_email = :e",
            # attribute_exists(user_id) is load-bearing: UpdateItem is an UPSERT, so a row
            # deleted between the scan and this write (account wipe, purge sweep) would be
            # RESURRECTED carrying only {user_id, paperclip_email} — no status, no created_at.
            # _item_to_company then KeyErrors on every get() (hard 500 on /teams, the proxy and
            # /container/status) and try_acquire_seed_claim's attribute_not_exists(user_id) can
            # never succeed again. Same guard update_status uses, for the same reason.
            ConditionExpression=(
                "attribute_exists(user_id) AND (attribute_not_exists(paperclip_email) OR paperclip_email = :empty)"
            ),
            ExpressionAttributeValues={":e": email, ":empty": ""},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def _report_coverage(rows: list[dict], *, label: str) -> int:
    """Log the rollout gate's number: how many ``status=active`` rows still
    carry no ``paperclip_email``. Returns that count (0 is the green light)."""
    active = [r for r in rows if r.get("status") == "active"]
    missing = [r for r in active if not r.get("paperclip_email")]
    logger.info(
        "%s: %d active rows, %d carry paperclip_email, %d missing",
        label,
        len(active),
        len(active) - len(missing),
        len(missing),
    )
    for r in missing:
        logger.error(
            "STILL MISSING user_id=%s paperclip_user_id=%s company=%s",
            r.get("user_id"),
            r.get("paperclip_user_id") or "-",
            r.get("company_id") or "-",
        )
    return len(missing)


def plan(rows: list[dict], emails: dict[str, str]) -> tuple[list[tuple[str, str]], int, int]:
    """``(targets, mismatches, orphans)`` — the whole risk surface of this
    script, kept pure so it is testable without AWS.

    ``targets`` are ``(user_id, email)`` pairs to write. A row is a target only
    when it has a Better Auth account and no stored address yet.
    """
    targets: list[tuple[str, str]] = []
    orphans = 0
    mismatches = 0
    for r in rows:
        user_id = r.get("user_id")
        pc_user_id = r.get("paperclip_user_id")
        if not user_id:
            continue
        stored = r.get("paperclip_email") or ""
        aurora = emails.get(pc_user_id) if pc_user_id else None
        if stored:
            # Already backfilled. Never overwrite — a stored value IS the
            # sign-in truth the doors use, and clobbering it with a guess is
            # exactly the failure mode this script exists to avoid. Report
            # disagreement for a human instead.
            if aurora and aurora != stored:
                mismatches += 1
                logger.error(
                    "MISMATCH user_id=%s stored=%s aurora=%s — left untouched, resolve by hand",
                    user_id,
                    stored,
                    aurora,
                )
            continue
        if not pc_user_id:
            # A seed claim or credential-less failed marker: no Better Auth
            # account exists, so there is no address to record. The next
            # provision attempt writes one.
            continue
        if not aurora:
            orphans += 1
            logger.warning(
                "NO AURORA ROW user_id=%s paperclip_user_id=%s status=%s — Better Auth user is gone",
                user_id,
                pc_user_id,
                r.get("status"),
            )
            continue
        targets.append((user_id, aurora))
    return targets, mismatches, orphans


async def _run(env: str, dry_run: bool) -> int:
    rows = await _scan_company_rows()
    active_count = sum(1 for r in rows if r.get("status") == "active")
    emails = await _better_auth_emails(env)
    logger.info(
        "Scanned %d company rows (%d active); Better Auth holds %d users",
        len(rows),
        active_count,
        len(emails),
    )

    targets, mismatches, orphans = plan(rows, emails)

    logger.info(
        "%d rows need a paperclip_email (%d mismatched, %d orphaned)",
        len(targets),
        mismatches,
        orphans,
    )
    for user_id, email in targets:
        logger.info("  %s -> %s", user_id, email)

    if dry_run:
        # Same exit-code contract as a real run: non-zero means "not ready",
        # so `--dry-run && <touch a mailbox>` cannot pass on a fleet that is
        # still missing values.
        missing = _report_coverage(rows, label="BEFORE (dry run)")
        logger.info("DRY RUN — nothing written")
        return 1 if (missing or mismatches) else 0

    written = 0
    raced = 0
    for user_id, email in targets:
        if await _write_email(user_id, email):
            written += 1
        else:
            raced += 1
            logger.info("SKIPPED user_id=%s — a concurrent writer set it first", user_id)

    logger.info("Done: %d written, %d skipped", written, raced)

    # Re-scan rather than trusting the writes: this count is what greenlights
    # touching a mailbox, and the door only swings one way.
    still_missing = _report_coverage(await _scan_company_rows(), label="VERIFY")
    if still_missing or mismatches:
        # Non-zero so an operator cannot mistake a partial run for the green
        # light to touch a mailbox.
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed PaperclipCompany.paperclip_email from the Better Auth user table in Aurora.",
    )
    parser.add_argument("--env", choices=["dev", "prod"], required=True, help="Environment to backfill.")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Table naming reads DYNAMODB_TABLE_PREFIX, which DEFAULTS TO "lightbulb-dev-"
    # — ENVIRONMENT does not affect it. Set the prefix explicitly, and
    # OVERWRITE rather than setdefault so an inherited dev value in the
    # operator's shell cannot silently point a `--env prod` run at the dev
    # table and report a confident all-clear about a fleet it never read.
    #
    # This must happen before core.config is imported (settings is built at
    # import time and core.dynamodb snapshots the prefix at ITS import). Every
    # import in this module is function-local for exactly that reason — do not
    # hoist them to the top.
    os.environ["ENVIRONMENT"] = args.env
    os.environ["DYNAMODB_TABLE_PREFIX"] = f"lightbulb-{args.env}-"

    from core.dynamodb import table_name

    resolved = table_name("paperclip-companies")
    expected = f"lightbulb-{args.env}-paperclip-companies"
    if resolved != expected:
        logger.error("Table resolution mismatch: got %s, expected %s — refusing to run", resolved, expected)
        return 2

    logger.info("Scanning table: %s", resolved)
    return asyncio.run(_run(args.env, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
