from pathlib import Path

import yaml

from core.services.hermes_profiles import MANAGED_OVERLAY_YAML

REPO = Path(__file__).resolve().parents[5]
BAKED = REPO / "apps/infra/hermes/managed/config.yaml"


def parsed():
    return yaml.safe_load(MANAGED_OVERLAY_YAML)


def test_the_overlay_pins_bedrock_discovery_off():
    assert parsed()["bedrock"]["discovery"]["enabled"] is False


def test_the_overlay_pins_shared_telemetry_and_updates_off():
    """Fix round 1, I5: the real key is ``updates.check`` (default True, read at ``banner.py:387``
    in the ``lightbulb/hermes-spike:m1-rc4`` image) — ``updates.check_on_start`` has zero
    occurrences anywhere in that image, so the previous pin was a no-op that only ever asserted
    the constant against itself."""
    data = parsed()
    assert data["telemetry"]["shared_metrics"]["enabled"] is False
    assert data["telemetry"]["shared_metrics"]["send"] is False
    assert data["updates"]["check"] is False
    assert "check_on_start" not in data["updates"]


def test_the_overlay_carries_the_approvals_deny_backstop():
    assert "rm -rf /" in parsed()["approvals"]["deny"]


def test_the_overlay_keeps_the_builtin_browser_tools():
    """Hermes 0.21.3 defaults to Browser-Use-CLI mode whenever uvx is runnable, and our base
    image ships /usr/local/bin/uvx. That mode is the FIRST branch of check_browser_requirements
    (browser_tool_install.py:301-303) and returns False, hiding browser_* before the
    executable-path sentinel is ever consulted -- measured in the dev spike, 2026-09-19.
    "off" is Hermes's own documented value for "keep the built-in browser_* tools"."""
    assert parsed()["browser"]["backend"] == "off"


def test_the_overlay_pins_nous_guest_off():
    """M4-R38(b): the Nous free tier is the one credential-less lane on Hermes's own vision
    auto-chain (hermes_cli/anon_auth.py::guest_enabled reads nous.guest, default True) --
    pinned off here since the managed overlay wins AFTER env expansion and cannot be undone
    from inside a profile's own config.yaml. Verified live against the
    lightbulb/hermes-spike:m1-rc4 image: hermes_cli.config.load_config_readonly() (DEFAULT_CONFIG
    + config.yaml + managed scope) reports nous.guest == False once this overlay is on disk at
    $HERMES_MANAGED_DIR, even with HERMES_GUEST_ONBOARDING=1 set."""
    assert parsed()["nous"]["guest"] is False


def test_the_overlay_holds_no_secret_and_no_env_reference():
    # Managed wins AFTER expansion, so a ${VAR} here would be pinned as a literal.
    assert "${" not in MANAGED_OVERLAY_YAML
    assert "sk-" not in MANAGED_OVERLAY_YAML and "pcp_" not in MANAGED_OVERLAY_YAML


def test_the_baked_image_file_is_byte_identical_to_the_constant():
    assert BAKED.read_text() == MANAGED_OVERLAY_YAML


def test_the_dockerfile_installs_it_read_only_at_etc_hermes():
    dockerfile = (REPO / "apps/infra/hermes/Dockerfile.spike").read_text()
    assert "COPY managed/config.yaml /etc/hermes/config.yaml" in dockerfile
    assert "chown root:root /etc/hermes/config.yaml" in dockerfile
    assert "chmod 0444 /etc/hermes/config.yaml" in dockerfile
