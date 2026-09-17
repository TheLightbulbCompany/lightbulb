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
    data = parsed()
    assert data["telemetry"]["shared_metrics"]["enabled"] is False
    assert data["telemetry"]["shared_metrics"]["send"] is False
    assert data["updates"]["check_on_start"] is False


def test_the_overlay_carries_the_approvals_deny_backstop():
    assert "rm -rf /" in parsed()["approvals"]["deny"]


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
