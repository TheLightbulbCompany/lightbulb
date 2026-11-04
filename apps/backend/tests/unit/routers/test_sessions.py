"""Sessions the CLI uploads by choice: metadata row in DynamoDB, the full session in S3."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import asyncio
import json
import time
from unittest.mock import patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from core.auth import AuthContext
from core.config import settings
from core.repositories import usage_repo
from core.services import session_store
from routers import sessions as router
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

BUCKET = "test-sessions"


@pytest.fixture
def s3(table):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    with patch.object(settings, "OWNER_STATE_BUCKET", BUCKET), patch.object(settings, "AWS_REGION", "us-east-1"):
        yield client


@pytest.fixture(autouse=True)
def usage_table(table):
    """The usage-counters table, in the same moto context as the messaging one."""
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="test-usage-counters",
        KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}, {"AttributeName": "period", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "owner_id", "AttributeType": "S"},
            {"AttributeName": "period", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    with patch("core.dynamodb._table_prefix", "test-"), patch.object(settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "k" * 32):
        yield


@pytest.fixture
def app(s3):
    from main import messaging_validation_error_handler

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, messaging_validation_error_handler)  # main's, as deployed
    app.include_router(router.router, prefix="/api/v1/sessions")
    app.include_router(router.shared_router, prefix="/api/v1/shared")
    return app


def as_(app, user_id, org_id=None):
    app.dependency_overrides[router.caller] = lambda: AuthContext(user_id=user_id, org_id=org_id, org_role="org:member")
    return TestClient(app)


def session(sid="s1", started="2026-10-27T10:00:00Z", **extra):
    return {
        "id": sid,
        "harness": "claude-code",
        "computer": "mac",
        "repo": "lightbulb",
        "branch": "main",
        "started": started,
        "ended": "2026-10-27T11:00:00Z",
        "prompt_tokens": 10,
        "completion_tokens": 20,
        **extra,
    }


def call(sid="s1", seq=1, **extra):
    return {
        "session_id": sid,
        "seq": seq,
        "at": "2026-10-27T10:01:00Z",
        "tool": "Bash",
        "duration_ms": 5,
        "ok": True,
        **extra,
    }


def keys(s3):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


def test_sessions_post_requires_identity(app):
    client = TestClient(app)
    assert (
        client.post(
            "/api/v1/sessions", json={"sessions": [session()], "tool_calls": [], "mode": "metadata"}
        ).status_code
        == 401
    )
    assert client.get("/api/v1/sessions").status_code == 401
    assert client.delete("/api/v1/sessions/s1").status_code == 401


def test_metadata_rows_have_no_summaries(app, s3):
    client = as_(app, "user_ada")
    r = client.post("/api/v1/sessions", json={"sessions": [session()], "tool_calls": [call()], "mode": "metadata"})
    assert r.status_code == 200 and r.json() == {"stored": 1}
    assert keys(s3) == []  # metadata only: nothing in S3
    [row] = client.get("/api/v1/sessions").json()["sessions"]
    assert row["full"] is False and row["user_id"] == "user_ada" and "uploaded_at" in row
    assert "summary" not in json.dumps(row) and "tool_calls" not in row


def test_full_upload_goes_to_s3_under_the_tier_prefix_and_is_idempotent(app, s3):
    client = as_(app, "user_ada")
    body = {
        "sessions": [session()],
        "tool_calls": [call(summary="ran tests"), call("other", 1, summary="x")],
        "mode": "full",
    }
    assert client.post("/api/v1/sessions", json=body).json() == {"stored": 1}
    assert client.post("/api/v1/sessions", json=body).json() == {"stored": 1}
    assert keys(s3) == ["sessions/free/user_ada/s1.json"]
    stored = json.loads(s3.get_object(Bucket=BUCKET, Key=keys(s3)[0])["Body"].read())
    assert [c["summary"] for c in stored["tool_calls"]] == ["ran tests"]  # only its own session's calls
    assert len(client.get("/api/v1/sessions").json()["sessions"]) == 1
    # Re-uploaded as metadata only: the full object is gone.
    client.post("/api/v1/sessions", json={"sessions": [session()], "tool_calls": [], "mode": "metadata"})
    assert keys(s3) == []


def test_mode_full_without_tool_calls_is_stored_full(app, s3):
    client = as_(app, "user_ada")
    client.post("/api/v1/sessions", json={"sessions": [session()], "tool_calls": [], "mode": "full"})
    assert keys(s3) == ["sessions/free/user_ada/s1.json"]
    assert client.get("/api/v1/sessions").json()["sessions"][0]["full"] is True
    # A metadata upload is metadata whatever it carries.
    client.post(
        "/api/v1/sessions", json={"sessions": [session()], "tool_calls": [call(summary="s")], "mode": "metadata"}
    )
    assert keys(s3) == [] and client.get("/api/v1/sessions").json()["sessions"][0]["full"] is False


def test_free_identity_uploads_without_workspace(app, s3):
    client = as_(app, "user_ada")
    body = {"sessions": [session()], "tool_calls": [call(summary="s")], "mode": "full"}
    assert client.post("/api/v1/sessions", json=body).json() == {"stored": 1}
    assert keys(s3) == ["sessions/free/user_ada/s1.json"]
    [row] = client.get("/api/v1/sessions").json()["sessions"]
    assert "workspace_id" not in row and row["full"] is True
    assert client.delete("/api/v1/sessions/s1").status_code == 204 and keys(s3) == []


def test_list_sessions_scoped_to_workspace(app, s3):
    ada = as_(app, "user_ada", "org_1")
    ada.post(
        "/api/v1/sessions",
        json={
            "sessions": [session("a", "2026-10-27T09:00:00Z")],
            "tool_calls": [call("a", summary="s")],
            "mode": "full",
        },
    )
    as_(app, "user_bob", "org_1").post(
        "/api/v1/sessions",
        json={"sessions": [session("b", "2026-10-27T10:00:00Z")], "tool_calls": [], "mode": "metadata"},
    )
    as_(app, "user_cy", "org_2").post(
        "/api/v1/sessions", json={"sessions": [session("c")], "tool_calls": [], "mode": "metadata"}
    )
    as_(app, "user_dee").post(
        "/api/v1/sessions", json={"sessions": [session("d")], "tool_calls": [], "mode": "metadata"}
    )
    assert keys(s3) == ["sessions/workspace/user_ada/a.json"]

    ids = lambda c, q="": [s["id"] for s in c.get(f"/api/v1/sessions{q}").json()["sessions"]]  # noqa: E731
    assert ids(as_(app, "user_bob", "org_1")) == ["b", "a"]  # the workspace's, newest first
    assert ids(as_(app, "user_bob", "org_1"), "?since=2026-10-27T09:30:00Z") == ["b"]
    assert ids(as_(app, "user_dee")) == ["d"]  # a signed-in identity sees only its own
    assert ids(as_(app, "user_eve", "org_3")) == []


def test_delete_uploader_only(app, s3):
    as_(app, "user_ada", "org_1").post(
        "/api/v1/sessions", json={"sessions": [session("a")], "tool_calls": [call("a", summary="s")], "mode": "full"}
    )
    as_(app, "user_dee").post(
        "/api/v1/sessions", json={"sessions": [session("a")], "tool_calls": [], "mode": "metadata"}
    )
    assert as_(app, "user_eve", "org_2").delete("/api/v1/sessions/a").status_code == 404
    # A teammate reads Ada's session but may not delete it.
    assert len(as_(app, "user_bob", "org_1").get("/api/v1/sessions").json()["sessions"]) == 1
    assert as_(app, "user_bob", "org_1").delete("/api/v1/sessions/a").status_code == 404
    assert keys(s3) == ["sessions/workspace/user_ada/a.json"]
    assert as_(app, "user_ada", "org_1").delete("/api/v1/sessions/a").status_code == 204
    assert keys(s3) == []
    assert as_(app, "user_bob", "org_1").get("/api/v1/sessions").json()["sessions"] == []
    assert as_(app, "user_ada", "org_1").delete("/api/v1/sessions/a").status_code == 404
    assert len(as_(app, "user_dee").get("/api/v1/sessions").json()["sessions"]) == 1  # not Dee's


async def test_delete_sessions_removes_everything_a_user_uploaded(app, s3):
    as_(app, "user_ada", "org_1").post(
        "/api/v1/sessions",
        json={"sessions": [session("a"), session("b")], "tool_calls": [call("a", summary="s")], "mode": "full"},
    )
    as_(app, "user_bob", "org_1").post(
        "/api/v1/sessions", json={"sessions": [session("c")], "tool_calls": [], "mode": "metadata"}
    )
    assert await session_store.delete_sessions("user_ada") == 2
    assert keys(s3) == []
    assert [s["id"] for s in await session_store.list_sessions(workspace_id="org_1")] == ["c"]


def test_bad_ids_are_refused(app):
    client = as_(app, "user_ada")
    assert (
        client.post(
            "/api/v1/sessions", json={"sessions": [session("../x")], "tool_calls": [], "mode": "metadata"}
        ).status_code
        == 422
    )


def test_reupload_under_other_tier_leaves_no_orphans(app, s3, table):
    full = lambda: {"sessions": [session()], "tool_calls": [call(summary="s")], "mode": "full"}  # noqa: E731
    pks = lambda: sorted(i["pk"] for i in table.scan()["Items"])  # noqa: E731
    for first, second in (("org_1", None), ("org_1", "org_2")):
        as_(app, "user_ada", first).post("/api/v1/sessions", json=full())
        as_(app, "user_ada", second).post("/api/v1/sessions", json=full())
        want = ["WSESSIONS#org_2"] if second else []
        assert sorted(p for p in pks() if p.startswith("WSESSIONS#")) == want
        assert keys(s3) == [f"sessions/{'workspace' if second else 'free'}/user_ada/s1.json"]
        assert asyncio.run(session_store.delete_sessions("user_ada")) == 1
        assert pks() == [] and keys(s3) == []


def test_over_limit_upload_is_422(app):
    client = as_(app, "user_ada")
    long = {"sessions": [session()], "tool_calls": [call(summary="x" * 2001)], "mode": "full"}
    r = client.post("/api/v1/sessions", json=long)
    assert r.status_code == 422 and "xxx" not in r.text  # the rejected summary is not echoed
    many = {"sessions": [session()], "tool_calls": [call(seq=i) for i in range(5001)], "mode": "full"}
    assert client.post("/api/v1/sessions", json=many).status_code == 422


def test_body_user_and_workspace_ids_are_ignored(app, s3):
    client = as_(app, "user_ada")
    row = session(user_id="user_eve", workspace_id="org_9")
    body = {"sessions": [row], "tool_calls": [], "mode": "metadata", "user_id": "user_eve", "workspace_id": "org_9"}
    assert client.post("/api/v1/sessions", json=body).json() == {"stored": 1}
    [mine] = client.get("/api/v1/sessions").json()["sessions"]
    assert mine["user_id"] == "user_ada" and "workspace_id" not in mine
    assert as_(app, "user_eve", "org_9").get("/api/v1/sessions").json()["sessions"] == []


def test_usage_includes_capture_tokens_per_agent(app, s3):
    client = as_(app, "user_ada", "org_1")
    body = {"sessions": [session("a"), session("b", completion_tokens=5)], "tool_calls": [], "mode": "metadata"}
    client.post("/api/v1/sessions", json=body)
    client.post("/api/v1/sessions", json=body)  # a re-upload is the same session, not more tokens
    as_(app, "user_bob", "org_1").post(
        "/api/v1/sessions", json={"sessions": [session("c")], "tool_calls": [], "mode": "metadata"}
    )
    as_(app, "user_eve").post(  # no workspace: nothing to bill
        "/api/v1/sessions", json={"sessions": [session("d")], "tool_calls": [], "mode": "metadata"}
    )
    usage = {m["user_id"]: m for m in asyncio.run(usage_repo.get_member_usage("org_1", "2026-10"))}
    assert set(usage) == {"user_ada", "user_bob"}
    assert (usage["user_ada"]["capture_prompt_tokens"], usage["user_ada"]["capture_completion_tokens"]) == (20, 25)
    assert (usage["user_bob"]["capture_prompt_tokens"], usage["user_bob"]["capture_completion_tokens"]) == (10, 20)
    assert usage["user_ada"]["request_count"] == 0  # not an LLM request


def share(client, sid="s1"):
    return client.post(f"/api/v1/sessions/{sid}/share")


def test_share_link_expires_in_7_days(app, s3):
    client = as_(app, "user_ada", "org_1")
    client.post(
        "/api/v1/sessions", json={"sessions": [session()], "tool_calls": [call(summary="ran tests")], "mode": "full"}
    )
    with (
        patch.object(settings, "PAPERCLIP_SERVICE_TOKEN_KEY", "k" * 32),
        patch.object(settings, "FRONTEND_URL", "https://app.example"),
    ):
        r = share(client)
        assert r.status_code == 200 and r.json()["url"].startswith("https://app.example/shared/")
        token = r.json()["url"].rsplit("/", 1)[1]
        anon = TestClient(app)
        got = anon.get(f"/api/v1/shared/{token}")
        assert got.status_code == 200
        assert "user_id" not in got.text and "org_1" not in got.text
        assert got.json()["session"]["id"] == "s1"
        assert [c["summary"] for c in got.json()["tool_calls"]] == ["ran tests"]
        now = time.time()
        with patch("time.time", return_value=now + 7 * 86400 - 60):
            assert anon.get(f"/api/v1/shared/{token}").status_code == 200
        with patch("time.time", return_value=now + 7 * 86400 + 60):
            assert anon.get(f"/api/v1/shared/{token}").status_code == 404
        body, _, sig = token.partition(".")
        assert anon.get(f"/api/v1/shared/{body}.{'0' * len(sig)}").status_code == 404  # forged
        assert anon.get("/api/v1/shared/garbage").status_code == 404
        client.delete("/api/v1/sessions/s1")
        assert anon.get(f"/api/v1/shared/{token}").status_code == 404  # session gone


def test_share_requires_the_uploader(app, s3):
    assert share(TestClient(app)).status_code == 401  # before any override is set
    as_(app, "user_ada", "org_1").post(
        "/api/v1/sessions", json={"sessions": [session()], "tool_calls": [], "mode": "metadata"}
    )
    assert share(as_(app, "user_eve", "org_2")).status_code == 404  # another workspace
    assert share(as_(app, "user_eve")).status_code == 404
    assert share(as_(app, "user_ada", "org_1")).status_code == 200  # the uploader
    assert share(as_(app, "user_bob", "org_1")).status_code == 404  # a teammate reads it, never publishes it


def test_upload_full_triggers_ingest_and_metadata_does_not(app, s3):
    from unittest.mock import AsyncMock

    c = as_(app, "u1", "org_x")
    body = {"sessions": [session()], "tool_calls": [call()]}
    with patch("routers.sessions.evaluator.ingest_session", new=AsyncMock(return_value=True)) as ingest:
        assert c.post("/api/v1/sessions", json={"mode": "metadata", **body}).status_code == 200
        ingest.assert_not_called()
        assert c.post("/api/v1/sessions", json={"mode": "full", **body}).status_code == 200
        ingest.assert_awaited_once()
        assert ingest.await_args.kwargs == {"workspace_id": "org_x"}
        assert ingest.await_args.args[2][0]["tool"] == "Bash"
    with patch("routers.sessions.evaluator.ingest_session", new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert c.post("/api/v1/sessions", json={"mode": "full", **body}).status_code == 200  # never fails the upload


def test_a_free_accounts_full_upload_is_stored_and_never_evaluated(app, s3):
    from unittest.mock import AsyncMock

    body = {"sessions": [session()], "tool_calls": [call()], "mode": "full"}
    with patch("routers.sessions.evaluator.ingest_session", new=AsyncMock(return_value=True)) as ingest:
        r = as_(app, "u_free").post("/api/v1/sessions", json=body)
        assert r.status_code == 200 and r.json()["stored"]
        ingest.assert_not_called()


def test_rows_expire_with_their_retention_and_read_as_gone_once_past_it(app, s3, table):
    as_(app, "user_ada", "org_1").post("/api/v1/sessions", json={"sessions": [session("w")], "mode": "metadata"})
    as_(app, "user_dee").post("/api/v1/sessions", json={"sessions": [session("f")], "mode": "metadata"})
    rows = {i["sk"]: i for i in table.scan()["Items"]}
    now = time.time()
    assert abs(int(rows["S#w"]["expiresAt"]) - (now + 365 * 86400)) < 60  # the table's TTL attribute
    assert abs(int(rows["S#f"]["expiresAt"]) - (now + 30 * 86400)) < 60
    assert all("expiresAt" not in r for r in as_(app, "user_dee").get("/api/v1/sessions").json()["sessions"])
    with patch("time.time", return_value=now + 31 * 86400):  # DynamoDB may not have removed it yet
        assert as_(app, "user_dee").get("/api/v1/sessions").json()["sessions"] == []
        assert asyncio.run(session_store.get_session("f", user_id="user_dee", workspace_id=None)) is None
        assert asyncio.run(session_store.read_shared("user_dee", "f")) is None
        assert len(as_(app, "user_ada", "org_1").get("/api/v1/sessions").json()["sessions"]) == 1


def test_a_share_link_whose_object_aged_out_is_404(app, s3):
    client = as_(app, "user_ada")
    client.post("/api/v1/sessions", json={"sessions": [session()], "tool_calls": [call(summary="x")], "mode": "full"})
    with patch.object(settings, "FRONTEND_URL", "https://app.example"):
        token = share(client).json()["url"].rsplit("/", 1)[1]
    s3.delete_object(Bucket=BUCKET, Key="sessions/free/user_ada/s1.json")  # the lifecycle rule ran first
    assert TestClient(app).get(f"/api/v1/shared/{token}").status_code == 404


def test_a_cli_session_id_with_a_colon_round_trips_through_upload_list_and_share(app, s3):
    sid = "claude:abc"  # the CLI's "<harness>:<id>"
    client = as_(app, "user_ada", "org_1")
    r = client.post(
        "/api/v1/sessions", json={"sessions": [session(sid)], "tool_calls": [call(sid, summary="x")], "mode": "full"}
    )
    assert r.status_code == 200
    assert [s["id"] for s in client.get("/api/v1/sessions").json()["sessions"]] == [sid]
    assert keys(s3) == [f"sessions/workspace/user_ada/{sid}.json"]  # unencoded in the key, as in the API
    with patch.object(settings, "FRONTEND_URL", "https://app.example"):
        token = share(client, sid).json()["url"].rsplit("/", 1)[1]
    got = TestClient(app).get(f"/api/v1/shared/{token}").json()
    assert got["session"]["id"] == sid and [c["summary"] for c in got["tool_calls"]] == ["x"]
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 204
    assert client.post("/api/v1/sessions", json={"sessions": [session("a/b")], "mode": "metadata"}).status_code == 422


def test_delete_forgets_the_session_in_the_evaluator_and_its_failure_is_still_204(app, s3):
    from unittest.mock import AsyncMock

    c = as_(app, "user_ada", "org_1")
    for sid in ("a", "b"):
        c.post("/api/v1/sessions", json={"sessions": [session(sid)], "mode": "metadata"})
    with patch("routers.sessions.evaluator.forget_session", new=AsyncMock()) as forget:
        assert c.delete("/api/v1/sessions/a").status_code == 204
        forget.assert_awaited_once_with("user_ada", "a", workspace_id="org_1")
        assert c.delete("/api/v1/sessions/a").status_code == 404
        forget.assert_awaited_once()  # nothing deleted: nothing to forget
    with patch("routers.sessions.evaluator.forget_session", new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert c.delete("/api/v1/sessions/b").status_code == 204
