import asyncio
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
        if isinstance(body, Exception):
            raise body
        if isinstance(body, str):
            return httpx.Response(status, text=body)
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
    out = await vc.create_deployment(
        CREDS, name="app", files=files, target="production", build_command="npm run build", commit="c9"
    )
    r = seen[0]
    assert r.url.params["forceNew"] == "1" and r.url.params["skipAutoDetectionConfirmation"] == "1"
    assert r.url.params["teamId"] == "team_1"
    assert body(r) == {
        "name": "app",
        "project": "prj_1",
        "files": files,
        "target": "production",
        "projectSettings": {"framework": "nextjs", "buildCommand": "npm run build"},
        "env": {"LIGHTBULB_COMMIT": "c9"},
        "build": {"env": {"LIGHTBULB_COMMIT": "c9"}},
    }
    assert out == {"id": "dpl_1", "url": "x.vercel.app", "readyState": "QUEUED"}


async def test_deployment_state_maps_ready_and_error(calls):
    seen, routes = calls
    routes[("GET", "/v13/deployments/dpl_1")] = (200, {"readyState": "ERROR", "url": "u", "errorMessage": "boom"})
    assert await vc.deployment_state(CREDS, "dpl_1") == {
        "readyState": "ERROR",
        "url": "u",
        "errorMessage": "boom",
        "target": None,
    }
    routes[("GET", "/v13/deployments/dpl_1")] = (200, {"readyState": "READY", "url": "u", "target": "production"})
    assert await vc.deployment_state(CREDS, "dpl_1") == {
        "readyState": "READY",
        "url": "u",
        "errorMessage": None,
        "target": "production",
    }
    assert len(seen) == 2 and seen[0].url.params["teamId"] == "team_1"


async def test_deployment_events_oldest_first_and_filters_empty(calls):
    seen, routes = calls
    routes[("GET", "/v3/deployments/dpl_1/events")] = (
        200,
        [
            {"created": 2000, "payload": {"text": "Built"}},
            {"created": 1500, "text": ""},
            {"created": 1000, "text": "Installing"},
        ],
    )
    out = await vc.deployment_events(CREDS, "dpl_1", limit=10)
    assert out == [{"at": 1000, "message": "Installing"}, {"at": 2000, "message": "Built"}]
    p = seen[0].url.params
    assert p["limit"] == "10" and p["direction"] == "backward" and p["teamId"] == "team_1"


async def test_runtime_logs_parses_ndjson_and_keeps_tail(calls):
    seen, routes = calls
    lines = [{"timestampInMs": i, "message": f"m{i}"} for i in range(1, 5)] + [{"timestampInMs": 9, "message": ""}]
    routes[("GET", "/v1/projects/prj_1/deployments/dpl_1/runtime-logs")] = (
        200,
        "\n".join(json.dumps(x) for x in lines) + "\n",
    )
    out = await vc.runtime_logs(CREDS, "dpl_1", limit=2)
    assert out == [{"at": 3, "message": "m3"}, {"at": 4, "message": "m4"}]
    assert seen[0].url.params["teamId"] == "team_1"


async def test_add_domain_returns_verification_and_lowest_rank_cname(calls):
    seen, routes = calls
    routes[("POST", "/v10/projects/prj_1/domains")] = (200, {"verified": False, "verification": [{"type": "TXT"}]})
    routes[("GET", "/v6/domains/a.example.com/config")] = (
        200,
        {"recommendedCNAME": [{"rank": 2, "value": "b."}, {"rank": 1, "value": "c.vercel-dns.com."}]},
    )
    out = await vc.add_domain(CREDS, "a.example.com")
    assert out == {"verified": False, "verification": [{"type": "TXT"}], "cname": "c.vercel-dns.com."}
    assert body(seen[0]) == {"name": "a.example.com"}
    cfg = seen[1]
    assert cfg.url.params["projectIdOrName"] == "prj_1" and cfg.url.params["teamId"] == "team_1"


async def test_add_domain_cname_falls_back(calls):
    _, routes = calls
    routes[("POST", "/v10/projects/prj_1/domains")] = (200, {"verified": True})
    routes[("GET", "/v6/domains/a.example.com/config")] = (200, {"recommendedCNAME": []})
    assert (await vc.add_domain(CREDS, "a.example.com"))["cname"] == "cname.vercel-dns.com."


async def test_add_domain_idempotent_when_already_on_this_project(calls):
    _, routes = calls
    routes[("POST", "/v10/projects/prj_1/domains")] = (
        409,
        {"error": {"code": "domain_already_in_use", "message": "x"}},
    )
    routes[("GET", "/v9/projects/prj_1/domains/a.example.com")] = (200, {"verified": True})
    routes[("GET", "/v6/domains/a.example.com/config")] = (200, {"recommendedCNAME": []})
    out = await vc.add_domain(CREDS, "a.example.com")
    assert out["verified"] is True


async def test_add_domain_owned_elsewhere_still_raises(calls):
    _, routes = calls
    routes[("POST", "/v10/projects/prj_1/domains")] = (
        409,
        {"error": {"code": "domain_already_in_use", "message": "x"}},
    )
    routes[("GET", "/v9/projects/prj_1/domains/a.example.com")] = (
        404,
        {"error": {"code": "not_found", "message": "n"}},
    )
    with pytest.raises(vc.VercelError) as e:
        await vc.add_domain(CREDS, "a.example.com")
    assert e.value.code == "domain_already_in_use"


async def test_alias_deployment_is_scoped_to_the_project(calls):
    seen, routes = calls
    path = "/v4/aliases/a.example.com"
    routes[("GET", path)] = (200, {"deploymentId": "dpl_1"})
    assert await vc.alias_deployment(CREDS, "a.example.com") == "dpl_1"
    assert seen[0].url.params["projectId"] == "prj_1"
    routes[("GET", path)] = (404, {"error": {"code": "not_found", "message": "n"}})
    assert await vc.alias_deployment(CREDS, "a.example.com") is None


async def test_upsert_env_partial_failure_raises_without_values(calls):
    _, routes = calls
    routes[("POST", "/v10/projects/prj_1/env")] = (
        201,
        {"created": [], "failed": [{"error": {"code": "invalid_name", "message": "bad envvalue123"}}]},
    )
    with pytest.raises(vc.VercelError) as e:
        await vc.upsert_env(CREDS, {"K": "envvalue123"}, ["production"])
    assert e.value.code == "invalid_name" and "envvalue123" not in str(e.value)


async def test_create_deployment_omits_null_target(calls):
    seen, routes = calls
    routes[("POST", "/v13/deployments")] = (200, {"id": "d"})
    await vc.create_deployment(CREDS, name="app", files=[], target=None, build_command="b", commit="c")
    assert "target" not in body(seen[0])


async def test_bad_response_and_network_error_and_quoting(calls):
    seen, routes = calls
    routes[("GET", "/v13/deployments/dpl_1")] = (200, [1])
    with pytest.raises(vc.VercelError) as e:
        await vc.deployment_state(CREDS, "dpl_1")
    assert e.value.code == "bad_response"
    routes[("GET", "/v13/deployments/dpl_1")] = (200, httpx.ConnectError("boom tok_SECRET"))
    with pytest.raises(vc.VercelError) as e:
        await vc.deployment_state(CREDS, "dpl_1")
    assert e.value.code == "network" and "tok_SECRET" not in str(e.value)
    await vc.rollback(CREDS, "a/b")
    assert seen[-1].url.raw_path.startswith(b"/v1/projects/prj_1/rollback/a%2Fb")


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
    routes[("GET", "/v1/projects/prj_1/deployments/d/runtime-logs")] = (200, "")
    routes[("POST", "/v9/projects/prj_1/domains/x/verify")] = (200, {})
    with caplog.at_level(logging.DEBUG):
        await vc.upsert_env(CREDS, {"K": "envvalue123"}, ["production"])
        await vc.upload_file(CREDS, b"x")
        await vc.deployment_state(CREDS, "d")
        await vc.deployment_events(CREDS, "d")
        await vc.runtime_logs(CREDS, "d")
        await vc.verify_domain(CREDS, "x")
        await vc.rollback(CREDS, "d")
        await vc.disable_protection(CREDS)
        await vc.delete_project(CREDS)
    assert "tok_SECRET" not in caplog.text and "envvalue123" not in caplog.text


async def test_empty_bodies_ok_for_unread_calls(calls):
    _, routes = calls
    routes[("DELETE", "/v9/projects/prj_1")] = (204, "")
    routes[("POST", "/v1/projects/prj_1/rollback/d")] = (200, "")
    routes[("PATCH", "/v9/projects/prj_1")] = (200, "")
    await vc.delete_project(CREDS)
    await vc.rollback(CREDS, "d")
    await vc.disable_protection(CREDS)


async def test_add_domain_bad_rank_is_last(calls):
    _, routes = calls
    routes[("POST", "/v10/projects/prj_1/domains")] = (200, {"verified": True})
    routes[("GET", "/v6/domains/a.example.com/config")] = (
        200,
        {"recommendedCNAME": [{"rank": "x", "value": "bad."}, {"rank": 3, "value": "good."}]},
    )
    assert (await vc.add_domain(CREDS, "a.example.com"))["cname"] == "good."


async def test_runtime_logs_returns_tail_when_stream_stays_open(calls, monkeypatch):
    class Hang(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"timestampInMs": 1, "message": "a"}\n{"timestampInMs": 2, "message": "b"}\n'
            await asyncio.sleep(10)

    # R56: the stream window is its own (short) constant, not the per-request timeout
    assert vc.LOGS_WINDOW_S <= 8 < vc.TIMEOUT_S
    monkeypatch.setattr(vc, "LOGS_WINDOW_S", 0.2)
    monkeypatch.setattr(vc, "_transport", httpx.MockTransport(lambda req: httpx.Response(200, stream=Hang())))
    assert await vc.runtime_logs(CREDS, "d", limit=1) == [{"at": 2, "message": "b"}]


async def test_runtime_logs_error_status(calls):
    _, routes = calls
    routes[("GET", "/v1/projects/prj_1/deployments/d/runtime-logs")] = (
        403,
        {"error": {"code": "forbidden", "message": "no"}},
    )
    with pytest.raises(vc.VercelError) as e:
        await vc.runtime_logs(CREDS, "d")
    assert e.value.code == "forbidden"


async def test_assign_alias_posts_alias_to_the_deployment(calls):
    seen, _ = calls
    await vc.assign_alias(CREDS, "dpl/1", "crew-preview.apps.example.com")
    r = seen[0]
    assert r.method == "POST" and r.url.raw_path.startswith(b"/v2/deployments/dpl%2F1/aliases")
    assert r.url.params["teamId"] == "team_1"
    assert body(r) == {"alias": "crew-preview.apps.example.com"}


async def test_promote_points_production_at_the_deployment(calls):
    seen, routes = calls
    routes[("POST", "/v10/projects/prj_1/promote/dpl_9")] = (201, {})
    await vc.promote(CREDS, "dpl_9")
    r = seen[0]
    assert (r.method, r.url.path) == ("POST", "/v10/projects/prj_1/promote/dpl_9")
    assert r.url.params["teamId"] == "team_1"
