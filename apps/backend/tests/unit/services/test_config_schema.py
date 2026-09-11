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


def test_errors_never_echo_the_offending_value():
    """jsonschema's `err.message` embeds repr(instance). openclaw.json holds real
    secrets (gateway.auth.token literally; MCP/plugin env + apiKey), the gate
    validates the WHOLE merged doc, and routers/config.py returns the resulting
    ConfigPatchError text in an HTTP body — so a violation must never carry the
    value that caused it."""
    secret = "sk-live-do-not-log-me"
    doc = {
        **VALID_CONFIG,
        "gateway": {"bind": "lan", "auth": {"mode": "token", "token": secret}},
        # A whole subtree failing type/anyOf is the dangerous case: the raw
        # message would embed the entire dict, secret included.
        "mcp": {"servers": {"jira": ["not", "an", "object", secret]}},
    }
    errors = config_schema.schema_errors(doc)
    assert errors, "fixture must actually violate the schema"
    assert secret not in "; ".join(errors)
    # Still pinpoints where the problem is.
    assert any("mcp.servers.jira" in e for e in errors)


def test_gate_error_and_logs_never_echo_the_offending_value(caplog):
    secret = "sk-live-do-not-log-me"
    doc = {**VALID_CONFIG, "mcp": {"servers": {"jira": ["not", "an", "object", secret]}}}
    with pytest.raises(config_schema.ConfigSchemaError) as exc:
        config_schema.gate_config(doc, owner_id="user_1", context="unit", enforce=True)
    assert secret not in str(exc.value)
    assert secret not in caplog.text


def test_schema_unavailable_returns_none_not_errors():
    with patch.object(config_schema, "_SCHEMA_PATH", config_schema._SCHEMA_PATH.with_name("nope.json")):
        config_schema._validator.cache_clear()
        assert config_schema.schema_errors(VALID_CONFIG) is None


def test_explicit_schema_selection_isolated_across_validation_gate_and_prune(tmp_path, monkeypatch):
    paths = {}
    for key in ("old", "new"):
        path = tmp_path / f"{key}.json"
        path.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {key: {"type": "string"}},
                    "additionalProperties": False,
                }
            )
        )
        paths[key] = path
    monkeypatch.setattr(config_schema, "_SCHEMA_PATHS", paths)
    for accepted, rejected in (("old", "new"), ("new", "old"), ("old", "new")):
        doc = {accepted: "value"}
        assert config_schema.schema_errors(doc, schema_id=accepted) == []
        assert config_schema.schema_errors(doc, schema_id=rejected)
        config_schema.gate_config(doc, owner_id="user_1", context="unit", schema_id=accepted, enforce=True)
        with pytest.raises(config_schema.ConfigSchemaError):
            config_schema.gate_config(doc, owner_id="user_1", context="unit", schema_id=rejected, enforce=True)
        patch_value = {"old": "keep if old", "new": "keep if new"}
        original = dict(patch_value)
        assert config_schema.prune_schema_rejected({}, patch_value, schema_id=accepted) == {
            accepted: patch_value[accepted]
        }
        assert patch_value == original


@pytest.mark.parametrize("failure", ["unknown", "missing", "json", "schema"])
def test_explicit_schema_unavailable_never_falls_back(tmp_path, monkeypatch, caplog, failure):
    secret = "synthetic-schema-value-do-not-echo"
    path = tmp_path / "selected.json"
    if failure == "json":
        path.write_text(secret)  # invalid JSON
    elif failure == "schema":
        path.write_text(json.dumps({"type": secret}))  # invalid JSON Schema
    monkeypatch.setattr(config_schema, "_SCHEMA_PATHS", {} if failure == "unknown" else {"selected": path})
    operations = (
        lambda: config_schema.schema_errors(VALID_CONFIG, schema_id="selected"),
        lambda: config_schema.prune_schema_rejected({}, {}, schema_id="selected"),
        lambda: config_schema.gate_config(VALID_CONFIG, owner_id="user_1", context="unit", schema_id="selected"),
    )
    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "audit"):
        for operation in operations:
            with pytest.raises(config_schema.ConfigSchemaError) as exc:
                operation()
            assert secret not in str(exc.value)
    assert secret not in caplog.text


def test_explicit_schema_real_artifact_matches_default_diagnostics():
    invalid = {**VALID_CONFIG, "gateway": {"bind": "bogus"}}

    assert config_schema.schema_errors(VALID_CONFIG, schema_id="2026.8.1-isol8.3") == []
    assert config_schema.schema_errors(invalid, schema_id="2026.8.1-isol8.3") == config_schema.schema_errors(invalid)


@pytest.mark.parametrize("schema_id", ["../openclaw_config.schema", "", "2026.9.3-unregistered"])
def test_explicit_schema_rejects_unregistered_selector_without_opening(monkeypatch, schema_id):
    def fail_if_opened(*args, **kwargs):
        raise AssertionError("unregistered schema identifier reached the filesystem")

    monkeypatch.setattr(config_schema.Path, "read_text", fail_if_opened)
    with pytest.raises(config_schema.ConfigSchemaError, match="required OpenClaw config schema is unavailable"):
        config_schema.schema_errors(VALID_CONFIG, schema_id=schema_id)


def test_explicit_schema_preserves_audit_and_enforce_policy(caplog):
    invalid = {**VALID_CONFIG, "gateway": {"bind": "bogus"}}
    schema_id = "2026.8.1-isol8.3"

    with patch.object(config_schema.settings, "OPENCLAW_CONFIG_SCHEMA_MODE", "audit"):
        config_schema.gate_config(invalid, owner_id="user_1", context="unit", schema_id=schema_id)
        with pytest.raises(config_schema.ConfigSchemaError):
            config_schema.gate_config(
                invalid,
                owner_id="user_1",
                context="unit",
                enforce=True,
                schema_id=schema_id,
            )
    assert "SCHEMA-INVALID" in caplog.text


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


# --- prune_schema_rejected -------------------------------------------------
#
# Guards the config REGEN on an image roll. The regen renders a fresh,
# version-correct config and then restores the runtime-appended state the
# generator does not know about. That restore preserves "anything the regen did
# not emit", which is right within one upstream version and WRONG across a bump:
# the new schema's DELETED keys also match that description, so the restore
# resurrects exactly what the regen removed. Measured on 7.2 -> 8.1, the restore
# re-added root `audit`/`web` and `skills.workshop.autonomous.enabled`, turning a
# clean regen back into a crash-loop.


def test_prune_drops_schema_rejected_keys_and_keeps_runtime_state():
    """The pruner must be surgical: remove what the deployed schema rejects,
    keep every schema-valid runtime subtree the container actually needs."""
    base = {"skills": {"workshop": {"autonomous": {"mode": "propose"}}}}
    restore = {
        # Rejected by 2026.8.1: deleted root sections, and the pre-8.1 spelling
        # of the workshop knob (`enabled` became `mode`).
        "audit": {"enabled": False},
        "web": {"enabled": True},
        "skills": {
            "workshop": {"autonomous": {"enabled": True}},
            # Valid runtime state that MUST survive — a marketplace deploy's
            # skill entry. skills.entries is an open map in the schema.
            "entries": {"market_skill": {"enabled": True, "config": {"k": "v"}}},
        },
        # Valid runtime state the generator never emits.
        "bindings": [{"agentId": "agent_x", "match": {"channel": "slack"}}],
        "mcp": {"servers": {"deployed_mcp": {"command": "node"}}},
    }

    pruned = config_schema.prune_schema_rejected(base, restore)

    assert "audit" not in pruned
    assert "web" not in pruned
    assert "enabled" not in pruned.get("skills", {}).get("workshop", {}).get("autonomous", {})
    # Surgical, not scorched-earth: siblings of a rejected key survive.
    assert pruned["skills"]["entries"]["market_skill"] == {"enabled": True, "config": {"k": "v"}}
    assert pruned["bindings"] == [{"agentId": "agent_x", "match": {"channel": "slack"}}]
    assert pruned["mcp"]["servers"]["deployed_mcp"] == {"command": "node"}


def test_prune_leaves_a_clean_patch_untouched():
    """No-op when nothing is rejected — the pruner must not perturb a restore
    patch that is already valid, or every same-version roll pays for this."""
    base = {"meta": {"lastTouchedVersion": "2026.8.1"}}
    restore = {"bindings": [{"agentId": "a", "match": {"channel": "slack"}}]}
    assert config_schema.prune_schema_rejected(base, restore) == restore


def test_prune_strips_branches_left_empty():
    """A subtree whose only content was rejected must not land as a bare {} —
    the generator deliberately omitted it, and writing an empty section back
    reintroduces a key the regen meant to drop."""
    base = {"skills": {"workshop": {"autonomous": {"mode": "propose"}}}}
    pruned = config_schema.prune_schema_rejected(base, {"skills": {"workshop": {"autonomous": {"enabled": True}}}})
    assert pruned == {}


def test_prune_degrades_to_passthrough_without_a_schema():
    """Schema unavailable is OUR bug, not the caller's. Same posture as the
    rest of this module: degrade to audit-only rather than silently deleting
    an owner's runtime state because we could not load an artifact."""
    restore = {"audit": {"enabled": False}, "bindings": []}
    with patch.object(config_schema, "_validator", return_value=None):
        assert config_schema.prune_schema_rejected({}, restore) == restore
