"""The openclaw.json schema gate (issue #941).

Covers the validator itself, both rollout modes, the schema-unavailable
degradation, and the one write path that ENFORCES: a marketplace seller slice
merged by apply_deploy_aux_mutations.
"""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import config_schema  # noqa: E402
from core.services.config_patcher import (  # noqa: E402
    ConfigPatchError,
    apply_deploy_aux_mutations,
    patch_openclaw_config,
)

# Minimal but REAL shapes — everything here is drawn from the vendored schema,
# not invented, so a failure means the gate broke rather than the fixture.
VALID_CONFIG = {
    "gateway": {"bind": "lan"},
    "agents": {"list": [{"id": "agent_a", "name": "Friday"}]},
    "plugins": {"entries": {"memory-core": {"enabled": True}}},
    "tools": {"profile": "full"},
}


@pytest.fixture(autouse=True)
def _clear_validator_cache():
    """The compiled validator is process-cached; tests that stub the schema path
    must not leak that stub into the next test."""
    config_schema._validator.cache_clear()
    yield
    config_schema._validator.cache_clear()


@pytest.fixture
def efs_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = os.path.join(tmpdir, "user_1")
        os.makedirs(user_dir)
        with open(os.path.join(user_dir, "openclaw.json"), "w") as f:
            json.dump(VALID_CONFIG, f)
        with patch("core.services.config_patcher._efs_mount_path", tmpdir):
            yield tmpdir


# --- the validator ----------------------------------------------------------


def test_valid_config_has_no_errors():
    assert config_schema.schema_errors(VALID_CONFIG) == []


@pytest.mark.parametrize(
    "mutation",
    [
        # Every one of these is a shape OpenClaw's loader rejects at boot. The
        # first three are verbatim from the live dev brick in issue #941.
        pytest.param({"gateway": {"bind": "bogus"}}, id="gateway.bind-not-in-enum"),
        pytest.param({"hooks": "not-an-object"}, id="hooks-scalar"),
        pytest.param({"web": 12345}, id="web-scalar"),
        pytest.param({"tools": {"profile": "full", "allowed": []}}, id="tools.allowed-unknown-key"),
    ],
)
def test_invalid_shapes_are_reported(mutation):
    doc = {**VALID_CONFIG, **mutation}
    errors = config_schema.schema_errors(doc)
    assert errors, f"expected {mutation} to be rejected by the vendored schema"


def test_schema_unavailable_returns_none_not_errors():
    with patch.object(config_schema, "_SCHEMA_PATH", config_schema._SCHEMA_PATH.with_name("nope.json")):
        config_schema._validator.cache_clear()
        assert config_schema.schema_errors(VALID_CONFIG) is None


# --- the gate ---------------------------------------------------------------


def test_gate_audits_by_default(caplog):
    bad = {**VALID_CONFIG, "gateway": {"bind": "bogus"}}
    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "audit"):
        config_schema.gate_config(bad, owner_id="user_1", context="unit")  # does not raise
    assert "SCHEMA-INVALID" in caplog.text


def test_gate_enforces_when_fleet_knob_is_set():
    bad = {**VALID_CONFIG, "gateway": {"bind": "bogus"}}
    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "enforce"):
        with pytest.raises(config_schema.ConfigSchemaError):
            config_schema.gate_config(bad, owner_id="user_1", context="unit")


def test_gate_enforces_untrusted_even_in_audit_mode():
    bad = {**VALID_CONFIG, "gateway": {"bind": "bogus"}}
    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "audit"):
        with pytest.raises(config_schema.ConfigSchemaError):
            config_schema.gate_config(bad, owner_id="user_1", context="unit", enforce=True)


def test_gate_degrades_to_audit_when_schema_unavailable():
    """A missing artifact is OUR bug — it must not turn every config write into
    a failure, not even on the enforced path."""
    bad = {**VALID_CONFIG, "gateway": {"bind": "bogus"}}
    with patch.object(config_schema, "_SCHEMA_PATH", config_schema._SCHEMA_PATH.with_name("nope.json")):
        config_schema._validator.cache_clear()
        config_schema.gate_config(bad, owner_id="user_1", context="unit", enforce=True)  # does not raise


# --- the write paths --------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_mode_still_persists_an_invalid_patch(efs_dir):
    """Deliberate: flipping every writer to enforce on day one would let a wrong
    vendored schema brick config delivery fleet-wide."""
    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "audit"):
        await patch_openclaw_config("user_1", {"gateway": {"bind": "bogus"}})
    written = json.loads(open(os.path.join(efs_dir, "user_1", "openclaw.json")).read())
    assert written["gateway"]["bind"] == "bogus"


@pytest.mark.asyncio
async def test_enforce_mode_rejects_the_patch_and_leaves_disk_untouched(efs_dir):
    config_path = os.path.join(efs_dir, "user_1", "openclaw.json")
    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "enforce"):
        with pytest.raises(ConfigPatchError):
            await patch_openclaw_config("user_1", {"gateway": {"bind": "bogus"}})
    assert json.loads(open(config_path).read()) == VALID_CONFIG
    # Rejected before the backup, so no .bak was minted either.
    assert not os.path.exists(config_path + ".bak")


@pytest.mark.asyncio
async def test_malicious_seller_slice_is_rejected_in_audit_mode(efs_dir):
    """The sharpest vector in #941: apply_deploy_aux_mutations deep-merges an
    UNTRUSTED marketplace slice. It enforces regardless of the fleet knob."""
    config_path = os.path.join(efs_dir, "user_1", "openclaw.json")
    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "audit"):
        with pytest.raises(ConfigPatchError, match="would not load in OpenClaw"):
            await apply_deploy_aux_mutations(
                "user_1",
                # `enabled` is a boolean in OpenClaw's plugin schema; a slice
                # that gets it wrong bricks the buyer's container at next boot.
                plugins_patch={"entries": {"evil": {"enabled": "yes-please"}}},
            )
    assert json.loads(open(config_path).read()) == VALID_CONFIG


@pytest.mark.asyncio
async def test_valid_seller_slice_still_deploys(efs_dir):
    await apply_deploy_aux_mutations(
        "user_1",
        plugins_patch={"entries": {"resy": {"enabled": True}}},
        mcp_patch={"servers": {"resy": {"command": "node"}}},
    )
    written = json.loads(open(os.path.join(efs_dir, "user_1", "openclaw.json")).read())
    assert written["plugins"]["entries"]["resy"] == {"enabled": True}
    assert written["mcp"]["servers"]["resy"]["command"] == "node"
