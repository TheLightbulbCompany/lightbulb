"""Stripe Link Agent Wallet: OAuth, token rotation, spend requests, policy, the dark gate.

No real Link calls: every request goes through an httpx.MockTransport. Secrets Manager and the
messaging table are in-memory fakes.
"""

import asyncio
import base64
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.config import settings  # noqa: E402
from core.repositories import billing_repo, messaging_repo  # noqa: E402
from core.services import link_wallet, purchases  # noqa: E402
from main import app  # noqa: E402

OWNER = "org_a"
ADMIN = "user_admin"
CANARY_CARD = "4242424242424242"
CANARY_TOKEN = "liwltoken_CANARY"
CANARY_REFRESH = "liwlrefresh_CANARY"


class FakeTable:
    def __init__(self):
        self.items: dict[tuple, dict] = {}

    def put_item(self, Item, ConditionExpression=None):
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)

    def update_item(self, Key, ExpressionAttributeNames, ExpressionAttributeValues, **_):
        row = self.items[(Key["pk"], Key["sk"])]
        for name, field in ExpressionAttributeNames.items():
            row[field] = ExpressionAttributeValues[":" + name[1:]]

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)


class Link:
    """A fake Link: records every call, hands out rotating tokens."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.refreshes = 0
        self.spend_status = "pending_approval"
        self.scope = "payment_methods.agentic userinfo:read"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/auth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
                n = self.refreshes
            else:
                n = 0
            return httpx.Response(
                200,
                json={
                    "access_token": f"{CANARY_TOKEN}{n}",
                    "refresh_token": f"{CANARY_REFRESH}{n}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": self.scope,
                },
            )
        if path == "/auth/revoke":
            return httpx.Response(200, json={})
        if path == "/spend_requests" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "lsrq_1",
                    "status": self.spend_status,
                    "amount": 3500,
                    "approval_url": "https://app.link.com/activity/approve/lsrq_1",
                    # Adversarial: a credential Link should not send here. It must not survive.
                    "card": {"number": CANARY_CARD, "cvc": "123"},
                    "link_pay_token": "lpt_CANARY",
                },
            )
        if path.startswith("/spend_requests/"):
            return httpx.Response(200, json={"id": "lsrq_1", "status": "approved"})
        return httpx.Response(404)

    def bodies(self, path: str) -> list[dict]:
        import json

        return [json.loads(c.content) for c in self.calls if c.url.path == path and c.content]


@pytest.fixture
def world(monkeypatch):
    link = Link()
    monkeypatch.setattr(link_wallet, "_transport", httpx.MockTransport(link.handler))
    for k, v in (
        ("LINK_WALLET_ENABLED", True),
        ("LINK_CLIENT_ID", "cid"),
        ("LINK_CLIENT_SECRET", "csecret"),
        ("LINK_PUBLISHABLE_KEY", "pk_test_x"),
    ):
        monkeypatch.setattr(settings, k, v)

    secrets_store: dict = {}
    monkeypatch.setattr(
        link_wallet.integration_secret_store, "get", AsyncMock(side_effect=lambda o, p: secrets_store.get((o, p)))
    )

    async def put(o, p, cred):
        secrets_store[(o, p)] = dict(cred)

    async def delete(o, p):
        return secrets_store.pop((o, p), None) is not None

    monkeypatch.setattr(link_wallet.integration_secret_store, "put", put)
    monkeypatch.setattr(link_wallet.integration_secret_store, "delete", delete)

    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def guard(owner_id):
        async with locks.setdefault(owner_id, asyncio.Lock()):
            yield

    monkeypatch.setattr(link_wallet.owner_lifecycle, "owner_lifecycle_guard", guard)
    monkeypatch.setattr(link_wallet.owner_lifecycle, "revoked_since", AsyncMock(return_value=False))
    members = {ADMIN}
    monkeypatch.setattr(link_wallet, "_connector_is_member", AsyncMock(side_effect=lambda o, u: u in members))

    table = FakeTable()
    monkeypatch.setattr(messaging_repo, "_table", lambda: table)
    monkeypatch.setattr(messaging_repo, "_get", AsyncMock(side_effect=lambda pk, sk: table.items.get((pk, sk))))
    monkeypatch.setattr(
        messaging_repo,
        "_query",
        AsyncMock(
            side_effect=lambda pk, begins_with=None, **_: [
                v for (p, s), v in table.items.items() if p == pk and s.startswith(begins_with)
            ]
        ),
    )

    limits = {"perPurchaseCents": 10000, "projectMonthlyCents": 20000, "merchants": []}
    policy = {"money_moves": True}
    monkeypatch.setattr(billing_repo, "get_purchase_limits", AsyncMock(side_effect=lambda o: limits))
    monkeypatch.setattr(billing_repo, "get_capability_policy", AsyncMock(side_effect=lambda o: policy))
    monkeypatch.setattr(purchases, "_issue_project", AsyncMock(return_value=("proj_1", "Launch")))
    cards = AsyncMock(return_value="card_1")
    monkeypatch.setattr(purchases, "file_board_card", cards)

    class W:
        pass

    w = W()
    w.link, w.secrets, w.members, w.table, w.limits, w.policy, w.cards = (
        link,
        secrets_store,
        members,
        table,
        limits,
        policy,
        cards,
    )
    return w


async def connect(w, expires_at=None):
    rec = {"owner_id": OWNER, "user_id": ADMIN, "verifier": "v" * 64}
    assert await link_wallet.exchange_code(rec, "code123")
    if expires_at is not None:
        w.secrets[(OWNER, link_wallet.PROVIDER)]["expires_at"] = expires_at


def ask(**over):
    args = dict(
        agent_id="agent_1",
        merchant="Acme",
        url="https://shop.acme.com/x",
        amount=35,
        why="Buying the annual seat licence the team needs for the launch.",
        issue_id="iss_1",
    )
    return purchases.request_purchase(OWNER, **{**args, **over})


# ---- OAuth ---------------------------------------------------------------------------------


async def test_authorize_url_is_pkce_s256_and_verifier_stays_server_side(world, monkeypatch):
    put = AsyncMock()
    monkeypatch.setattr(link_wallet.oauth_state_repo, "put", put)
    url = await link_wallet.start_connect(OWNER, ADMIN)
    q = parse_qs(urlsplit(url).query)
    assert urlsplit(url).netloc == "login.link.com"
    assert q["scope"] == ["payment_methods.agentic userinfo:read"]
    assert q["code_challenge_method"] == ["S256"] and q["response_type"] == ["code"]
    verifier = put.await_args.kwargs["verifier"]
    assert q["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    ]
    assert q["state"] == [put.await_args.args[0]] and verifier not in url


async def test_code_exchange_stores_tokens_and_sends_pkce(world):
    await connect(world)
    call = next(c for c in world.link.calls if c.url.path == "/auth/token")
    form = parse_qs(call.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code_verifier"] == ["v" * 64]
    assert call.headers["authorization"] == "Bearer pk_test_x"
    assert world.secrets[(OWNER, "link_wallet")]["connected_by"] == ADMIN
    assert (await link_wallet.status(OWNER))["connected"] is True


async def test_a_subset_grant_without_payment_scope_is_refused_and_revoked(world):
    world.link.scope = "userinfo:read"
    rec = {"owner_id": OWNER, "user_id": ADMIN, "verifier": "v" * 64}
    assert await link_wallet.exchange_code(rec, "c") is False
    assert (OWNER, "link_wallet") not in world.secrets
    assert any(c.url.path == "/auth/revoke" for c in world.link.calls)


async def test_refresh_rotates_and_persists_the_new_refresh_token(world):
    await connect(world, expires_at=0)
    token = await link_wallet._access_token(OWNER)
    assert token == f"{CANARY_TOKEN}1"
    assert world.secrets[(OWNER, "link_wallet")]["refresh_token"] == f"{CANARY_REFRESH}1"
    sent = parse_qs([c for c in world.link.calls if c.url.path == "/auth/token"][-1].content.decode())
    assert sent["refresh_token"] == [f"{CANARY_REFRESH}0"]


async def test_concurrent_refresh_spends_the_refresh_token_once(world):
    await connect(world, expires_at=0)
    tokens = await asyncio.gather(*(link_wallet._access_token(OWNER) for _ in range(5)))
    assert world.link.refreshes == 1
    assert set(tokens) == {f"{CANARY_TOKEN}1"}


async def test_disconnect_revokes_then_deletes(world):
    await connect(world)
    assert await link_wallet.disconnect(OWNER) is True
    revoke = next(c for c in world.link.calls if c.url.path == "/auth/revoke")
    form = parse_qs(revoke.content.decode())
    assert form["token"] == [f"{CANARY_REFRESH}0"] and form["token_type_hint"] == ["refresh_token"]
    assert (OWNER, "link_wallet") not in world.secrets


async def test_connector_leaving_reads_disconnected_and_ends_the_grant(world):
    await connect(world)
    world.members.discard(ADMIN)
    assert (await link_wallet.status(OWNER))["connected"] is False
    assert (OWNER, "link_wallet") not in world.secrets
    assert any(c.url.path == "/auth/revoke" for c in world.link.calls)
    with pytest.raises(purchases.PurchaseRefused) as e:
        await ask()
    assert e.value.code == "wallet_disconnected"


# ---- policy matrix ---------------------------------------------------------------------------


async def test_wallet_disconnected_refuses(world):
    with pytest.raises(purchases.PurchaseRefused) as e:
        await ask()
    assert e.value.code == "wallet_disconnected"


async def test_over_per_purchase_cap(world):
    await connect(world)
    with pytest.raises(purchases.PurchaseRefused) as e:
        await ask(amount=100.01)
    assert e.value.code == "over_purchase_cap" and not world.table.items


async def test_over_project_monthly_cap(world):
    await connect(world)
    await ask(amount=100, why="First licence batch for the launch project, ask one.")
    world.table.items[next(iter(world.table.items))]["status"] = "pending_approval"  # Link holds it
    await ask(amount=100, why="Second licence batch for the launch project, ask two.")
    world.table.items[list(world.table.items)[-1]]["status"] = "pending_approval"
    with pytest.raises(purchases.PurchaseRefused) as e:
        await ask(amount=1, why="One more small thing for the launch project, ask three.")
    assert e.value.code == "over_project_cap"


async def test_no_limits_set_is_fail_closed(world):
    await connect(world)
    world.limits.update(perPurchaseCents=0, projectMonthlyCents=0)
    with pytest.raises(purchases.PurchaseRefused) as e:
        await ask()
    assert e.value.code == "no_limits"


async def test_retry_returns_the_same_request_and_files_one_card(world):
    await connect(world)
    first = await ask()
    again = await ask()
    assert first == again and first["status"] == "needs_owner"
    assert len(world.table.items) == 1 and world.cards.await_count == 1


async def test_unlisted_merchant_waits_for_the_owner_and_never_reaches_link(world):
    await connect(world)
    world.limits["merchants"] = ["https://other.example.com"]
    world.policy["money_moves"] = False
    out = await ask()
    assert out["status"] == "needs_owner" and not world.link.bodies("/spend_requests")


async def test_allowlisted_merchant_with_money_moves_on_still_waits_for_the_owner(world):
    await connect(world)
    world.limits["merchants"] = ["https://acme.com"]
    assert (await ask())["status"] == "needs_owner"
    assert not world.link.bodies("/spend_requests")


async def test_allowlisted_with_money_moves_off_goes_to_link_with_our_id_as_idempotency_key(world):
    await connect(world)
    world.limits["merchants"] = ["https://acme.com"]
    world.policy["money_moves"] = False
    out = await ask()
    body = world.link.bodies("/spend_requests")[0]
    assert out["status"] == "pending_approval" and body["idempotency_key"] == out["requestId"]
    assert body["amount"] == 3500 and len(body["context"]) >= 100
    assert body["metadata"] == {"agentId": "agent_1", "issueId": "iss_1", "projectId": "proj_1"}


async def test_owner_click_creates_the_link_request_rechecks_policy_and_mints_the_url(world):
    await connect(world)
    rid = (await ask())["requestId"]
    out = await purchases.approval_link(OWNER, rid)
    assert out == {"status": "pending_approval", "approvalUrl": "https://app.link.com/activity/approve/lsrq_1"}
    # a not-yet-sent ask is refused when the owner lowers the cap below it
    rid3 = (await ask(amount=50, why="A different purchase to be checked after the cap moves."))["requestId"]
    world.limits["perPurchaseCents"] = 100
    with pytest.raises(purchases.PurchaseRefused) as e:
        await purchases.approval_link(OWNER, rid3)
    assert e.value.code == "over_purchase_cap"


async def test_a_card_failure_leaves_no_ledger_row_so_a_retry_starts_clean(world):
    await connect(world)
    world.cards.side_effect = RuntimeError("board down")
    with pytest.raises(RuntimeError):
        await ask()
    assert not world.table.items
    world.cards.side_effect = None
    assert (await ask())["status"] == "needs_owner"


# ---- no card or token reaches a log, row or response -------------------------------------------


async def test_canary_no_credential_reaches_logs_rows_or_returns(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.limits["merchants"] = ["https://acme.com"]
    world.policy["money_moves"] = False
    await connect(world, expires_at=0)
    out = await ask()
    out2 = await purchases.approval_link(OWNER, out["requestId"])
    await link_wallet.disconnect(OWNER)
    blob = repr((out, out2, list(world.table.items.values()), [r.getMessage() for r in caplog.records]))
    for canary in (CANARY_CARD, "lpt_CANARY", CANARY_TOKEN, CANARY_REFRESH, "csecret"):
        assert canary not in blob
    assert "approval_url" not in repr(list(world.table.items.values())) and "app.link.com" not in repr(
        list(world.table.items.values())
    )


# ---- the dark gate and the routes ------------------------------------------------------------------


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id=ADMIN, org_id=OWNER, org_role="org:admin", email="a@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_every_route_404s_while_the_flag_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "LINK_WALLET_ENABLED", False)
    base = "/api/v1/company/link-wallet"
    for method, path in (
        ("get", "/status"),
        ("post", "/connect"),
        ("get", "/callback?code=x&state=y"),
        ("post", "/disconnect"),
        ("get", "/limits"),
        ("get", "/purchases"),
        ("post", "/purchases/" + "a" * 32 + "/approval-link"),
    ):
        assert getattr(client, method)(base + path).status_code == 404, path
    r = client.put(base + "/limits", json={"perPurchaseCents": 1, "projectMonthlyCents": 1, "merchants": []})
    assert r.status_code == 404
    r = client.post("/api/v1/internal/link-wallet/org_a/requests", json={})
    assert r.status_code == 404


def test_a_member_cannot_reach_the_owner_routes(world):
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_m", org_id=OWNER, org_role="org:member", email="m@example.com"
    )
    try:
        c = TestClient(app)
        assert c.post("/api/v1/company/link-wallet/connect").status_code == 403
        assert (
            c.put(
                "/api/v1/company/link-wallet/limits", json={"perPurchaseCents": 1, "projectMonthlyCents": 1}
            ).status_code
            == 403
        )
    finally:
        app.dependency_overrides.clear()


def test_the_agent_door_needs_the_hook_token(world):
    r = TestClient(app).post(
        "/api/v1/internal/link-wallet/org_a/requests",
        json={"agentId": "a", "merchant": "m", "url": "https://x.com", "amount": 1, "why": "x" * 20, "issueId": "i"},
    )
    assert r.status_code == 401
