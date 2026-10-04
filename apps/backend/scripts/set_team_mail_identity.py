"""Move a workspace's team mail onto a new slug, optionally with a public address.

Runs INSIDE the backend task (ECS exec), like ``roll_hermes_runtimes``:

    uv run python -m scripts.set_team_mail_identity --owner-id <org> --new-slug <slug> \\
        [--public-address agents@<AGENT_EMAIL_DOMAIN>] [--dry-run]

The routing address becomes ``agents@{new-slug}.{domain}``: the slug row, the address row (with
its settings and meetings) and the 30-day inbox history move to it. ``--public-address`` is what
the workspace then sends as and shows (``agent_email.public_for``); only the platform's own
``agents@{domain}`` is accepted. Mail to it reaches SES only through a forwarding rule a person
sets up outside AWS, so the script prints that rule.

Idempotent: every step compares the current state with the target and writes only the
difference, so a re-run after a partial failure finishes the move. The old address row is
deleted LAST, so a re-run can always find what it still has to move. ``--dry-run`` writes
nothing. Exit 0 done, 1 failed, 2 refused.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from boto3.dynamodb.conditions import Key

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import agent_email_repo, agent_mail_inbox_repo, company_slug_repo
from core.services.company_identity import RESERVED_SLUGS, slugify

#: The owner-editable state and history a team row carries over to its new address.
_COPY = ("verified_senders", "send_to_anyone", "status", "outbound_disabled", "meetings", "created_at")
_PAGE = 100


class Refused(Exception):
    pass


async def _query_all(table, **kwargs) -> list[dict]:
    items: list[dict] = []
    while True:
        page = await run_in_thread(table.query, **kwargs)
        items.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


async def run(*, owner_id: str, new_slug: str, public: str | None, dry_run: bool) -> None:
    def step(msg: str) -> None:
        print(("would " if dry_run else "") + msg)

    domain = (settings.AGENT_EMAIL_DOMAIN or "").strip().lower()
    new_slug = new_slug.strip().lower()
    public = public.strip().lower() if public else None

    # 1. Validate.
    if not domain:
        raise Refused("AGENT_EMAIL_DOMAIN is not set")
    if not new_slug or slugify(new_slug) != new_slug:
        raise Refused(f"{new_slug!r} is not a valid slug")
    if new_slug in RESERVED_SLUGS:
        print(f"note: {new_slug!r} is a reserved slug; allowed because this is a platform-run tool")
    if public and public != f"agents@{domain}":
        raise Refused(f"--public-address must be exactly agents@{domain}")
    slug_table = company_slug_repo._get_table()
    slug_rows = await _query_all(slug_table, IndexName="by-owner", KeyConditionExpression=Key("owner_id").eq(owner_id))
    if not slug_rows:
        raise Refused(f"{owner_id} has no slug row")
    holder = await company_slug_repo.get_by_slug(new_slug)
    if holder and holder.get("owner_id") != owner_id:
        raise Refused(f"slug {new_slug!r} is held by another owner")
    new_addr = f"agents@{new_slug}.{domain}"
    rows = await agent_email_repo.query_by_owner(owner_id)
    old_rows = [
        r for r in rows if not r.get("alias_of") and r["address"].startswith("agents@") and r["address"] != new_addr
    ]
    if len(old_rows) > 1:
        raise Refused(f"{owner_id} has several team rows: {[r['address'] for r in old_rows]}")
    for r in rows:
        if not r.get("alias_of") and not r["address"].startswith("agents@"):
            print(f"note: legacy row {r['address']} is left in place")
    old = old_rows[0] if old_rows else None
    new = await agent_email_repo.get_by_address(new_addr)
    if not old and not new:
        raise Refused(f"{owner_id} has no team address row to move")
    public = public or (old or new or {}).get("public_address")
    if public:
        alias = await agent_email_repo.get_by_address(public)
        if alias and alias.get("owner_id") != owner_id:
            raise Refused(f"{public} belongs to another owner")
    print(f"ok: {owner_id} -> {new_addr}" + (f" (public {public})" if public else ""))

    # 2. Slug: the new row first, then the old ones, so get_by_owner answers the new slug.
    if not holder:
        step(f"reserve slug {new_slug}")
        if not dry_run and not await company_slug_repo.reserve(slug=new_slug, owner_id=owner_id):
            raise Refused(f"slug {new_slug!r} was taken while running")
    for r in slug_rows:
        if r["slug"] != new_slug:
            step(f"delete slug row {r['slug']}")
            if not dry_run:
                await run_in_thread(slug_table.delete_item, Key={"slug": r["slug"]})

    # 3. Address row: the new routing row (old row's settings win while it exists), then the alias.
    table = agent_email_repo._get_table()
    item = {k: v for k, v in (old or new).items() if k in _COPY and v not in (None, set())}
    item.update(address=new_addr, owner_id=owner_id)
    item.setdefault("created_at", utc_now_iso())
    meetings = set((old or {}).get("meetings") or ()) | set((new or {}).get("meetings") or ())
    if meetings:
        item["meetings"] = meetings
    if public:
        item["public_address"] = public
    if item != new:
        step(f"write address row {new_addr}")
        if not dry_run:
            await run_in_thread(table.put_item, Item=item)
    if public:
        want = {"address": public, "alias_of": new_addr, "owner_id": owner_id}
        if await agent_email_repo.get_by_address(public) != want:
            step(f"write alias row {public} -> {new_addr}")
            if not dry_run:
                await run_in_thread(table.put_item, Item=want)

    # 4. Inbox history: copy with the same received_key and TTL, then delete the old rows.
    if old:
        inbox = agent_mail_inbox_repo._get_table()
        moved = 0
        kwargs = {"KeyConditionExpression": Key("address").eq(old["address"]), "Limit": _PAGE}
        while True:
            # Deleting as we go: each page restarts from the (now shorter) head.
            page = await run_in_thread(inbox.query, **kwargs)
            items = page.get("Items", [])
            if dry_run:
                moved += len(items)
                if not page.get("LastEvaluatedKey"):
                    break
                kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
                continue
            for it in items:
                await run_in_thread(inbox.put_item, Item={**it, "address": new_addr})
                await run_in_thread(
                    inbox.delete_item, Key={"address": it["address"], "received_key": it["received_key"]}
                )
            moved += len(items)
            if not items:
                break
        step(f"move {moved} inbox row(s) {old['address']} -> {new_addr}")
        step(f"delete address row {old['address']}")
        if not dry_run:
            await run_in_thread(table.delete_item, Key={"address": old["address"]})

    # 5. Facts: agents read their address from lightbulb-facts.json.
    step(f"rewrite owner facts for {owner_id}")
    if not dry_run:
        from core.services.owner_facts import write_owner_facts

        await write_owner_facts(owner_id)

    # 6. What a person must do outside AWS.
    print("TODO (person):")
    if public:
        print(f"  - Google Workspace routing rule: {public} -> {new_addr}")
    print("  - replace the old address on calendar events that invited it")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--new-slug", required=True)
    parser.add_argument("--public-address")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        await run(owner_id=args.owner_id, new_slug=args.new_slug, public=args.public_address, dry_run=args.dry_run)
    except Refused as e:
        print(f"refused: {e}")
        return 2
    except Exception as e:  # noqa: BLE001 -- an ops script reports and exits
        print(f"failed: {e!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
