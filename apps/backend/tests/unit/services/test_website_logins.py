"""Workspace website logins: the store, the ask round trip, its Activity rendering, and the routes.

Moto stands in for the messaging table, the integrations table and Secrets Manager, so every
test also proves where a password lands (Secrets Manager) and where it must never land
(DynamoDB, the Activity item, a room message, a log line)."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import json
import logging
from unittest.mock import AsyncMock, patch

import boto3
import pytest

from core.auth import AuthContext
from core.services import activity, website_logins as store
from core.services.activity import ActivityActionRequest
from core.services.messaging import ApiProblem
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    FRIDAY,
    OWNER,
    USER_A,
    _room,
    repo,
    table,
)

PASSWORD = "hunter2-Sup3r-s3cret"


@pytest.fixture
def env(table):
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="integrations",
        KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}, {"AttributeName": "provider", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "owner_id", "AttributeType": "S"},
            {"AttributeName": "provider", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    with (
        patch("core.dynamodb._dynamodb_resource", resource),
        patch("core.services.website_logins.get_table", lambda name: resource.Table(name)),
        patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": FRIDAY, "name": "Friday"}])),
        patch("core.services.live_events.publish_owner_users", AsyncMock()),
        patch("core.config.settings.ENCRYPTION_KEY", "k" * 40),
        patch("core.repositories.paperclip_repo.PaperclipRepo.get_workspace", AsyncMock(return_value=None)),
    ):
        yield resource


def _dump_everything(resource) -> str:
    rows = [resource.Table(name).scan()["Items"] for name in ("integrations", "test-messaging")]
    return json.dumps(rows, default=str)


def admin(user=USER_A):
    return AuthContext(user_id=user, org_id=OWNER, org_role="org:admin")


def member(user=USER_A):
    return AuthContext(user_id=user, org_id=OWNER, org_role="org:member")


async def test_add_list_resolve_delete_keeps_the_password_out_of_dynamodb(env, caplog):
    caplog.set_level(logging.INFO)
    saved = await store.add_login(OWNER, "app.supademo.com", "me@example.com", PASSWORD, added_by=USER_A)
    assert saved["origin"] == "https://app.supademo.com" and saved["id"].startswith("vault_")
    assert [r["username"] for r in await store.list_logins(OWNER)] == ["me@example.com"]
    assert PASSWORD not in _dump_everything(env)

    # Same origin + username replaces the password and keeps the id.
    again = await store.add_login(
        OWNER, "https://app.supademo.com:443/login", "me@example.com", "new-pass-1", added_by=USER_A
    )
    assert again["id"] == saved["id"] and len(await store.list_logins(OWNER)) == 1

    found = await store.resolve_login(OWNER, saved["id"], agent_id=FRIDAY)
    assert found == {"username": "me@example.com", "password": "new-pass-1"}
    assert (await store.list_logins(OWNER))[0]["lastUsedAt"]

    assert await store.delete_login(OWNER, saved["id"], actor=USER_A) is True
    assert await store.list_logins(OWNER) == []
    assert await store.resolve_login(OWNER, saved["id"], agent_id=FRIDAY) is None
    assert await store.delete_login(OWNER, saved["id"], actor=USER_A) is False
    # Audit lines exist, and none carries a value.
    audit = [r.getMessage() for r in caplog.records if "website_login." in r.getMessage()]
    assert any("website_login.added" in m for m in audit) and any("website_login.deleted" in m for m in audit)
    assert not any(PASSWORD in m or "new-pass-1" in m for m in caplog.text.splitlines())


@pytest.mark.parametrize("bad", ["", "localhost", "ftp://x.com", "https://x.com:99999"])
async def test_origin_must_be_a_web_address(env, bad):
    with pytest.raises(store.LoginError):
        await store.add_login(OWNER, bad, "u", "p", added_by=USER_A)


async def test_login_ask_round_trip_through_activity(env):
    room_id, _ = await _room()
    created = await store.create_request(
        OWNER,
        kind="login",
        origin="https://supademo.com/signin",
        agent_id=FRIDAY,
        room_id=room_id,
        thread_root_message_id=None,
    )
    rid = created["requestId"]

    # The room got the agent's notice (no dispatch), linking to the Activity item.
    messages, _ = await repo.list_messages(room_id, 0, 50)
    notice = messages[-1]
    assert notice["author"]["id"] == FRIDAY and f"focus=login:{rid}" in notice["text"]
    work = [r for r in env.Table("test-messaging").scan()["Items"] if str(r["sk"]).startswith("WORK#")]
    assert work == []

    # The room's human sees a pinned Needs-you item with the secure form.
    rows = await repo.list_activity(OWNER, USER_A, before_sk=None, limit=10)
    items = await activity._render_rows(OWNER, USER_A, rows, None, admin=True)
    item = next(i for i in items if i.kind == "login")
    assert activity.needs_you(item) and item.summary.title == "Friday needs the login for supademo.com"
    assert [a.id for a in item.actions] == ["provide", "decline"] and item.actions[0].input.kind == "login"
    # A member (not admin) sees it without actions, and cannot answer.
    viewer = await activity._render_rows(OWNER, USER_A, rows, None, admin=False)
    assert next(i for i in viewer if i.kind == "login").actions == []
    with pytest.raises(ApiProblem):
        await activity.run_action(
            member(), item.id, "provide", ActivityActionRequest(value="{}"), teams_ctx=AsyncMock()
        )

    assert (await store.poll_request(OWNER, rid))["status"] == "pending"
    done = await activity.run_action(
        admin(),
        item.id,
        "provide",
        ActivityActionRequest(value=json.dumps({"username": "me@example.com", "password": PASSWORD})),
        teams_ctx=AsyncMock(),
    )
    assert done.state == "resolved" and done.actions == []
    polled = await store.poll_request(OWNER, rid)
    assert polled["status"] == "answered" and polled["loginId"].startswith("vault_")
    assert await store.resolve_login(OWNER, polled["loginId"], agent_id=FRIDAY) == {
        "username": "me@example.com",
        "password": PASSWORD,
    }
    assert PASSWORD not in _dump_everything(env)
    assert PASSWORD not in done.model_dump_json()


async def test_code_ask_is_handed_over_once_and_erased(env):
    created = await store.create_request(
        OWNER,
        kind="code",
        origin="https://accounts.google.com",
        agent_id=FRIDAY,
        room_id=None,
        thread_root_message_id=None,
    )
    rid = created["requestId"]
    row = await store.answer(OWNER, rid, user_id=USER_A, value={"code": "123 456"})
    assert row["status"] == "answered" and "123456" not in json.dumps(row)
    assert "123456" not in _dump_everything(env)
    assert (await store.poll_request(OWNER, rid)) == {"status": "answered", "kind": "code", "code": "123456"}
    assert (await store.poll_request(OWNER, rid))["status"] == "consumed"


async def test_decline_and_cancel_settle_once(env):
    rid = (
        await store.create_request(
            OWNER,
            kind="login",
            origin="x.com",
            agent_id=FRIDAY,
            room_id=None,
            thread_root_message_id=None,
        )
    )["requestId"]
    await store.answer(OWNER, rid, user_id=USER_A, value=None)
    assert (await store.poll_request(OWNER, rid))["status"] == "declined"
    await store.cancel_request(OWNER, rid)  # no-op on a settled ask
    assert (await store.poll_request(OWNER, rid))["status"] == "declined"


async def test_admin_routes_never_return_a_password(env, async_client_org_admin):
    with patch("routers.website_logins.resolve_owner_id", lambda auth: OWNER):
        res = await async_client_org_admin.post(
            "/api/v1/website-logins", json={"origin": "supademo.com", "username": "u@x.com", "password": PASSWORD}
        )
        assert res.status_code == 201 and PASSWORD not in res.text
        listed = await async_client_org_admin.get("/api/v1/website-logins")
        assert listed.status_code == 200 and PASSWORD not in listed.text
        assert listed.json()["logins"][0]["origin"] == "https://supademo.com"
        assert (await async_client_org_admin.delete(f"/api/v1/website-logins/{res.json()['id']}")).status_code == 204
        assert (await async_client_org_admin.delete(f"/api/v1/website-logins/{res.json()['id']}")).status_code == 404


async def test_members_cannot_manage_logins(env, async_client_org_member):
    assert (await async_client_org_member.get("/api/v1/website-logins")).status_code == 403
    body = {"origin": "supademo.com", "username": "u", "password": "p"}
    assert (await async_client_org_member.post("/api/v1/website-logins", json=body)).status_code == 403
    assert (await async_client_org_member.delete("/api/v1/website-logins/vault_0123456789ab")).status_code == 403


async def test_internal_routes_refuse_the_hook_token_and_take_the_vault_token(env, async_client):
    """The agent's shell holds the hook token; only the bridge holds the vault token."""
    from core.services.agentcore_runtime import derive_vault_token

    row = {"id": "row-1", "hook_token": "tok"}
    with patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=row)):
        await store.add_login(OWNER, "supademo.com", "u@x.com", PASSWORD, added_by=USER_A)
        base = f"/api/v1/internal/website-logins/{OWNER}"
        hook = {"Authorization": "Bearer tok"}
        assert (await async_client.get(base)).status_code == 401
        assert (await async_client.get(base, headers=hook)).status_code == 401
        assert (await async_client.post(f"{base}/logins/x/resolve", json={}, headers=hook)).status_code == 401
        assert (await async_client.get(f"{base}/requests/r1", headers=hook)).status_code == 401  # the 2FA-code read
        good = {"Authorization": f"Bearer {derive_vault_token(OWNER, 'row-1')}"}
        # Another owner's (or another row incarnation's) vault token is not this one.
        assert (
            await async_client.get(base, headers={"Authorization": f"Bearer {derive_vault_token(OWNER, 'row-0')}"})
        ).status_code == 401
        listed = (await async_client.get(base, headers=good)).json()["logins"]
        assert PASSWORD not in json.dumps(listed)
        res = await async_client.post(
            f"{base}/logins/{listed[0]['id']}/resolve",
            json={"agentId": FRIDAY},
            headers=good,
        )
        assert res.json() == {"username": "u@x.com", "password": PASSWORD}
