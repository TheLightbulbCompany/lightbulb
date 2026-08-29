"""The in-container paths and the CLI pin the provisioning rail depends on,
asserted against the sources so an edit cannot drift them silently."""

import os
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

_INFRA = Path(__file__).resolve().parents[4] / "infra"
_STACK = _INFRA / "lib" / "stacks" / "container-stack.ts"
_DOCKERFILE = _INFRA / "openclaw" / "Dockerfile"
_SKILL = _INFRA / "openclaw" / "skills" / "lightbulb" / "SKILL.md"

# The CWD Friday runs the CLI in — the EFS root, a sibling of every
# workspace-{agent_id}/, which is what keeps `.projects/` out of a marketplace
# artifact by construction (see test_agent_export_projects_exclusions.py).
EFS_HOME = "/home/node/.openclaw"


def test_efs_home_is_the_container_mount_point():
    assert f'containerPath: "{EFS_HOME}"' in _STACK.read_text()


def test_xdg_config_home_is_under_the_efs_mount():
    assert f'XDG_CONFIG_HOME: "{EFS_HOME}/.config"' in _STACK.read_text()


def test_stripe_cli_is_pinned_with_a_checksum():
    body = _DOCKERFILE.read_text()
    assert "ARG STRIPE_CLI_VERSION=" in body
    assert "ARG STRIPE_CLI_SHA256=" in body
    assert "sha256sum -c -" in body


def test_the_skill_tells_friday_to_run_the_cli_in_the_efs_root():
    """If the skill ever names a workspace dir instead, `.projects/` starts
    landing inside an exportable agent workspace."""
    assert EFS_HOME in _SKILL.read_text()
