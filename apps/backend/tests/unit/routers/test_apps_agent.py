"""Builder routes (/api/v1/apps/build): a workspace agent's hook token or a signed-in computer's proof."""

import io
import json
import os
import tarfile
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext  # noqa: E402
from core.services import apps_service as svc  # noqa: E402
from core.services import network_proof, vercel_client  # noqa: E402
from core.services.stripe_provisioning import ProvisioningError  # noqa: E402
from main import app  # noqa: E402
from routers import apps as mod  # noqa: E402

BASE = "/api/v1/apps/build"
HOOK = {"Authorization": "Bearer hook-token", "X-Lightbulb-Owner": "org_a", "X-Lightbulb-Agent-Id": "agt_1"}
PROOF = {"X-Lightbulb-Proof": "p", "X-Lightbulb-Challenge": "c", "X-Lightbulb-Sig": "s"}
UP_BODY = {
    "name": "crew",
    "room_id": "room_1",
    "services": ["database"],
    "open_to": "room",
    "budget_usd": 20,
    "source_id": "src_1",
    "origin": {"kind": "idea", "ref": "i1"},
}
UP_RESULT = {
    "app_id": "app_1",
    "state": "awaiting_approval",
    "cards": [{"workflow_id": "pwf_1", "provider": "Vercel", "service": "project", "state": "awaiting_approval"}],
    "canvas_url": "https://web.test/chat?panel=canvas&app=app_1",
    "sample_data": True,
}
STATUS = {
    "app_id": "app_1",
    "name": "crew",
    "room_id": "room_1",
    "state": "ready",
    "open_to": "room",
    "budget_usd": 20,
    "preview_url": "https://crew-preview.apps.example.com",
    "live_url": None,
    "canvas_url": "https://web.test/chat?panel=canvas&app=app_1",
    "live_version": None,
    "origin": {"kind": "idea", "ref": "i1"},
    "versions": [],
    "resources": [
        {"provider": "Vercel", "service": "project", "state": "active", "resource_id": "r1", "environment": "prod"}
    ],
    "last_opened_at": None,
    "spend_month_cents": None,
    "preview": {"deployment_id": "dpl_1", "commit": "c1", "ready_at": "2026-10-29T00:00:00Z"},
}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    return TestClient(app)


@pytest.fixture(autouse=True)
def hook(monkeypatch):
    m = AsyncMock(return_value={"owner_id": "org_a"})
    monkeypatch.setattr(mod, "_require_hook_token", m)
    return m


@pytest.fixture(autouse=True)
def paid(monkeypatch):
    m = AsyncMock()
    monkeypatch.setattr(mod, "require_always_on_entitlement", m)
    return m


@pytest.fixture
def proof(monkeypatch):
    m = AsyncMock(return_value=(AuthContext(user_id="user_1", org_id="org_a", org_role="org:member"), "label"))
    monkeypatch.setattr(mod.network_login, "computer_identity", m)
    monkeypatch.setattr(mod.network_login, "signed_proof", lambda request: ("key", "challenge", {"sub": "user_1"}))
    return m


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(text.encode())
            tf.addfile(info, io.BytesIO(text.encode()))
    return buf.getvalue()


def test_build_routes_accept_hook_token_or_computer_proof_and_nothing_else(client, hook, paid, proof, monkeypatch):
    up = AsyncMock(return_value=UP_RESULT)
    monkeypatch.setattr(svc, "up", up)

    assert client.post(f"{BASE}/up", json=UP_BODY).status_code == 401
    up.assert_not_awaited()

    assert client.post(f"{BASE}/up", json=UP_BODY, headers=HOOK).status_code == 200
    hook.assert_awaited_once()
    assert hook.await_args.args[0] == "org_a"
    assert up.await_args.kwargs["actor"] == svc.BuilderCaller("org_a", "agt_1", "agent")
    paid.assert_awaited_with("org_a", message=mod.PAID_MESSAGE)

    no_agent = {k: v for k, v in HOOK.items() if k != "X-Lightbulb-Agent-Id"}
    assert client.post(f"{BASE}/up", json=UP_BODY, headers=no_agent).status_code == 200
    assert up.await_args.kwargs["actor"] == svc.BuilderCaller("org_a", "agent", "agent")

    with_run = {**HOOK, "X-Lightbulb-Run": "run_1"}
    assert client.post(f"{BASE}/up", json=UP_BODY, headers=with_run).status_code == 200
    assert up.await_args.kwargs["actor"] == svc.BuilderCaller("org_a", "agt_1", "agent", run_id="run_1")
    bad_run = {**HOOK, "X-Lightbulb-Run": "a b"}  # a malformed run id links nothing; the call still works
    assert client.post(f"{BASE}/up", json=UP_BODY, headers=bad_run).status_code == 200
    assert up.await_args.kwargs["actor"] == svc.BuilderCaller("org_a", "agt_1", "agent")

    bad_agent = {**HOOK, "X-Lightbulb-Agent-Id": "a b\n"}
    assert client.post(f"{BASE}/up", json=UP_BODY, headers=bad_agent).status_code == 422

    assert client.post(f"{BASE}/up", json=UP_BODY, headers=PROOF).status_code == 200
    proof.assert_awaited_once()
    assert up.await_args.args[0] == "org_a"
    assert up.await_args.kwargs["actor"] == svc.BuilderCaller("org_a", "user_1", "person")

    hook.side_effect = HTTPException(status_code=401, detail="invalid hook token")
    calls = up.await_count
    assert client.post(f"{BASE}/up", json=UP_BODY, headers=HOOK).status_code == 401
    assert up.await_count == calls

    paid.side_effect = HTTPException(status_code=403, detail="paid")
    assert client.post(f"{BASE}/up", json=UP_BODY, headers=PROOF).status_code == 403
    assert up.await_count == calls


def test_proof_caller_without_workspace_gets_409(client, proof, monkeypatch):
    proof.return_value = (AuthContext(user_id="user_1"), "label")
    st = AsyncMock(return_value=STATUS)
    monkeypatch.setattr(svc, "status", st)
    r = client.get(f"{BASE}/app_1/status", headers=PROOF)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "workspace_required"
    st.assert_not_awaited()


def test_up_returns_up_result(client, monkeypatch):
    up = AsyncMock(return_value=UP_RESULT)
    monkeypatch.setattr(svc, "up", up)
    r = client.post(f"{BASE}/up", json={**UP_BODY, "issue_id": "iss_1", "cost_cents": 12}, headers=HOOK)
    assert r.status_code == 200 and r.json() == UP_RESULT
    kw = up.await_args.kwargs
    assert (kw["name"], kw["room_id"], kw["services"], kw["open_to"], kw["budget_usd"], kw["source_id"]) == (
        "crew",
        "room_1",
        ["database"],
        "room",
        20,
        "src_1",
    )
    assert kw["origin"] == {"kind": "idea", "ref": "i1"} and kw["issue_id"] == "iss_1" and kw["cost_cents"] == 12
    assert kw["approved_destructive"] is None and kw["requested_by"] is None
    client.post(f"{BASE}/up", json={**UP_BODY, "requested_by": "user_ada"}, headers=HOOK)
    assert up.await_args.kwargs["requested_by"] == "user_ada"


def test_status_shape_matches_contract(client, monkeypatch):
    monkeypatch.setattr(svc, "status", AsyncMock(return_value=STATUS))
    r = client.get(f"{BASE}/app_1/status", headers=HOOK)
    assert r.status_code == 200 and r.json() == STATUS
    monkeypatch.setattr(
        svc, "status", AsyncMock(side_effect=svc.AppsRefused("app_not_found", "That app does not exist."))
    )
    r = client.get(f"{BASE}/app_1/status", headers=HOOK)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "app_not_found"


def test_publish_rollback_destroy_pass_through(client, monkeypatch):
    pub, rb = AsyncMock(return_value={**UP_RESULT, "version": 2}), AsyncMock(return_value=UP_RESULT)
    de = AsyncMock(return_value={"state": "awaiting_owner", "next": "the owner removes it from Canvas"})
    monkeypatch.setattr(svc, "publish", pub)
    monkeypatch.setattr(svc, "rollback", rb)
    monkeypatch.setattr(svc, "destroy", de)
    r = client.post(f"{BASE}/app_1/publish", json={"source_id": "s2", "reason": "fix"}, headers=HOOK)
    assert r.json()["version"] == 2
    assert pub.await_args.args == ("org_a", "app_1") and pub.await_args.kwargs["source_id"] == "s2"
    assert client.post(f"{BASE}/app_1/rollback", json={"version": 1}, headers=HOOK).status_code == 200
    assert rb.await_args.kwargs == {"version": 1, "run_id": None}
    assert (
        client.post(f"{BASE}/app_1/rollback", json={}, headers={**HOOK, "X-Lightbulb-Run": "run_9"}).status_code == 200
    )
    assert rb.await_args.kwargs == {"version": None, "run_id": "run_9"}
    r = client.post(f"{BASE}/app_1/destroy", json={}, headers=HOOK)
    assert r.json()["state"] == "awaiting_owner"


def test_source_upload_and_its_errors(client, monkeypatch):
    put = AsyncMock(return_value={"app_id": "app_1", "source_id": "s1"})
    monkeypatch.setattr(svc, "put_source", put)
    form = {"name": "crew", "room_id": "room_1", "commit": "c1"}
    files = {"archive": ("src.tar.gz", b"tarbytes", "application/gzip")}
    r = client.post(f"{BASE}/source", data=form, files=files, headers=HOOK)
    assert r.status_code == 200 and r.json() == {"app_id": "app_1", "source_id": "s1"}
    assert put.await_args.args == ("org_a",)
    assert put.await_args.kwargs["archive"] == b"tarbytes" and put.await_args.kwargs["commit"] == "c1"
    assert put.await_args.kwargs["placeholder"] is False  # an old CLI sends no field
    client.post(f"{BASE}/source", data={**form, "placeholder": "true"}, files=files, headers=HOOK)
    assert put.await_args.kwargs["placeholder"] is True

    monkeypatch.setattr(mod, "MAX_UPLOAD", 4)
    assert client.post(f"{BASE}/source", data=form, files=files, headers=HOOK).status_code == 413
    monkeypatch.setattr(mod, "MAX_UPLOAD", 1 << 30)

    for exc, status, code in (
        (svc.apps_repo.NameTaken("crew"), 409, "name_taken"),
        (svc.apps_source.SourceTooLarge("big"), 413, "source_too_large"),
        (svc.apps_source.SourceRefused("bad path"), 422, "source_refused"),
        (svc.AppsRefused("invalid_name", "bad"), 422, "invalid_name"),
    ):
        put.side_effect = exc
        r = client.post(f"{BASE}/source", data=form, files=files, headers=HOOK)
        assert r.status_code == status and r.json()["detail"]["code"] == code


def test_upstream_failures_map_to_502_without_leaking(client, monkeypatch):
    st = AsyncMock(side_effect=vercel_client.VercelError("forbidden", "Vercel said no"))
    monkeypatch.setattr(svc, "status", st)
    r = client.get(f"{BASE}/app_1/status", headers=HOOK)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "vercel_forbidden"
    assert r.json()["detail"]["next"]
    st.side_effect = svc.apps_db.AppsDbError("db_error", "UndefinedTable (42P01)")
    assert client.get(f"{BASE}/app_1/status", headers=HOOK).status_code == 502
    st.side_effect = ProvisioningError("resource_missing", "gone")
    r = client.get(f"{BASE}/app_1/status", headers=HOOK)
    assert r.status_code == 502 and r.json()["detail"]["code"] == "stripe_error"


def test_destructive_source_needs_an_approved_card(client, monkeypatch):
    """Through the real service: the backend reads the uploaded migrations, not the CLI's word."""
    record = {
        "app_id": "app_1",
        "owner_id": "org_a",
        "name": "crew",
        "room_id": "room_1",
        "state": "ready",
        "open_to": "room",
        "budget_usd": 20,
        "sources": {"s2": {"commit": "c2", "at": "2026-10-29T00:00:00Z"}},
        "preview_want": "s1",
        "published_migrations": ["0000_init.sql"],
    }
    tar = _tar({"drizzle/0000_init.sql": "CREATE TABLE t (a int);", "drizzle/0001_drop.sql": "DROP TABLE t;"})
    monkeypatch.setattr(svc.apps_repo, "get_by_name", AsyncMock(return_value=record))
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=record))
    monkeypatch.setattr(svc.apps_source, "get", AsyncMock(return_value=tar))
    monkeypatch.setattr(
        svc.messaging_repo, "get_room", AsyncMock(return_value={"roomId": "room_1", "ownerId": "org_a"})
    )
    patch_ = AsyncMock()
    monkeypatch.setattr(svc.apps_repo, "patch", patch_)
    body = {**UP_BODY, "source_id": "s2"}

    r = client.post(f"{BASE}/up", json=body, headers=HOOK)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "destructive_migration"
    assert "0001_drop.sql" in r.json()["detail"]["message"] and r.json()["detail"]["next"]

    monkeypatch.setattr(svc, "_approval_ok", AsyncMock(return_value=False))
    r = client.post(f"{BASE}/up", json={**body, "approved_destructive": "card_1"}, headers=HOOK)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "approval_invalid"
    patch_.assert_not_awaited()


def test_errors_route_redacts_secrets(client, monkeypatch):
    record = {
        "app_id": "app_1",
        "owner_id": "org_a",
        "name": "crew",
        "state": "ready",
        "preview": {"deployment_id": "dpl_9", "commit": "c1", "ready_at": "x"},
    }
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=record))
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
                }
            ]
        ),
    )
    secrets = {
        "apps/app_1/vercel/prod": {"token": "vtok_SECRET_123", "org_id": "team_1", "project_id": "prj_1"},
        "apps/app_1/lightbulb": {"cookie_secret": "cookieSECRETvalue"},
    }
    monkeypatch.setattr(svc.integration_secret_store, "get", AsyncMock(side_effect=lambda o, k: secrets.get(k)))
    monkeypatch.setattr(
        svc.vercel,
        "runtime_logs",
        AsyncMock(
            return_value=[
                {"at": 1, "message": "[lightbulb:client] boom cookieSECRETvalue"},
                {"at": 2, "message": "fail postgres://u:pw@h/db with vtok_SECRET_123"},
            ]
        ),
    )
    r = client.get(f"{BASE}/app_1/errors", headers=HOOK)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"client", "server"}
    assert body["client"] == [{"at": 1, "message": "[lightbulb:client] boom [redacted]"}]
    assert body["server"] == [{"at": 2, "message": "fail [redacted] with [redacted]"}]


def test_builder_cannot_touch_another_owners_app(client, monkeypatch):
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value={"app_id": "app_1", "owner_id": "org_b"}))
    for method, path in (("get", "status"), ("get", "errors"), ("post", "destroy"), ("post", "rollback")):
        kw = {"json": {}} if method == "post" else {}
        r = getattr(client, method)(f"{BASE}/app_1/{path}", headers=HOOK, **kw)
        assert r.status_code == 404, path


@pytest.fixture
def previewing(monkeypatch):
    monkeypatch.setattr(svc.settings, "NETWORK_PROOF_KEY", "proof-key")
    record = {
        "app_id": "app_1",
        "owner_id": "org_a",
        "name": "crew",
        "room_id": "room_1",
        "open_to": "room",
        "state": "live",
        "preview_url": "https://crew-preview.apps.test/",
        "live_url": "https://crew.apps.test",
    }
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=record))
    patch = AsyncMock()
    monkeypatch.setattr(svc.apps_repo, "patch", patch)
    return record, patch


def test_test_link_is_a_preview_only_builder_pass(client, previewing):
    _, patch = previewing
    r = client.post(f"{BASE}/app_1/test-link", headers=HOOK)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert set(r.json()) == {"url"}
    url = r.json()["url"]
    assert url.startswith("https://crew-preview.apps.test/_lightbulb/session#token=")
    assert "crew.apps.test" not in url
    head, payload, _ = url.split("#token=", 1)[1].split(".")
    assert json.loads(network_proof._unb64(head))["typ"] == network_proof.APP_PASS_TYP
    claims = json.loads(network_proof._unb64(payload))
    assert claims.pop("exp") - claims.pop("iat") == 300
    # Exactly the door's fields; "crew:preview" is not the LIGHTBULB_APP that the live door compares to.
    assert claims == {
        "sub": "builder:agt_1",
        "name": "Builder agent",
        "email": "",
        "ws": "org_a",
        "room": "room_1",
        "app": "crew:preview",
    }
    patch.assert_not_awaited()  # never last_opened_at


def test_reset_preview_data_is_a_builder_call(client, monkeypatch):
    reset = AsyncMock(return_value={"reset": True})
    monkeypatch.setattr(svc, "reset_preview_data", reset)
    assert client.post(f"{BASE}/app_1/reset-preview-data").status_code == 401
    r = client.post(f"{BASE}/app_1/reset-preview-data", headers=HOOK)
    assert r.status_code == 200 and r.json() == {"reset": True}
    reset.assert_awaited_once_with("org_a", "app_1")


def test_test_link_names_a_signed_in_builder_as_a_builder_too(client, previewing, proof):
    r = client.post(f"{BASE}/app_1/test-link", headers=PROOF)
    assert r.status_code == 200
    payload = r.json()["url"].split("#token=", 1)[1].split(".")[1]
    assert json.loads(network_proof._unb64(payload))["sub"] == "builder:user_1"


def test_test_link_refused_without_credentials_or_for_another_workspace(client, previewing):
    record, _ = previewing
    assert client.post(f"{BASE}/app_1/test-link").status_code == 401
    record["owner_id"] = "org_b"
    r = client.post(f"{BASE}/app_1/test-link", headers=HOOK)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "app_not_found"


def test_test_link_without_a_preview_never_falls_back_to_live(client, previewing):
    record, _ = previewing
    record["preview_url"] = None
    for open_to in ("room", "public"):
        record["open_to"] = open_to
        r = client.post(f"{BASE}/app_1/test-link", headers=HOOK)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_ready"
        assert "crew.apps.test" not in r.text


def test_test_link_for_a_public_app_is_the_plain_preview_address(client, previewing):
    record, patch = previewing
    record["open_to"] = "public"
    r = client.post(f"{BASE}/app_1/test-link", headers=HOOK)
    assert r.status_code == 200 and r.json() == {"url": "https://crew-preview.apps.test"}
    patch.assert_not_awaited()


def _multipart(form: dict, archive: bytes) -> tuple[bytes, str]:
    boundary = "lbboundary"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in form.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="archive"; filename="s.tgz"\r\n'
        "Content-Type: application/gzip\r\n\r\n".encode()
        + archive
        + b"\r\n"
    )
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def test_chunked_upload_is_bounded_and_replayed_for_both_auth_paths(client, proof, monkeypatch):
    """No Content-Length: the cap counts the stream; under it, the proof hash and the form both see the body."""
    put = AsyncMock(return_value={"app_id": "app_1", "source_id": "s1"})
    monkeypatch.setattr(svc, "put_source", put)
    seen = []

    async def identity(request):
        seen.append(await request.body())
        return AuthContext(user_id="user_1", org_id="org_a"), "label"

    proof.side_effect = identity
    body, ctype = _multipart({"name": "crew", "room_id": "room_1", "commit": "c1"}, b"tarbytes")

    def chunks(data):
        def gen():
            yield from (data[i : i + 7] for i in range(0, len(data), 7))

        return gen()

    for headers in (HOOK, PROOF):
        r = client.post(f"{BASE}/source", content=chunks(body), headers={**headers, "Content-Type": ctype})
        assert r.status_code == 200, r.text
        assert put.await_args.kwargs["archive"] == b"tarbytes"
    assert seen == [body]

    monkeypatch.setattr(mod, "MAX_UPLOAD", 20)
    calls = put.await_count
    for headers in (HOOK, PROOF):
        r = client.post(f"{BASE}/source", content=chunks(body), headers={**headers, "Content-Type": ctype})
        assert r.status_code == 413 and r.json()["detail"]["code"] == "source_too_large"
    assert put.await_count == calls and seen == [body]

    monkeypatch.setattr(mod, "MAX_UPLOAD", 1 << 30)
    r = client.post(f"{BASE}/source", content=body, headers={**HOOK, "Content-Type": ctype, "Content-Length": "12x"})
    assert r.status_code == 400


def test_room_outside_the_workspace_is_404(client, monkeypatch):
    monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(return_value={"roomId": "r", "ownerId": "org_b"}))
    monkeypatch.setattr(svc.apps_repo, "get_by_name", AsyncMock(side_effect=AssertionError("no record touched")))
    r = client.post(f"{BASE}/up", json=UP_BODY, headers=HOOK)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "room_not_found"
    r = client.post(
        f"{BASE}/source",
        data={"name": "crew", "room_id": "r", "commit": "c1"},
        files={"archive": ("s.tgz", b"x", "application/gzip")},
        headers=HOOK,
    )
    assert r.status_code == 404 and r.json()["detail"]["code"] == "room_not_found"


def test_unauthenticated_upload_is_refused_before_the_body_is_read(client, hook, monkeypatch):
    """Neither credential, a bad hook token, or a bad proof signature: 401 with the stream untouched."""
    put = AsyncMock()
    monkeypatch.setattr(svc, "put_source", put)
    hook.side_effect = HTTPException(status_code=401, detail="invalid hook token")
    for headers in ({}, HOOK, PROOF):  # PROOF here is unpatched: the real header-only check refuses it
        pulled = []

        def gen():
            for i in range(1000):
                pulled.append(i)
                yield b"x" * 1024

        r = client.post(f"{BASE}/source", content=gen(), headers={**headers, "Content-Type": "multipart/form-data"})
        assert r.status_code == 401, headers
        assert len(pulled) <= 1, headers  # the transport may prime one chunk; the app read none
    put.assert_not_awaited()
