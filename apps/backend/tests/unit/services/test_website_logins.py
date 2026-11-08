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
        patch("core.repositories.workspace_repo.WorkspaceRepo.get_workspace", AsyncMock(return_value=None)),
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
        await activity.run_action(member(), item.id, "provide", ActivityActionRequest(value="{}"))

    assert (await store.poll_request(OWNER, rid))["status"] == "pending"
    done = await activity.run_action(
        admin(),
        item.id,
        "provide",
        ActivityActionRequest(value=json.dumps({"username": "me@example.com", "password": PASSWORD})),
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


async def test_code_ask_is_only_a_line_sending_the_person_to_the_browser(env):
    """Owner ruling 2026-10-07: a sign-in code is typed into the shared browser, never into a form
    for the agent. So: one line in the room, no request row, no Activity item, nothing to answer."""
    room_id, _ = await _room()
    created = await store.create_request(
        OWNER,
        kind="code",
        origin="https://accounts.intuit.com/app/sign-in",
        agent_id=FRIDAY,
        room_id=room_id,
        thread_root_message_id=None,
        hint="  Check your\n text messages ",
    )
    assert created == {"requestId": None, "status": "posted"}
    messages, _ = await repo.list_messages(room_id, 0, 50)
    notice = messages[-1]
    assert notice["author"]["id"] == FRIDAY
    assert "accounts.intuit.com is asking for a verification code (Check your text messages)" in notice["text"]
    assert "panel=browser" in notice["text"] and "focus=login" not in notice["text"]
    rows = env.Table("test-messaging").scan()["Items"]
    assert not [r for r in rows if str(r["sk"]).startswith(("LOGINREQ#", "WORK#"))]
    assert not [
        r for r in await repo.list_activity(OWNER, USER_A, before_sk=None, limit=10) if r.get("kind") == "login"
    ]

    # A run browsing on a selected Mac: the code goes into that Mac's agent Chrome, not the panel.
    await store.create_request(
        OWNER,
        kind="code",
        origin="https://x.com",
        agent_id=FRIDAY,
        room_id=room_id,
        thread_root_message_id=None,
        surface="mac",
    )
    messages, _ = await repo.list_messages(room_id, 0, 50)
    assert "Lightbulb agent Chrome window on your Mac" in messages[-1]["text"]
    assert "panel=browser" not in messages[-1]["text"]

    # The room write failed: the answer says so, so the bridge does not wait for a person nobody told.
    with patch("core.repositories.messaging_repo.create_message", AsyncMock(side_effect=RuntimeError("down"))):
        unposted = await store.create_request(
            OWNER, kind="code", origin="https://x.com", agent_id=FRIDAY, room_id=room_id, thread_root_message_id=None
        )
    assert unposted == {"requestId": None, "status": "unposted"}
    # And with no room at all there is nothing to post into.
    nowhere = await store.create_request(
        OWNER, kind="code", origin="https://x.com", agent_id=FRIDAY, room_id=None, thread_root_message_id=None
    )
    assert nowhere == {"requestId": None, "status": "unposted"}


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
    assert (await async_client_org_member.get("/api/v1/website-logins/sites")).status_code == 403
    patched = await async_client_org_member.patch("/api/v1/website-logins/vault_0123456789ab", json={"username": "x"})
    assert patched.status_code == 403
    with patch("core.services.browser_control.sign_out", AsyncMock()) as sign_out:
        res = await async_client_org_member.post("/api/v1/website-logins/sites/sign-out", json={"site": "google.com"})
    assert res.status_code == 403
    sign_out.assert_not_awaited()


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


# ---- website sign-ins: one row per site, edit, sign out, the connector hint ------------------


@pytest.mark.parametrize(
    ("value", "site"),
    [
        ("https://accounts.google.com", "google.com"),
        (".google.com", "google.com"),
        ("mail.google.com:8443", "google.com"),
        ("https://app.example.co.uk:8443/login", "example.co.uk"),
        ("shop.example.com.au", "example.com.au"),
        ("someone.github.io", "someone.github.io"),  # shared hosting: each tenant is a site
        ("shop.example.com.ph", "example.com.ph"),  # a suffix no short allowlist would carry
        ("a.b.co.uk", "b.co.uk"),
        ("accounts.google.com", "google.com"),
        ("t.co", "t.co"),  # a two-label ccTLD host is the site itself
        ("x.y.s3.amazonaws.com", "y.s3.amazonaws.com"),
        ("intranet.corp.madeuptld", "intranet.corp.madeuptld"),  # unknown: split, never merged
        ("com.ph", "com.ph"),  # a bare suffix is nobody's site (is_site refuses it)
        ("example.com", "example.com"),
        ("10.0.0.7", "10.0.0.7"),
        ("localhost", "localhost"),
    ],
)
def test_site_of_groups_hosts_by_registrable_domain(value, site):
    assert store.site_of(value) == site


async def test_update_fixes_a_username_and_replaces_a_password(env, caplog):
    caplog.set_level(logging.INFO)
    saved = await store.add_login(
        OWNER, "supademo.com", "me@exmaple.com", PASSWORD, added_by=USER_A, added_by_name="ann"
    )
    other = await store.add_login(OWNER, "supademo.com", "taken@example.com", "p2", added_by=USER_A)

    fixed = await store.update_login(OWNER, saved["id"], username=" me@example.com ", actor="user_b")
    # Same id, same origin, who added it and when unchanged; the password untouched.
    assert fixed == {**saved, "username": "me@example.com"}
    assert await store.resolve_login(OWNER, saved["id"], agent_id=FRIDAY) == {
        "username": "me@example.com",
        "password": PASSWORD,
    }
    replaced = await store.update_login(OWNER, saved["id"], password="n3w-Passw0rd", actor="user_b")
    assert replaced["username"] == "me@example.com" and "password" not in replaced
    assert (await store.resolve_login(OWNER, saved["id"], agent_id=FRIDAY))["password"] == "n3w-Passw0rd"
    row = env.Table("integrations").get_item(Key={"owner_id": OWNER, "provider": store.LOGINS_KEY})["Item"]
    assert row["logins"][saved["id"]]["updatedBy"] == "user_b" and row["logins"][saved["id"]]["addedBy"] == USER_A
    assert "n3w-Passw0rd" not in _dump_everything(env)

    with pytest.raises(store.LoginError, match="already saved for supademo.com"):
        await store.update_login(OWNER, saved["id"], username="taken@example.com", actor=USER_A)
    for bad in ({"username": "  "}, {"password": ""}, {}):
        with pytest.raises(store.LoginError):
            await store.update_login(OWNER, saved["id"], actor=USER_A, **bad)
    assert await store.update_login(OWNER, "vault_ffffffffffff", username="x", actor=USER_A) is None
    assert await store.update_login(OWNER, "not-an-id", username="x", actor=USER_A) is None
    assert (await store.resolve_login(OWNER, other["id"], agent_id=FRIDAY))["username"] == "taken@example.com"
    assert "website_login.updated" in caplog.text and "n3w-Passw0rd" not in caplog.text


async def test_sites_merge_saved_logins_with_the_browsers_sign_ins(env):
    a = await store.add_login(OWNER, "accounts.google.com", "a@x.com", PASSWORD, added_by=USER_A)
    b = await store.add_login(OWNER, "mail.google.com", "b@x.com", PASSWORD, added_by=USER_A)
    c = await store.add_login(OWNER, "app.supademo.com", "c@x.com", PASSWORD, added_by=USER_A)
    with patch("core.services.browser_control.signed_in_sites", AsyncMock(return_value={"google.com", "github.com"})):
        sites = await store.list_sites(OWNER)
    assert sites == [
        {"site": "github.com", "signedIn": True, "logins": []},
        {"site": "google.com", "signedIn": True, "logins": [a, b]},
        {"site": "supademo.com", "signedIn": False, "logins": [c]},
    ]
    # The browser's save being unreadable never hides the saved logins.
    with patch("core.services.browser_control.signed_in_sites", AsyncMock(side_effect=RuntimeError("s3"))):
        assert [s["site"] for s in await store.list_sites(OWNER)] == ["google.com", "supademo.com"]


async def test_site_routes_edit_and_sign_out_without_returning_a_secret(env, async_client_org_admin):
    from core.services import browser_control

    client = async_client_org_admin
    with (
        patch("routers.website_logins.resolve_owner_id", lambda auth: OWNER),
        patch("core.services.browser_control.signed_in_sites", AsyncMock(return_value={"supademo.com"})),
        patch("core.services.browser_control.sign_out", AsyncMock()) as sign_out,
    ):
        body = {"origin": "app.supademo.com", "username": "u@x.com", "password": PASSWORD}
        created = (await client.post("/api/v1/website-logins", json=body)).json()
        listed = await client.get("/api/v1/website-logins/sites")
        assert listed.status_code == 200 and PASSWORD not in listed.text
        assert listed.json() == {"sites": [{"site": "supademo.com", "signedIn": True, "logins": [created]}]}

        url = f"/api/v1/website-logins/{created['id']}"
        patched = await client.patch(url, json={"username": "v@x.com", "password": "n3w-Passw0rd"})
        assert patched.status_code == 200 and patched.json() == {**created, "username": "v@x.com"}
        assert "n3w-Passw0rd" not in patched.text
        assert (await client.patch(url, json={})).status_code == 400
        assert (await client.patch(url, json={"username": " "})).status_code == 400
        await client.post("/api/v1/website-logins", json={**body, "username": "w@x.com"})
        clash = await client.patch(url, json={"username": "w@x.com"})
        assert clash.status_code == 400 and "already saved" in clash.json()["detail"]
        assert (
            await client.patch("/api/v1/website-logins/vault_ffffffffffff", json={"username": "x"})
        ).status_code == 404

        out = await client.post("/api/v1/website-logins/sites/sign-out", json={"site": " Supademo.com "})
        assert out.status_code == 204 and out.content == b""
        sign_out.assert_awaited_once()
        assert sign_out.await_args.args == (OWNER, "supademo.com")
        # A site is a registrable domain, never a subdomain, URL or bare word.
        for bad in ("app.supademo.com", "https://supademo.com", "supademo", "", "com.ph", "co.uk", "github.io", "com"):
            res = await client.post("/api/v1/website-logins/sites/sign-out", json={"site": bad})
            assert res.status_code in (400, 422), bad
        sign_out.assert_awaited_once()
        sign_out.side_effect = browser_control.BrowserError(browser_control.AGENT_BUSY)
        busy = await client.post("/api/v1/website-logins/sites/sign-out", json={"site": "supademo.com"})
        assert busy.status_code == 409 and busy.json()["detail"] == browser_control.AGENT_BUSY


@pytest.mark.parametrize(
    ("origin", "connected", "hint"),
    [
        ("https://accounts.google.com", {"google"}, "Google is connected. This password is only for the browser."),
        ("https://github.com/login", {"github"}, "GitHub is connected. This password is only for the browser."),
        (
            "https://login.microsoftonline.com",
            {"microsoft365"},
            "Microsoft 365 is connected. This password is only for the browser.",
        ),
        ("https://www.notion.so", {"notion"}, "Notion is connected. This password is only for the browser."),
        ("https://acme.slack.com", {"slack"}, "Slack is connected. This password is only for the browser."),
        ("https://accounts.google.com", set(), None),  # not connected: no hint
        ("https://accounts.google.com", {"github"}, None),  # another service's connector
        ("https://supademo.com", {"google", "github", "slack"}, None),
    ],
)
async def test_a_login_ask_carries_a_hint_when_the_sites_connector_is_connected(env, origin, connected, hint):
    async def get(owner_id, provider):
        return {"status": "connected"} if provider in connected else {"status": "reconnect_required"}

    installs = [{"status": "active"}] if "slack" in connected else [{"status": "revoked"}]
    with (
        patch("core.repositories.integrations_repo.get", get),
        patch("core.repositories.slack_installs_repo.list_for_owner", AsyncMock(return_value=installs)),
    ):
        created = await store.create_request(
            OWNER, kind="login", origin=origin, agent_id=FRIDAY, room_id=None, thread_root_message_id=None
        )
    row = await store.get_request(OWNER, created["requestId"])
    item = activity.render_login_request(row, seen=None, admin=True)
    assert item.actions[0].input.hint == hint
    assert item.model_dump(by_alias=True)["actions"][0]["input"]["hint"] == hint


@pytest.mark.parametrize(
    ("site", "ok"),
    [
        ("example.com.ph", True),
        ("google.com", True),
        ("t.co", True),
        ("someone.github.io", True),
        ("10.0.0.7", True),
        ("intranet.corp.madeuptld", True),  # no suffix the list knows: the exact host is the site
        ("com.ph", False),  # public suffixes: would cover every site under them
        ("co.uk", False),
        ("github.io", False),
        ("com", False),
        ("localhost", False),
        ("shop.example.com.ph", False),  # a subdomain is not a site
        (".example.com", False),
        ("Example.com", False),
    ],
)
def test_only_a_registrable_domain_can_be_signed_out(site, ok):
    assert store.is_site(site) is ok


def test_the_suffix_list_never_fetches():
    assert store._PSL.suffix_list_urls == ()  # the bundled snapshot only
