import hashlib
import json
import logging

import httpx
import pytest

from core.services import vercel_client as vc

CREDS = {"token": "tok_SECRET", "org_id": "team_1", "project_id": "prj_1"}


@pytest.fixture
def calls(monkeypatch):
    seen, routes = [], {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        status, body = routes.get((req.method, req.url.path), (200, {}))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(vc, "_transport", httpx.MockTransport(handler))
    seen_routes = routes
    return seen, seen_routes


def body(req):
    return json.loads(req.content)


async def test_upsert_env_posts_encrypted_targets(calls):
    seen, _ = calls
    await vc.upsert_env(CREDS, {"A": "1", "B": "2"}, ["production", "preview"])
    r = seen[0]
    assert (r.method, r.url.path) == ("POST", "/v10/projects/prj_1/env")
    assert r.url.params["upsert"] == "true" and r.url.params["teamId"] == "team_1"
    assert r.headers["authorization"] == "Bearer tok_SECRET"
    assert body(r) == [
        {"key": "A", "value": "1", "type": "encrypted", "target": ["production", "preview"]},
        {"key": "B", "value": "2", "type": "encrypted", "target": ["production", "preview"]},
    ]


async def test_upload_file_sends_sha1_digest(calls):
    seen, _ = calls
    sha = await vc.upload_file(CREDS, b"hello")
    assert sha == hashlib.sha1(b"hello").hexdigest()
    r = seen[0]
    assert (r.method, r.url.path) == ("POST", "/v2/files")
    assert r.headers["x-vercel-digest"] == sha and r.content == b"hello"
    assert r.url.params["teamId"] == "team_1"


async def test_create_deployment_sends_files_and_nextjs_settings(calls):
    seen, routes = calls
    routes[("POST", "/v13/deployments")] = (
        200,
        {"id": "dpl_1", "url": "x.vercel.app", "readyState": "QUEUED", "extra": 1},
    )
    files = [{"file": "a.txt", "sha": "abc", "size": 1}]
    out = await vc.create_deployment(CREDS, name="app", files=files, target="production", build_command="npm run build")
    r = seen[0]
    assert r.url.params["forceNew"] == "1" and r.url.params["skipAutoDetectionConfirmation"] == "1"
    assert r.url.params["teamId"] == "team_1"
    assert body(r) == {
        "name": "app",
        "project": "prj_1",
        "files": files,
        "target": "production",
        "projectSettings": {"framework": "nextjs", "buildCommand": "npm run build"},
    }
    assert out == {"id": "dpl_1", "url": "x.vercel.app", "readyState": "QUEUED"}


async def test_deployment_state_maps_ready_and_error(calls):
    seen, routes = calls
    routes[("GET", "/v13/deployments/dpl_1")] = (200, {"readyState": "ERROR", "url": "u", "errorMessage": "boom"})
    assert await vc.deployment_state(CREDS, "dpl_1") == {"readyState": "ERROR", "url": "u", "errorMessage": "boom"}
    routes[("GET", "/v13/deployments/dpl_1")] = (200, {"readyState": "READY", "url": "u"})
    assert await vc.deployment_state(CREDS, "dpl_1") == {"readyState": "READY", "url": "u", "errorMessage": None}
    assert len(seen) == 2 and seen[0].url.params["teamId"] == "team_1"


async def test_deployment_events_maps_messages(calls):
    seen, routes = calls
    routes[("GET", "/v3/deployments/dpl_1/events")] = (
        200,
        [{"created": 1000, "text": "Installing"}, {"created": 2000, "payload": {"text": "Built"}}],
    )
    out = await vc.deployment_events(CREDS, "dpl_1", limit=10)
    assert out == [{"at": 1000, "message": "Installing"}, {"at": 2000, "message": "Built"}]
    assert seen[0].url.params["limit"] == "10" and seen[0].url.params["teamId"] == "team_1"


async def test_add_domain_returns_verification_and_cname(calls):
    seen, routes = calls
    routes[("POST", "/v10/projects/prj_1/domains")] = (200, {"verified": False, "verification": [{"type": "TXT"}]})
    routes[("GET", "/v9/projects/prj_1/domains/a.example.com")] = (200, {"cname": "c.vercel-dns.com."})
    out = await vc.add_domain(CREDS, "a.example.com")
    assert out == {"verified": False, "verification": [{"type": "TXT"}], "cname": "c.vercel-dns.com."}
    assert body(seen[0]) == {"name": "a.example.com"}


async def test_verify_domain(calls):
    _, routes = calls
    routes[("POST", "/v9/projects/prj_1/domains/a.example.com/verify")] = (200, {"verified": True})
    assert await vc.verify_domain(CREDS, "a.example.com") is True


async def test_rollback_uses_v1_route(calls):
    seen, _ = calls
    await vc.rollback(CREDS, "dpl_0")
    assert (seen[0].method, seen[0].url.path) == ("POST", "/v1/projects/prj_1/rollback/dpl_0")


async def test_disable_protection_and_delete_project(calls):
    seen, _ = calls
    await vc.disable_protection(CREDS)
    await vc.delete_project(CREDS)
    assert (seen[0].method, seen[0].url.path) == ("PATCH", "/v9/projects/prj_1")
    assert body(seen[0]) == {"ssoProtection": None, "passwordProtection": None}
    assert (seen[1].method, seen[1].url.path) == ("DELETE", "/v9/projects/prj_1")


async def test_missing_credentials(calls):
    with pytest.raises(vc.VercelError) as e:
        await vc.rollback({"token": "t", "org_id": "o"}, "d")
    assert e.value.code == "missing_credentials"


async def test_error_has_code_and_no_token(calls):
    _, routes = calls
    routes[("POST", "/v1/projects/prj_1/rollback/d")] = (
        403,
        {"error": {"code": "forbidden", "message": "nope tok_SECRET"}},
    )
    with pytest.raises(vc.VercelError) as e:
        await vc.rollback(CREDS, "d")
    assert e.value.code == "forbidden" and "tok_SECRET" not in str(e.value)
    routes[("POST", "/v1/projects/prj_1/rollback/d")] = (500, {})
    with pytest.raises(vc.VercelError) as e:
        await vc.rollback(CREDS, "d")
    assert e.value.code == "http_500"


async def test_token_never_in_logs(calls, caplog):
    _, routes = calls
    routes[("GET", "/v13/deployments/d")] = (200, {"readyState": "READY"})
    routes[("GET", "/v3/deployments/d/events")] = (200, [])
    routes[("POST", "/v9/projects/prj_1/domains/x/verify")] = (200, {})
    with caplog.at_level(logging.DEBUG):
        await vc.upsert_env(CREDS, {"K": "envvalue123"}, ["production"])
        await vc.upload_file(CREDS, b"x")
        await vc.deployment_state(CREDS, "d")
        await vc.deployment_events(CREDS, "d")
        await vc.verify_domain(CREDS, "x")
        await vc.rollback(CREDS, "d")
        await vc.disable_protection(CREDS)
        await vc.delete_project(CREDS)
    assert "tok_SECRET" not in caplog.text and "envvalue123" not in caplog.text
