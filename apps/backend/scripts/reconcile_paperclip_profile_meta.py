"""Reconcile Paperclip agent profile-meta drift against openclaw.json.

The inspector's Overview edits a per-agent profile-meta block
(``role`` / ``title`` / ``capabilities`` / ``icon``) and dual-writes it
best-effort to BOTH Paperclip's Aurora row (via PATCH /api/agents/{id})
AND openclaw.json on EFS. The live path
(``routers/websocket_chat.py:_intercept_agents_update``) swallows
Paperclip PATCH failures — its own docstring anticipates the drift:
"if Paperclip's row drifts, the next manual edit or a reconciliation
job can re-sync." This is that reconciliation job.

openclaw.json (EFS) is the source of truth. Each ``agents.list[]``
entry's ``id`` IS the Paperclip agent UUID — a direct join, no mapping
layer. For each company we list its Paperclip agents, read the owner's
openclaw.json, intersect by agent id, decode the openclaw side through
``paperclip_meta.read_from_agent_entry``, and PATCH any Paperclip row
that has drifted off it.

MANUAL / operator-gated. This script is NOT wired into any scheduled
worker, cron, or provisioning path — it is run by hand by an operator.
Requirements:

  * AWS SSO (``aws sso login --profile isol8-admin``) — reads the
    ``paperclip-companies`` DDB table and the Paperclip admin
    credentials secret.
  * A reachable ``PAPERCLIP_INTERNAL_URL`` (run from a backend
    container or with backend env loaded).

ALWAYS dry-run first, eyeball the planned RECONCILE lines, then
``--apply``::

    cd apps/backend
    uv run python -m scripts.reconcile_paperclip_profile_meta --env prod          # dry-run
    uv run python -m scripts.reconcile_paperclip_profile_meta --env prod --apply  # apply

Optional ``--owner-id`` / ``--agent-id`` scope a single owner / agent.

Adaptation notes
----------------

Mirrors ``scripts.migrate_paperclip_session_strategy`` (argparse,
logging, http/cookie setup, ``_scan_company_owner_map``, the
non-list ``list_agents`` guard, the per-agent try/except continue, and
the ``finally await http.aclose()`` teardown). ``ENVIRONMENT`` is set
on ``os.environ`` early so ``get_admin_session_cookie`` reads the right
``isol8/{env}/paperclip_admin_credentials`` secret; heavy imports stay
lazy inside ``main`` so ``--help`` and unit tests don't require AWS.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any

from core.containers.config import read_openclaw_config_from_efs
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_meta import (
    PAPERCLIP_AGENT_ROLES,
    PaperclipMeta,
    read_from_agent_entry,
)

logger = logging.getLogger("reconcile_paperclip_profile_meta")

# Nullable profile-meta fields that round-trip through ``PaperclipMeta``
# and Paperclip's ``updateAgentSchema``. ``role`` is handled separately
# (enum, never wiped — always a valid value on both sides).
_NULLABLE_FIELDS = ("title", "capabilities", "icon")


def _normalize_paperclip_role(raw: Any) -> str:
    """Coerce a Paperclip row's ``role`` the same way
    ``read_from_agent_entry`` coerces openclaw's: unknown / blank /
    non-string → ``"general"``. So both sides of the comparison go
    through identical normalization.
    """
    if isinstance(raw, str) and raw in PAPERCLIP_AGENT_ROLES:
        return raw
    return "general"


def _normalize_paperclip_field(raw: Any) -> str | None:
    """Coerce a Paperclip row's nullable field the same way
    ``read_from_agent_entry`` coerces openclaw's: a non-blank string
    stays, everything else (None / missing / whitespace-only) → None.
    """
    return raw if isinstance(raw, str) and raw.strip() else None


def diff_profile_meta(
    openclaw_meta: PaperclipMeta,
    paperclip_agent: dict,
) -> dict | None:
    """Compute the minimal ``update_agent`` PATCH kwargs that bring a
    Paperclip agent row in line with openclaw.json (the source of
    truth). Returns ``None`` if Paperclip already matches.

    Both sides are normalized through the SAME coercion the live
    dual-write path uses (``paperclip_meta.read_from_agent_entry``):

      * ``role`` — blank / unknown → ``"general"`` on both sides; only
        emitted when the normalized values differ.
      * ``title`` / ``capabilities`` / ``icon`` — a non-blank string
        stays, everything else → ``None`` ("cleared"). When openclaw
        has cleared a field that Paperclip still carries, the PATCH
        sends ``""`` to WIPE it (Paperclip semantics: ``None`` = leave
        alone, ``""`` = wipe), NOT ``None``.

    Only changed fields appear. Idempotent: applying the returned diff
    to the Paperclip row and re-diffing yields ``None``.
    """
    patch: dict[str, str] = {}

    # role — openclaw is already a valid role (read_from_agent_entry
    # coerced it); normalize the Paperclip side identically.
    pc_role = _normalize_paperclip_role(paperclip_agent.get("role"))
    if pc_role != openclaw_meta.role:
        patch["role"] = openclaw_meta.role

    # nullable fields — wipe with "" when openclaw cleared them.
    openclaw_values = {
        "title": openclaw_meta.title,
        "capabilities": openclaw_meta.capabilities,
        "icon": openclaw_meta.icon,
    }
    for field in _NULLABLE_FIELDS:
        oc_value = openclaw_values[field]  # already None-or-non-blank-str
        pc_value = _normalize_paperclip_field(paperclip_agent.get(field))
        if oc_value == pc_value:
            continue
        # Drift. openclaw cleared it (None) → "" to WIPE; else set the
        # openclaw string.
        patch[field] = "" if oc_value is None else oc_value

    return patch or None


async def _scan_company_owner_map() -> dict[str, str]:
    """Paginate the ``paperclip-companies`` table and return a
    ``{company_id: owner_id}`` map.

    Copied from ``scripts.migrate_paperclip_session_strategy``. ``owner_id``
    is sourced from the ``org_id`` column (post-cutover that column
    carries the container's owner — Clerk org_id for orgs, Clerk
    user_id for personal owners; identical across every row in the same
    company per the ``PaperclipCompany.org_id`` invariant). Multiple
    users in the same org share a single ``company_id`` but all share
    the same ``org_id``, so the first row per company is canonical; we
    warn if rows disagree (would indicate upstream corruption).
    """
    from core.dynamodb import get_table, run_in_thread

    table = get_table("paperclip-companies")
    owner_by_company: dict[str, str] = {}
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            cid = item.get("company_id")
            owner = item.get("org_id")
            if not cid or not owner:
                continue
            existing = owner_by_company.get(cid)
            if existing is None:
                owner_by_company[cid] = owner
            elif existing != owner:
                logger.warning(
                    "company_id=%s has rows with disagreeing org_id (seen %s and %s); keeping first-seen — investigate",
                    cid,
                    existing,
                    owner,
                )
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return owner_by_company


async def _reconcile_company(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    company_id: str,
    owner_id: str,
    apply: bool,
    agent_id_filter: str | None,
) -> tuple[int, int]:
    """Reconcile one company. Returns ``(matched, reconciled)``.

    * ``matched`` — agents present in both Paperclip and openclaw.json
      whose Paperclip row has drifted off openclaw.json.
    * ``reconciled`` — agents actually PATCHed (always 0 on dry-run).
    """
    raw = await admin_client.list_agents(
        session_cookie=admin_cookie,
        company_id=company_id,
    )
    # ``list_agents`` is typed as ``dict`` upstream but actually returns
    # a JSON array (same handling as the migrate script).
    if not isinstance(raw, list):
        logger.warning(
            "list_agents returned non-list for company=%s (got %s) — skipping",
            company_id,
            type(raw).__name__,
        )
        return 0, 0

    paperclip_by_id = {a.get("id"): a for a in raw if isinstance(a, dict) and a.get("id")}

    config = await read_openclaw_config_from_efs(owner_id)
    if not isinstance(config, dict):
        # No openclaw.json on EFS yet (None) or unparseable — nothing to
        # reconcile against. openclaw.json is the source of truth, so
        # without it we have no opinion.
        return 0, 0

    agents_list = ((config.get("agents") or {}).get("list")) or []
    openclaw_meta_by_id: dict[str, PaperclipMeta] = {}
    for entry in agents_list:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id:
            openclaw_meta_by_id[entry_id] = read_from_agent_entry(entry)

    # Intersect by id — only agents present on BOTH sides are compared.
    matched = 0
    reconciled = 0
    for agent_id in sorted(set(paperclip_by_id) & set(openclaw_meta_by_id)):
        if agent_id_filter is not None and agent_id != agent_id_filter:
            continue
        openclaw_meta = openclaw_meta_by_id[agent_id]
        paperclip_agent = paperclip_by_id[agent_id]
        patch = diff_profile_meta(openclaw_meta, paperclip_agent)
        if patch is None:
            continue
        matched += 1
        for field, new_value in patch.items():
            old_value = paperclip_agent.get(field)
            logger.info(
                "%s %s/%s %s %r->%r",
                "RECONCILE" if apply else "DRY-RUN",
                company_id,
                agent_id,
                field,
                old_value,
                new_value,
            )
        if apply:
            try:
                await admin_client.update_agent(
                    session_cookie=admin_cookie,
                    agent_id=agent_id,
                    **patch,
                )
                reconciled += 1
            except PaperclipApiError as e:
                logger.error(
                    "FAILED %s/%s status=%s: %s",
                    company_id,
                    agent_id,
                    e.status_code,
                    e,
                )
    return matched, reconciled


async def main(
    *,
    apply: bool,
    env: str,
    owner_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Set ENVIRONMENT before the Paperclip session helper reads the
    # ``isol8/{env}/paperclip_admin_credentials`` secret.
    os.environ["ENVIRONMENT"] = env

    # Lazy imports — keep ``--help`` and unit tests free of AWS / config
    # side effects (settings constructs at import time).
    import httpx

    from core.config import settings
    from core.services.paperclip_admin_session import get_admin_session_cookie

    if not settings.PAPERCLIP_INTERNAL_URL:
        raise RuntimeError(
            "PAPERCLIP_INTERNAL_URL is not configured. Run this script "
            "from a backend container (or with backend env loaded) so the "
            "internal Paperclip URL is reachable."
        )

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)

        owner_by_company = await _scan_company_owner_map()
        if owner_id is not None:
            owner_by_company = {cid: owner for cid, owner in owner_by_company.items() if owner == owner_id}

        company_ids = sorted(owner_by_company.keys())
        total_matched = 0
        total_reconciled = 0
        for company_id in company_ids:
            matched, reconciled = await _reconcile_company(
                admin_client=admin_client,
                admin_cookie=admin_cookie,
                company_id=company_id,
                owner_id=owner_by_company[company_id],
                apply=apply,
                agent_id_filter=agent_id,
            )
            total_matched += matched
            total_reconciled += reconciled

        logger.info(
            "DONE: companies=%d drifted=%d reconciled=%d (apply=%s)",
            len(company_ids),
            total_matched,
            total_reconciled,
            apply,
        )
    finally:
        await http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reconcile Paperclip agent profile-meta drift against openclaw.json (source of truth).",
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        required=True,
        help="Environment whose Paperclip admin credentials secret to use.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually PATCH drifted agents. Default is a dry-run that only logs.",
    )
    parser.add_argument(
        "--owner-id",
        default=None,
        help="Scope to a single owner (Clerk org_id or user_id).",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="Scope to a single agent UUID.",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            apply=args.apply,
            env=args.env,
            owner_id=args.owner_id,
            agent_id=args.agent_id,
        )
    )
