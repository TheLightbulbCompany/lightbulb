import json
from pathlib import Path

from core.services.credentials.materializers import gog_keyring


def test_writes_client_config_and_token(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    cred = {
        "client_id": "cid",
        "client_secret": "cs",
        "refresh_token": "r",
        "access_token": "a",
        "account_email": "u@example.com",
    }
    gog_keyring.write("org_1", "google", cred)
    base = Path(tmp_path) / "org_1" / ".config" / "gogcli"
    cfg = json.loads((base / "config.json").read_text())
    assert cfg["installed"]["client_id"] == "cid"
    assert oct((base / "config.json").stat().st_mode)[-3:] == "600"
    tok = json.loads((base / "tokens.json").read_text())
    assert tok["refresh_token"] == "r"
    assert oct((base / "tokens.json").stat().st_mode)[-3:] == "600"


def test_remove_unlinks_both_files(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    cred = {"client_id": "cid", "client_secret": "cs", "refresh_token": "r"}
    gog_keyring.write("org_1", "google", cred)
    base = Path(tmp_path) / "org_1" / ".config" / "gogcli"
    assert (base / "config.json").exists() and (base / "tokens.json").exists()
    gog_keyring.remove("org_1", "google", cred)
    assert not (base / "config.json").exists()
    assert not (base / "tokens.json").exists()


def test_remove_is_idempotent_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    # No files written first — remove must not raise.
    gog_keyring.remove("org_1", "google", {})
