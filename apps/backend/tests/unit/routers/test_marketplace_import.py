"""HTTP contract for the backup-import door. The normalizer + staging export
have their own unit files; here: presign shape + owner-scoped key, S3 stream →
preview, Ultra gate, per-agent confirm wiring, cleanup."""

import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.containers import workspace as workspace_module  # noqa: E402
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

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append({"op": op, **Params, "ExpiresIn": ExpiresIn})
        return f"https://s3.example/{Params['Key']}?sig=1"

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise marketplace_import.ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
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


def test_import_rejects_foreign_or_malformed_key(client):
    c, _, _, _ = client
    assert c.post("/api/v1/marketplace/import", json={"key": "imports/someone_else/abc.tar.gz"}).status_code == 400
    assert c.post("/api/v1/marketplace/import", json={"key": f"imports/{OWNER}/../x.tar.gz"}).status_code == 400


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
