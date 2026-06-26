"""Re-publish the founders' vertical agents from their current EFS workspaces.

Re-snapshots each published vertical into a new listing version so the
marketplace artifacts pick up:
  - the export-hygiene fix (snapshots no longer carry .bak / temp cruft), and
  - for Ora and Thread, a real per-agent BOOTSTRAP.md — they shipped with the
    generic "Hello, World" onboarding placeholder, which declares none of their
    actual integrations. The correct bootstraps are embedded below and seeded
    onto EFS here (overwriting the placeholder) before the snapshot.

Must run in the backend ECS task — it reads/writes the shared EFS mount at
/mnt/efs (export_agent_from_efs + the bootstrap seed). Dry-run by default.

Per published listing (newest published version = N):
  seed bootstrap (ora/thread only)
  -> create_new_version            (N+1 draft, clone of published row)
  -> export_agent_from_efs         (clean re-snapshot of live workspace)
  -> replace_artifact              (upload to the N+1 row)
  -> submit_for_review             (N+1 -> review)
  -> publish_new_version           (only with --approve: N -> retired, N+1 -> published)

Without --approve the new versions land in 'review' for a human to approve in
the admin UI (the size/safety scan runs there). EFS owner is passed explicitly
via --owner-id so no tenant id is baked into the source.

Usage:
  uv run python -m scripts.republish_verticals --env prod --owner-id org_XXXX
  uv run python -m scripts.republish_verticals --env prod --owner-id org_XXXX --apply
  uv run python -m scripts.republish_verticals --env prod --owner-id org_XXXX --apply --approve
"""

from __future__ import annotations

import argparse
import asyncio

# Real per-agent first-run bootstraps for the two agents that shipped the stock
# "Hello, World" placeholder. Kept inline so the exact bytes seeded onto EFS are
# reviewable in this file's diff. Each declares the agent's real integrations.
ORA_BOOTSTRAP = """# BOOTSTRAP.md — Ora, first-run setup

_You just woke up. Before you can do your job, make sure your calendar is
connected. Work through this once, tell your operator what's missing, then delete
this file — you won't need it again._

## What I do
I protect your time, coordinate your meetings, and show you where your calendar
actually goes — without the autonomy that makes AI scheduling go wrong. I
propose; you decide.

## What I need connected (core)

**Google Calendar** — the calendar I read and schedule against.
- Sign in: run `gog login` once and finish the OAuth in the browser.
- Check: `gog whoami`

## Already in the image (no action)
`gog` (the Google Workspace CLI) is present — it just needs the one-time login.

## First-run procedure
1. Run `gog login`, then `gog whoami` to confirm.
2. Tell your operator if Google Calendar isn't connected yet.
3. Once it's connected, delete this file.
"""

THREAD_BOOTSTRAP = """# BOOTSTRAP.md — Thread, first-run setup

_You just woke up. Before you can do your job, make sure the channels you route
across are connected. Work through this once, tell your operator what's still
missing, then delete this file — you won't need it again._

## What I do
I'm the single persistent inbox where everything inbound surfaces and everything
outbound routes — one place, any channel, always knowing where to send a reply.

## What I need connected (core)

**Channels** — the messaging surfaces I unify and route across.
- Link them in **Settings → Channels** (Slack, Telegram, Discord, WhatsApp, …).
- Check: open Settings → Channels and confirm at least one is connected (green).

## Already wired (no action)
Channel transport is handled by the platform — the connection lives on the
channel link, not in my workspace, so there are no binaries or keys for me to
hold. Don't claim a channel is live until it shows connected in Settings.

## First-run procedure
1. Confirm at least one channel is linked in Settings → Channels.
2. Post one message to your operator listing which channels are connected and
   which they still want.
3. Once your channels are linked, delete this file.
"""

# slug -> real bootstrap to seed before snapshotting.
_SEED_BOOTSTRAPS = {"ora": ORA_BOOTSTRAP, "thread": THREAD_BOOTSTRAP}


def _published_listings(table, seller_id: str | None) -> list[dict]:
    """All published listing rows (optionally scoped to one seller)."""
    rows: list[dict] = []
    kwargs: dict = {
        "FilterExpression": "#s = :pub",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":pub": "published"},
    }
    while True:
        resp = table.scan(**kwargs)
        rows.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    # Belt-and-suspenders: DDB applies the status FilterExpression, but filter in
    # Python too so the function is self-contained (and unit-testable without a
    # filter-aware fake table).
    rows = [r for r in rows if r.get("status") == "published"]
    if seller_id:
        rows = [r for r in rows if r.get("seller_id") == seller_id]
    return rows


def _stored_agent_id(row: dict) -> str | None:
    return row.get("agent_id") or (row.get("manifest_json") or {}).get("agent_id")


async def _republish_one(row: dict, *, owner_id: str, approved_by: str, apply: bool, approve: bool) -> str:
    """Re-publish one listing from EFS. Returns a one-line status string."""
    from core.services import agent_export, marketplace_service

    listing_id = row["listing_id"]
    slug = row.get("slug", "?")
    seller_id = row.get("seller_id")
    cur_version = int(row["version"])
    agent_id = _stored_agent_id(row)
    if not agent_id:
        return f"SKIP   {slug:8} {listing_id[:8]} — no agent_id on row"
    if not seller_id:
        return f"SKIP   {slug:8} {listing_id[:8]} — no seller_id on row"

    seeded = slug in _SEED_BOOTSTRAPS
    tag = f"{slug:8} {listing_id[:8]} v{cur_version}->v{cur_version + 1}"
    plan = f"{tag} re-snapshot" + ("  +seed-bootstrap" if seeded else "") + ("  +approve" if approve else "  ->review")
    if not apply:
        return f"PLAN   {plan}"

    # agent_id is the live workspace UUID. Legacy slug-keyed listings are being
    # retired (they get deleted/backfilled, not accommodated here), so we don't
    # slug-resolve — the seed path and the snapshot both key off the row's UUID.

    # 1) Seed the real bootstrap onto EFS (ora/thread) so the snapshot carries it.
    if seeded:
        agent_dir = agent_export._resolve_agent_dir(owner_id, agent_id)
        (agent_dir / "BOOTSTRAP.md").write_text(_SEED_BOOTSTRAPS[slug], encoding="utf-8")

    # 2) New version draft (clone of the published row).
    new_row = await marketplace_service.create_new_version(listing_id=listing_id, seller_id=seller_id)
    new_version = int(new_row["version"])

    # 3) Clean re-snapshot of the live workspace. Preserve the original bundle
    #    selection + ACP flag so the new artifact matches the prior publish.
    bundle_mcps = row.get("bundle_mcps")
    bundle_skills = row.get("bundle_skills")
    requires_acp = bool((row.get("manifest_json") or {}).get("requires_acp"))
    requires_desktop = bool((row.get("manifest_json") or {}).get("requires_desktop"))
    package = agent_export.export_agent_from_efs(
        seller_id=owner_id,
        agent_id=agent_id,
        selected_mcps=list(bundle_mcps) if bundle_mcps else None,
        selected_skills=list(bundle_skills) if bundle_skills else None,
        enable_acp=requires_acp,
        requires_desktop=requires_desktop,
    )
    # Preserve the agent's Paperclip routines in the manifest (mirrors the
    # artifact-from-agent endpoint; best-effort — never blocks the republish).
    package.manifest["routines"] = await agent_export.capture_agent_routines(
        owner_id=owner_id,
        agent_id=agent_id,
    )

    # 4) Upload to the N+1 row.
    await marketplace_service.replace_artifact(
        listing_id=listing_id,
        seller_id=seller_id,
        artifact_bytes=package.tarball_bytes,
        manifest=package.manifest,
        openclaw_slice=package.openclaw_slice,
        agent_id=agent_id,
        version=new_version,
        bundle_mcps=list(bundle_mcps) if bundle_mcps else None,
        bundle_skills=list(bundle_skills) if bundle_skills else None,
    )

    # 5) Draft -> review.
    await marketplace_service.submit_for_review(listing_id=listing_id, seller_id=seller_id, version=new_version)

    # 6) Optionally publish (atomic: prev published -> retired, new review -> published).
    if approve:
        await marketplace_service.publish_new_version(
            listing_id=listing_id,
            prev_version=cur_version,
            new_version=new_version,
            approved_by=approved_by,
        )
        return f"PUBLISHED {tag}" + ("  (seeded)" if seeded else "")
    return f"IN-REVIEW {tag} v{new_version}" + ("  (seeded)" if seeded else "")


async def _amain(args) -> int:
    import boto3

    from core.config import settings

    table_name = settings.MARKETPLACE_LISTINGS_TABLE
    if not table_name:
        print("ERROR: MARKETPLACE_LISTINGS_TABLE unset")
        return 2
    table = boto3.resource("dynamodb").Table(table_name)

    rows = _published_listings(table, args.seller_id)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        rows = [r for r in rows if r.get("slug") in wanted]
    rows.sort(key=lambda r: r.get("slug", ""))

    print(f"=== republish_verticals ({args.env}) — {len(rows)} published listings ===")
    if not rows:
        print("  (nothing matched)")
        return 0
    results = []
    for row in rows:
        try:
            results.append(
                await _republish_one(
                    row,
                    owner_id=args.owner_id,
                    approved_by=args.approved_by,
                    apply=args.apply,
                    approve=args.approve,
                )
            )
        except Exception as exc:  # noqa: BLE001 — report per-listing + continue
            results.append(f"ERROR  {row.get('slug', '?'):8} {row['listing_id'][:8]} — {exc}")
    for line in results:
        print(f"  {line}")

    if not args.apply:
        print("\nDRY-RUN — pass --apply to re-snapshot + submit (add --approve to publish).")
    return 0


def main() -> int:
    from core.constants import SYSTEM_ACTOR_ID

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=["dev", "prod"], required=True)
    parser.add_argument("--owner-id", required=True, help="EFS owner id (org_…/user_…) whose workspaces to snapshot")
    parser.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    parser.add_argument("--approve", action="store_true", help="also publish each new version (else leave in review)")
    parser.add_argument(
        "--approved-by",
        default=SYSTEM_ACTOR_ID,
        help="audit id recorded as published_by on --approve (default: system sentinel)",
    )
    parser.add_argument("--seller-id", help="scope to one seller_id")
    parser.add_argument("--only", help="comma-separated slugs to limit to (e.g. ora,thread,tally)")
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
