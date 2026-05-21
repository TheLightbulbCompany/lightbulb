"""DynamoDB repository for paperclip-companies table.

PK: user_id (S)
Maps Isol8 user -> Paperclip Better Auth account + the org-shared
Paperclip company. Each row is per-USER (not per-org); members of the
same Isol8 org share the same ``company_id`` value across multiple
rows.

Async repository following the same pattern as peer repos in
``core/repositories/`` (api_key_repo, channel_link_repo, etc.): every
DynamoDB call is wrapped in ``run_in_thread`` and exposed as ``async def``
so future async callers (paperclip_provisioning, cleanup cron) can
``await`` directly without ``asyncio.to_thread`` at each call site.

**Multi-member org auth model.**

Paperclip's REST API has no per-user board-API-key minting endpoint
(it's only mintable via the CLI auth challenge flow). The right design
is therefore:

* One Paperclip company per Isol8 *org* (Clerk org). All Isol8 users
  belonging to the same org share the same ``company_id`` value.
* One Better Auth account per Isol8 *user*, identified by
  ``paperclip_user_id`` and authenticated with a per-user random
  password (Fernet-encrypted at rest as ``paperclip_password_encrypted``).
* Members are added to the shared company via Paperclip's
  server-side invite flow:
  ``signUp → admin createInvite → user acceptInvite → admin approveJoinRequest``.
* The OpenClaw service token (``service_token_encrypted``) remains
  per-user — it's how the seeded Paperclip agent reaches the user's
  own OpenClaw container.

To efficiently find an existing company_id when a new member joins,
``get_org_company_id`` queries the ``by-org-id`` GSI (PK: org_id).
For v1 we accept that the GSI projection is small (just company_id +
keys) since member-join is rare relative to read-by-user lookups.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread

logger = logging.getLogger(__name__)


PaperclipCompanyStatus = Literal["provisioning", "active", "failed", "disabled"]


@dataclass
class PaperclipCompany:
    """Per-USER row. ``company_id`` is shared across all rows belonging
    to the same Isol8 org; the other fields are per-user.
    """

    user_id: str
    """Isol8 user ID (Clerk user)."""

    org_id: str
    """Isol8 org ID (Clerk org). Identical for all rows in the same org."""

    company_id: str
    """Paperclip company ID. Same value for every Isol8 user in the
    same org — the Paperclip company is org-scoped, not user-scoped."""

    paperclip_user_id: str
    """Better Auth user ID inside Paperclip. Created via
    ``POST /api/auth/sign-up/email`` at provisioning time."""

    paperclip_password_encrypted: str
    """Fernet-encrypted random password for the Better Auth account.
    Used by the proxy router to sign the user in (server-to-server)
    and forward the Set-Cookie back to the browser."""

    service_token_encrypted: str
    """Fernet-encrypted long-lived OpenClaw service-token JWT. Baked
    into the seeded Paperclip agent's openclaw-gateway adapter so the
    agent can reach the user's own OpenClaw container via the
    existing WS gateway."""

    status: PaperclipCompanyStatus
    created_at: datetime
    updated_at: datetime
    last_error: Optional[str] = None
    scheduled_purge_at: Optional[datetime] = None


class PaperclipRepo:
    """Async repository for the paperclip-companies DynamoDB table.

    The table name is injected so the same repo class is usable for the
    real per-environment table (e.g. ``isol8-dev-paperclip-companies``)
    and for moto-backed unit tests. Internally we resolve the boto3
    Table via ``core.dynamodb.get_table`` so prefix-patching in tests
    (and the LocalStack endpoint override) Just Work.
    """

    def __init__(self, table_name: str, region: str | None = None):  # noqa: ARG002 - region kept for back-compat
        # ``region`` is accepted for back-compat with existing call sites
        # but ignored: the shared boto3 resource in core.dynamodb already
        # handles region + endpoint configuration.
        self._table_name = table_name

    def _table(self):
        return get_table(self._table_name)

    async def put(self, company: PaperclipCompany) -> None:
        item = {
            "user_id": company.user_id,
            "org_id": company.org_id,
            "company_id": company.company_id,
            "paperclip_user_id": company.paperclip_user_id,
            "paperclip_password_encrypted": company.paperclip_password_encrypted,
            "service_token_encrypted": company.service_token_encrypted,
            "status": company.status,
            "created_at": company.created_at.isoformat(),
            "updated_at": company.updated_at.isoformat(),
        }
        if company.last_error is not None:
            item["last_error"] = company.last_error
        if company.scheduled_purge_at is not None:
            item["scheduled_purge_at"] = company.scheduled_purge_at.isoformat()
        await run_in_thread(self._table().put_item, Item=item)

    async def get(self, user_id: str) -> Optional[PaperclipCompany]:
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": user_id},
        )
        if "Item" not in resp:
            return None
        item = resp["Item"]
        return PaperclipCompany(
            user_id=item["user_id"],
            org_id=item["org_id"],
            company_id=item["company_id"],
            paperclip_user_id=item["paperclip_user_id"],
            paperclip_password_encrypted=item["paperclip_password_encrypted"],
            service_token_encrypted=item["service_token_encrypted"],
            status=item["status"],
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
            last_error=item.get("last_error"),
            scheduled_purge_at=(
                datetime.fromisoformat(item["scheduled_purge_at"]) if item.get("scheduled_purge_at") else None
            ),
        )

    async def get_org_company_id(self, org_id: str) -> Optional[str]:
        """Return the shared ``company_id`` for any one user in this org.

        Used at member-join time (``organizationMembership.created``):
        if any other user in the org already has a row, we reuse their
        company_id rather than creating a new Paperclip company.

        Implementation: queries the ``by-org-id`` GSI (PK: ``org_id``).
        Returns the first row's ``company_id``. All rows in the same
        org are required (by the provisioner) to carry the same
        ``company_id``, so picking any one is correct.

        Why a GSI rather than a scan: while v1 traffic is small enough
        a scan would work, member-join can fire repeatedly during
        bulk org imports and a GSI lookup is O(matches) instead of
        O(table). The GSI is declared in
        ``database-stack.ts`` alongside the existing ``by-status-purge-at``
        index, with ``ProjectionType: KEYS_ONLY`` (so reads only need
        ``user_id`` to chain into a base-table ``get`` if more fields
        are needed) — but for ``get_org_company_id`` we project
        ``company_id`` via ``ALL`` so a single GSI query suffices.
        """
        kwargs = {
            "IndexName": "by-org-id",
            "KeyConditionExpression": Key("org_id").eq(org_id),
            "Limit": 1,
        }
        resp = await run_in_thread(self._table().query, **kwargs)
        items = resp.get("Items", [])
        if not items:
            return None
        first = items[0]
        # If the GSI projection only includes keys, fall back to a base
        # get to retrieve company_id. With ProjectionType=ALL this
        # branch is a no-op.
        if "company_id" in first:
            return first["company_id"]
        base = await self.get(first["user_id"])
        return base.company_id if base is not None else None

    async def list_by_org_id(self, org_id: str) -> list[dict]:
        """Return all rows in ``org_id`` via the ``by-org-id`` GSI.

        Returns raw DDB items (dicts) rather than ``PaperclipCompany``
        instances because callers (notably the Teams BFF members
        endpoint) typically only need a small subset of fields
        (``user_id``, ``paperclip_user_id``) to build a Paperclip→Clerk
        principal map. The GSI is declared with ``ProjectionType=ALL``
        in ``database-stack.ts`` so every field is available without a
        base-table chain read.

        Paginates ``LastEvaluatedKey`` to handle orgs larger than a
        single Query page; for typical Isol8 org sizes (1-10 users)
        one page suffices.
        """
        items: list[dict] = []
        last_evaluated_key: dict | None = None
        while True:
            kwargs: dict = {
                "IndexName": "by-org-id",
                "KeyConditionExpression": Key("org_id").eq(org_id),
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = await run_in_thread(self._table().query, **kwargs)
            items.extend(resp.get("Items", []))
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                return items

    async def count_org_members(self, org_id: str) -> int:
        """Count how many rows currently exist for ``org_id`` via the
        ``by-org-id`` GSI.

        Used by ``PaperclipProvisioning.purge`` to decide whether to
        archive the underlying Paperclip company: callers delete the
        target user's row FIRST, then call this — a result of 0 means
        the purged user was the last member and the company can be
        archived; anything else means co-tenants still need it.

        Implementation: GSI Query with ``Select="COUNT"`` so DynamoDB
        returns just the count without materializing items. We
        paginate ``LastEvaluatedKey`` because Query's per-page count
        cap (~1MB or 1000 items) would otherwise undercount large orgs.
        For typical Isol8 org sizes (1-10 users) one page suffices.
        """
        count = 0
        last_evaluated_key: dict | None = None
        while True:
            kwargs: dict = {
                "IndexName": "by-org-id",
                "KeyConditionExpression": Key("org_id").eq(org_id),
                "Select": "COUNT",
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = await run_in_thread(self._table().query, **kwargs)
            count += int(resp.get("Count", 0))
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                return count

    async def update_status(
        self,
        user_id: str,
        *,
        status: PaperclipCompanyStatus,
        last_error: Optional[str] = None,
        scheduled_purge_at: Optional[datetime] = None,
    ) -> None:
        update_expr = "SET #s = :s, updated_at = :u"
        expr_names = {"#s": "status"}
        expr_values: dict = {
            ":s": status,
            ":u": datetime.now(timezone.utc).isoformat(),
        }
        if last_error is not None:
            update_expr += ", last_error = :e"
            expr_values[":e"] = last_error
        if scheduled_purge_at is not None:
            update_expr += ", scheduled_purge_at = :p"
            expr_values[":p"] = scheduled_purge_at.isoformat()
        await run_in_thread(
            self._table().update_item,
            Key={"user_id": user_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )

    async def delete(self, user_id: str) -> None:
        await run_in_thread(
            self._table().delete_item,
            Key={"user_id": user_id},
        )

    async def scan_purge_due(self, now: datetime) -> list[PaperclipCompany]:
        """Return disabled rows whose ``scheduled_purge_at <= now`` via the GSI.

        Paginates through ``LastEvaluatedKey`` so larger result sets past
        the DynamoDB single-page Query limit (~1MB) are not silently
        dropped (mirrors the pagination pattern in
        ``api_key_repo.delete_all_for_owner``).
        """
        out: list[PaperclipCompany] = []
        last_evaluated_key: dict | None = None
        while True:
            kwargs: dict = {
                "IndexName": "by-status-purge-at",
                "KeyConditionExpression": (
                    Key("status").eq("disabled") & Key("scheduled_purge_at").lte(now.isoformat())
                ),
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = await run_in_thread(self._table().query, **kwargs)
            for item in resp.get("Items", []):
                company = await self.get(item["user_id"])
                if company is not None:
                    out.append(company)
                else:
                    logger.warning(
                        "paperclip_repo.scan_purge_due: GSI returned %s but base item missing",
                        item.get("user_id"),
                    )
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                return out
