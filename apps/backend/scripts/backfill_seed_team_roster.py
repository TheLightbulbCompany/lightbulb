"""One-shot backfill: roster metadata for seeded Friday/Atlas agents.

``provision_org`` now registers Friday and Atlas with ``title``,
``capabilities``, and (for Atlas) ``reportsTo`` — the roster is Friday's
delegation source of truth, and a bare ``role="general"`` Atlas reads as a
generalist, which is how Friday ends up assigning it domain work.
Companies provisioned before that change still carry bare rows; this
script fills them in.

Run via::

    cd apps/backend
    uv run python -m scripts.backfill_seed_team_roster --env prod
    uv run python -m scripts.backfill_seed_team_roster --env prod --apply

Defaults to dry-run; pass ``--apply`` to actually PATCH agents.

Idempotent and non-destructive: only EMPTY fields are filled — a title,
capabilities text, or reportsTo the owner customized is never overwritten,
and a fully-populated company produces no PATCH at all. Seed identification
prefers the persisted ``seeded_agent_ids`` marker (rename-proof, authoritative
when present — a missing seeded agent is skipped, never substituted); only
markerless legacy rows fall back to role/name matching, so a renamed Atlas on
such a row is skipped.

Scan + repo patterns mirror ``backfill_paperclip_for_legacy_users.py``;
admin client construction mirrors ``paperclip_provisioning
.build_paperclip_provisioning``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger("backfill_seed_team_roster")


def compute_agent_patches(agents: list[dict], seeded_agent_ids: list[str] | None = None) -> dict[str, dict]:
    """Return ``{agent_id: patch_body}`` for the seed team, filling only empty fields.

    ``seeded_agent_ids`` is the ORDERED persisted marker: ``provision_org``
    appends Friday first, Atlas second, and neither current role nor name is
    a safe handle on marker-bearing rows (Teams lets owners create extra
    role="ceo" agents AND re-role/rename the seeds — a role-edited Friday
    must never be stamped with Atlas metadata). A lone seeded id (the other
    mint failed at provision time) is disambiguated by role/name and skipped
    when ambiguous. When a seeded id is absent from the roster, do NOT fall
    through to role/name matching. Companies whose row predates the marker
    fall back to role/name matching. Pure — no I/O — so it carries the unit
    tests for the fill-only-empty contract.
    """
    from core.services.paperclip_provisioning import (
        ATLAS_CAPABILITIES,
        ATLAS_TITLE,
        FRIDAY_CAPABILITIES,
        FRIDAY_TITLE,
    )

    friday: dict | None = None
    atlas: dict | None = None
    friday_ref_id: str | None = None  # reportsTo target — survives a role-edited Friday
    if seeded_agent_ids:
        by_id = {a["id"]: a for a in agents if a.get("id")}
        if len(seeded_agent_ids) >= 2:
            friday = by_id.get(seeded_agent_ids[0])
            atlas = by_id.get(seeded_agent_ids[1])
            # reportsTo target only when the seeded Friday still exists in
            # the roster — never point Atlas at a deleted/absent agent id.
            friday_ref_id = seeded_agent_ids[0] if friday is not None else None
        else:
            lone = by_id.get(seeded_agent_ids[0])
            is_ceo_role = lone is not None and lone.get("role") == "ceo"
            is_atlas_name = lone is not None and lone.get("name") == "Atlas"
            if is_ceo_role and not is_atlas_name:
                friday = lone
                friday_ref_id = lone["id"]
            elif is_atlas_name and not is_ceo_role:
                atlas = lone
            # else: ambiguous lone seed — signals absent OR conflicting (a
            # ceo-roled agent still named "Atlas" could be a promoted Atlas
            # or a renamed Friday) — skip rather than guess.
        if friday is not None and friday.get("role") != "ceo":
            # Owner re-roled the seeded Friday; their customization outranks
            # the backfill — don't stamp CEO metadata onto it. It still
            # anchors Atlas's reportsTo via friday_ref_id.
            friday = None
        if atlas is not None and atlas.get("role") != "general":
            # Symmetric guard: a seeded Atlas the owner promoted/re-roled
            # keeps their customization — no recruiter stamp, no reparenting.
            atlas = None
    else:
        # No marker (row predates seeded_agent_ids): name+role matching is
        # the only handle available for these legacy rows — for BOTH seeds.
        # Teams permits extra role="ceo" agents, so bare role would let a
        # user-created CEO returned first shadow the real seeded Friday; a
        # renamed legacy seed is skipped (consistent with the Atlas match).
        friday = next(
            (a for a in agents if a.get("name") == "Friday" and a.get("role") == "ceo" and a.get("id")),
            None,
        )
        friday_ref_id = friday["id"] if friday else None
        atlas = next(
            (a for a in agents if a.get("name") == "Atlas" and a.get("role") == "general" and a.get("id")),
            None,
        )

    patches: dict[str, dict] = {}
    if friday:
        body: dict[str, Any] = {}
        if not friday.get("title"):
            body["title"] = FRIDAY_TITLE
        if not friday.get("capabilities"):
            body["capabilities"] = FRIDAY_CAPABILITIES
        if body:
            patches[friday["id"]] = body
    if atlas:
        body = {}
        if not atlas.get("title"):
            body["title"] = ATLAS_TITLE
        if not atlas.get("capabilities"):
            body["capabilities"] = ATLAS_CAPABILITIES
        if friday_ref_id and not atlas.get("reportsTo"):
            body["reportsTo"] = friday_ref_id
        if body:
            patches[atlas["id"]] = body
    return patches


async def _scan_owner_ids() -> list[str]:
    """Paginate the ``containers`` table and return every ``owner_id``."""
    from core.dynamodb import get_table, run_in_thread

    table = get_table("containers")
    owner_ids: set[str] = set()
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            owner_id = item.get("owner_id")
            if owner_id:
                owner_ids.add(owner_id)
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return sorted(owner_ids)


async def _resolve_seeded_agent_id_list(owner_id: str, repo) -> list[str]:
    """ORDERED seeded ids (Friday first when both minted — provision_org
    appends Friday, then Atlas; DynamoDB lists preserve order). Mirrors
    ``paperclip_openclaw_pairing._resolve_seeded_agent_ids`` but keeps the
    order that carries seed identity, and stays fail-CLOSED: raises on
    lookup failure so the caller skips the owner instead of degrading to
    role/name matching."""
    from core.auth import is_org_owner_id

    if is_org_owner_id(owner_id):
        for row in await repo.list_by_org_id(owner_id):
            ids = row.get("seeded_agent_ids")
            if ids:
                return list(ids)
        return []
    row = await repo.get(owner_id)
    return list(row.seeded_agent_ids) if row and row.seeded_agent_ids else []


async def process_company(
    admin,
    cookie: str,
    owner_id: str,
    company_id: str,
    *,
    apply: bool,
    seeded_agent_ids: list[str] | None = None,
) -> int:
    """Compute + (optionally) apply patches for one company. Returns patch count."""
    resp = await admin.list_agents(company_id=company_id, session_cookie=cookie)
    # The route returns a JSON array; the client annotation says dict — accept both.
    agents = resp if isinstance(resp, list) else (resp or {}).get("agents", [])

    patches = compute_agent_patches(agents, seeded_agent_ids)
    if not patches and seeded_agent_ids:
        present = sum(1 for a in agents if a.get("id") in set(seeded_agent_ids))
        if present < len(seeded_agent_ids):
            print(
                f"  WARN {owner_id} ({company_id}): {len(seeded_agent_ids) - present} seeded "
                "agent(s) missing from roster — needs manual attention"
            )
    if not patches and not seeded_agent_ids and not any(a.get("role") == "ceo" for a in agents):
        print(f"  WARN {owner_id} ({company_id}): no ceo-role agent — nothing identifiable to patch")
    for agent_id, body in patches.items():
        name = next((a.get("name") for a in agents if a.get("id") == agent_id), "?")
        print(f"  {'PATCH' if apply else 'would-patch'} {owner_id} {name} ({agent_id}): {sorted(body)}")
        if apply:
            await admin.patch_agent(agent_id=agent_id, body=body, session_cookie=cookie)
    return len(patches)


async def main(*, apply: bool, env: str) -> int:
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo = PaperclipRepo(table_name="paperclip-companies")
    owner_ids = await _scan_owner_ids()

    companies: list[tuple[str, str]] = []
    for owner_id in owner_ids:
        company_id = await repo.get_org_company_id(owner_id)
        if company_id:
            companies.append((owner_id, company_id))

    print(f"[{env}] Found {len(companies)} owner(s) with a Paperclip company")

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    admin = PaperclipAdminClient(http_client=http)
    total = 0
    try:
        cookie = await get_admin_session_cookie(http)
        for owner_id, company_id in companies:
            try:
                # Ordered, rename/role-proof seed identity; empty list for
                # rows that predate the marker → compute falls back to
                # role/name matching. Raises on lookup failure → ERR + skip.
                seeded = await _resolve_seeded_agent_id_list(owner_id, repo)
                total += await process_company(
                    admin, cookie, owner_id, company_id, apply=apply, seeded_agent_ids=seeded
                )
            except Exception as e:  # noqa: BLE001 — keep sweeping on per-company failures
                print(f"  ERR  {owner_id} ({company_id}): {e}")
    finally:
        await http.aclose()

    print(f"{'Applied' if apply else 'Would apply'} {total} agent patch(es)")
    if not apply:
        print("DRY RUN — pass --apply to patch")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill title/capabilities/reportsTo for seeded Friday/Atlas rows. Defaults to dry-run.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually PATCH agents (default: dry-run).")
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "prod"],
        help=(
            "Target environment. Must match ENVIRONMENT when that is set; "
            "exported as ENVIRONMENT otherwise (get_admin_session_cookie and "
            "settings.PAPERCLIP_INTERNAL_URL resolve per-env from it)."
        ),
    )
    args = parser.parse_args()

    # The admin-session helper picks its Secrets Manager path from
    # ENVIRONMENT, and settings resolve the Paperclip URL from it — an
    # unset/conflicting value would sign in against the wrong stage (or
    # `isol8//...`). Set it from --env BEFORE any core import runs, and
    # refuse a mismatch rather than silently preferring either value.
    import os

    current_env = os.environ.get("ENVIRONMENT")
    if current_env and current_env != args.env:
        print(f"ENVIRONMENT={current_env} conflicts with --env {args.env}; refusing to run.")
        sys.exit(1)
    os.environ["ENVIRONMENT"] = args.env

    sys.exit(asyncio.run(main(apply=args.apply, env=args.env)))
