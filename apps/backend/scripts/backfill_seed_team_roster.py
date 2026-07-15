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
and a fully-populated company produces no PATCH at all. Companies whose
Atlas was renamed (name is the only handle — role "general" is shared with
marketplace hires) are skipped with a warning.

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


def compute_agent_patches(agents: list[dict], seeded_agent_ids: set[str] | None = None) -> dict[str, dict]:
    """Return ``{agent_id: patch_body}`` for the seed team, filling only empty fields.

    Friday = the ``role == "ceo"`` agent (unique per company — default-agent
    selection depends on it). Atlas is identified by the persisted
    ``seeded_agent_ids`` marker when available (the non-ceo seeded id —
    rename-proof, and immune to a marketplace hire the owner named "Atlas");
    companies whose row predates that marker fall back to the literal
    ``name == "Atlas"`` + ``role == "general"`` match. Pure — no I/O — so it
    carries the unit tests for the fill-only-empty contract.
    """
    from core.services.paperclip_provisioning import (
        ATLAS_CAPABILITIES,
        ATLAS_TITLE,
        FRIDAY_CAPABILITIES,
        FRIDAY_TITLE,
    )

    friday = next((a for a in agents if a.get("role") == "ceo" and a.get("id")), None)
    if seeded_agent_ids:
        # A seeded marker exists, so it is authoritative: when the non-ceo
        # seeded id is absent from the roster (deleted/disabled Atlas, partial
        # response), do NOT fall through to name matching — that could stamp a
        # user/marketplace hire that happens to be named "Atlas" with the
        # recruiter metadata.
        atlas = next(
            (a for a in agents if a.get("id") in seeded_agent_ids and a.get("role") != "ceo"),
            None,
        )
    else:
        # No marker (row predates seeded_agent_ids): literal name match is the
        # only handle available for these legacy rows.
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
        if friday and not atlas.get("reportsTo"):
            body["reportsTo"] = friday["id"]
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


async def process_company(
    admin,
    cookie: str,
    owner_id: str,
    company_id: str,
    *,
    apply: bool,
    seeded_agent_ids: set[str] | None = None,
) -> int:
    """Compute + (optionally) apply patches for one company. Returns patch count."""
    resp = await admin.list_agents(company_id=company_id, session_cookie=cookie)
    # The route returns a JSON array; the client annotation says dict — accept both.
    agents = resp if isinstance(resp, list) else (resp or {}).get("agents", [])

    patches = compute_agent_patches(agents, seeded_agent_ids)
    if not patches and not any(a.get("role") == "ceo" for a in agents):
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
    from core.services.paperclip_openclaw_pairing import get_seeded_agent_ids

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
                # Rename-proof Atlas identification; empty set for rows that
                # predate the marker → compute falls back to name matching.
                seeded = await get_seeded_agent_ids(owner_id=owner_id, repo=repo)
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
        help="Environment label (informational; doesn't change DDB targeting).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, env=args.env)))
