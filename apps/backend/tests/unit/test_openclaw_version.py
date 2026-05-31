"""Tests for the single-source-of-truth OpenClaw version resolver.

core/openclaw_version.py resolves the pinned tags from env vars (injected by
CDK in deployed containers) with a fallback to reading openclaw-version.json
directly (local dev / CI). These tests verify both paths plus that the JSON
fallback matches the actual repo file — the check the old per-constant drift
tests used to provide, now in one place.
"""

import json
from pathlib import Path

import pytest

from core import openclaw_version


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    # The loader memoizes the repo JSON; clear it so each test sees a clean
    # state, and drop any ambient env vars so fallback behavior is testable.
    openclaw_version._load_pinned.cache_clear()
    monkeypatch.delenv("OPENCLAW_UPSTREAM_VERSION", raising=False)
    monkeypatch.delenv("OPENCLAW_DEV_TAG", raising=False)
    monkeypatch.delenv("OPENCLAW_PROD_TAG", raising=False)
    yield
    openclaw_version._load_pinned.cache_clear()


def _pinned() -> dict:
    # apps/backend/tests/unit/test_openclaw_version.py -> repo root is parents[4]
    repo_root = Path(__file__).resolve().parents[4]
    return json.loads((repo_root / "openclaw-version.json").read_text())


def test_json_fallback_matches_repo_file():
    """With no env vars set, the resolver reads openclaw-version.json and
    returns exactly what the repo file pins."""
    pinned = _pinned()
    assert openclaw_version.upstream_version() == pinned["tag"]
    assert openclaw_version.dev_tag() == pinned["dev"]["tag"]
    assert openclaw_version.prod_tag() == pinned["prod"]["tag"]


def test_env_var_takes_precedence(monkeypatch):
    """CDK-injected env vars win over the JSON fallback (the deployed-container
    path, where the JSON file isn't present in the image)."""
    monkeypatch.setenv("OPENCLAW_UPSTREAM_VERSION", "9999.9.9")
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "9999.9.9-devsha")
    monkeypatch.setenv("OPENCLAW_PROD_TAG", "9999.9.9-prodsha")
    assert openclaw_version.upstream_version() == "9999.9.9"
    assert openclaw_version.dev_tag() == "9999.9.9-devsha"
    assert openclaw_version.prod_tag() == "9999.9.9-prodsha"


def test_upstream_version_falls_back_when_no_env_and_no_json(monkeypatch):
    """Non-CDK docker run: no env vars AND repo JSON unreachable -> a non-empty
    hardcoded fallback, so meta.lastTouchedVersion never ends up empty (which
    would reintroduce OpenClaw's auto-restore/version warning)."""
    monkeypatch.setattr(openclaw_version, "_load_pinned", lambda: {})
    assert openclaw_version.upstream_version() == openclaw_version._FALLBACK_UPSTREAM_VERSION
    assert openclaw_version.upstream_version()  # non-empty
