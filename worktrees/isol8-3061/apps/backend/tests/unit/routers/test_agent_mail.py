from unittest.mock import AsyncMock, patch

import pytest

from routers import agent_mail

OWNER, AGENT, TOKEN = "user_a", "agent_1", "hook-token-for-a"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _reset():
    agent_mail._RATE.clear()
    yield
    agent_mail._RATE.clear()


# `client` comes from tests/conftest.py: TestClient(app) with get_current_user
# overridden to AuthContext(user_id="user_test_123") (personal). Hook-token
# routes ignore that override; the /addresses routes rely on it.


def _container(owner_id=OWNER):
    return {"owner_id": owner_id, "hook_token": TOKEN}


def test_send_missing_bearer_401(client):
    assert (
        client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send", json={"agent_id": AGENT, "subject": "s", "body_text": "b"}
        ).status_code
        == 401
    )


def test_send_wrong_token_401(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        r = client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send",
            json={"agent_id": AGENT, "subject": "s", "body_text": "b"},
            headers={"Authorization": "Bearer WRONG"},
        )
    assert r.status_code == 401


def test_send_both_to_and_reply_key_400(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        r = client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send",
            json={"agent_id": AGENT, "subject": "s", "body_text": "b", "to": "a@b.c", "reply_key": "x/y"},
            headers=H,
        )
    assert r.status_code == 400


def test_send_delegates(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "send", AsyncMock(return_value="sent")) as send,
    ):
        r = client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send",
            json={"agent_id": AGENT, "subject": "s", "body_text": "b", "to": "v@x.y"},
            headers=H,
        )
    assert r.status_code == 200 and r.json() == {"status": "sent"}
    assert send.call_args.kwargs["to"] == "v@x.y" and send.call_args.kwargs["agent_id"] == AGENT


# ---- ported from the deleted routers/agent_mail_send.py test suite --------
# (same auth boundary, now on POST /u/{owner_id}/send instead of
# the deleted owner-send router — see git show 28e83f275:apps/backend/tests/unit/routers/test_agent_mail_send.py)


def _send_body(agent_id=AGENT):
    return {"agent_id": agent_id, "subject": "s", "body_text": "b"}


def test_send_unknown_owner_401(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=_send_body(), headers=H)
    assert r.status_code == 401


def test_send_owner_as_token_holder_cannot_send_for_a_different_owner(client):
    """owner_b's row carries a DIFFERENT hook_token, so owner_a's token fails
    the compare — the whole point of naming the owner in the path."""
    with patch.object(
        agent_mail.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_b", "hook_token": "hook-token-for-b"}),
    ):
        r = client.post(
            "/api/v1/agent-mail/u/user_b/send",
            json=_send_body(),
            headers=H,
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_send_non_ascii_bearer_is_401_not_500():
    """hmac.compare_digest raises TypeError on a non-ASCII `str`, which would
    surface as a 500 instead of a 401.

    Driven directly against `_require_hook_token`: httpx refuses to transmit a
    non-ASCII header at all, but Starlette decodes real headers as latin-1, so
    a raw ASGI request genuinely can hand us one.
    """
    from fastapi import HTTPException

    class _Req:
        headers = {"Authorization": "Bearer ünïcode"}

    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        with pytest.raises(HTTPException) as exc:
            await agent_mail._require_hook_token(OWNER, _Req())
    assert exc.value.status_code == 401


def test_send_rate_limit_returns_429(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "send", AsyncMock(return_value="sent")),
    ):
        for _ in range(agent_mail.RATE_LIMIT_PER_MINUTE):
            assert client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=_send_body(), headers=H).status_code == 200
        assert client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=_send_body(), headers=H).status_code == 429


def test_send_401s_do_not_charge_the_rate_limit_bucket(client):
    """Auth runs before the limiter, so an unauthenticated flood cannot lock
    the real owner out."""
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        for _ in range(80):
            client.post(
                f"/api/v1/agent-mail/u/{OWNER}/send",
                json=_send_body(),
                headers={"Authorization": "Bearer WRONG"},
            )
    assert len(agent_mail._RATE._buckets[OWNER]) == 0


def test_inbox_lists_with_keys_and_truncates(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value="a@acme.isol8.co")),
        patch.object(
            agent_mail,
            "list_messages",
            AsyncMock(
                return_value=[
                    {
                        "received_key": "2026#m1",
                        "from_addr": "g@github.com",
                        "subject": "Verify",
                        "received_at": "2026",
                        "text": "x" * 9000,
                    }
                ]
            ),
        ),
    ):
        r = client.get(f"/api/v1/agent-mail/u/{OWNER}/inbox?agent_id={AGENT}&limit=5", headers=H)
    body = r.json()
    assert body["address"] == "a@acme.isol8.co"
    assert body["messages"][0]["key"] == "a@acme.isol8.co/2026#m1"
    assert len(body["messages"][0]["text"]) == 8000


def test_inbox_without_address_is_empty(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value=None)),
    ):
        r = client.get(f"/api/v1/agent-mail/u/{OWNER}/inbox?agent_id={AGENT}", headers=H)
    assert r.json() == {"address": None, "messages": []}


def test_address_mints(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")),
        patch.object(agent_mail, "ensure_agent_address", AsyncMock(return_value=("a@acme.isol8.co", "ok"))),
    ):
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/address", json={"agent_id": AGENT}, headers=H)
    assert r.json() == {"address": "a@acme.isol8.co", "reason": "ok"}


def test_validate_sender_entries():
    assert agent_mail._validate_senders(["Bob@X.y", "@ClientCo.com"]) == ["bob@x.y", "@clientco.com"]
    for bad in ["nope", "@", "a@", "@x", "two@@x.y"]:
        with pytest.raises(ValueError):
            agent_mail._validate_senders([bad])


# ---- Clerk routes (owner = user_test_123 via the shared client fixture) ----


def test_list_addresses_projects_active_rows(client):
    rows = [
        {
            "address": "f@acme.isol8.co",
            "agent_id": "agent_f",
            "verified_senders": ["o@x.y"],
            "send_to_anyone": False,
            "status": "active",
        },
        {"address": "old@acme.isol8.co", "agent_id": "agent_f", "status": "alias"},
    ]
    with patch.object(agent_mail.agent_email_repo, "query_by_owner", AsyncMock(return_value=rows)) as q:
        r = client.get("/api/v1/agent-mail/addresses")
    assert r.status_code == 200
    assert q.call_args.args == ("org_test_456",)
    assert r.json() == {
        "addresses": [
            {
                "address": "f@acme.isol8.co",
                "agent_id": "agent_f",
                "verified_senders": ["o@x.y"],
                "send_to_anyone": False,
                "outbound_disabled": None,
                "status": "active",
            }
        ]
    }


def test_patch_address_lowercases_and_mints_when_missing(client):
    updated = {
        "address": "f@acme.isol8.co",
        "agent_id": "agent_f",
        "verified_senders": ["bob@x.y", "@clientco.com"],
        "send_to_anyone": True,
        "status": "active",
    }
    with (
        patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value=None)),
        patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")),
        patch.object(agent_mail, "ensure_agent_address", AsyncMock(return_value=("f@acme.isol8.co", "ok"))) as mint,
        patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value=updated)) as upd,
    ):
        r = client.patch(
            "/api/v1/agent-mail/addresses/agent_f",
            json={"verified_senders": ["Bob@X.y", "@ClientCo.com"], "send_to_anyone": True},
        )
    assert r.status_code == 200 and r.json()["send_to_anyone"] is True
    assert mint.call_args.kwargs["agent_id"] == "agent_f" and mint.call_args.kwargs["owner_id"] == "org_test_456"
    assert upd.call_args.args == ("f@acme.isol8.co",)
    assert upd.call_args.kwargs == {"verified_senders": ["bob@x.y", "@clientco.com"], "send_to_anyone": True}


def test_patch_address_bad_sender_400(client):
    with patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value="f@acme.isol8.co")):
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"verified_senders": ["nope"]})
    assert r.status_code == 400


def test_patch_address_reenable_sends_null(client):
    with (
        patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value="f@acme.isol8.co")),
        patch.object(
            agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value={"address": "f@acme.isol8.co"})
        ) as upd,
    ):
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"outbound_disabled": None})
    assert r.status_code == 200
    assert upd.call_args.kwargs == {"outbound_disabled": None}


def test_patch_address_nonnull_outbound_disabled_400(client):
    """outbound_disabled is read-only from the owner settings surface except
    for the explicit-null re-enable — any other value must be rejected, not
    silently ignored."""
    with (
        patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value="f@acme.isol8.co")),
        patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock()) as upd,
    ):
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"outbound_disabled": "spam"})
    assert r.status_code == 400 and r.json()["detail"]["status"] == "outbound_disabled_readonly"
    upd.assert_not_called()


def test_patch_address_mint_failure_409(client):
    with (
        patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value=None)),
        patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")),
        patch.object(agent_mail, "ensure_agent_address", AsyncMock(return_value=(None, "company_name_unavailable"))),
    ):
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"send_to_anyone": True})
    assert r.status_code == 409 and r.json()["detail"]["status"] == "company_name_unavailable"
