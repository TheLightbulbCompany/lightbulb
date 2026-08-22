from unittest.mock import AsyncMock, patch

import pytest

from routers import agent_mail

OWNER, AGENT, TOKEN = "user_a", "agent_1", "hook-token-for-a"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _reset():
    agent_mail._RATE_BUCKETS_PER_OWNER.clear()
    yield
    agent_mail._RATE_BUCKETS_PER_OWNER.clear()


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
    assert q.call_args.args == ("user_test_123",)
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
    assert mint.call_args.kwargs["agent_id"] == "agent_f" and mint.call_args.kwargs["owner_id"] == "user_test_123"
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


def test_patch_address_mint_failure_409(client):
    with (
        patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value=None)),
        patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")),
        patch.object(agent_mail, "ensure_agent_address", AsyncMock(return_value=(None, "company_name_unavailable"))),
    ):
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"send_to_anyone": True})
    assert r.status_code == 409 and r.json()["detail"]["status"] == "company_name_unavailable"
