"""Agent purchases: policy check, the ledger, the Needs-you card, the Link spend request.

An agent can only ASK. Every amount reaches Link through ``_policy`` (structured limits, no
free-text judging) and then Link's own customer approval; nothing here lets a caller approve,
change a limit, or read a token or credential.

Ledger: one row per request, ``PURCHASE#<id>`` under the owner's partition of the messaging
table -- the same place ``website_logins`` keeps its ``LOGINREQ#`` asks, with no TTL because the
monthly cap sums these rows. The id is a hash of the intent (agent, issue, merchant, amount,
reason), so a retry lands on the same row and the same Link idempotency key.

Approval: the card carries the RECORD of the ask. The "Approve in Link" URL is minted per click
(``approval_link``) and never stored on the card or the row.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import billing_repo, messaging_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import link_wallet, website_logins
from core.services.board_cards import file_board_card
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

CARD_MARKER = "purchase_request"
SK_PREFIX = "PURCHASE#"
# Statuses whose amount counts against the project's monthly cap: Link holds or has spent it.
COUNTED = ("pending_approval", "approved", "submitted", "succeeded")
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
        # Reconcile locally, no Link call: a request still pending past the approval window has
        # expired there, and must stop counting against the project's cap.
        if r.get("status") == "pending_approval" and now - float(r.get("linkedAt") or 0) > APPROVAL_WINDOW_S:
            r["status"] = "expired"
            try:
                await _set(owner_id, r["requestId"], status="expired")
            except Exception:  # noqa: BLE001 -- the in-memory value already excludes it; the write retries next read
                logger.warning("purchases: could not persist expiry owner_id=%s", owner_id)
    return sorted(rows, key=lambda r: r.get("createdAt", ""), reverse=True)


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


async def _issue_project(owner_id: str, issue_id: str) -> tuple[str, str | None]:
    company_id = await resolve_company_id_for_owner(
        owner_id=owner_id, repo=PaperclipRepo(table_name="paperclip-companies")
    )
    if not company_id or not issue_id:
        raise PurchaseRefused("issue_required", "Purchases must be tied to one of your issues.")
    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    try:
        issue = await admin.get_issue(session_cookie=cookie, issue_id=issue_id)
    except Exception:  # noqa: BLE001 -- an unreadable issue is an unknown issue
        raise PurchaseRefused("issue_required", "Purchases must be tied to one of your issues.") from None
    if not isinstance(issue, dict) or issue.get("companyId", company_id) != company_id:
        raise PurchaseRefused("issue_required", "Purchases must be tied to one of your issues.")
    project = issue.get("project") if isinstance(issue.get("project"), dict) else {}
    return str(issue.get("projectId") or "none"), project.get("name")


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
    owner_id: str, *, agent_id: str, merchant: str, url: str, amount: object, why: str, issue_id: str
) -> dict:
    """Returns ``{requestId, status}`` only. Raises ``PurchaseRefused`` /
    ``link_wallet.LinkWalletError`` with a code the route maps to the wire."""
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
    project_id, project_name = await _issue_project(owner_id, issue_id)
    limits = await billing_repo.get_purchase_limits(owner_id)
    rows = await list_rows(owner_id)
    _policy(limits, cents, project_id, site, rows)

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
        # money_moves is the existing "owner approval required?" switch (default True). Turning it
        # off lets an allowlisted merchant go straight to Link's own customer approval.
        if _allowlisted(limits, site) and not (await billing_repo.get_capability_policy(owner_id))["money_moves"]:
            row["status"] = (await _create_link_request(owner_id, row)).get("status") or "pending_approval"
        card = await file_board_card(
            owner_id,
            marker=CARD_MARKER,
            build_payload=lambda company: {
                "title": f"Approve a ${cents / 100:.2f} purchase at {merchant} for {company}",
                "request_id": request_id,
                "merchant": merchant,
                "merchant_url": merchant_url,
                "amount_display": f"${cents / 100:.2f}",
                "reason": why,
                "project": project_name or project_id,
            },
            issue_id=issue_id,
            dedupe={"request_id": request_id},
        )
        if card is None:
            raise PurchaseRefused("board_unavailable", "The owner's board isn't ready yet. Try again later.")
    except Exception:
        # Not filed, so not an ask: drop the row and let a retry (same id, same Link idempotency
        # key) start clean instead of reading back a dead one.
        await run_in_thread(messaging_repo._table().delete_item, Key={"pk": row["pk"], "sk": row["sk"]})
        raise
    return {"requestId": request_id, "status": row["status"]}


# ---------------------------------------------------------------------------
# the owner's click
# ---------------------------------------------------------------------------


async def approval_link(owner_id: str, request_id: str) -> dict:
    """Mint the "Approve in Link" URL for this click (owner route). Creates the Link spend request
    first if the ask has not reached Link, re-running the policy: the caps may have moved since."""
    row = await get(owner_id, request_id)
    if row is None:
        raise PurchaseRefused("not_found", "No such purchase request.")
    if row["status"] in ("failed", "denied", "expired", "canceled"):
        return {"status": row["status"], "approvalUrl": None}
    if not row.get("linkId"):
        limits = await billing_repo.get_purchase_limits(owner_id)
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
