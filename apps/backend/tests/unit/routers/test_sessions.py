"""Sessions the CLI uploads by choice: metadata row in DynamoDB, the full session in S3."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import json
from unittest.mock import patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext
from core.config import settings
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


@pytest.fixture
def app(s3):
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1/sessions")
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


def test_delete_owner_or_workspace_member_only(app, s3):
    as_(app, "user_ada", "org_1").post(
        "/api/v1/sessions", json={"sessions": [session("a")], "tool_calls": [call("a", summary="s")], "mode": "full"}
    )
    as_(app, "user_dee").post(
        "/api/v1/sessions", json={"sessions": [session("a")], "tool_calls": [], "mode": "metadata"}
    )
    assert as_(app, "user_eve", "org_2").delete("/api/v1/sessions/a").status_code == 404
    assert as_(app, "user_bob", "org_1").delete("/api/v1/sessions/a").status_code == 204  # any member
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
