from pathlib import Path

from core.services.credentials.materializers import aws_cli

_EXPECTED_INI = (
    "[default]\n"
    "role_arn = arn:aws:iam::111122223333:role/r\n"
    "credential_source = EcsContainer\n"
    "external_id = ext123\n"
    "region = us-east-1\n"
)


def _cred() -> dict:
    return {
        "role_arn": "arn:aws:iam::111122223333:role/r",
        "external_id": "ext123",
        "region": "us-east-1",
    }


def test_writes_0600_aws_config(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    aws_cli.write("o", "aws", _cred())
    p = Path(tmp_path) / "o" / ".aws" / "config"
    assert p.exists()
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert p.read_text() == _EXPECTED_INI


def test_uses_ecs_container_credential_source_no_source_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    aws_cli.write("o", "aws", _cred())
    body = (Path(tmp_path) / "o" / ".aws" / "config").read_text()
    # EcsContainer credential_source lets the baked `aws` CLI pull task-role creds
    # then AssumeRole into the owner's cross-account role.
    assert "credential_source = EcsContainer" in body
    # source_profile is mutually exclusive with credential_source — it must NOT
    # appear, or the AWS CLI rejects the profile.
    assert "source_profile" not in body


def test_default_profile_so_bare_aws_uses_it(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    aws_cli.write("o", "aws", _cred())
    body = (Path(tmp_path) / "o" / ".aws" / "config").read_text()
    assert body.startswith("[default]\n")


def test_remove_unlinks_config(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    aws_cli.write("o", "aws", _cred())
    p = Path(tmp_path) / "o" / ".aws" / "config"
    assert p.exists()
    aws_cli.remove("o", "aws", _cred())
    assert not p.exists()


def test_remove_is_idempotent_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    # No file written first — remove must not raise (missing_ok=True).
    aws_cli.remove("o", "aws", _cred())
