"""Member routes (/api/v1/apps): Canvas's list, the admin's settings and typed-name removal, the issuer key."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch as mpatch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.config import settings  # noqa: E402
from core.services import apps_service as svc  # noqa: E402
from core.services import network_proof  # noqa: E402
from main import app  # noqa: E402
from routers import apps as mod  # noqa: E402
from routers import company_provisioning  # noqa: E402

BASE = "/api/v1/apps"


def _as(role: str):
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id=f"user_{role}", org_id="org_a", org_role=f"org:{role}", email="x@example.com"
    )
    return TestClient(app)


@pytest.fixture
def admin():
    yield _as("admin")
    app.dependency_overrides.clear()


@pytest.fixture
def member():
    yield _as("member")
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def paid(monkeypatch):
    m = AsyncMock()
    monkeypatch.setattr(mod, "require_always_on_entitlement", m)
    monkeypatch.setattr(company_provisioning, "require_always_on_entitlement", m)  # the rail's _owner_gate
    return m


def _record(app_id, name, room, open_to, **kw):
    return {
        "app_id": app_id,
        "owner_id": "org_a",
        "name": name,
        "room_id": room,
        "state": "ready",
        "open_to": open_to,
        "budget_usd": 20,
        "preview_url": None,
        "live_url": None,
        "live_version": None,
        "origin": {"kind": "idea"},
        "versions": [{"n": 1, "commit": "c1", "deployment_id": "d1", "at": "t", "by": "agent"}],
        "created_by": None,
        "actor_id": "agent",
        "actor_kind": "agent",
        **kw,
    }


def test_list_marks_can_open_by_room_membership(member, monkeypatch):
    records = [
        _record("app_1", "mine", "room_in", "room", created_by="user_founder", actor_kind="person"),
        _record("app_2", "theirs", "room_out", "room", actor_id="agt_1"),
        _record("app_3", "inv", "room_in", "invited"),
        _record("app_4", "inv-out", "room_out", "invited"),
        _record("app_5", "ws", "room_out", "workspace"),
        _record("app_6", "pub", "room_gone", "public"),
        _record("app_7", "gone", "room_in", "room", state="destroyed"),
        _record("app_8", "solo", None, "workspace"),
        _record("app_9", "private", None, "me", created_by="user_founder"),
    ]
    rooms = {
        "room_in": {"roomId": "room_in", "ownerId": "org_a", "name": "Ops"},
        "room_out": {"roomId": "room_out", "ownerId": "org_a", "name": "Sales"},
    }
    monkeypatch.setattr(svc.apps_repo, "list_for_owner", AsyncMock(return_value=records))
    monkeypatch.setattr(
        svc.wf_repo,
        "list_for_owner",
        AsyncMock(
            return_value=[
                {
                    "workflow_id": "pwf_1",
                    "app_id": "app_1",
                    "provider_name": "Vercel",
                    "service_ref": "project",
                    "environment": "prod",
                    "state": "active",
                    "resource_id": "r1",
                    "created_at": "1",
                }
            ]
        ),
    )
    monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: rooms.get(r)))
    reads = AsyncMock(side_effect=lambda o, u, r, ro: (u, r) == ("user_member", "room_in"))
    monkeypatch.setattr(svc.messaging, "still_reads_room", reads)
    monkeypatch.setattr(
        svc.messaging, "human_display_name", AsyncMock(side_effect=lambda u: "Ada" if u == "user_founder" else u)
    )
    monkeypatch.setattr(svc.messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "agt_1", "name": "Friday"}]))
    monkeypatch.setattr(svc, "_progress", AsyncMock(side_effect=AssertionError("the list must not call providers")))

    assert member.get(f"{BASE}/").status_code == 200
    r = member.get(BASE, follow_redirects=False)
    assert r.status_code == 200
    rows = {a["name"]: a for a in r.json()["apps"]}
    assert "gone" not in rows
    assert {n: a["can_open"] for n, a in rows.items()} == {
        "mine": True,
        "theirs": False,
        "inv": False,  # "invited" is the invitees (by link) and managers, never the room
        "inv-out": False,
        "ws": True,
        "pub": True,
        "solo": True,
    }  # "private" is its creator's only: not listed to another member
    assert rows["mine"]["room_name"] == "Ops" and rows["pub"]["room_name"] is None
    assert rows["theirs"]["room_name"] is None  # a room the caller is not in is not named
    assert rows["mine"]["created_by_name"] == "Ada" and rows["mine"]["agent_name"] is None
    assert rows["theirs"]["agent_name"] == "Friday" and rows["theirs"]["created_by_name"] is None
    assert rows["mine"]["resources"] == [
        {"provider": "Vercel", "service": "project", "state": "active", "resource_id": "r1", "environment": "prod"}
    ]
    assert "versions" not in rows["mine"]
    assert {"app_id", "state", "open_to", "canvas_url", "live_version", "spend_month_cents"} <= set(rows["mine"])


def test_list_survives_an_unavailable_roster(member, monkeypatch):
    monkeypatch.setattr(svc.apps_repo, "list_for_owner", AsyncMock(return_value=[_record("app_2", "a", "r", "public")]))
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(return_value=None))
    monkeypatch.setattr(
        svc.messaging, "load_agent_roster", AsyncMock(side_effect=svc.messaging.ApiProblem("RUNTIME_UNAVAILABLE", "x"))
    )
    r = member.get(f"{BASE}/")
    assert r.status_code == 200 and r.json()["apps"][0]["agent_name"] is None


def test_patch_requires_creator_or_org_admin(admin, member, monkeypatch, paid):
    upd = AsyncMock(return_value={"app_id": "app_1", "open_to": "public"})
    monkeypatch.setattr(svc, "update_settings", upd)
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=_record("app_1", "crew", "r", "room")))
    r = member.patch(f"{BASE}/app_1", json={"open_to": "public"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
    upd.assert_not_awaited()

    admin = _as("admin")
    r = admin.patch(f"{BASE}/app_1", json={"open_to": "public", "budget_usd": 50})
    assert r.status_code == 200 and r.json()["open_to"] == "public"
    assert upd.await_args.args == ("org_a", "app_1")
    assert upd.await_args.kwargs == {"open_to": "public", "budget_usd": 50, "display_name": None}
    paid.assert_awaited_with("org_a", message=mod.PAID_MESSAGE)

    assert admin.patch(f"{BASE}/app_1", json={"open_to": "me"}).status_code == 200  # only me
    r = admin.patch(f"{BASE}/app_1", json={"display_name": "  Crew Rota  "})
    assert r.status_code == 200 and upd.await_args.kwargs["display_name"] == "Crew Rota"
    for bad in (
        {"open_to": "everyone"}, {"budget_usd": -1}, {"budget_usd": "ten"},
        {"display_name": ""}, {"display_name": "   "}, {"display_name": "x" * 61},
    ):  # fmt: skip
        assert admin.patch(f"{BASE}/app_1", json=bad).status_code == 422, bad


def test_delete_requires_typed_name_and_removes_resources(member, monkeypatch):
    monkeypatch.setattr(
        svc.apps_repo, "get", AsyncMock(return_value={"app_id": "app_1", "owner_id": "org_a", "name": "crew"})
    )
    assert member.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "crew"}).status_code == 403

    admin = _as("admin")
    rows = AsyncMock(return_value=[])
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", rows)
    r = admin.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "crews"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "confirm_name_mismatch"
    assert r.json()["detail"]["message"] == 'Type "Crew" to confirm.'  # the display name, never the slug
    rows.assert_not_awaited()
    assert admin.request("DELETE", f"{BASE}/app_1", json={}).status_code == 422

    rm = AsyncMock(return_value={"state": "destroyed"})
    monkeypatch.setattr(svc, "remove", rm)
    r = admin.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "crew"})
    assert r.status_code == 200 and r.json() == {"state": "destroyed"}
    assert rm.await_args.args == ("org_a", "app_1")
    assert rm.await_args.kwargs == {"confirm_name": "crew", "user_id": "user_admin"}


def test_creator_or_admin_manages_and_no_one_else(monkeypatch):
    """R73: PATCH, invites, publish and DELETE are the creator's or an org admin's."""
    record = _record("app_1", "crew", "r", "room", created_by="user_member", actor_kind="person")
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(side_effect=lambda a, **_: record if a == "app_1" else None))
    calls = {n: AsyncMock(return_value={"ok": n}) for n in ("update_settings", "invite", "member_publish", "remove")}
    for n, m in calls.items():
        monkeypatch.setattr(svc, n, m)

    def attempts(c):
        return {
            "update_settings": c.patch(f"{BASE}/app_1", json={"budget_usd": 5}),
            "invite": c.post(f"{BASE}/app_1/invites", json={"emails": ["a@x.co"]}),
            "member_publish": c.post(f"{BASE}/app_1/publish", json={}),
            "remove": c.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "crew"}),
        }

    for who in ("member", "admin"):  # the creator (a plain member), then an admin who did not create it
        for n, r in attempts(_as(who)).items():
            assert r.status_code == 200 and r.json() == {"ok": n}, (who, n, r.text)
    assert calls["member_publish"].await_args.args == ("org_a", "app_1")
    assert calls["member_publish"].await_args.kwargs == {"user_id": "user_admin"}

    record["created_by"] = "user_someone"
    for n, r in attempts(_as("member")).items():
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed", n
    assert all(m.await_count == 2 for m in calls.values())
    assert _as("admin").post(f"{BASE}/app_zzz/publish", json={}).status_code == 404
    app.dependency_overrides.clear()


def test_member_publish_without_a_ready_preview_is_409(admin, monkeypatch):
    record = _record("app_1", "crew", "r", "room")
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=record))
    monkeypatch.setattr(svc, "_progress", AsyncMock(side_effect=AssertionError("refused before any provider call")))
    r = admin.post(f"{BASE}/app_1/publish", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_ready" and r.json()["detail"]["next"] == "wait for the preview"


def test_list_rows_carry_can_publish_error_and_display_name(monkeypatch):
    records = [
        _record("app_1", "crew-rota", "r", "public", created_by="user_member", error={"code": "x", "message": "boom", "next": "n"}),
        _record("app_2", "theirs", "r", "public", created_by="user_x", publish_error={"code": "b", "message": "pub", "next": "n"}, display_name="Sales Desk"),
        _record("app_3", "quiet", "r", "public"),
    ]  # fmt: skip
    monkeypatch.setattr(svc.apps_repo, "list_for_owner", AsyncMock(return_value=records))
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(return_value=None))
    monkeypatch.setattr(svc.messaging, "human_display_name", AsyncMock(side_effect=lambda u: u))
    monkeypatch.setattr(svc.messaging, "load_agent_roster", AsyncMock(return_value=[]))

    rows = {a["name"]: a for a in _as("member").get(BASE).json()["apps"]}
    assert {n: a["can_publish"] for n, a in rows.items()} == {"crew-rota": True, "theirs": False, "quiet": False}
    assert {n: a["error"] for n, a in rows.items()} == {
        "crew-rota": {"message": "boom"},
        "theirs": {"message": "pub"},
        "quiet": None,
    }
    assert {n: a["display_name"] for n, a in rows.items()} == {
        "crew-rota": "Crew Rota",
        "theirs": "Sales Desk",
        "quiet": "Quiet",
    }
    rows = _as("admin").get(BASE).json()["apps"]
    assert all(a["can_publish"] for a in rows)
    app.dependency_overrides.clear()


def test_issuer_is_public_and_returns_kid(monkeypatch):
    app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "proof-key")
    r = TestClient(app).get(f"{BASE}/issuer")
    assert r.status_code == 200
    assert r.json() == {"keys": [network_proof.public_key()]}
    assert r.json()["keys"][0]["kid"]


# --- open (Task 14) -----------------------------------------------------------------------------

ROOM = "0b6f8d4e-1c2a-4f3b-9d5e-7a8b9c0d1e2f"


def _claims(url: str) -> dict:
    token = url.split("#token=", 1)[1]
    return json.loads(network_proof._unb64(token.split(".")[1]))


@pytest.fixture
def opening(monkeypatch):
    """One room app of org_a with both addresses, a real membership check over fake room rows."""
    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "proof-key")
    record = _record(
        "app_1", "crew", ROOM, "room", live_url="https://crew.apps.test", preview_url="https://crew-preview.apps.test"
    )
    members = {(ROOM, "user_member"): {"role": "member"}}
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(side_effect=lambda a, **_: record if a == "app_1" else None))
    patch = AsyncMock()
    monkeypatch.setattr(svc.apps_repo, "patch", patch)
    monkeypatch.setattr(
        svc.messaging_repo, "get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": "org_a", "name": "Ops"})
    )
    monkeypatch.setattr(svc.messaging_repo, "get_member", AsyncMock(side_effect=lambda r, k, u: members.get((r, u))))
    monkeypatch.setattr(svc.messaging, "human_display_name", AsyncMock(return_value="Mia"))
    return record, members, patch


def test_open_refuses_after_leaving_the_room(member, opening):
    record, members, _ = opening
    r = member.post(f"{BASE}/app_1/open")
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://crew.apps.test/_lightbulb/session#token=")
    claims = _claims(url)
    assert claims["exp"] - claims["iat"] == 900
    assert {k: claims[k] for k in ("sub", "name", "email", "ws", "room", "app")} == {
        "sub": "user_member",
        "name": "Mia",
        "email": "x@example.com",
        "ws": "org_a",
        "room": ROOM,
        "app": "crew",  # R42: the NAME, which the door compares to LIGHTBULB_APP
    }

    del members[(ROOM, "user_member")]
    r = member.post(f"{BASE}/app_1/open")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_member"
    record["open_to"] = "invited"  # R35: invited still means the room, plus outsiders by link
    assert member.post(f"{BASE}/app_1/open").status_code == 403
    record["open_to"] = "workspace"
    assert member.post(f"{BASE}/app_1/open").status_code == 200


def test_open_public_mode_returns_plain_url(member, opening, monkeypatch):
    record, _, _ = opening
    record["open_to"] = "public"
    monkeypatch.setattr(svc.messaging_repo, "get_member", AsyncMock(side_effect=AssertionError("no check")))
    r = member.post(f"{BASE}/app_1/open")
    assert r.status_code == 200 and r.json() == {"url": "https://crew.apps.test"}


def test_open_sets_last_opened_at(member, opening):
    record, _, patch = opening
    r = member.post(f"{BASE}/app_1/open?target=preview")
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://crew-preview.apps.test/_lightbulb/session#token=")
    assert patch.await_args.args[0] == "app_1" and set(patch.await_args.args[1]) == {"last_opened_at"}

    record["live_url"] = None
    r = member.post(f"{BASE}/app_1/open")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_live"
    assert member.post(f"{BASE}/app_1/open?target=other").status_code == 422
    assert member.post(f"{BASE}/app_2/open").status_code == 404
    record["owner_id"] = "org_b"
    assert member.post(f"{BASE}/app_1/open?target=preview").status_code == 404


# --- invites (Task 14) --------------------------------------------------------------------------


@pytest.fixture
def invites(monkeypatch):
    """A moto apps table holding two apps of org_a; mail is captured, provider reads stubbed."""
    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "proof-key")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-apps",
            KeySchema=[{"AttributeName": "app_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("app_id", "owner_id", "name")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner-index",
                    "KeySchema": [
                        {"AttributeName": "owner_id", "KeyType": "HASH"},
                        {"AttributeName": "name", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for app_id, name in (("app_a", "alpha"), ("app_b", "beta")):
            table.put_item(Item=_record(app_id, name, ROOM, "room", live_url=f"https://{name}.apps.test"))
        sent = []

        async def send(**kw):
            sent.append(kw)
            return "msg_1"

        monkeypatch.setattr(svc.email_service, "send_email", send)
        monkeypatch.setattr(svc, "status", AsyncMock(return_value={}))
        with mpatch("core.dynamodb._table_prefix", "test-"), mpatch("core.dynamodb._dynamodb_resource", resource):
            yield table, sent


def _token(mail: dict) -> str:
    return mail["text"].split("/_lightbulb/invite#t=", 1)[1].split()[0]


def test_invites_require_admin_and_set_open_to_invited(member, invites):
    table, sent = invites
    assert member.post(f"{BASE}/app_a/invites", json={"emails": ["a@x.co"]}).status_code == 403

    admin = _as("admin")
    for bad in ({"emails": []}, {"emails": ["not-an-email"]}, {"emails": [f"u{i}@x.co" for i in range(21)]}, {}):
        assert admin.post(f"{BASE}/app_a/invites", json=bad).status_code == 422, bad
    # A room app stays a room app (its room keeps it; the links work too). An only-me app becomes "invited".
    r = admin.post(f"{BASE}/app_b/invites", json={"emails": ["dee@x.co"]})
    assert r.status_code == 200 and r.json() == {"invited": 1, "open_to": "room"}
    sent.clear()
    table.update_item(
        Key={"app_id": "app_a"}, UpdateExpression="SET open_to = :m", ExpressionAttributeValues={":m": "me"}
    )
    r = admin.post(f"{BASE}/app_a/invites", json={"emails": ["Bo@X.co", "cy@x.co"]})
    assert r.status_code == 200 and r.json() == {"invited": 2, "open_to": "invited"}
    assert table.get_item(Key={"app_id": "app_a"})["Item"]["open_to"] == "invited"
    assert [m["to"] for m in sent] == ["bo@x.co", "cy@x.co"]
    assert all(m["kind"] == "apps_invite" and "https://alpha.apps.test/_lightbulb/invite#t=" in m["html"] for m in sent)
    # only the token's hash is stored
    stored = [i for i in table.scan()["Items"] if i["app_id"].startswith("INVITE#")]
    assert len(stored) == 3 and all(_token(m) not in str(stored) for m in sent)
    assert admin.post(f"{BASE}/app_zzz/invites", json={"emails": ["a@x.co"]}).status_code == 404


def test_invites_to_a_public_app_send_the_plain_url(invites):
    """R48: a public app needs no pass, so nothing is stored and the mail carries no token."""
    table, sent = invites
    table.update_item(
        Key={"app_id": "app_b"}, UpdateExpression="SET open_to = :p", ExpressionAttributeValues={":p": "public"}
    )
    r = _as("admin").post(f"{BASE}/app_b/invites", json={"emails": ["a@x.co"]})
    assert r.status_code == 200 and r.json() == {"invited": 1, "open_to": "public"}
    assert table.get_item(Key={"app_id": "app_b"})["Item"]["open_to"] == "public"
    assert not [i for i in table.scan()["Items"] if i["app_id"].startswith("INVITE#")]
    (mail,) = sent
    assert "#t=" not in mail["html"] + mail["text"] and "https://beta.apps.test" in mail["text"]


def test_invite_token_works_once_and_expires(invites):
    table, sent = invites
    admin = _as("admin")
    admin.post(f"{BASE}/app_a/invites", json={"emails": ["bo@x.co", "cy@x.co"]})
    app.dependency_overrides.clear()
    anyone = TestClient(app)
    first, second = (_token(m) for m in sent)

    r = anyone.get(f"{BASE}/invite/{first}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    claims = json.loads(network_proof._unb64(r.json()["token"].split(".")[1]))
    assert {k: claims[k] for k in ("sub", "name", "email", "ws", "room", "app")} == {
        "sub": "invite:bo@x.co",
        "name": "bo@x.co",
        "email": "bo@x.co",
        "ws": "org_a",
        "room": "",
        "app": "alpha",
    }
    assert claims["exp"] - claims["iat"] == 900
    assert network_proof.verify(r.json()["token"], "invite:bo@x.co") is None

    used = anyone.get(f"{BASE}/invite/{first}")
    for item in table.scan()["Items"]:
        if item["app_id"].startswith("INVITE#") and item["email"] == "cy@x.co":
            table.update_item(
                Key={"app_id": item["app_id"]},
                UpdateExpression="SET expires_at = :e",
                ExpressionAttributeValues={":e": "2020-01-01T00:00:00+00:00"},
            )
    expired = anyone.get(f"{BASE}/invite/{second}")
    invalid = anyone.get(f"{BASE}/invite/{'x' * 43}")
    assert used.status_code == expired.status_code == invalid.status_code == 404
    assert used.json() == expired.json() == invalid.json()
    assert used.json()["detail"]["code"] == "invite_invalid"


def test_invite_pass_is_scoped_to_one_app(invites):
    table, sent = invites
    admin = _as("admin")
    admin.post(f"{BASE}/app_a/invites", json={"emails": ["bo@x.co"]})
    admin.post(f"{BASE}/app_b/invites", json={"emails": ["bo@x.co"]})
    app.dependency_overrides.clear()
    anyone = TestClient(app)
    a, b = (anyone.get(f"{BASE}/invite/{_token(m)}").json()["token"] for m in sent)
    assert json.loads(network_proof._unb64(a.split(".")[1]))["app"] == "alpha"
    assert json.loads(network_proof._unb64(b.split(".")[1]))["app"] == "beta"

    # R59: a room app switched back keeps its links until expiry; a public one (no pass) stops honouring them
    admin = _as("admin")
    admin.post(f"{BASE}/app_a/invites", json={"emails": ["dee@x.co"]})
    table.update_item(
        Key={"app_id": "app_a"}, UpdateExpression="SET open_to = :p", ExpressionAttributeValues={":p": "public"}
    )
    app.dependency_overrides.clear()
    assert TestClient(app).get(f"{BASE}/invite/{_token(sent[-1])}").status_code == 404


def test_inviting_to_a_workspace_app_keeps_workspace_and_links_work(invites):
    """R59: invites are additive; only a room app becomes "invited"."""
    table, sent = invites
    table.update_item(
        Key={"app_id": "app_b"}, UpdateExpression="SET open_to = :p", ExpressionAttributeValues={":p": "workspace"}
    )
    r = _as("admin").post(f"{BASE}/app_b/invites", json={"emails": ["a@x.co"]})
    assert r.status_code == 200 and r.json() == {"invited": 1, "open_to": "workspace"}
    assert table.get_item(Key={"app_id": "app_b"})["Item"]["open_to"] == "workspace"
    app.dependency_overrides.clear()
    assert TestClient(app).get(f"{BASE}/invite/{_token(sent[-1])}").status_code == 200


def test_invite_swap_storage_down_is_a_503_never_cached():
    from botocore.exceptions import EndpointConnectionError

    down = AsyncMock(side_effect=EndpointConnectionError(endpoint_url="https://dynamodb"))
    with mpatch.object(mod.svc, "swap_invite", down):
        r = TestClient(app).get(f"{BASE}/invite/{'x' * 43}")
    assert r.status_code == 503 and r.headers["cache-control"] == "no-store"


def test_remove_accepts_the_display_name_or_the_slug(admin, monkeypatch):
    record = {"app_id": "app_1", "owner_id": "org_a", "name": "crew-rota", "display_name": "Night Crew"}
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=record))
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc.apps_dns, "remove", AsyncMock())
    monkeypatch.setattr(svc.apps_source, "delete_all", AsyncMock())
    monkeypatch.setattr(svc.integration_secret_store, "delete", AsyncMock())
    patch = AsyncMock()
    monkeypatch.setattr(svc.apps_repo, "patch", patch)
    r = admin.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "Crew Rota"})  # the old title, no longer it
    assert r.status_code == 409
    patch.assert_not_awaited()
    for typed in ("crew-rota", " night crew "):
        assert admin.request("DELETE", f"{BASE}/app_1", json={"confirm_name": typed}).status_code == 200
    assert patch.await_count == 2


# --- member routes refuse a non-manager and another workspace's app -------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "rollback", {"version": 1}),
        ("POST", "ask-agent", {}),
        ("POST", "ask", {"text": "make it blue"}),
        ("GET", "invites", None),
        ("DELETE", "invites", {"email": "a@x.co"}),
    ],
)
def test_new_member_routes_are_manager_gated(monkeypatch, method, path, body):
    records = {
        "mine": _record("mine", "crew", ROOM, "room", created_by="user_founder"),
        "foreign": {**_record("foreign", "other", ROOM, "room"), "owner_id": "org_b"},
    }
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(side_effect=lambda a, **_: records.get(a)))
    for name in ("rollback", "ask_agent_to_fix", "list_invites", "revoke_invite"):
        monkeypatch.setattr(svc, name, AsyncMock(side_effect=AssertionError("the gate comes first")))
    monkeypatch.setattr(mod.app_conversation, "ask_as_member", AsyncMock(side_effect=AssertionError("gate first")))
    try:
        r = _as("member").request(method, f"{BASE}/mine/{path}", json=body)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "not_allowed"
        r = _as("admin").request(method, f"{BASE}/foreign/{path}", json=body)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "app_not_found"
    finally:
        app.dependency_overrides.clear()


# --- put back the previous version ----------------------------------------------------------------


def test_member_rollback_calls_rollback_for_the_creator(monkeypatch):
    record = _record("app_1", "crew", ROOM, "room", created_by="user_member")
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=record))
    rollback = AsyncMock(return_value={"state": "live"})
    monkeypatch.setattr(svc, "rollback", rollback)
    r = _as("member").post(f"{BASE}/app_1/rollback", json={"version": 2})
    app.dependency_overrides.clear()
    assert r.status_code == 200
    assert rollback.await_args.args == ("org_a", "app_1") and rollback.await_args.kwargs == {"version": 2}


def test_list_rows_name_the_previous_version_and_a_failed_publish(admin, monkeypatch):
    versions = [{"n": n, "commit": "c", "deployment_id": f"d{n}", "at": f"2026-10-0{n}T00:00:00+00:00", "by": "a"} for n in (1, 2, 3)]  # fmt: skip
    records = [
        _record("app_1", "three", "r", "public", versions=versions, live_version=3),
        _record("app_2", "backed", "r", "public", versions=versions, live_version=2, publish_error={"message": "no"}),
        _record("app_3", "first", "r", "public", versions=versions[:1], live_version=1),
        _record("app_4", "preview", "r", "public", versions=[]),
    ]
    monkeypatch.setattr(svc.apps_repo, "list_for_owner", AsyncMock(return_value=records))
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(return_value=None))
    monkeypatch.setattr(svc.messaging, "human_display_name", AsyncMock(side_effect=lambda u: u))
    monkeypatch.setattr(svc.messaging, "load_agent_roster", AsyncMock(return_value=[]))
    rows = {a["name"]: a for a in admin.get(BASE).json()["apps"]}
    # No stamp on these older versions: the record can't say the door matches, so going back rebuilds.
    assert rows["three"]["previous_version"] == {"n": 2, "at": "2026-10-02T00:00:00+00:00", "rebuild": True}
    assert rows["backed"]["previous_version"]["n"] == 1
    assert rows["first"]["previous_version"] is None and rows["preview"]["previous_version"] is None
    assert rows["backed"]["publish_failed"] is True and rows["three"]["publish_failed"] is False
    assert "versions" not in rows["three"]  # deployment ids stay off Canvas


def _rows_of(admin, monkeypatch, records):
    monkeypatch.setattr(svc.apps_repo, "list_for_owner", AsyncMock(return_value=records))
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(return_value=None))
    monkeypatch.setattr(svc.messaging, "human_display_name", AsyncMock(side_effect=lambda u: u))
    monkeypatch.setattr(svc.messaging, "load_agent_roster", AsyncMock(return_value=[]))
    return {a["name"]: a for a in admin.get(BASE).json()["apps"]}


def test_list_rows_follow_the_version_history_and_say_when_going_back_rebuilds(admin, monkeypatch):
    v = lambda n, **kw: {"n": n, "commit": "c", "deployment_id": f"d{n}", "at": f"t{n}", "by": "a", **kw}  # noqa: E731
    versions = [v(1, open_to="public"), v(2, open_to="room", replaced=1), v(3, open_to="room", replaced=2), v(4, open_to="room", replaced=2)]  # fmt: skip
    rows = _rows_of(
        admin,
        monkeypatch,
        [
            _record("app_1", "fled", "r", "room", versions=versions, live_version=4),
            _record("app_2", "door", "r", "room", versions=versions, live_version=2),
        ],
    )
    assert rows["fled"]["previous_version"] == {"n": 2, "at": "t2", "rebuild": False}  # not the v3 they fled
    assert rows["door"]["previous_version"] == {"n": 1, "at": "t1", "rebuild": True}


def test_list_rows_put_a_failed_publish_before_an_older_error(admin, monkeypatch):
    rows = _rows_of(
        admin,
        monkeypatch,
        [_record("app_1", "both", "r", "room", error={"message": "old preview"}, publish_error={"message": "new publish"})],
    )  # fmt: skip
    assert rows["both"]["error"] == {"message": "new publish"}


def test_list_rows_say_whether_a_preview_is_newer_and_a_publish_is_running(admin, monkeypatch):
    v = {"n": 1, "commit": "c", "deployment_id": "d1", "at": "t1", "by": "a", "source_id": "s1"}
    ready = {"deployment_id": "p", "commit": "c", "ready_at": "t", "source_id": "s2"}
    same = {**ready, "source_id": "s1"}
    rows = _rows_of(
        admin,
        monkeypatch,
        [
            _record("a1", "newer", "r", "room", versions=[v], live_version=1, live_source_id="s1", preview=ready),
            _record("a2", "same", "r", "room", versions=[v], live_version=1, live_source_id="s1", preview=same),
            _record("a3", "backed", "r", "room", versions=[v], live_version=1, live_source_id=None, preview=ready),
            _record("a4", "backsame", "r", "room", versions=[v], live_version=1, live_source_id=None, preview=same),
            _record("a5", "none", "r", "room", versions=[v], live_version=1, publish_pending={"deployment_id": "x"}),
        ],
    )
    assert {k: r["preview_newer"] for k, r in rows.items()} == {
        "newer": True, "same": False, "backed": True, "backsame": False, "none": False,
    }  # fmt: skip
    assert rows["none"]["publishing"] is True and rows["newer"]["publishing"] is False


# --- ask the agent to fix it ----------------------------------------------------------------------


AGENT_A, AGENT_B = "00000000-0000-4000-8000-00000000000a", "00000000-0000-4000-8000-00000000000b"


@pytest.fixture
def asking(monkeypatch):
    """A failed app built by agent B, in a real room path: membership over fake rows, the message captured."""
    record = _record(
        "app_1", "crew-rota", ROOM, "room", actor_id=AGENT_B, publish_error={"message": "Build @everyone postgres://u:p@db/x"}
    )  # fmt: skip
    state = SimpleNamespace(
        record=record,
        humans={"user_admin"},
        agents=[
            {"kind": "agent", "id": AGENT_A, "mentionHandle": "ada", "listenMode": "active"},
            {"kind": "agent", "id": AGENT_B, "mentionHandle": "bob", "listenMode": "active"},
            {"kind": "human", "id": "user_admin", "role": "member"},
        ],
        posted=[],
        workflows=[],
    )
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(side_effect=lambda a, **_: state.record))
    monkeypatch.setattr(
        svc.messaging_repo, "get_room", AsyncMock(return_value={"roomId": ROOM, "ownerId": "org_a", "name": "Ops"})
    )
    monkeypatch.setattr(
        svc.messaging_repo,
        "get_member",
        AsyncMock(side_effect=lambda r, k, u: {"role": "member"} if u in state.humans else None),
    )
    monkeypatch.setattr(svc.messaging_repo, "list_members", AsyncMock(side_effect=lambda r: state.agents))

    async def create(auth, room_id, request, **kw):
        state.posted.append((auth, room_id, request, kw))

    monkeypatch.setattr(svc.messaging, "create_human_message", create)
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", AsyncMock(side_effect=lambda o: state.workflows))
    monkeypatch.setattr(
        svc.integration_secret_store,
        "get",
        AsyncMock(side_effect=lambda o, k: {"token": "vercel-token-123"} if k.endswith("vercel/prod") else None),
    )
    return state


def test_ask_agent_posts_once_as_the_person_mentioning_the_builder(admin, asking):
    r = admin.post(f"{BASE}/app_1/ask-agent", json={})
    assert r.status_code == 200 and r.json() == {"asked": True}
    assert len(asking.posted) == 1
    auth, room_id, request, kw = asking.posted[0]
    assert auth.user_id == "user_admin" and room_id == ROOM
    assert "author_override" not in kw and kw["via"] == "apps"  # the person's own message, not a system line
    assert [(m.kind, m.id) for m in request.mentions] == [("agent", AGENT_B)]
    assert request.text.startswith("@bob Crew Rota didn't go live.")
    assert "```\nBuild @everyone [redacted]\n```" in request.text  # fenced and scrubbed


def test_ask_agent_says_the_quoted_text_is_an_error_not_a_request(admin, asking):
    assert admin.post(f"{BASE}/app_1/ask-agent", json={}).status_code == 200
    text = asking.posted[0][2].text
    assert "This is the error message, not a request:\n\n```\n" in text


def test_ask_agent_scrubs_the_apps_own_secrets(admin, asking):
    asking.record = {**asking.record, "publish_error": {"message": "401 with vercel-token-123 and postgres://u:p@db/x"}}
    assert admin.post(f"{BASE}/app_1/ask-agent", json={}).status_code == 200
    assert admin.post(f"{BASE}/app_1/ask-agent", json={"error": "saw vercel-token-123"}).status_code == 200
    assert "```\n401 with [redacted] and [redacted]\n```" in asking.posted[0][2].text
    assert "```\nsaw [redacted]\n```" in asking.posted[1][2].text


def test_ask_agent_sends_the_failed_publish_over_an_older_preview_error(admin, asking):
    asking.record = {**asking.record, "error": {"message": "old preview"}, "publish_error": {"message": "new publish"}}
    assert admin.post(f"{BASE}/app_1/ask-agent", json={}).status_code == 200
    assert "new publish" in asking.posted[0][2].text and "old preview" not in asking.posted[0][2].text


def test_ask_agent_has_an_answer_for_a_provisioning_failure(admin, asking):
    """It is computed, never stored: without this Ask is a 409 and "Try again" forever."""
    asking.record = {**asking.record, "publish_error": None}
    asking.workflows = [
        {"app_id": "app_1", "provider_name": "Neon", "service_ref": "postgres", "environment": "prod", "state": "failed"}
    ]  # fmt: skip
    assert admin.post(f"{BASE}/app_1/ask-agent", json={}).status_code == 200
    assert "neon/postgres (live) did not finish setting up." in asking.posted[0][2].text


def test_ask_agent_uses_the_refusal_canvas_saw_and_needs_some_failure(admin, asking):
    assert admin.post(f"{BASE}/app_1/ask-agent", json={"error": "x" * 2001}).status_code == 422
    assert admin.post(f"{BASE}/app_1/ask-agent", json={"error": "Refused ``` now"}).status_code == 200
    assert "Refused ''' now" in asking.posted[0][2].text
    asking.record = {**asking.record, "publish_error": None}
    asking.posted.clear()
    r = admin.post(f"{BASE}/app_1/ask-agent", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nothing_failed"
    assert asking.posted == []


def test_ask_agent_for_a_stopped_app_says_only_where_it_stopped(admin, asking):
    asking.record = {
        **asking.record,
        "publish_error": None,
        "stuck": {"at": "2026-11-03T00:00:00Z", "step": "building"},
    }
    assert admin.post(f"{BASE}/app_1/ask-agent", json={}).status_code == 200
    request = asking.posted[0][2]
    assert request.text == "@bob Crew Rota stopped at building. Can you pick it up again and post here?"
    assert [m.id for m in request.mentions] == [AGENT_B]


def test_ask_agent_falls_back_when_the_builder_cannot_hear(admin, asking):
    asking.agents = [a for a in asking.agents if a["id"] != AGENT_B]  # left the room
    assert admin.post(f"{BASE}/app_1/ask-agent", json={}).status_code == 200
    assert [m.id for m in asking.posted[0][2].mentions] == [AGENT_A]
    asking.agents = [{"kind": "agent", "id": AGENT_A, "mentionHandle": "ada", "listenMode": "observe"}]
    r = admin.post(f"{BASE}/app_1/ask-agent", json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_agent"
    assert len(asking.posted) == 1


def test_ask_agent_refuses_a_manager_outside_the_room(admin, asking):
    asking.humans.clear()  # an org admin who is not in the room can't post as themselves there
    r = admin.post(f"{BASE}/app_1/ask-agent", json={})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_member"
    assert "Ops" not in r.text and asking.posted == []


# --- the invited people list ----------------------------------------------------------------------


def test_list_and_revoke_invites(invites):
    table, sent = invites
    admin = _as("admin")
    assert admin.post(f"{BASE}/app_a/invites", json={"emails": ["bo@x.co", "cy@x.co"]}).status_code == 200
    assert admin.post(f"{BASE}/app_a/invites", json={"emails": ["bo@x.co"]}).status_code == 200  # a second link
    assert admin.post(f"{BASE}/app_b/invites", json={"emails": ["bo@x.co"]}).status_code == 200  # another app
    table.put_item(
        Item={"app_id": "INVITE#old", "kind": "invite", "target_app_id": "app_a", "invite_owner_id": "org_a",
              "email": "old@x.co", "expires_at": "2020-01-01T00:00:00+00:00"}
    )  # fmt: skip
    # Another workspace's row naming this app: never listed, never revoked.
    table.put_item(
        Item={"app_id": "INVITE#foreign", "kind": "invite", "target_app_id": "app_a", "invite_owner_id": "org_other",
              "email": "bo@x.co", "expires_at": "2099-01-01T00:00:00+00:00"}
    )  # fmt: skip
    table.put_item(
        Item={"app_id": "INVITE#foreign2", "kind": "invite", "target_app_id": "app_a", "invite_owner_id": "org_other",
              "email": "zed@x.co", "expires_at": "2099-01-01T00:00:00+00:00"}
    )  # fmt: skip
    r = admin.get(f"{BASE}/app_a/invites")
    assert r.status_code == 200 and r.json() == {"emails": ["bo@x.co", "cy@x.co"]}  # no hashes, no expired
    assert "INVITE#" not in r.text

    r = admin.request("DELETE", f"{BASE}/app_a/invites", json={"email": "BO@x.co"})
    assert r.status_code == 200 and r.json() == {"revoked": 2}
    assert admin.get(f"{BASE}/app_a/invites").json() == {"emails": ["cy@x.co"]}
    assert admin.get(f"{BASE}/app_b/invites").json() == {"emails": ["bo@x.co"]}  # this app's links only
    assert admin.request("DELETE", f"{BASE}/app_a/invites", json={"email": "not-an-email"}).status_code == 422
    assert admin.request("DELETE", f"{BASE}/app_a/invites", json={"email": "no@x.co"}).json() == {"revoked": 0}
    assert table.get_item(Key={"app_id": "INVITE#foreign"}).get("Item")  # same email, another owner: left alone

    bo_a, cy, bo_a2, bo_b = (_token(m) for m in sent)
    app.dependency_overrides.clear()
    anyone = TestClient(app)
    assert anyone.get(f"{BASE}/invite/{bo_a}").status_code == 404  # revoked: refused like one never issued
    assert anyone.get(f"{BASE}/invite/{bo_a2}").status_code == 404
    assert anyone.get(f"{BASE}/invite/{cy}").status_code == 200
    assert anyone.get(f"{BASE}/invite/{bo_b}").status_code == 200


async def test_invite_scan_reads_consistently(monkeypatch):
    table = MagicMock()
    table.scan.return_value = {"Items": []}
    monkeypatch.setattr(svc.apps_repo, "_table", lambda: table)
    await svc.apps_repo.list_invites("app_a")
    assert table.scan.call_args.kwargs["ConsistentRead"] is True  # a revoke must not reappear on the next read


def test_invite_email_names_the_app_by_its_display_name_escaped(invites):
    table, sent = invites
    table.update_item(
        Key={"app_id": "app_a"},
        UpdateExpression="SET display_name = :d",
        ExpressionAttributeValues={":d": "Sign-up <b>now</b>"},
    )
    _as("admin").post(f"{BASE}/app_a/invites", json={"emails": ["bo@x.co"]})
    app.dependency_overrides.clear()
    assert sent[0]["subject"] == "You're invited to Sign-up <b>now</b>"
    assert "Sign-up &lt;b&gt;now&lt;/b&gt;" in sent[0]["html"] and "<b>" not in sent[0]["html"]
    assert "alpha" not in sent[0]["text"].split("https://")[0]  # the slug stays in the address


def test_conversation_opens_for_any_signed_in_member_and_the_service_decides(member, monkeypatch):
    opened = AsyncMock(return_value={"room_id": "c1"})
    monkeypatch.setattr(mod.app_conversation, "open_conversation", opened)
    assert member.post(f"{BASE}/app_1/conversation").json() == {"room_id": "c1"}
    assert opened.await_args.args[0].user_id == "user_member" and opened.await_args.args[1] == "app_1"
    opened.side_effect = svc.AppsRefused("not_a_member", "no")
    r = member.post(f"{BASE}/app_1/conversation")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_a_member"


def test_any_member_starts_an_app_from_canvas(monkeypatch):
    start = AsyncMock(
        return_value={"app_id": "app_9", "conversation_id": "c9", "canvas_url": "u", "next": "", "relayed": True}
    )
    monkeypatch.setattr(mod.app_conversation, "start_as_member", start)
    try:
        r = _as("member").post(f"{BASE}/start", json={"request": "a booking page"})
        assert r.status_code == 200 and r.json()["app_id"] == "app_9"
        assert start.await_args.args[1] == "a booking page"
        assert _as("member").post(f"{BASE}/start", json={"request": ""}).status_code == 422
    finally:
        app.dependency_overrides.clear()
