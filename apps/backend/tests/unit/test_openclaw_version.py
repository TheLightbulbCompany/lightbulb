"""Tests for the single-source-of-truth OpenClaw version resolver.

core/openclaw_version.py resolves the pinned tags from env vars (injected by
CDK in deployed containers) with a fallback to reading openclaw-version.json
directly (local dev / CI), then a hardcoded last-resort default. These tests
cover all three paths plus that the JSON fallback matches the actual repo file
— the check the old per-constant drift tests used to provide, now in one place.
"""

import json
from pathlib import Path

import pytest

from core import openclaw_version


def _clear_pinned_cache():
    # A test may monkeypatch _load_pinned with a plain function (no lru_cache).
    # The autouse fixture's teardown runs before monkeypatch reverts, so guard
    # the cache_clear via getattr to avoid AttributeError on the stand-in.
    clear = getattr(openclaw_version._load_pinned, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    # The loader memoizes the repo JSON; clear it so each test sees a clean
    # state, and drop any ambient env vars so fallback behavior is testable.
    _clear_pinned_cache()
    monkeypatch.delenv("OPENCLAW_UPSTREAM_VERSION", raising=False)
    monkeypatch.delenv("OPENCLAW_DEV_TAG", raising=False)
    monkeypatch.delenv("OPENCLAW_PROD_TAG", raising=False)
    yield
    _clear_pinned_cache()


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


def test_load_pinned_no_crash_for_shallow_image_path(monkeypatch, tmp_path):
    """In the deployed backend image __file__ is /app/core/openclaw_version.py
    (only 3 ancestors) and no openclaw-version.json exists above it. The upward
    walk must return {} without IndexError — the old hardcoded parents[3]
    crashed here, defeating the very fallback this module adds."""
    fake_file = tmp_path / "app" / "core" / "openclaw_version.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("")
    monkeypatch.setattr(openclaw_version, "__file__", str(fake_file))
    _clear_pinned_cache()

    assert openclaw_version._load_pinned() == {}  # must not raise
    # With no JSON found and no env var, upstream_version still yields the
    # non-empty hardcoded fallback.
    assert openclaw_version.upstream_version() == openclaw_version._FALLBACK_UPSTREAM_VERSION


def test_deployed_upstream_version_uses_dev_tag_prefix(monkeypatch):
    """meta.lastTouchedVersion tracks the DEPLOYED image: in dev it's the
    <upstream> prefix of dev.tag, NOT the build-target `tag` (which can run
    ahead during the bump → promotion window)."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("OPENCLAW_UPSTREAM_VERSION", "2026.5.28")
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "2026.5.12-0654e00")
    monkeypatch.setenv("OPENCLAW_PROD_TAG", "2026.5.12-0654e00")
    assert openclaw_version.deployed_upstream_version() == "2026.5.12"


def test_deployed_upstream_version_uses_prod_tag_in_prod(monkeypatch):
    """In prod, derive from prod.tag — independent of a more-advanced dev.tag
    during a staggered promotion."""
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("OPENCLAW_UPSTREAM_VERSION", "2026.5.28")
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "2026.5.28-devsha")
    monkeypatch.setenv("OPENCLAW_PROD_TAG", "2026.5.12-prodsha")
    assert openclaw_version.deployed_upstream_version() == "2026.5.12"


def test_deployed_upstream_version_falls_back_to_tag_when_no_deployed_tag(monkeypatch):
    """With no deployed dev/prod tag resolvable, fall back to the build-target
    upstream version rather than empty (empty reintroduces the warning)."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setattr(openclaw_version, "_load_pinned", lambda: {"tag": "2026.5.28"})
    assert openclaw_version.deployed_upstream_version() == "2026.5.28"


def test_deployed_upstream_at_least_true_when_dev_tag_is_newer(monkeypatch):
    """The gate is satisfied once this env's deployed image reaches the target;
    drives emission of 6.1-only config keys (e.g. mcp.runtimeScope)."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "2026.6.1-isol8.1-9a12256")
    assert openclaw_version.deployed_upstream_at_least("2026.6.1") is True


def test_deployed_upstream_at_least_false_when_dev_tag_is_older(monkeypatch):
    """A container still on the pre-6.1 image must NOT get 6.1-only keys — its
    strict schema would reject them at boot."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "2026.5.28-9ae309b")
    assert openclaw_version.deployed_upstream_at_least("2026.6.1") is False


def test_deployed_upstream_at_least_is_per_environment(monkeypatch):
    """Gate flips per-env from the relevant tag: prod still on 5.28 stays gated
    even while dev.tag has moved to 6.1 (staggered promotion)."""
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "2026.6.1-isol8.1-9a12256")
    monkeypatch.setenv("OPENCLAW_PROD_TAG", "2026.5.28-9ae309b")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert openclaw_version.deployed_upstream_at_least("2026.6.1") is False
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert openclaw_version.deployed_upstream_at_least("2026.6.1") is True


def test_deployed_upstream_at_least_equal_boundary(monkeypatch):
    """Equal version satisfies the >= gate (it's at-least, not strictly-after)."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "2026.6.1-isol8.1-9a12256")
    assert openclaw_version.deployed_upstream_at_least("2026.6.1") is True


# --- fork patch level (isol8.N) gates ------------------------------------------


@pytest.mark.parametrize(
    "tag, level",
    [
        ("2026.7.2-isol8.8-8f700f0", 8),
        ("2026.7.2-isol8.6-303527a", 6),
        ("2026.7.2-isol8.10", 10),
        ("2026.5.12-0654e00", 0),  # pre-fork tag: no isol8 segment
        ("sha256:abc", 0),
        ("", 0),
        (None, 0),
    ],
)
def test_isol8_patch_level(tag, level):
    assert openclaw_version.isol8_patch_level(tag) == level


def test_deployed_fork_at_least_explicit_tag_wins_over_pin(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("OPENCLAW_PROD_TAG", "2026.7.2-isol8.8-8f700f0")
    # A live container still on isol8.6 must NOT pass the gate just because
    # the environment pin already moved on.
    assert openclaw_version.deployed_fork_at_least("2026.7.2", 7, tag="2026.7.2-isol8.6-303527a") is False
    assert openclaw_version.deployed_fork_at_least("2026.7.2", 7, tag="2026.7.2-isol8.8-8f700f0") is True


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("2026.7.2-isol8.7-abc1234", True),  # the patch that introduced the feature
        ("2026.7.2-isol8.6-303527a", False),  # same base, one patch too early
        ("2026.8.1-isol8.1-abc1234", True),  # newer base: counter restarts, pick carried forward
        ("2026.9.0", True),  # newer base with no fork segment at all
        ("2026.7.1-isol8.9-abc1234", False),  # older base, however many patches
        ("2026.5.12-0654e00", False),  # pre-fork tag
        ("", False),  # unreadable image
    ],
)
def test_deployed_fork_at_least_is_base_aware(tag, expected):
    assert openclaw_version.deployed_fork_at_least("2026.7.2", 7, tag=tag) is expected


def test_deployed_fork_at_least_uses_env_pin_when_no_tag(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("OPENCLAW_PROD_TAG", "2026.7.2-isol8.8-8f700f0")
    monkeypatch.setenv("OPENCLAW_DEV_TAG", "2026.7.2-isol8.6-303527a")
    assert openclaw_version.deployed_fork_at_least("2026.7.2", 7) is True
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert openclaw_version.deployed_fork_at_least("2026.7.2", 7) is False
