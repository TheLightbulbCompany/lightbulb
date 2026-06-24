"""One-time bootstrap: create DORMANT Paperclip routines for the founders' agents.

For each of our 5 deployed agents, create the agent's recurring routines via
``agent_routine_provisioning.create_agent_routines``. Every routine's schedule
trigger is created ``enabled: False`` (dormant) — it exists and proves the
plumbing but fires zero runs and costs nothing until a human flips it on later.

The cadences below are deliberately conservative — daily / weekly / month-end
only, NEVER hourly or 4-hourly — so that even once a trigger is enabled the
cost stays bounded.

Idempotent: re-running skips routines already present for the agent (matched by
``assigneeAgentId`` + title). Dry-run by default; pass ``--apply`` to create.

Requires AWS SSO (``aws sso login --profile isol8-admin``) and a backend
environment that can reach Paperclip's internal URL — run it from the backend
container (or a context where ``settings.PAPERCLIP_INTERNAL_URL`` resolves).

    cd apps/backend && uv run python -m scripts.bootstrap_agent_routines \
        --owner-id org_xxx                       # dry run
    cd apps/backend && uv run python -m scripts.bootstrap_agent_routines \
        --owner-id org_xxx --apply
"""

from __future__ import annotations

import argparse
import asyncio

# Per-agent routine specs. Each ``description`` is a one-line instruction to run
# that agent's matching workflow(s) per its Standing Order and drain+deliver its
# ``state.*/notifications`` (the Standing Order itself ships separately — fine if
# it doesn't exist yet). Cadences are daily / weekly / month-end ONLY.
AGENT_ROUTINES: dict[str, dict[str, list[dict[str, str]]]] = {
    "pitch-scout": {
        "agent_id": "d71a38fa-fb95-4c93-961a-dd0cac1bdf26",
        "routines": [
            {
                "title": "Morning sweep",
                "cron": "0 6 * * *",
                "description": (
                    "Run pitch-scout's morning-sweep workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
            {
                "title": "Weekly report",
                "cron": "0 8 * * 1",
                "description": (
                    "Run pitch-scout's weekly-report workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
        ],
    },
    "pulse": {
        "agent_id": "6637ae95-ab05-462b-bcb1-3d4d343fc3be",
        "routines": [
            {
                "title": "Daily monitoring sweep",
                "cron": "0 7 * * *",
                "description": (
                    "Run pulse's daily monitoring-sweep workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
            {
                "title": "Weekly digest",
                "cron": "0 8 * * 1",
                "description": (
                    "Run pulse's weekly-digest workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
        ],
    },
    "lens": {
        "agent_id": "beaab243-a187-4db4-9c46-e3bb18ad7c6c",
        "routines": [
            {
                "title": "Daily monitoring sweep",
                "cron": "0 7 * * *",
                "description": (
                    "Run lens's daily monitoring-sweep workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
            {
                "title": "Weekly maintenance",
                "cron": "0 8 * * 1",
                "description": (
                    "Run lens's weekly-maintenance workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
        ],
    },
    "tally": {
        "agent_id": "9e8c7c6f-613d-4e96-ad4b-97a96efcba2f",
        "routines": [
            {
                "title": "Daily feed",
                "cron": "0 7 * * *",
                "description": (
                    "Run tally's daily-feed workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
            {
                "title": "Weekly summary",
                "cron": "0 8 * * 1",
                "description": (
                    "Run tally's weekly-summary workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
            {
                "title": "Month-end close",
                "cron": "0 9 1 * *",
                "description": (
                    "Run tally's month-end-close workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
        ],
    },
    "vera": {
        "agent_id": "f9fc524c-acef-4467-8387-91ac7cbd00ef",
        "routines": [
            {
                "title": "Escalation health check",
                "cron": "0 9 * * *",
                "description": (
                    "Run vera's escalation-health-check workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
            {
                "title": "Weekly report",
                "cron": "0 8 * * 1",
                "description": (
                    "Run vera's weekly-report workflow per its Standing Order, then "
                    "drain and deliver state.*/notifications."
                ),
            },
        ],
    },
}


async def _run(*, owner_id: str, apply: bool) -> None:
    import httpx

    from core.config import settings
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.agent_routine_provisioning import (
        build_dormant_trigger_body,
        create_agent_routines,
    )
    from core.services.paperclip_admin_client import PaperclipAdminClient
    from core.services.paperclip_admin_session import get_admin_session_cookie
    from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=30.0)
    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
        if company_id is None:
            raise SystemExit(
                f"No Paperclip company for owner {owner_id!r}; provision it first "
                "(visit /chat or run the legacy backfill)."
            )

        print(f"Owner    : {owner_id}")
        print(f"Company  : {company_id}")
        print(f"Mode     : {'APPLY' if apply else 'DRY RUN'}")
        print("Dormant trigger body (example, enabled is always False):")
        print(f"  {build_dormant_trigger_body('0 6 * * *')}")
        print()

        if not apply:
            for agent_key, spec in AGENT_ROUTINES.items():
                print(f"[{agent_key}] agent_id={spec['agent_id']}")
                for r in spec["routines"]:
                    print(f"  would create DORMANT routine: title={r['title']!r} cron={r['cron']!r}")
            print("\nDRY RUN — re-run with --apply to create.")
            return

        admin = PaperclipAdminClient(http_client=http)
        session_cookie = await get_admin_session_cookie(http)

        total = 0
        for agent_key, spec in AGENT_ROUTINES.items():
            created = await create_agent_routines(
                admin=admin,
                session_cookie=session_cookie,
                company_id=company_id,
                agent_id=spec["agent_id"],
                routines=spec["routines"],
            )
            total += len(created)
            print(f"[{agent_key}] agent_id={spec['agent_id']} created={len(created)} ids={created}")

        print(
            f"\nDone. Created {total} DORMANT routine(s). "
            "Every schedule trigger is enabled=False — flip on individually when ready."
        )
    finally:
        await http.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--owner-id",
        required=True,
        help="the founders' owner id (org_id for orgs, user_id for personal)",
    )
    ap.add_argument("--apply", action="store_true", help="actually create routines (default: dry run)")
    args = ap.parse_args()
    asyncio.run(_run(owner_id=args.owner_id, apply=args.apply))


if __name__ == "__main__":
    main()
