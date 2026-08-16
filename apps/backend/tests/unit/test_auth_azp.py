"""SEC-8: `azp` (authorized-party) verification in core.auth.

Clerk session tokens carry no `aud` claim; the token-origin fence is the
`azp` claim checked by ``_check_authorized_party`` inside ``_decode_token``
(the single funnel every backend Clerk verification goes through). Audit
mode logs + meters a mismatch without rejecting; enforce mode rejects 401.
Native-app tokens (no `azp`) always pass, matching Clerk SDK semantics.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from core.auth import _check_authorized_party
from core.config import settings

_PARTIES = "https://dev.isol8.co,https://admin.dev.isol8.co"


@pytest.fixture
def azp_settings(monkeypatch):
    """Set the allowlist; return a setter for the mode."""
    monkeypatch.setattr(settings, "CLERK_AUTHORIZED_PARTIES", _PARTIES)

    def _mode(mode: str):
        monkeypatch.setattr(settings, "CLERK_AZP_MODE", mode)

    return _mode


def test_matching_azp_passes(azp_settings):
    azp_settings("enforce")
    _check_authorized_party({"sub": "user_1", "azp": "https://dev.isol8.co"})


def test_missing_azp_passes_even_in_enforce(azp_settings):
    # Mobile (clerk-expo) tokens carry no azp — per Clerk's guidance the
    # check is skipped when the claim is absent. Rejecting here would brick
    # every native client.
    azp_settings("enforce")
    _check_authorized_party({"sub": "user_1"})


def test_empty_allowlist_disables_check(monkeypatch):
    monkeypatch.setattr(settings, "CLERK_AUTHORIZED_PARTIES", "")
    monkeypatch.setattr(settings, "CLERK_AZP_MODE", "enforce")
    _check_authorized_party({"sub": "user_1", "azp": "https://evil.example"})


def test_mismatch_in_audit_logs_and_meters_but_passes(azp_settings, caplog):
    azp_settings("audit")
    with patch("core.auth.put_metric") as metric, caplog.at_level("WARNING"):
        _check_authorized_party({"sub": "user_1", "azp": "https://evil.example"})
    metric.assert_called_once_with("auth.azp.mismatch", dimensions={"mode": "audit"})
    assert any("azp" in r.message for r in caplog.records)


def test_mismatch_in_enforce_rejects_401(azp_settings):
    azp_settings("enforce")
    with patch("core.auth.put_metric") as metric, pytest.raises(HTTPException) as exc:
        _check_authorized_party({"sub": "user_1", "azp": "https://evil.example"})
    assert exc.value.status_code == 401
    metric.assert_called_once_with("auth.azp.mismatch", dimensions={"mode": "enforce"})
