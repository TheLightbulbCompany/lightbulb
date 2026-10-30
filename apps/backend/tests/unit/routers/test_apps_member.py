"""Member routes (/api/v1/apps): Canvas's list, the admin's settings and typed-name removal, the issuer key."""

import os
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

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
        "inv": True,
        "inv-out": False,
        "ws": True,
        "pub": True,
    }
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


def test_patch_requires_org_admin(admin, member, monkeypatch, paid):
    upd = AsyncMock(return_value={"app_id": "app_1", "open_to": "public"})
    monkeypatch.setattr(svc, "update_settings", upd)
    assert member.patch(f"{BASE}/app_1", json={"open_to": "public"}).status_code == 403
    upd.assert_not_awaited()

    admin = _as("admin")
    r = admin.patch(f"{BASE}/app_1", json={"open_to": "public", "budget_usd": 50})
    assert r.status_code == 200 and r.json()["open_to"] == "public"
    assert upd.await_args.args == ("org_a", "app_1")
    assert upd.await_args.kwargs == {"open_to": "public", "budget_usd": 50}
    paid.assert_awaited_with("org_a", message=mod.PAID_MESSAGE)

    for bad in ({"open_to": "everyone"}, {"budget_usd": -1}, {"budget_usd": "ten"}):
        assert admin.patch(f"{BASE}/app_1", json=bad).status_code == 422, bad


def test_delete_requires_typed_name_and_removes_resources(member, monkeypatch):
    assert member.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "crew"}).status_code == 403

    admin = _as("admin")
    monkeypatch.setattr(
        svc.apps_repo, "get", AsyncMock(return_value={"app_id": "app_1", "owner_id": "org_a", "name": "crew"})
    )
    rows = AsyncMock(return_value=[])
    monkeypatch.setattr(svc.wf_repo, "list_for_owner", rows)
    r = admin.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "Crew"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "confirm_name_mismatch"
    rows.assert_not_awaited()
    assert admin.request("DELETE", f"{BASE}/app_1", json={}).status_code == 422

    rm = AsyncMock(return_value={"state": "destroyed"})
    monkeypatch.setattr(svc, "remove", rm)
    r = admin.request("DELETE", f"{BASE}/app_1", json={"confirm_name": "crew"})
    assert r.status_code == 200 and r.json() == {"state": "destroyed"}
    assert rm.await_args.args == ("org_a", "app_1")
    assert rm.await_args.kwargs == {"confirm_name": "crew", "user_id": "user_admin"}


def test_issuer_is_public_and_returns_kid(monkeypatch):
    app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "proof-key")
    r = TestClient(app).get(f"{BASE}/issuer")
    assert r.status_code == 200
    assert r.json() == {"keys": [network_proof.public_key()]}
    assert r.json()["keys"][0]["kid"]
