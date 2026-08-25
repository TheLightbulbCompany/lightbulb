"""HTTP contract for the backup-import door. The normalizer + staging export
have their own unit files; here: presign shape + owner-scoped key, S3 stream →
preview, Ultra gate, per-agent confirm wiring, cleanup."""

import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.containers import workspace as workspace_module  # noqa: E402
from core.services.openclaw_backup_plugins import PluginApplyResult  # noqa: E402
from routers import marketplace_import  # noqa: E402

OWNER = "user_owner_1"
KEY = f"imports/{OWNER}/0123456789abcdef0123456789abcdef.tar.gz"


def _auth() -> AuthContext:
    # If AuthContext's constructor differs, copy the override used in
    # tests/unit/routers/test_marketplace_listings.py.
    return AuthContext(user_id=OWNER, email="o@example.com", org_id=None, org_role=None)


def _archive(agents_list=None, extra=None) -> bytes:
    tree = {
        "openclaw.json": json.dumps(
            {
                "agents": {
                    "defaults": {"workspace": "~/.openclaw/workspace"},
                    **({"list": agents_list} if agents_list else {}),
                },
                "mcp": {"servers": {"gh": {"command": "npx", "env": {"T": "secret"}}}},
            }
        ),
        "workspace/AGENTS.md": "# AGENTS.md\n",
        "workspace/IDENTITY.md": "- **Name:** Quill\n- **Vibe:** dry\n",
        "skills/remotion/SKILL.md": "# remotion\n",
        **(extra or {}),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in tree.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.deleted: list[str] = []
        self.presigned: list[dict] = []
        self.etag = '"etag-1"'  # bump/override in a test to simulate the object changing mid-import

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append({"op": op, **Params, "ExpiresIn": ExpiresIn})
        return f"https://s3.example/{Params['Key']}?sig=1"

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise marketplace_import.ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]), "ETag": self.etag}

    def get_object(self, Bucket, Key, IfMatch=None):
        if IfMatch is not None and IfMatch != self.etag:
            raise marketplace_import.ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)


@pytest.fixture
def client(tmp_path: Path):
    app = FastAPI()
    app.include_router(marketplace_import.router)
    app.dependency_overrides[get_current_user] = _auth
    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    s3 = FakeS3({KEY: _archive()})
    with (
        patch("routers.marketplace_import.get_workspace", return_value=fake_ws),
        patch("core.services.agent_export.get_workspace", return_value=fake_ws),
        patch("routers.marketplace_import._s3", return_value=s3),
        patch("routers.marketplace_import.settings") as settings,
        patch("routers.marketplace_import.container_repo.get_by_owner_id", new_callable=AsyncMock) as get_row,
        patch("routers.marketplace_import.count_paperclip_agents_for_owner", new_callable=AsyncMock, return_value=2),
        patch(
            "routers.marketplace_import.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"plan": "flat"},
        ),
    ):
        settings.MARKETPLACE_ARTIFACTS_BUCKET = "bkt"
        get_row.return_value = {"owner_id": OWNER, "gateway_token": "gw"}
        yield TestClient(app), tmp_path / "users" / OWNER, s3, get_row


def test_presign_scopes_key_to_owner(client):
    c, _, s3, _ = client
    r = c.post("/api/v1/marketplace/import/presign", json={"filename": "backup.tar.gz", "size_bytes": 1234})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"].startswith(f"imports/{OWNER}/") and body["key"].endswith(".tar.gz")
    assert body["upload_url"].startswith("https://s3.example/")
    assert s3.presigned[0]["ContentType"] == "application/gzip"


def test_presign_413_over_cap(client):
    c, _, _, _ = client
    r = c.post(
        "/api/v1/marketplace/import/presign",
        json={"filename": "x.tar.gz", "size_bytes": marketplace_import.backup_import.MAX_UPLOAD_BYTES + 1},
    )
    assert r.status_code == 413


def test_import_streams_from_s3_and_returns_preview(client):
    c, user_root, s3, _ = client
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["import_id"].startswith("import-") and body["kind"] == "state-dir"
    assert body["agent_id"] == "main" and body["name"] == "Quill"
    assert body["mcp_servers"] == ["gh"] and "remotion" in body["skills"]
    assert body["existing_agents"] == 2 and body["standard_max_agents"] == 10 and body["plan"] == "flat"
    staging = user_root / f"workspace-{body['import_id']}"
    assert (staging / "raw").is_dir() and (staging / "workspace" / "openclaw.json").is_file()
    assert s3.deleted == [KEY]  # raw upload consumed


def test_import_412_etag_mismatch_returns_409(client):
    """Every pass over the upload is its own GET: the names-only detection
    pass, then extract_archive's pass 1 and pass 2. If the object changes
    after the first GET, the next one must fail on the IfMatch precondition
    rather than silently splicing two different archives together — the
    router maps that 412 to a 409 telling the user to re-upload. Here the
    mismatch lands on GET 2, the extractor's first pass."""
    c, user_root, s3, _ = client
    real_get = s3.get_object
    calls = {"n": 0}

    def flaky_get(Bucket, Key, IfMatch=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise marketplace_import.ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        return real_get(Bucket, Key, IfMatch=IfMatch)

    s3.get_object = flaky_get
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 409, r.text
    assert "upload it again" in r.json()["detail"]
    assert calls["n"] == 2
    assert not list(user_root.glob("workspace-import-*")) if user_root.exists() else True


def test_import_surfaces_safety_flags_without_required_snippet(client):
    # export_built_agent_from_staging scans with include_snippets=False, which
    # strips "snippet" from every flag dict (marketplace_safety.flags_without_snippets).
    # SafetyFlag.snippet must tolerate that or this 500s (ResponseValidationError).
    c, _, s3, _ = client
    s3.objects[KEY] = _archive(
        extra={
            "workspace/IDENTITY.md": '- **Name:** Quill\napi_key = "sk-live-abcdefghijklmnop"\n',
        }
    )
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 200, r.text
    assert r.json()["safety_flags"]


def test_import_rejects_foreign_or_malformed_key(client):
    c, _, _, _ = client
    assert c.post("/api/v1/marketplace/import", json={"key": "imports/someone_else/abc.tar.gz"}).status_code == 400
    assert c.post("/api/v1/marketplace/import", json={"key": f"imports/{OWNER}/../x.tar.gz"}).status_code == 400
    other_owner_key = "imports/someone_else/0123456789abcdef0123456789abcdef.tar.gz"
    assert c.post("/api/v1/marketplace/import", json={"key": other_owner_key}).status_code == 400


def test_import_404_missing_object_and_no_container(client):
    c, _, _, get_row = client
    assert (
        c.post(
            "/api/v1/marketplace/import", json={"key": f"imports/{OWNER}/ffffffffffffffffffffffffffffffff.tar.gz"}
        ).status_code
        == 404
    )
    get_row.return_value = None
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 404


def test_import_400_unrecognized_archive_cleans_up(client):
    c, user_root, s3, _ = client
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="README.txt")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"hi"))
    s3.objects[KEY] = buf.getvalue()
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400 and "no OpenClaw" in r.json()["detail"]
    assert not list(user_root.glob("workspace-import-*")) if user_root.exists() else True


def test_import_413_export_too_large_cleans_up(client):
    c, user_root, _, _ = client
    with patch(
        "routers.marketplace_import.agent_export.export_built_agent_from_staging",
        side_effect=marketplace_import.agent_export.ExportTooLargeError(
            file_count=50_000, byte_count=999_999_999, largest=[("workspace", 999_999_999)]
        ),
    ):
        r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 413, r.text
    assert not list(user_root.glob("workspace-import-*")) if user_root.exists() else True


def _start(c) -> str:
    return c.post("/api/v1/marketplace/import", json={"key": KEY}).json()["import_id"]


def test_confirm_registers_each_selected_agent_and_removes_staging(client):
    c, user_root, s3, _ = client
    s3.objects[KEY] = _archive(
        agents_list=[{"id": "main", "default": True}, {"id": "work", "name": "Work Bot"}],
        extra={"workspace-work/AGENTS.md": "# work\n"},
    )
    import_id = _start(c)
    with patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg:
        reg.side_effect = [
            {"agent_id": "uuid-main", "name": "Quill", "registered": True, "publish_hint": "", "warnings": ["w1"]},
            {"agent_id": "uuid-work", "name": "Work Bot", "registered": True, "publish_hint": "", "warnings": []},
        ]
        r = c.post(
            f"/api/v1/marketplace/import/{import_id}/confirm", json={"agent_ids": ["main", "work"], "mcp_servers": []}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["agent_id"] for a in body["agents"]] == ["uuid-main", "uuid-work"]
    assert body["agents"][0]["warnings"] == ["w1"] and body["restarting"] is False
    first = reg.await_args_list[0].kwargs
    assert first["claim_label"] == f"import:{import_id}"
    assert first["payload"].staging_agent_id == f"{import_id}-main" and first["payload"].name == "Quill"
    assert first["package"].openclaw_slice.get("mcp") in (None, {"servers": {}})  # mcp deselected
    assert reg.await_args_list[1].kwargs["payload"].staging_agent_id == f"{import_id}-work"
    assert not (user_root / f"workspace-{import_id}").exists()


def test_confirm_partial_failure_keeps_staging_and_reports(client):
    c, user_root, s3, _ = client
    s3.objects[KEY] = _archive(
        agents_list=[{"id": "main", "default": True}, {"id": "work", "name": "Work Bot"}],
        extra={"workspace-work/AGENTS.md": "# work\n"},
    )
    import_id = _start(c)
    with patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg:
        reg.side_effect = [
            {"agent_id": "uuid-main", "name": "Quill", "registered": True, "publish_hint": "", "warnings": []},
            HTTPException(status_code=503, detail="container starting — retry in a moment"),
        ]
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={})
    assert r.status_code == 200
    results = r.json()["agents"]
    assert results[0]["ok"] is True and results[1]["ok"] is False and "container starting" in results[1]["error"]
    assert (user_root / f"workspace-{import_id}" / "raw").is_dir()


def test_confirm_ultra_gate_409_for_standard_plan(client):
    c, _, s3, _ = client
    s3.objects[KEY] = _archive(agents_list=[{"id": f"a{i}"} for i in range(9)])
    import_id = _start(c)  # existing 2 + 9 = 11 > 10
    with patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg:
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ultra_required"
    assert r.json()["detail"]["existing_agents"] == 2 and r.json()["detail"]["importing"] == 9
    reg.assert_not_awaited()


def test_confirm_ultra_plan_passes_gate(client):
    c, _, s3, _ = client
    s3.objects[KEY] = _archive(agents_list=[{"id": f"a{i}"} for i in range(9)])
    import_id = _start(c)
    with (
        patch(
            "routers.marketplace_import.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"plan": "ultra_pro"},
        ),
        patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg,
    ):
        reg.return_value = {"agent_id": "u", "name": "n", "registered": True, "publish_hint": "", "warnings": []}
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"agent_ids": ["a0"]})
    assert r.status_code == 200


def _plugin_archive() -> bytes:
    return _archive(
        extra={
            "plugins/installs.json": json.dumps(
                {
                    "version": 1,
                    "plugins": [
                        {
                            "pluginId": "weather",
                            "packageName": "openclaw-weather",
                            "packageVersion": "1.2.0",
                            "origin": "npm",
                            "rootDir": "/home/node/.openclaw/extensions/weather",
                            "enabled": True,
                        }
                    ],
                }
            ),
            "extensions/weather/package.json": json.dumps({"name": "openclaw-weather", "version": "1.2.0"}),
            "extensions/weather/index.js": "module.exports = {}\n",
        }
    )


def test_confirm_with_plugins_installs_and_bounces_container(client):
    c, _, s3, _ = client
    s3.objects[KEY] = _plugin_archive()
    import_id = _start(c)
    mock_ecs = MagicMock()
    mock_ecs.force_redeploy_user_service = AsyncMock()
    with (
        patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg,
        patch("routers.marketplace_import.apply_plugins", new_callable=AsyncMock) as apply,
        patch("routers.marketplace_import.get_ecs_manager", return_value=mock_ecs),
    ):
        reg.return_value = {
            "agent_id": "uuid-main",
            "name": "Quill",
            "registered": True,
            "publish_hint": "",
            "warnings": [],
        }
        apply.return_value = PluginApplyResult(installed=["weather"])
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"plugins": ["weather"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plugins_installed"] == ["weather"]
    assert body["restarting"] is True
    apply.assert_awaited_once()
    assert apply.await_args.kwargs["owner_id"] == OWNER
    assert apply.await_args.kwargs["chosen"] == {"weather"}
    assert [p.id for p in apply.await_args.kwargs["plugins"]] == ["weather"]
    mock_ecs.force_redeploy_user_service.assert_awaited_once_with(OWNER)


def test_confirm_bounce_failure_is_best_effort(client):
    c, user_root, s3, _ = client
    s3.objects[KEY] = _plugin_archive()
    import_id = _start(c)
    mock_ecs = MagicMock()
    mock_ecs.force_redeploy_user_service = AsyncMock(side_effect=RuntimeError("ecs unreachable"))
    with (
        patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg,
        patch("routers.marketplace_import.apply_plugins", new_callable=AsyncMock) as apply,
        patch("routers.marketplace_import.get_ecs_manager", return_value=mock_ecs),
    ):
        reg.return_value = {
            "agent_id": "uuid-main",
            "name": "Quill",
            "registered": True,
            "publish_hint": "",
            "warnings": [],
        }
        apply.return_value = PluginApplyResult(installed=["weather"])
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"plugins": ["weather"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restarting"] is False
    assert any("workspace restart could not be started" in w for w in body["warnings"])
    # all agents succeeded, so staging is still removed despite the bounce failure
    assert not (user_root / f"workspace-{import_id}").exists()


def test_confirm_no_plugins_field_selects_all(client):
    c, _, s3, _ = client
    s3.objects[KEY] = _plugin_archive()
    import_id = _start(c)
    mock_ecs = MagicMock()
    mock_ecs.force_redeploy_user_service = AsyncMock()
    with (
        patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg,
        patch("routers.marketplace_import.apply_plugins", new_callable=AsyncMock) as apply,
        patch("routers.marketplace_import.get_ecs_manager", return_value=mock_ecs),
    ):
        reg.return_value = {
            "agent_id": "uuid-main",
            "name": "Quill",
            "registered": True,
            "publish_hint": "",
            "warnings": [],
        }
        apply.return_value = PluginApplyResult(installed=["weather"])
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={})
    assert r.status_code == 200, r.text
    apply.assert_awaited_once()
    assert apply.await_args.kwargs["chosen"] is None


def test_confirm_plugins_empty_skips_apply_and_bounce(client):
    c, _, s3, _ = client
    s3.objects[KEY] = _plugin_archive()
    import_id = _start(c)
    mock_ecs = MagicMock()
    mock_ecs.force_redeploy_user_service = AsyncMock()
    with (
        patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg,
        patch("routers.marketplace_import.apply_plugins", new_callable=AsyncMock) as apply,
        patch("routers.marketplace_import.get_ecs_manager", return_value=mock_ecs),
    ):
        reg.return_value = {
            "agent_id": "uuid-main",
            "name": "Quill",
            "registered": True,
            "publish_hint": "",
            "warnings": [],
        }
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"plugins": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plugins_installed"] == []
    assert body["restarting"] is False
    apply.assert_not_awaited()
    mock_ecs.force_redeploy_user_service.assert_not_awaited()


def test_confirm_with_include_history_stages_and_bounces_container(client):
    c, _, s3, _ = client
    sqlite_bytes = b"SQLite format 3\x00" + bytes(84)
    s3.objects[KEY] = _archive(
        extra={
            "agents/main/agent/openclaw-agent.sqlite": sqlite_bytes,
            "agents/main/sessions/sessions.json": "{}",
        }
    )
    import_id = _start(c)
    mock_ecs = MagicMock()
    mock_ecs.force_redeploy_user_service = AsyncMock()
    with (
        patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg,
        patch("routers.marketplace_import.stage_history", return_value=["staged"]) as stage,
        patch("routers.marketplace_import.get_ecs_manager", return_value=mock_ecs),
    ):
        reg.return_value = {
            "agent_id": "uuid-main",
            "name": "Quill",
            "registered": True,
            "publish_hint": "",
            "warnings": [],
        }
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"include_history": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["history_staged"] == ["uuid-main"]
    assert body["restarting"] is True
    assert "staged" in body["warnings"]
    stage.assert_called_once()
    assert stage.call_args.kwargs["owner_id"] == OWNER
    assert stage.call_args.kwargs["source_agent_id"] == "main"
    assert stage.call_args.kwargs["new_agent_id"] == "uuid-main"
    mock_ecs.force_redeploy_user_service.assert_awaited_once_with(OWNER)


def test_confirm_without_include_history_skips_stage_history(client):
    c, _, s3, _ = client
    import_id = _start(c)
    mock_ecs = MagicMock()
    mock_ecs.force_redeploy_user_service = AsyncMock()
    with (
        patch("routers.marketplace_import.register_staged_agent_under_claim", new_callable=AsyncMock) as reg,
        patch("routers.marketplace_import.stage_history") as stage,
        patch("routers.marketplace_import.get_ecs_manager", return_value=mock_ecs),
    ):
        reg.return_value = {
            "agent_id": "uuid-main",
            "name": "Quill",
            "registered": True,
            "publish_hint": "",
            "warnings": [],
        }
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["history_staged"] == []
    assert body["restarting"] is False
    stage.assert_not_called()
    mock_ecs.force_redeploy_user_service.assert_not_awaited()


def test_confirm_unknown_import_404_and_bad_id_400(client):
    c, _, _, _ = client
    assert c.post("/api/v1/marketplace/import/import-000000000000/confirm", json={}).status_code == 404
    assert c.post("/api/v1/marketplace/import/not-an-id/confirm", json={}).status_code == 400


def test_discard_removes_staging(client):
    c, user_root, _, _ = client
    import_id = _start(c)
    assert c.delete(f"/api/v1/marketplace/import/{import_id}").status_code == 204
    assert not (user_root / f"workspace-{import_id}").exists()


def test_import_sweeps_stale_staging_dirs(client, monkeypatch):
    c, user_root, _, _ = client
    user_root.mkdir(parents=True, exist_ok=True)
    stale = user_root / "workspace-import-000000000000"
    stale.mkdir()
    (stale / "raw").mkdir()
    old = 0  # epoch → definitely older than 24h
    os.utime(stale, (old, old))
    _start(c)
    assert not stale.exists()
