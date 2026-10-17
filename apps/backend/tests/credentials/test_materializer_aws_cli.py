import http.server
import json
import shutil
import os
import subprocess
import threading
from pathlib import Path

import pytest

from core.services.credentials.materializers import aws_cli


def _cred() -> dict:
    return {
        "role_arn": "arn:aws:iam::111122223333:role/r",
        "external_id": "ext123",
        "region": "us-east-1",
        "cli_token_hash": "deadbeef",
        "cli_token": "tok_plain_abc",
    }


def _write(tmp_path, monkeypatch, owner="o") -> Path:
    # Materializers only run inside a projection (credentials/hermes_cli.py), whose scratch root
    # stands for one owner's home -- so the root is the ContextVar's, never a settings path.
    monkeypatch.setattr(aws_cli, "materializer_root", lambda: tmp_path / owner)
    monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "https://api-test.isol8.co")
    aws_cli.write(owner, "aws", _cred())
    return Path(tmp_path) / owner / ".aws"


def test_config_uses_credential_process_and_no_secrets(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    body = (aws_dir / "config").read_text()
    # Exactly ONE section, and it resolves the helper through the SHELL's $HOME -- the only
    # path that works when the file is a per-profile projection rather than a fixed mount.
    assert body == (
        "[default]\n"
        "region = us-east-1\n"
        "credential_process = sh -c 'exec /usr/bin/env node \"$HOME/.aws/lightbulb-creds.mjs\"'\n"
    )
    assert "isol8" not in body
    # The config must carry no assume-role or secret material: a profile mixing
    # role_arn with credential_process would flip botocore into assume-role mode,
    # and external_id must never leave the backend.
    for forbidden in ("role_arn", "external_id", "credential_source", "cli_token", "aws_access_key_id"):
        assert forbidden not in body


def test_profile_is_default_because_no_in_process_sdk_can_read_it(tmp_path, monkeypatch):
    """The ECS rail needed a NAMED profile: the gateway process's own $HOME was the mount
    holding this file, so a [default] section would have re-credentialled its Bedrock calls.
    In the microVM only the CHILD shell's HOME is rewritten to the profile, so nothing
    in-process can read it -- and [default] is what lets the shell work with no AWS_PROFILE
    to deliver at all."""
    body = (_write(tmp_path, monkeypatch) / "config").read_text()
    assert body.startswith("[default]\n")
    assert "[profile " not in body


def test_file_modes(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    assert oct((aws_dir / "config").stat().st_mode)[-3:] == "600"
    assert oct((aws_dir / aws_cli.HELPER_FILENAME).stat().st_mode)[-3:] == "700"
    assert oct((aws_dir / aws_cli.TOKEN_FILENAME).stat().st_mode)[-3:] == "600"


def test_token_file_holds_owner_and_plaintext_token(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch, owner="org_x")
    tok = json.loads((aws_dir / aws_cli.TOKEN_FILENAME).read_text())
    assert tok == {"owner_id": "org_x", "token": "tok_plain_abc"}


def test_helper_bakes_api_base_and_no_placeholders(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    helper = (aws_dir / aws_cli.HELPER_FILENAME).read_text()
    assert '(process.env.ISOL8_API_URL || "https://api-test.isol8.co")' in helper
    assert "__API_BASE__" not in helper


def test_remove_unlinks_all_artifacts_and_is_idempotent(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    # Simulate a runtime cache the helper wrote.
    (aws_dir / aws_cli.CACHE_FILENAME).write_text("{}")
    aws_cli.remove("o", "aws", _cred())
    for name in ("config", aws_cli.HELPER_FILENAME, aws_cli.TOKEN_FILENAME, aws_cli.CACHE_FILENAME):
        assert not (aws_dir / name).exists()
    aws_cli.remove("o", "aws", _cred())  # second remove must not raise


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Stub credential endpoint: asserts bearer + owner and returns fixed creds."""

    calls: list[dict] = []
    creds = {
        "Version": 1,
        "AccessKeyId": "ASIATEST",
        "SecretAccessKey": "secret",
        "SessionToken": "session",
        "Expiration": "2099-01-01T00:00:00+00:00",
    }

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _StubHandler.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        payload = json.dumps(_StubHandler.creds).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # keep pytest output clean
        pass


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_helper_prefers_the_runtimes_api_url_over_the_baked_one(tmp_path, monkeypatch):
    """A helper written before a domain move must follow the runtime's ISOL8_API_URL."""
    _StubHandler.calls = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(aws_cli, "materializer_root", lambda: tmp_path / "org_x")
        monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "http://127.0.0.1:9")  # dead
        aws_cli.write("org_x", "aws", _cred())
        helper = Path(tmp_path) / "org_x" / ".aws" / aws_cli.HELPER_FILENAME
        env = {**os.environ, "ISOL8_API_URL": f"http://127.0.0.1:{server.server_port}/"}
        r = subprocess.run(["node", str(helper)], capture_output=True, text=True, timeout=30, env=env)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == _StubHandler.creds and len(_StubHandler.calls) == 1
    finally:
        server.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_helper_end_to_end_fetch_then_cache(tmp_path, monkeypatch):
    """Run the ACTUAL materialized helper under node against a local stub:
    first invocation fetches + caches; second serves the cache (no new call)."""
    _StubHandler.calls = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(aws_cli, "materializer_root", lambda: tmp_path / "org_x")
        monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", f"http://127.0.0.1:{server.server_port}")
        aws_cli.write("org_x", "aws", _cred())
        helper = Path(tmp_path) / "org_x" / ".aws" / aws_cli.HELPER_FILENAME

        r1 = subprocess.run(["node", str(helper)], capture_output=True, text=True, timeout=30)
        assert r1.returncode == 0, r1.stderr
        assert json.loads(r1.stdout) == _StubHandler.creds
        assert len(_StubHandler.calls) == 1
        assert _StubHandler.calls[0]["auth"] == "Bearer tok_plain_abc"
        assert _StubHandler.calls[0]["body"] == {"owner_id": "org_x"}

        r2 = subprocess.run(["node", str(helper)], capture_output=True, text=True, timeout=30)
        assert r2.returncode == 0, r2.stderr
        assert json.loads(r2.stdout) == _StubHandler.creds
        assert len(_StubHandler.calls) == 1  # cache hit — no second fetch
    finally:
        server.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_helper_rejects_malformed_200_body(tmp_path, monkeypatch):
    """A 200 with a truthy-but-malformed body ({}) must not be emitted or
    cached as credentials — with no cache the helper exits 1 with a lightbulb:
    error instead of handing the CLI an empty credential object."""
    _StubHandler.calls = []
    orig_creds = _StubHandler.creds
    _StubHandler.creds = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(aws_cli, "materializer_root", lambda: tmp_path / "org_x")
        monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", f"http://127.0.0.1:{server.server_port}")
        aws_cli.write("org_x", "aws", _cred())
        aws_dir = Path(tmp_path) / "org_x" / ".aws"
        r = subprocess.run(["node", str(aws_dir / aws_cli.HELPER_FILENAME)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 1
        assert r.stdout == ""  # nothing emitted — the CLI must not see {} as creds
        assert "lightbulb" in r.stderr
        # And the malformed payload must not poison the cache.
        assert not (aws_dir / aws_cli.CACHE_FILENAME).exists()
    finally:
        _StubHandler.creds = orig_creds
        server.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_helper_clear_error_when_backend_unreachable_and_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(aws_cli, "materializer_root", lambda: tmp_path / "org_x")
    # Unroutable port — connection refused immediately.
    monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "http://127.0.0.1:9")
    aws_cli.write("org_x", "aws", _cred())
    helper = Path(tmp_path) / "org_x" / ".aws" / aws_cli.HELPER_FILENAME
    r = subprocess.run(["node", str(helper)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "lightbulb" in r.stderr  # names us, not a bare stack trace
