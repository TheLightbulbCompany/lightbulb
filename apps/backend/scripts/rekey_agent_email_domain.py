"""One-shot: move every agent email address from ``isol8.co`` to ``thelightbulb.company``.

WHY THIS IS A RE-KEY, NOT AN UPDATE
-----------------------------------
``address`` is the PARTITION KEY of ``isol8-{env}-agent-email-addresses`` —
inbound SES mail is routed by recipient, so the address has to be the key.
There is no such thing as "updating" it: the new address is a new item.

What makes that safe is ``status``. ``agent_email.resolve_from_address``
picks the ``status == "active"`` row for an agent as its from-address, and
``agent_email_repo``'s docstring already reserves ``"alias"`` for "a superseded
row that must keep routing". So the migration is:

    1. PUT a new row at ``{agent}@{slug}.thelightbulb.company``, status
       ``active``, carrying every other attribute of the old row verbatim
       (``verified_senders``, ``send_to_anyone``, ``outbound_disabled``,
       ``created_at``, ``public``).
    2. DEMOTE the old row to ``status="alias"`` — never delete it.

Step 2 is a demote and NOT a delete on purpose. These addresses are published:
they are in the From: header of every mail an agent has ever sent, which means
they sit in other people's address books, mail clients' autocomplete, and
allowlists. Deleting the row makes inbound to the already-published address
resolve to no agent at all — a hard bounce for a correspondent who did nothing
wrong. An alias row still routes.

ORDER MATTERS. The new row is written BEFORE the old one is demoted. A crash
between the two leaves the agent with two ``active`` rows, both routing to the
same agent — benign, and the next run repairs it. The reverse order would
leave a window with NO active row, which is ``resolve_from_address`` returning
None: the agent cannot send at all.

WHY IT DOES NOT USE ``agent_email_repo.put``
--------------------------------------------
That primitive is a CREATE: it always stamps a fresh ``created_at`` and only
knows about ``verified_senders``, so it would drop ``send_to_anyone`` /
``outbound_disabled`` / ``public`` and reset the deterministic earliest-
``created_at`` tie-break ``owner_facts._lookup_email_address`` sorts on. Its
own docstring says a future alias flow must not call it. It also has no
demote primitive, and ``update_settings`` deliberately cannot touch ``status``.
So both writes here are direct, with the same conditional shape the repo uses:
``attribute_not_exists(address) OR owner_id = :owner`` on the put, and
``attribute_exists(address)`` on the demote so a row deleted between scan and
write is not resurrected.

VERIFIED SENDERS ARE REPORTED, NEVER REWRITTEN
----------------------------------------------
``verified_senders`` is seeded at mint time from the owner's CLERK address
(``mint_agent_email_address`` → ``verified_senders=[owner_email]``), and
``sender_allowed`` is what decides whether an inbound mail WAKES the agent. So
when a human's own mailbox moves off ``isol8.co``, their agent silently stops
waking on their mail — the address that is allowed is the one they no longer
send from.

This script REPORTS those rows and rewrites nothing. Guessing the human's new
address is exactly the one-way door ``backfill_paperclip_email`` documents:
a wrong entry looks migrated while permanently admitting the wrong sender (or
nobody). The owner self-serves it, and the door already exists:

    PATCH /api/v1/agent-mail/addresses/{agent_id}
        {"verified_senders": ["me@thelightbulb.company"]}

``_validate_senders`` accepts a bare ``@domain`` entry too, and
``sender_allowed`` matches it against the sender's domain exactly (no
subdomain matching), so ``["@thelightbulb.company"]`` admits the whole
company in one entry. ``patch_address`` resolves the agent's ACTIVE address
first, so after this script runs the PATCH lands on the new row, not the alias.

SES TENANTS RE-ASSOCIATE THEMSELVES; THE OLD ASSOCIATION DOES NOT GO AWAY
-------------------------------------------------------------------------
``agent_mail_tenant`` creates one SES tenant per owner and associates it to
``identity/{apex}``, where the apex is ``_identity_domain()`` — today
``isol8.co`` for any ``AGENT_EMAIL_DOMAIN`` ending in it, tomorrow
``thelightbulb.company``. ``_ensured`` is a PER-PROCESS memo, so after the
backend restarts with the new ``AGENT_EMAIL_DOMAIN`` every owner's tenant
re-runs ``_ensure_sync`` on that owner's next send and picks up the new
identity association on its own. Nothing here has to touch SES.

But ``create_tenant_resource_association`` only ADDS. The old
``arn:aws:ses:{region}:{account}:identity/isol8.co`` association stays on every
tenant forever, so the ``isol8.co`` SES identity can NEVER be deleted until
each per-owner tenant is dissociated from it
(``DeleteTenantResourceAssociation``, one call per tenant). That is deliberate
for now — the identity has to stay verified anyway while the alias rows keep
routing inbound. Treat identity deletion as a separate, much later step.

PREREQUISITE: ``thelightbulb.company`` must already be a VERIFIED SES domain
identity with the inbound receipt rule pointed at it before the flip, or the
new addresses are unroutable in both directions.

Usage::

    cd apps/backend
    uv run python -m scripts.rekey_agent_email_domain --env prod --dry-run
    uv run python -m scripts.rekey_agent_email_domain --env prod

``--dry-run`` (default off) reports the plan and writes nothing. Idempotent:
only ``status="active"`` rows still on the old domain are moved, so a second
run is a no-op. Every run ends with a verification pass; exit code 0 means no
active row is left on the old domain.

DELETE THIS SCRIPT once it has run against dev + prod — per CLAUDE.md, a
one-shot whose data is produced at creation time afterwards is git history.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field

logger = logging.getLogger("rekey_agent_email_domain")

OLD_DOMAIN = "isol8.co"
NEW_DOMAIN = "thelightbulb.company"

# Carried onto the new row verbatim. `address` is the new key and `status` is
# forced to "active"; everything else is the owner's own state and must survive
# the move. Listed rather than copied wholesale so a future attribute has to be
# considered here instead of silently riding along.
_CARRIED = ("owner_id", "agent_id", "verified_senders", "created_at", "send_to_anyone", "outbound_disabled", "public")


def rekey(address: str, old_domain: str, new_domain: str) -> str | None:
    """``{agent}@{slug}.{old}`` → ``{agent}@{slug}.{new}``, or None.

    Anchored to the END of the address: the company slug is its own subdomain
    label, so the domain to swap is the SUFFIX ``.{old_domain}``. A plain
    ``replace`` would also rewrite a slug that happened to contain the string.
    """
    suffix = "." + old_domain
    a = (address or "").strip().lower()
    if "@" not in a or not a.endswith(suffix):
        return None
    return a[: -len(suffix)] + "." + new_domain


@dataclass
class Plan:
    moves: list[tuple[dict, str]] = field(default_factory=list)
    """(old row, new address) — an active row to re-key."""
    collisions: list[tuple[str, str, str]] = field(default_factory=list)
    """(old address, new address, why) — the new key is already taken by
    someone else. Reported and skipped: the old row stays ACTIVE so the agent
    keeps working, and a human decides."""
    stale_senders: list[tuple[str, list[str]]] = field(default_factory=list)
    """(address, entries still on the old domain) — reported, never rewritten."""
    already_moved: int = 0
    aliases_left_alone: int = 0
    unrecognised: list[str] = field(default_factory=list)
    """Active rows on neither domain — a hand-made row, or a domain nobody
    told this script about. Never guessed at."""


def plan(rows: list[dict], old_domain: str, new_domain: str) -> Plan:
    """Decide what moves. Pure — the whole risk surface, and the only part worth
    testing without AWS.

    ``rows`` is the WHOLE table (the address is the key, so a collision can only
    be seen against every row), which is what makes the collision check possible
    before a single write.
    """
    p = Plan()
    by_address = {(r.get("address") or "").strip().lower(): r for r in rows}

    for row in rows:
        address = (row.get("address") or "").strip().lower()
        status = row.get("status")
        if not address:
            continue

        if status != "active":
            if address.endswith("." + old_domain):
                p.aliases_left_alone += 1
            continue

        if address.endswith("." + new_domain):
            p.already_moved += 1
            _note_stale_senders(p, address, row, old_domain)
            continue

        new_address = rekey(address, old_domain, new_domain)
        if new_address is None:
            p.unrecognised.append(address)
            continue

        existing = by_address.get(new_address)
        if existing is not None:
            # Same owner AND same agent is this script's own previous run
            # (or a crash between the two writes): the new row is already
            # correct, so all that is left is demoting the old one.
            if existing.get("owner_id") == row.get("owner_id") and existing.get("agent_id") == row.get("agent_id"):
                p.moves.append((row, new_address))
                _note_stale_senders(p, address, row, old_domain)
                continue
            p.collisions.append(
                (
                    address,
                    new_address,
                    f"held by owner={existing.get('owner_id')} agent={existing.get('agent_id')}",
                )
            )
            continue

        p.moves.append((row, new_address))
        _note_stale_senders(p, address, row, old_domain)

    return p


def _note_stale_senders(p: Plan, address: str, row: dict, old_domain: str) -> None:
    stale = [
        s
        for s in (row.get("verified_senders") or [])
        if (s or "").strip().lower().endswith("@" + old_domain) or (s or "").strip().lower() == "@" + old_domain
    ]
    if stale:
        p.stale_senders.append((address, stale))


def _new_item(row: dict, new_address: str) -> dict:
    item = {"address": new_address, "status": "active"}
    for k in _CARRIED:
        if k in row:
            item[k] = row[k]
    return item


async def _write_move(row: dict, new_address: str) -> str | None:
    """Put the new row, then demote the old one. Returns None on success, else
    a reason. Never raises for a lost conditional — that is data, not a crash."""
    from botocore.exceptions import ClientError

    from core.dynamodb import get_table, run_in_thread

    table = get_table("agent-email-addresses")
    owner_id = row.get("owner_id")
    old_address = row["address"]

    try:
        await run_in_thread(
            table.put_item,
            Item=_new_item(row, new_address),
            # Same shape as agent_email_repo.put: a concurrent mint or another
            # owner's row can never be overwritten.
            ConditionExpression="attribute_not_exists(address) OR owner_id = :owner",
            ExpressionAttributeValues={":owner": owner_id},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return f"new address {new_address} is held by another owner"
        raise

    try:
        await run_in_thread(
            table.update_item,
            Key={"address": old_address},
            UpdateExpression="SET #s = :alias",
            # Do not resurrect a row deleted between the scan and now (account
            # wipe, orphan sweep) as a bare {address, status} husk.
            ConditionExpression="attribute_exists(address)",
            ExpressionAttributeNames={"#s": "status"},  # `status` is a DDB reserved word
            ExpressionAttributeValues={":alias": "alias"},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # The new row landed, so the agent is fine; nothing to demote.
            return None
        raise
    return None


async def _scan() -> list[dict]:
    from core.repositories import agent_email_repo

    return await agent_email_repo.scan_all()


def _log_plan(p: Plan, old_domain: str, new_domain: str) -> None:
    logger.info(
        "Plan: %d to move, %d already on %s, %d alias rows left alone, %d collisions, %d unrecognised",
        len(p.moves),
        p.already_moved,
        new_domain,
        p.aliases_left_alone,
        len(p.collisions),
        len(p.unrecognised),
    )
    for row, new_address in p.moves:
        logger.info(
            "  MOVE %s -> %s (owner=%s agent=%s)",
            row["address"],
            new_address,
            row.get("owner_id"),
            row.get("agent_id"),
        )
    for old, new, why in p.collisions:
        logger.error("  COLLISION %s -> %s: %s — left ACTIVE on the old domain", old, new, why)
    for address in p.unrecognised:
        logger.warning("  UNRECOGNISED %s — on neither %s nor %s; skipped", address, old_domain, new_domain)
    for address, stale in p.stale_senders:
        # Not an error and not fixed here: rewriting it is a guess (see the
        # module docstring). The owner PATCHes their own address.
        logger.warning(
            "  STALE SENDERS %s: %s still on %s — this agent will stop waking on its owner's mail "
            "until the owner PATCHes /api/v1/agent-mail/addresses/{agent_id}",
            address,
            ", ".join(stale),
            old_domain,
        )


async def _verify(old_domain: str, new_domain: str) -> int:
    """Re-read the table and count what is left. Non-zero while ANY active row
    still sits on the old domain — the rollout gate reads this exit code, so it
    must never be a hopeful one."""
    rows = await _scan()
    still_old = [
        (r.get("address") or "")
        for r in rows
        if r.get("status") == "active" and (r.get("address") or "").endswith("." + old_domain)
    ]
    on_new = sum(1 for r in rows if r.get("status") == "active" and (r.get("address") or "").endswith("." + new_domain))
    aliases = sum(1 for r in rows if r.get("status") == "alias")
    logger.info(
        "Verify: %d active on %s, %d active still on %s, %d alias rows routing (%d rows total)",
        on_new,
        new_domain,
        len(still_old),
        old_domain,
        aliases,
        len(rows),
    )
    if still_old:
        logger.error("Active rows still on %s: %s", old_domain, ", ".join(sorted(still_old)))
        return 1
    return 0


async def _run(dry_run: bool) -> int:
    rows = await _scan()
    logger.info("Scanned %d address rows", len(rows))
    p = plan(rows, OLD_DOMAIN, NEW_DOMAIN)
    _log_plan(p, OLD_DOMAIN, NEW_DOMAIN)

    if dry_run:
        logger.info("DRY RUN — nothing written")
        # Still report the collisions: they are the reason a real run would not
        # reach a clean verify, and the operator wants to know BEFORE the flip.
        return 1 if p.collisions else 0

    failed: list[str] = []
    for row, new_address in p.moves:
        try:
            reason = await _write_move(row, new_address)
        except Exception:
            # One owner's failure must not strand the rest of the fleet.
            failed.append(row["address"])
            logger.exception("FAILED %s", row["address"])
            continue
        if reason:
            failed.append(row["address"])
            logger.error("FAILED %s: %s", row["address"], reason)
            continue
        logger.info("moved %s -> %s", row["address"], new_address)

    logger.info("Done: %d moved, %d failed", len(p.moves) - len(failed), len(failed))
    rc = await _verify(OLD_DOMAIN, NEW_DOMAIN)
    return rc or (1 if (failed or p.collisions) else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--dry-run", action="store_true", help="report the plan; write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Table naming reads DYNAMODB_TABLE_PREFIX, which DEFAULTS to "isol8-dev-"
    # — ENVIRONMENT does not affect it. An `--env prod` run that scanned dev
    # would report a clean verify for a fleet it never touched, and here the
    # clean verify is the green light to point SES at the new domain. Set the
    # prefix explicitly, and OVERWRITE so an inherited dev value in the
    # operator's shell cannot silently win.
    #
    # Must happen before core.config is imported (settings is built at import
    # time and core.dynamodb snapshots the prefix at ITS import). Every core.*
    # import in this module is function-local for that reason — do not hoist.
    os.environ["ENVIRONMENT"] = args.env
    os.environ["DYNAMODB_TABLE_PREFIX"] = f"isol8-{args.env}-"

    from core.dynamodb import table_name

    resolved = table_name("agent-email-addresses")
    expected = f"isol8-{args.env}-agent-email-addresses"
    if resolved != expected:
        logger.error("Table resolution mismatch: got %s, expected %s — refusing to run", resolved, expected)
        return 2
    logger.info("Table: %s  (%s -> %s)", resolved, OLD_DOMAIN, NEW_DOMAIN)

    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
