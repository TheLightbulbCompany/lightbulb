import tomllib
from pathlib import Path

from core.services.credentials.materializers import toml_cli


def test_writes_0600_toml(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    cred = {
        "cli": "outlook-email-pp-cli",
        "access_token": "a",
        "refresh_token": "r",
        "token_expiry": "2026-06-01T00:00:00Z",
        "client_id": "cid",
        "client_secret": "cs",
    }
    toml_cli.write("org_1", "outlook", cred)
    p = Path(tmp_path) / "org_1" / ".config" / "outlook-email-pp-cli" / "config.toml"
    assert p.exists()
    assert oct(p.stat().st_mode)[-3:] == "600"
    data = tomllib.loads(p.read_text())
    assert data["refresh_token"] == "r" and data["client_id"] == "cid"


def test_omits_empty_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    toml_cli.write("org_1", "x", {"cli": "x-pp-cli", "access_token": "a"})
    p = Path(tmp_path) / "org_1" / ".config" / "x-pp-cli" / "config.toml"
    data = tomllib.loads(p.read_text())
    assert data == {"access_token": "a"}  # empty refresh/client fields omitted
