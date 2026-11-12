"""Agent purchases: policy check, the ledger, the Link spend request.

An agent can only ASK. Every amount reaches Link through ``_policy`` (structured limits, no
free-text judging) and then Link's own customer approval; nothing here lets a caller approve,
change a limit, or read a token or credential.

Ledger: one row per request, ``PURCHASE#<id>`` under the owner's partition of the messaging
table -- the same place ``website_logins`` keeps its ``LOGINREQ#`` asks, with no TTL because the
monthly cap sums these rows. The id is a hash of the intent (agent, issue, merchant, amount,
reason), so a retry lands on the same row and the same Link idempotency key.

Approval: the person's yes is the Needs-you Question. The effects route asks one for a purchase
unless "ask before moving money" is off and the merchant is allowlisted (``needs_person``), and the
door (``effect_doors.require_intent``) lets the ask through only on that task's intent row. Then
Link's own customer approval. The "Approve in Link" URL is minted per click (``approval_link``)
and never stored on the row.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread, utc_now_iso
from core import tasks_schema as ts
from core.repositories import billing_repo, messaging_repo, tasks_repo
from core.services import link_wallet, owner_lifecycle, website_logins

logger = logging.getLogger(__name__)

SK_PREFIX = "PURCHASE#"
# Statuses whose amount counts against the project's monthly cap: Link holds or has spent it.
COUNTED = ("pending_approval", "approved", "submitted", "succeeded")
# Link-confirmed ends: the only statuses that release a pending row from the cap.
LINK_DEAD = ("denied", "expired", "canceled", "failed")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
# Link refuses a spend request above $500 (stripe/link-cli README "Limits": "Max amount per spend
# request: $500 (50,000 cents)"; the same figure is user-info's per_transaction limit). Refuse up
# front, and clamp the owner's per-purchase cap to it, instead of sending Link a doomed request.
MAX_AMOUNT_CENTS = 50_000
# README "Limits": "Approval window: 30 minutes". A pending row older than this has expired at Link.
APPROVAL_WINDOW_S = 30 * 60


class PurchaseRefused(Exception):
    """The ask is refused. ``code`` is machine-readable; the message is safe to show an agent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sk(request_id: str) -> str:
    return f"{SK_PREFIX}{request_id}"


def _month(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), tz=timezone.utc).strftime("%Y-%m")


def _text(value: object, limit: int) -> str:
    out = _UNSAFE.sub(" ", str(value or "")).strip()
    return out[:limit]


def to_cents(amount: object) -> int:
    try:
        cents = int((Decimal(str(amount)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        raise PurchaseRefused("bad_amount", "amount must be a number of dollars.") from None
    if not 0 < cents <= MAX_AMOUNT_CENTS:
        raise PurchaseRefused("bad_amount", "amount must be greater than zero and at most $500.")
    return cents


def request_id_for(owner_id: str, agent_id: str, issue_id: str, site: str, cents: int, why: str) -> str:
    return hashlib.sha256("\x1f".join((owner_id, agent_id, issue_id, site, str(cents), why)).encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


async def get(owner_id: str, request_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", request_id or ""):
        return None
    return await messaging_repo._get(messaging_repo.owner_pk(owner_id), sk(request_id))


async def list_rows(owner_id: str) -> list[dict]:
    rows = await messaging_repo._query(messaging_repo.owner_pk(owner_id), begins_with=SK_PREFIX)
    now = time.time()
    for r in rows:
        # Past Link's approval window a pending row may have expired, or been approved by an owner
        # who never came back. Only Link knows: ask it, and release the row only on a Link-confirmed
        # end. If Link cannot be reached the row keeps counting (fail closed for the cap).
        if r.get("status") == "pending_approval" and now - float(r.get("linkedAt") or 0) > APPROVAL_WINDOW_S:
            try:
                link = await link_wallet.retrieve_spend_request(owner_id, r["linkId"])
            except Exception:  # noqa: BLE001 -- unknown stays counted
                logger.warning("purchases: could not reconcile with Link owner_id=%s", owner_id)
                continue
            status = link.get("status")
            if status and status != "pending_approval":
                r["status"] = status
                try:
                    await _set(owner_id, r["requestId"], status=status)
                except Exception:  # noqa: BLE001 -- the in-memory value is right; the write retries next read
                    logger.warning("purchases: could not persist status owner_id=%s", owner_id)
    return sorted(rows, key=lambda r: r.get("createdAt", ""), reverse=True)


@asynccontextmanager
async def _serialized(owner_id: str):
    """The cap check and the Link request it admits are one step: two concurrent asks must not
    both pass the same headroom. The owner-lifecycle lease (a DynamoDB conditional write, shared by
    every backend task) is held across both; no new table."""
    try:
        async with owner_lifecycle.owner_lifecycle_guard(owner_id):
            yield
    except owner_lifecycle.OwnerLifecycleBusyError:
        raise PurchaseRefused("busy", "Another purchase is being checked. Try again in a moment.") from None


async def _set(owner_id: str, request_id: str, **fields) -> None:
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    await run_in_thread(
        messaging_repo._table().update_item,
        Key={"pk": messaging_repo.owner_pk(owner_id), "sk": sk(request_id)},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def spent_cents(rows: list[dict], project_id: str, *, exclude: str = "") -> int:
    month = _month()
    return sum(
        int(r["amountCents"])
        for r in rows
        if r.get("month") == month
        and r.get("projectId") == project_id
        and r.get("status") in COUNTED
        and r["requestId"] != exclude
    )


async def summary(owner_id: str) -> dict:
    """What the Budgets panel shows: limits, this month's purchase spend per project, recent asks.
    Separate from inference spend by construction -- it reads only this ledger."""
    rows = await list_rows(owner_id)
    month = _month()
    per_project: dict[str, dict] = {}
    for r in rows:
        if r.get("month") == month and r.get("status") in COUNTED:
            p = per_project.setdefault(
                r["projectId"], {"projectId": r["projectId"], "name": r.get("projectName"), "spentCents": 0}
            )
            p["spentCents"] += int(r["amountCents"])
    recent = [
        {k: r.get(k) for k in ("requestId", "merchant", "amountCents", "status", "projectName", "createdAt")}
        for r in rows[:20]
    ]
    return {
        "limits": await billing_repo.get_purchase_limits(owner_id),
        "projects": list(per_project.values()),
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def _allowlisted(limits: dict, site: str) -> bool:
    return any(website_logins.site_of(m) == site for m in limits["merchants"])


def _policy(limits: dict, cents: int, project_id: str, site: str, rows: list[dict], *, exclude: str = "") -> None:
    """Hard refusals only. Fail-closed: an unset limit refuses rather than allows."""
    per, monthly = min(limits["perPurchaseCents"], MAX_AMOUNT_CENTS), limits["projectMonthlyCents"]
    if per <= 0 or monthly <= 0:
        raise PurchaseRefused("no_limits", "Purchases are off until an admin sets purchase limits in Budgets.")
    if cents > per:
        raise PurchaseRefused("over_purchase_cap", "That is over the per-purchase limit.")
    if spent_cents(rows, project_id, exclude=exclude) + cents > monthly:
        raise PurchaseRefused("over_project_cap", "That would go over this project's monthly purchase limit.")


async def needs_person(owner_id: str, url: str) -> bool:
    """A person's yes (an answered Question) must come first: "ask before moving money" (``money_moves``,
    default on) is on, or the merchant is not on the owner's allowlist."""
    if (await billing_repo.get_capability_policy(owner_id))["money_moves"]:
        return True
    try:
        site = website_logins.site_of(website_logins.normalize_origin(url))
    except website_logins.LoginError:
        return True
    return not _allowlisted(await billing_repo.get_purchase_limits(owner_id), site)


async def _task_project(owner_id: str, task_id: str) -> tuple[str, str | None]:
    """``issueId`` on the wire is the task id: it must be a task of this workspace. Its project, else "none"."""
    task = await tasks_repo.get_task(owner_id, task_id) if task_id else None
    if task is None:
        raise PurchaseRefused("issue_required", "Purchases must be tied to one of your tasks.")
    project_id = task.get("projectId")
    if not project_id:
        return "none", None
    project = await tasks_repo._get(owner_id, ts.project_sk(project_id))
    return project_id, (project or {}).get("name")


# ---------------------------------------------------------------------------
# the agent's ask
# ---------------------------------------------------------------------------


def _context(merchant: str, site: str, why: str) -> str:
    # Link shows this to the customer and requires >= 100 characters.
    return (
        f"A Lightbulb agent asks to buy from {merchant} ({site}). "
        f"Reason given by the agent: {why} Approve only if you recognise this purchase."
    )


async def _create_link_request(owner_id: str, row: dict) -> dict:
    created = await link_wallet.create_spend_request(
        owner_id,
        request_id=row["requestId"],
        amount_cents=int(row["amountCents"]),
        merchant_name=row["merchant"],
        merchant_url=row["merchantUrl"],
        context=_context(row["merchant"], row["site"], row["why"]),
        metadata={"agentId": row["agentId"], "issueId": row["issueId"], "projectId": row["projectId"]},
    )
    await _set(
        owner_id,
        row["requestId"],
        linkId=created["id"],
        linkedAt=int(time.time()),
        month=_month(),
        status=created.get("status") or "pending_approval",
    )
    return created


async def request_purchase(
    owner_id: str,
    *,
    agent_id: str,
    merchant: str,
    url: str,
    amount: object,
    why: str,
    issue_id: str,
    approved: bool,
) -> dict:
    """Returns ``{requestId, status}`` only. Raises ``PurchaseRefused`` /
    ``link_wallet.LinkWalletError`` with a code the route maps to the wire.

    ``approved``: the door's intent row came from an answered Question (not from the approval mode)."""
    merchant, why, issue_id = _text(merchant, 80), _text(why, 500), _text(issue_id, 128)
    if not merchant or len(why) < 10:
        raise PurchaseRefused("bad_request", "merchant and a reason are required.")
    try:
        merchant_url = website_logins.normalize_origin(url)
    except website_logins.LoginError:
        raise PurchaseRefused("bad_url", "url must be the merchant's web address.") from None
    site = website_logins.site_of(merchant_url)
    cents = to_cents(amount)
    request_id = request_id_for(owner_id, agent_id, issue_id, site, cents, why)

    if existing := await get(owner_id, request_id):  # a retry: the same ask, not a second one
        return {"requestId": request_id, "status": existing["status"]}
    if not await link_wallet.connection(owner_id):
        raise PurchaseRefused("wallet_disconnected", "The Link wallet isn't connected. Ask an admin to connect it.")
    project_id, project_name = await _task_project(owner_id, issue_id)
    if not approved and await needs_person(owner_id, merchant_url):
        raise PurchaseRefused("needs_approval", "This purchase needs a person's yes first: ask through the task.")
    limits = await billing_repo.get_purchase_limits(owner_id)

    row = {
        "pk": messaging_repo.owner_pk(owner_id),
        "sk": sk(request_id),
        "requestId": request_id,
        "agentId": agent_id,
        "issueId": issue_id,
        "projectId": project_id,
        "projectName": project_name,
        "merchant": merchant,
        "merchantUrl": merchant_url,
        "site": site,
        "amountCents": cents,
        "why": why,
        "status": "needs_owner",
        "month": _month(),
        "createdAt": utc_now_iso(),
    }
    async with _serialized(owner_id):
        _policy(limits, cents, project_id, site, await list_rows(owner_id))
        try:
            await run_in_thread(
                messaging_repo._table().put_item,
                Item={k: v for k, v in row.items() if v is not None},
                ConditionExpression="attribute_not_exists(sk)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            return {"requestId": request_id, "status": (await get(owner_id, request_id) or row)["status"]}
        try:
            created = await _create_link_request(owner_id, row)
        except Exception:
            await run_in_thread(messaging_repo._table().delete_item, Key={"pk": row["pk"], "sk": row["sk"]})
            raise
    return {"requestId": request_id, "status": created.get("status") or "pending_approval"}


# ---------------------------------------------------------------------------
# the owner's click
# ---------------------------------------------------------------------------


async def approval_link(owner_id: str, request_id: str) -> dict:
    """Mint the "Approve in Link" URL for this click (owner route). Creates the Link spend request
    first if the ask has not reached Link (a ``needs_owner`` row from the card era), re-running the
    policy: the caps may have moved since."""
    row = await get(owner_id, request_id)
    if row is None:
        raise PurchaseRefused("not_found", "No such purchase request.")
    if row["status"] in ("failed", "denied", "expired", "canceled"):
        return {"status": row["status"], "approvalUrl": None}
    if not row.get("linkId"):
        limits = await billing_repo.get_purchase_limits(owner_id)
        async with _serialized(owner_id):
            row = await get(owner_id, request_id) or row  # a concurrent click may have created it
            if row.get("linkId"):
                link = await link_wallet.retrieve_spend_request(owner_id, row["linkId"])
            elif row["status"] == "canceled":
                return {"status": "canceled", "approvalUrl": None}
            else:
                _policy(
                    limits,
                    int(row["amountCents"]),
                    row["projectId"],
                    row["site"],
                    await list_rows(owner_id),
                    exclude=request_id,
                )
                link = await _create_link_request(owner_id, row)
    else:
        link = await link_wallet.retrieve_spend_request(owner_id, row["linkId"])
        if link.get("status") and link["status"] != row["status"]:
            await _set(owner_id, request_id, status=link["status"])
    status = link.get("status") or row["status"]
    return {"status": status, "approvalUrl": link.get("approval_url") if status == "pending_approval" else None}
