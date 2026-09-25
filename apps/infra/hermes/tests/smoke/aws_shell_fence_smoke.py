"""Prove the pinned Hermes child-env builders cannot inherit the runtime AWS role."""

import os
import tempfile
from pathlib import Path

from bridge import sync, turn
from tools.environments import local


with tempfile.TemporaryDirectory() as root:
    sync.HOME = Path(root)
    for agent in ("ceo", "engineering"):
        sync.ensure_profile_home(agent)

    # Fake platform sources. Never use real credentials in this smoke.
    os.environ["AWS_ACCESS_KEY_ID"] = "platform-access"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "platform-secret"
    os.environ["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = "http://platform-credentials"
    for agent in ("ceo", "engineering"):
        with turn._hermes_home_scope(agent), turn._secret_scope(agent), turn._terminal_scope(agent, f"agent:{agent}:lightbulb:channel:r1"):
            for child in (local._make_run_env({}), local._sanitize_subprocess_env(os.environ.copy())):
                assert "AWS_ACCESS_KEY_ID" not in child
                assert "AWS_SECRET_ACCESS_KEY" not in child
                assert "AWS_CONTAINER_CREDENTIALS_FULL_URI" not in child
                assert child["AWS_PROFILE"] == "default"
                assert child["AWS_EC2_METADATA_DISABLED"] == "true"
                assert child["AWS_CONFIG_FILE"] == str(sync._profile_dir(agent) / "home/.aws/config")
                assert child["AWS_SHARED_CREDENTIALS_FILE"] == str(
                    sync._profile_dir(agent) / "home/.aws/credentials"
                )
