"""Cross-process conformance test for the boot-secret contract (I4).

Every test in test_hermes_provisioning.py monkeypatches agentcore_runtime.write_boot_secret, so
nothing has ever proven the JSON key names or the secret-name template against a REAL reader --
only against a Python kwarg name, which can drift from the wire contract with a green suite on
both sides. Those two strings are a cross-process contract with the bridge
(apps/infra/hermes/bridge/__init__.py:45-54 reads hookToken/gatewayToken/bedrockBearer off
LIGHTBULB_SECRET_ID) and with core.billing.bedrock_gate_tokens._agentcore_presentable_hashes
(the same secret, the same keys, driving gate-token liveness/rotation).

This test writes with the REAL backend function (moto Secrets Manager) and reads with the
bridge's OWN reader, bridge.sync.load_boot_secrets, so a typo'd key fails here first instead of
shipping green on both sides and failing at container boot.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

# The bridge lives outside the backend's own package tree (apps/infra/hermes), so it is not on
# sys.path by default -- put it there before importing bridge.sync. Mirrors
# test_hermes_inbound_contract.py's own setup.
_HERMES_ROOT = Path(__file__).resolve().parents[4] / "infra" / "hermes"
if str(_HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(_HERMES_ROOT))

from bridge import sync as bridge_sync  # noqa: E402

from core.services import agentcore_runtime  # noqa: E402


async def test_write_boot_secret_round_trips_through_the_bridges_own_reader():
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")

        assert await agentcore_runtime.write_boot_secret(
            "org_a",
            hook_token="hook-1",
            gateway_token="gw-1",
            bedrock_bearer="bearer-1",
            provider_choice="openrouter_managed",
        )
        name = agentcore_runtime.boot_secret_name("org_a")
        assert name.startswith("lightbulb/")
        assert name.endswith("/org_a/boot")

        loaded = bridge_sync.load_boot_secrets(name, client=sm)
        assert loaded == {
            "hookToken": "hook-1",
            "gatewayToken": "gw-1",
            "bedrockBearer": "bearer-1",
            "providerChoice": "openrouter_managed",
            "providerSwitchFrozen": False,
        }


async def test_write_boot_secret_update_also_round_trips(monkeypatch):
    """create_or_update_secret's UPDATE branch (a rotation / a retry) must produce the same
    readable shape as the CREATE branch."""
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-east-1")

        await agentcore_runtime.write_boot_secret(
            "org_b",
            hook_token="hook-old",
            gateway_token="gw-old",
            bedrock_bearer="bearer-old",
            provider_choice="chatgpt_oauth",
        )
        changed = await agentcore_runtime.write_boot_secret(
            "org_b",
            hook_token="hook-old",
            gateway_token="gw-old",
            bedrock_bearer="bearer-new",
            provider_choice="chatgpt_oauth",
        )

        loaded = bridge_sync.load_boot_secrets(agentcore_runtime.boot_secret_name("org_b"), client=sm)
        assert loaded["bedrockBearer"] == "bearer-new"
        assert changed is True  # the bearer is a boot-time input


async def test_only_a_provider_choice_change_is_not_a_boot_time_change():
    with mock_aws():
        kwargs = {"hook_token": "h", "gateway_token": "g", "bedrock_bearer": "b"}
        assert (
            await agentcore_runtime.write_boot_secret("org_c", provider_choice="openrouter_managed", **kwargs) is True
        )
        assert (
            await agentcore_runtime.write_boot_secret("org_c", provider_choice="openrouter_managed", **kwargs) is False
        )
        assert await agentcore_runtime.write_boot_secret("org_c", provider_choice="chatgpt_oauth", **kwargs) is False
