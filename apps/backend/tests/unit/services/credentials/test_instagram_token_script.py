"""Behavioural test for skills/instagram/token.sh — the in-container token renewer.

The script only ever runs inside the Debian container, so it needs GNU date
(``date -d <iso>``) and jq. Both exist on the CI runner; a dev box without them
skips rather than reporting a phantom failure.

``curl`` is stubbed onto PATH so the renewal branch is exercised with no network:
the stub records that it was called and prints whatever Meta would have.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TOKEN_SCRIPT = (
    Path(__file__).resolve().parents[4] / "core" / "services" / "credentials" / "skills" / "instagram" / "token.sh"
)


def _has_gnu_date() -> bool:
    # BSD/macOS date has no `-d <iso>`; the container's GNU date does.
    return subprocess.run(["date", "-d", "2026-01-01T00:00:00+00:00", "+%s"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or not _has_gnu_date(),
    reason="token.sh needs jq + GNU date (present in the container and on CI runners)",
)


def _write_credentials(tmp_path: Path, *, days_to_expiry: int) -> Path:
    expiry = datetime.now(timezone.utc) + timedelta(days=days_to_expiry)
    cred = tmp_path / "credentials.json"
    cred.write_text(
        json.dumps(
            {
                "access_token": "OLD_TOKEN",
                "user_id": "17841400000000000",
                "username": "isol8.ai",
                "token_expiry": expiry.isoformat(),
            }
        )
    )
    return cred


def _stub_curl(tmp_path: Path, *, body: str) -> Path:
    """A fake curl that records invocation and prints `body`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "curl-was-called"
    (bindir / "curl").write_text(f"#!/bin/bash\ntouch \"{marker}\"\ncat <<'JSON'\n{body}\nJSON\n")
    (bindir / "curl").chmod(0o755)
    return marker


def _run(cred: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        "IG_CREDENTIALS": str(cred),
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
    }
    return subprocess.run(["bash", str(TOKEN_SCRIPT)], capture_output=True, text=True, env=env)


def test_outside_window_returns_token_without_calling_meta(tmp_path):
    """A token with 45 days left is nowhere near expiry — no network, no rewrite."""
    cred = _write_credentials(tmp_path, days_to_expiry=45)
    marker = _stub_curl(tmp_path, body='{"access_token":"SHOULD_NOT_BE_USED","expires_in":5184000}')

    result = _run(cred, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "OLD_TOKEN"
    assert not marker.exists(), "token.sh called Meta outside the renewal window"
    assert json.loads(cred.read_text())["access_token"] == "OLD_TOKEN"


def test_inside_window_renews_and_persists(tmp_path):
    """Inside 30 days: renew, hand back the NEW token, and persist it for next time."""
    cred = _write_credentials(tmp_path, days_to_expiry=10)
    marker = _stub_curl(tmp_path, body='{"access_token":"NEW_TOKEN","token_type":"bearer","expires_in":5184000}')

    result = _run(cred, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "NEW_TOKEN"
    assert marker.exists(), "token.sh did not attempt a renewal inside the window"

    persisted = json.loads(cred.read_text())
    assert persisted["access_token"] == "NEW_TOKEN"
    assert persisted["expires_in"] == 5184000
    # Unrelated fields must survive the rewrite — the agent reads user_id from here.
    assert persisted["user_id"] == "17841400000000000"
    assert persisted["username"] == "isol8.ai"
    # The refreshed expiry is ~60 days out, so the next call sits outside the window.
    assert (datetime.fromisoformat(persisted["token_expiry"]) - datetime.now(timezone.utc)).days > 55


def test_failed_renewal_still_returns_the_existing_token(tmp_path):
    """Meta erroring must not break the caller: the held token is still valid."""
    cred = _write_credentials(tmp_path, days_to_expiry=10)
    _stub_curl(tmp_path, body='{"error":{"message":"rate limited","code":4}}')

    result = _run(cred, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "OLD_TOKEN"
    assert json.loads(cred.read_text())["access_token"] == "OLD_TOKEN"


def test_missing_credentials_fails_loudly(tmp_path):
    """No credentials file: non-zero exit so `$(token.sh) || exit 1` stops the task."""
    _stub_curl(tmp_path, body="{}")
    result = _run(tmp_path / "absent.json", tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "reconnect" in result.stderr.lower()
