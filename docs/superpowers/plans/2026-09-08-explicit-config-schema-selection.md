# Explicit Config Schema Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select an explicitly registered config schema without falling back to the fleet artifact, while preserving existing callers' legacy behavior.

**Architecture:** Extend the existing validator, pruner and gate with one optional schema identifier. A fixed internal map resolves identifiers to bundled files; compilation is cached per identifier. No release assignment, deployment, artifact generation or caller migration occurs in this independently testable prerequisite.

**Tech Stack:** Python 3.12, installed jsonschema and pytest, stdlib pathlib/functools.

**Spec:** Submitted founder goal section 6 (dual old/candidate schemas), approved delivery R2, and `2026-09-08-lifecycle-reconciliation.md`.

## Global Constraints

- Do not upgrade other production owners or change the new-user production image default under this goal.
- Never bypass sandbox approvals, branch protection, identity checks, billing limits or failing release gates.
- No fabricated candidate artifact, source commit, image digest, or successful compatibility claim.
- Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion.
- Existing RPC default-deny policy, approvals/questions, credential redaction and safe provider/browser/device behavior remain unchanged.

## Supported combinations and scope

| Selector | Artifact | Unavailable behavior | Invalid config behavior |
|---|---|---|---|
| Omitted / `None` | Existing `_SCHEMA_PATH` | Existing audit degradation, including `enforce=True` | Existing enforce argument / fleet mode |
| Explicit `2026.8.1-isol8.3` | Same existing bundled 8.1 fork schema | Raise `ConfigSchemaError` | Existing enforce argument / fleet mode |
| Any unregistered identifier, including 9.3 | None | Raise `ConfigSchemaError`; never use the old artifact | No validation or pruning with a substitute |
| Additional registered fixture identifiers in unit tests | Synthetic contradictory schemas | Raise on unreadable/corrupt/invalid schema | Selected schema and existing gate policy |

The candidate schema is not available because the fork build is not yet qualified. Do not register a candidate pointing at the old schema or check in a fabricated schema. A subsequent artifact-vendoring task must preserve the old artifact at its own versioned path and add the exact built fork schema and source evidence before candidate runtime/config wiring is allowed; overwriting this shared old path while retaining its 8.1 identifier would be incorrect. This task proves selection/failure semantics, not 9.3 compatibility or the complete R2 milestone. Explicit selection controls which schema is used, not the audit/enforcement policy; staged activation will require `enforce=True`.

Current consumers are `config_patcher._locked_rmw`, `config.write_openclaw_config`, and `update_service.reapply_owner_config`. Their default calls remain unchanged. All three schema operations must accept the same identifier so future hot writes and migration pruning cannot accidentally choose different schemas. No new API endpoint or dependency is needed.

### Task 1: Add explicit selection and fail-closed artifact handling

**Files:**
- Modify: `apps/backend/core/services/config_schema.py`
- Modify: `apps/backend/tests/unit/services/test_config_schema.py`

**Interfaces:**
- Consumes: existing `_SCHEMA_PATH`, legacy roster normalization, `ConfigSchemaError`, log/metric policy and actual bundled 8.1 schema.
- Produces: `schema_errors(config: dict, *, schema_id: str | None = None) -> list[str] | None`.
- Produces: `prune_schema_rejected(base: dict, patch: dict, *, schema_id: str | None = None) -> dict`.
- Produces: `gate_config(config: dict, *, owner_id: str, context: str, enforce: bool = False, schema_id: str | None = None) -> None`.
- Internal: `_SCHEMA_PATHS = {"2026.8.1-isol8.3": _SCHEMA_PATH}` and cached `_validator(schema_id: str | None = None)`. No caller-supplied filesystem path and no filename interpolation.

- [ ] **Step 1: Write selection and unavailable-artifact tests.** Extend the existing test file/cache-clearing fixture. Use pytest's existing `tmp_path` and `monkeypatch`; synthetic schemas are selection probes, not claimed upstream artifacts. The following core test supplies the two contradictory schemas and verifies every operation uses the chosen one:

```python
def test_explicit_schema_selection_isolated_across_validation_gate_and_prune(tmp_path, monkeypatch):
    paths = {}
    for key in ("old", "new"):
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps({
            "type": "object", "properties": {key: {"type": "string"}},
            "additionalProperties": False,
        }))
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
```

Add these exact behavioral cases using a small parametrized test and the same real loader (do not mock `_validator` for explicit-error tests):

```python
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
```

Also check explicit real `2026.8.1-isol8.3` accepts `VALID_CONFIG` and yields the same invalid-shape diagnostics as omitted selection; reject an unregistered path-like identifier (`../openclaw_config.schema`) without opening a file. An explicit selection of an existing valid schema must still audit an invalid config when `enforce=False` and the fleet mode is audit, and reject it with `enforce=True`. Preserve the existing no-selector degradation tests and secret-redaction tests unchanged. These are separate compatibility requirements, not grounds to enable any unknown schema.

- [ ] **Step 2: Run the new tests and prove RED.** From `apps/backend`:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_config_schema.py -k explicit_schema -q -o addopts=''
```

Expected failure: the requested selector/map is not implemented, not a broken fixture, import, or external service. Record exact failures before changing production code.

- [ ] **Step 3: Implement the selector in the existing module.** Keep `None` mapped dynamically to `_SCHEMA_PATH` so existing local tests and default callers retain their behavior. Resolve a non-None identifier only through `_SCHEMA_PATHS`; unknown identifiers raise a generic `ConfigSchemaError` without echoing arbitrary input. Use `@lru_cache(maxsize=None)` on `_validator` (accepted keys are bounded by the fixed internal map plus None; explicit-selection exceptions are not cached, while the legacy None result remains cached). Compile using the installed validator and validate the artifact itself:

```python
schema = json.loads(schema_path.read_text())
jsonschema.Draft202012Validator.check_schema(schema)
return jsonschema.Draft202012Validator(schema)
```

Catch load/compile errors at this boundary. Logs may include the trusted registered path and exception type, but not `str(exc)`, schema contents or config values. Omitted selection continues to log the existing audit-degradation meaning and return None. Explicit selection raises `ConfigSchemaError("required OpenClaw config schema is unavailable")` using `from None`; unknown selection uses the same safe error. No fallback on failure. Update the module docstring to explain both modes precisely.

Thread the same selector through the existing functions without rewriting the pruning algorithm, normalization or metrics policy:

```python
# schema_errors and prune_schema_rejected
validator = _validator(schema_id)
# gate_config
errors = schema_errors(config, schema_id=schema_id)
```

An empty restore patch must still resolve an explicit schema before returning. Do not use the selected schema as a reason to change audit/enforce policy. Do not add any new candidate entry, artifact, release record, lock or production caller.

- [ ] **Step 4: Prove GREEN and compatibility.** Run the full module and existing provenance/staleness checks, then formatting/lint. Current tests use tmp directories and synthetic AWS values; no external calls are allowed.

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_config_schema.py tests/unit/test_vendor_openclaw_schema_source.py tests/chaos/test_schema_staleness.py -q -o addopts=''
RUFF_CACHE_DIR=/private/tmp/lightbulb-schema-selector-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff check core/services/config_schema.py tests/unit/services/test_config_schema.py
RUFF_CACHE_DIR=/private/tmp/lightbulb-schema-selector-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff format --check core/services/config_schema.py tests/unit/services/test_config_schema.py
git diff --check
```

If pre-existing formatting fails, show it at the task base and avoid a bulk unrelated rewrite. The controller owns long broader coverage verification; this task's module must be green independently.

- [ ] **Step 5: Self-review and commit only the two files.** Record exact RED/GREEN results and both supported and unsupported combinations. Independent spec/quality review is required before downstream release code can rely on this contract.

```sh
git add apps/backend/core/services/config_schema.py apps/backend/tests/unit/services/test_config_schema.py
git commit -m "feat(config): select registered runtime schemas explicitly"
```
