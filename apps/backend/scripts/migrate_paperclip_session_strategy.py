"""One-shot migration: switch Paperclip agents from sessionKeyStrategy
'fixed' to 'issue', and rotate org-row ``authToken``s to sub=owner_id.

Run this once per environment (dev, then prod) after deploying the
agent-pairing PR. Iterates every PaperclipCompany row in DDB,
``list_agents`` per (deduped) ``company_id``, and patches:

1. Every agent whose ``adapterConfig.sessionKeyStrategy`` is ``"fixed"``
   (flipped to ``"issue"``).
2. Every agent in an org-context company whose ``adapterConfig.authToken``
   was minted with the human creator's ``user_id`` instead of the
   container's ``owner_id`` (= org_id) — rotated to a fresh JWT minted
   with ``sub=owner_id``. The WS Lambda authorizer puts service tokens
   into the connection context with ``orgId=""`` and routes by ``sub``,
   so a member-keyed token routes Paperclip wakes to the member's
   PERSONAL container instead of the org's shared container, missing
   the agent entirely. (Codex P1 finding 4.)

The two transformations always run together: any agent whose
``sessionKeyStrategy`` is being flipped is also a candidate for
authToken rotation, so we batch them into a single ``patch_agent`` call.

Idempotent: agents that already have ``sessionKeyStrategy == "issue"``
AND a token whose ``sub`` already matches their company's ``owner_id``
are skipped. Safe to re-run.

Usage::

    cd apps/backend
    uv run python -m scripts.migrate_paperclip_session_strategy --dry-run
    uv run python -m scripts.migrate_paperclip_session_strategy --apply

Adaptation notes
----------------

The original plan template referenced ``PaperclipAdminClient.update_agent``
and ``PaperclipRepo.scan_all``; neither exists in this codebase. The
adaptations made here:

* ``update_agent`` → use the existing
  :meth:`PaperclipAdminClient.patch_agent` (``PATCH /api/agents/{agentId}``)
  with ``body={"adapterConfig": new_cfg}``.
* ``scan_all`` → there is no full-table scan helper on ``PaperclipRepo``
  (the repo is keyed by ``user_id`` and exposes ``get`` /
  ``get_org_company_id`` / ``list_by_org_id`` / ``count_org_members`` /
  ``scan_purge_due``). For a one-shot CLI we paginate the underlying
  DDB table directly via :func:`core.dynamodb.run_in_thread` +
  ``Table.scan`` — this is acceptable for the small fleet sizes the
  Paperclip companies table sees and avoids adding a method to the
  repository purely for a one-shot script.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx
import jwt as _jwt_lib

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services import service_token
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger("migrate_paperclip_session_strategy")


def _token_sub(token: str | None) -> str | None:
    """Return the ``sub`` claim of a service-token JWT without verifying
    the signature.

    The signing key may be rotated and we don't need cryptographic proof
    here — we only use the claim to decide whether the token already
    matches the canonical ``owner_id`` for its company. Verification at
    consumption time still happens in ``service_token.verify``.

    Returns ``None`` if the token is missing, malformed, or carries no
    ``sub`` — those paths fall through to the rotation branch (rotation
    is a strict superset of "leave it alone if already correct").
    """
    if not token:
        return None
    try:
        claims = _jwt_lib.decode(token, options={"verify_signature": False})
    except _jwt_lib.InvalidTokenError:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) else None


def needs_migration(agent: dict[str, Any], owner_id: str | None) -> bool:
    """True if this agent needs to be patched.

    Two trigger conditions, ORed:
    * ``sessionKeyStrategy == "fixed"`` (legacy → flip to ``"issue"``).
    * ``adapterConfig.authToken`` carries a ``sub`` claim that doesn't
      match the canonical ``owner_id`` for the agent's company (org
      rows where the token was minted with the member's user_id).

    Skips non-openclaw_gateway adapter types (no ``adapterConfig``).
    """
    cfg = agent.get("adapterConfig") or {}
    if cfg.get("sessionKeyStrategy") == "fixed":
        return True
    if owner_id is not None:
        token_sub = _token_sub(cfg.get("authToken"))
        if token_sub is not None and token_sub != owner_id:
            return True
    return False


async def migrate_one_agent(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    agent: dict[str, Any],
    owner_id: str | None,
) -> None:
    """Apply both transformations (strategy flip + authToken rotation)
    in a single ``patch_agent`` call.

    Preserves every other ``adapterConfig`` field verbatim (``url``,
    ``sessionKey``, …).
    """
    new_cfg = dict(agent["adapterConfig"])
    new_cfg["sessionKeyStrategy"] = "issue"

    # Rotate the service token if we know the canonical owner and the
    # current sub doesn't match. We always mint a fresh JWT (rather
    # than re-encrypting the old one) so the new ``iat``/``jti`` make
    # the rotation observable in audit logs.
    if owner_id is not None:
        token_sub = _token_sub(new_cfg.get("authToken"))
        if token_sub != owner_id:
            new_cfg["authToken"] = service_token.mint(owner_id)

    await admin_client.patch_agent(
        session_cookie=admin_cookie,
        agent_id=agent["id"],
        body={"adapterConfig": new_cfg, "replaceAdapterConfig": True},
    )


async def _scan_company_owner_map() -> dict[str, str]:
    """Paginate the ``paperclip-companies`` table and return a
    ``{company_id: owner_id}`` map.

    ``owner_id`` is sourced from the ``org_id`` column on the row
    (post-cutover that column carries the container's owner — Clerk
    org_id for orgs, Clerk user_id for personal owners; identical
    across every row in the same company per the
    ``PaperclipCompany.org_id`` invariant).

    Multiple users in the same org share a single ``company_id``, but
    they all share the same ``org_id`` too — so the first row we
    encounter for each company is canonical and later rows just
    confirm the value. We log a warning if rows in the same company
    disagree on ``org_id`` (would indicate a data corruption bug
    upstream).
    """
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


async def _migrate_company(
    *,
    admin_client: PaperclipAdminClient,
    admin_cookie: str,
    company_id: str,
    owner_id: str | None,
    apply: bool,
) -> tuple[int, int]:
    """Returns ``(matched, migrated)`` for the company."""
    raw = await admin_client.list_agents(
        session_cookie=admin_cookie,
        company_id=company_id,
    )
    # ``list_agents`` is typed as ``dict`` upstream but actually returns
    # a JSON array (see paperclip_openclaw_pairing for the same handling).
    if not isinstance(raw, list):
        logger.warning(
            "list_agents returned non-list for company=%s (got %s) — skipping",
            company_id,
            type(raw).__name__,
        )
        return 0, 0

    matched = 0
    migrated = 0
    for agent in raw:
        if not needs_migration(agent, owner_id):
            continue
        matched += 1
        logger.info(
            "%s %s/%s (%s) owner=%s",
            "MIGRATE" if apply else "DRY-RUN",
            company_id,
            agent.get("id"),
            agent.get("name", "?"),
            owner_id or "<unknown>",
        )
        if apply:
            try:
                await migrate_one_agent(
                    admin_client=admin_client,
                    admin_cookie=admin_cookie,
                    agent=agent,
                    owner_id=owner_id,
                )
                migrated += 1
            except PaperclipApiError as e:
                logger.error(
                    "FAILED %s/%s status=%s: %s",
                    company_id,
                    agent.get("id"),
                    e.status_code,
                    e,
                )
    return matched, migrated


async def main(apply: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

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
        company_ids = sorted(owner_by_company.keys())
        total_matched = 0
        total_migrated = 0
        for company_id in company_ids:
            matched, migrated = await _migrate_company(
                admin_client=admin_client,
                admin_cookie=admin_cookie,
                company_id=company_id,
                owner_id=owner_by_company.get(company_id),
                apply=apply,
            )
            total_matched += matched
            total_migrated += migrated

        logger.info(
            "DONE: companies=%d matched=%d migrated=%d (apply=%s)",
            len(company_ids),
            total_matched,
            total_migrated,
            apply,
        )
    finally:
        await http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Flip Paperclip agents from sessionKeyStrategy 'fixed' to 'issue'.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="List the agents that WOULD be migrated; make no changes.",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Actually patch each matching agent.",
    )
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
