import tomllib
from pathlib import Path

from core.services.credentials.materializers import toml_cli


def test_writes_0600_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(toml_cli, "materializer_root", lambda: tmp_path / "org_1")
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
    monkeypatch.setattr(toml_cli, "materializer_root", lambda: tmp_path / "org_1")
    toml_cli.write("org_1", "x", {"cli": "x-pp-cli", "access_token": "a"})
    p = Path(tmp_path) / "org_1" / ".config" / "x-pp-cli" / "config.toml"
    data = tomllib.loads(p.read_text())
    assert data == {"access_token": "a"}  # empty refresh/client fields omitted


def test_remove_unlinks_config(tmp_path, monkeypatch):
    monkeypatch.setattr(toml_cli, "materializer_root", lambda: tmp_path / "org_1")
    cred = {"cli": "outlook-email-pp-cli", "access_token": "a"}
    toml_cli.write("org_1", "outlook", cred)
    p = Path(tmp_path) / "org_1" / ".config" / "outlook-email-pp-cli" / "config.toml"
    assert p.exists()
    toml_cli.remove("org_1", "outlook", cred)
    assert not p.exists()


def test_remove_is_idempotent_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(toml_cli, "materializer_root", lambda: tmp_path / "org_1")
    # No file written first — remove must not raise.
    toml_cli.remove("org_1", "outlook", {"cli": "outlook-email-pp-cli"})


def test_remove_no_cli_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(toml_cli, "materializer_root", lambda: tmp_path / "org_1")
    # Missing "cli" key — guard prevents an exception.
    toml_cli.remove("org_1", "outlook", {})
