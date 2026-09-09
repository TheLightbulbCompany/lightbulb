# Task 2 implementation report

Status: **NEEDS_CONTEXT**. Independent schema-tooling preparation is implemented and verified. The final publisher source checkout, annotated tag, peeled commit, and candidate artifacts are unavailable, so no candidate was built, generated, registered, or claimed as Node20-proven.

## Preparation provenance

- Checkout: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-openclaw93-image`
- Branch: `codex/founder-openclaw93-image`
- Task 2 BASE / starting HEAD: `283595b4a16fdf8113674fadff0cb5c567dc13c7`
- Preserved Task 1 preparation: `b83e733532691341dbb02bc4b57ef0ec6544b697`
- Preserved merged application main: `149c5d2c6b72bd4afdfc4efd1de23c46b6edf694`
- Final OpenClaw 9.3 source/tag/commit: unavailable; not invented.
- Final candidate schema id/path: unavailable; not registered.
- Active fork checkout was not installed, built, edited, or tested.

## Implementation

- `apps/backend/scripts/vendor_openclaw_schema.sh`
  - Preserves no-argument legacy mode.
  - Adds an all-or-nothing explicit interface: `--source`, `--tag`, `--commit`, and `--schema-id`.
  - Requires the schema id to equal the annotated tag without `v`, validates the id before constructing fixed output paths, and rejects partial provenance, traversal, lightweight tags, commit mismatches, wrong HEAD, tracked changes, and relevant untracked inputs.
  - Enforces the fork runtime range (`>=24.16.0 <25` or `>=26.1.0`) and the exact `packageManager` pnpm version.
  - Uses the installed fork `node_modules/.bin/esbuild` directly, preserves the ESM `createRequire` banner, and targets Node20 syntax.
  - Builds/derives/imports/validates/hashes in a repository-local temporary directory, rechecks source identity after build and after conversion, and only then atomically renames individual completed artifacts.
  - Publishes explicit candidates immutably to `_schema/releases/<schema_id>/openclaw-config-schema.mjs`, `_schema/releases/<schema_id>/PROVENANCE.json`, and `core/services/openclaw_config_schemas/<schema_id>.json`.
  - Candidate provenance records schema id, verified tag, peeled commit, bundle SHA256, and JSON SHA256. Existing candidate paths are never overwritten.
- `apps/backend/tests/chaos/_schema/gen_json_schema.mjs` and `validate.mjs`
  - Accept an optional explicit bundle path and retain the existing default.
  - Convert filesystem paths to file URLs before dynamic import.
- `apps/backend/core/services/config_schema.py`
  - Documents additive versioned candidates while retaining the fleet default and authoritative `_SCHEMA_PATHS` registry unchanged.
- `apps/backend/tests/unit/test_vendor_openclaw_schema_source.py`
  - Covers legacy compatibility, explicit provenance completeness, annotated/peeled identity, source cleanliness, runtime/package-manager pins, failure preservation, immutable versioned publication, hashes, banner, direct esbuild, and Node20 target.
- `apps/backend/tests/unit/services/test_config_schema.py`
  - Covers traversal, empty, and candidate-shaped unknown selectors without filesystem fallback.
- `apps/backend/tests/chaos/test_schema_staleness.py`
  - Regenerates each registered JSON artifact from its own bundle, validates each registered candidate against its own provenance, and freezes the old fleet artifact hashes instead of comparing it to a future build-base tag.
- `apps/backend/tests/chaos/invariants/test_config_schema.py`
  - Exercises every registered bundle explicitly against deterministic trial, managed multi-agent, BYO, and ChatGPT-OAuth configs, including shared MCP runtime scope, browser session state, and enabled plugin inventory.

## Files changed

```text
apps/backend/core/services/config_schema.py
apps/backend/scripts/vendor_openclaw_schema.sh
apps/backend/tests/chaos/_schema/gen_json_schema.mjs
apps/backend/tests/chaos/_schema/validate.mjs
apps/backend/tests/chaos/invariants/test_config_schema.py
apps/backend/tests/chaos/test_schema_staleness.py
apps/backend/tests/unit/services/test_config_schema.py
apps/backend/tests/unit/test_vendor_openclaw_schema_source.py
.superpowers/sdd/2026-09-09-openclaw93-image-schema-integration/task-2-report.md
```

No Docker, workflow, plugin, proof-helper, manifest, AWS, registry, deployment, credential, or owner-state file was changed.

## Raw commands, output, and exit codes

### Worktree/base verification

```sh
git rev-parse --show-toplevel
git rev-parse --git-dir
git rev-parse --git-common-dir
git rev-parse --show-superproject-working-tree
git branch --show-current
git rev-parse HEAD
git status --short
git log -3 --oneline
```

```text
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-openclaw93-image
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.git/worktrees/founder-openclaw93-image
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.git
codex/founder-openclaw93-image
283595b4a16fdf8113674fadff0cb5c567dc13c7
283595b4a Merge commit '149c5d2c6b72bd4afdfc4efd1de23c46b6edf694' into codex/founder-openclaw93-image
b83e73353 Prepare OpenClaw 9.3 image identity checks
149c5d2c6 Add truthful Team Room workspace frame (#2402)
```

Exit code: `0`.

### Initial consolidated check

```sh
cd apps/backend
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest -q tests/unit/test_vendor_openclaw_schema_source.py tests/unit/services/test_config_schema.py tests/chaos/test_schema_staleness.py tests/chaos/invariants/test_config_schema.py
```

```text
collected 58 items
tests/unit/test_vendor_openclaw_schema_source.py ................F..     [ 32%]
tests/unit/services/test_config_schema.py .............................. [ 84%]
tests/chaos/test_schema_staleness.py ...s                                [ 91%]
tests/chaos/invariants/test_config_schema.py .....                       [100%]
coverage.exceptions.DataError: Couldn't use data file '.../.coverage...': unable to open database file
============= 1 failed, 56 passed, 1 skipped, 1 warning in 24.83s ==============
```

Exit code: `3`. The concrete test failure was the generation-failure branch exiting under `set -e` before emitting its diagnostic; the command also hit the restricted-worktree coverage write. The branch was fixed to handle conversion failure explicitly, and the consolidated command was rerun with permission for its local coverage data.

### Focused failure rerun after fix

```sh
bash -n scripts/vendor_openclaw_schema.sh
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest -q --no-cov tests/unit/test_vendor_openclaw_schema_source.py
```

```text
collected 19 items
tests/unit/test_vendor_openclaw_schema_source.py ...................     [100%]
============================== 19 passed in 7.24s ==============================
```

Exit code: `0`.

### Final consolidated affected checks

```sh
cd apps/backend
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest -q tests/unit/test_vendor_openclaw_schema_source.py tests/unit/services/test_config_schema.py tests/chaos/test_schema_staleness.py tests/chaos/invariants/test_config_schema.py
```

```text
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.0.2, pluggy-1.6.0
rootdir: /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-openclaw93-image/apps/backend
configfile: pytest.ini
plugins: anyio-4.12.1, asyncio-0.23.5, respx-0.20.2, schemathesis-4.10.2, cov-4.1.0, hypothesis-6.151.9, mock-3.12.0, Faker-40.11.0, subtests-0.15.0
asyncio: mode=Mode.AUTO
collected 60 items

tests/unit/test_vendor_openclaw_schema_source.py .....................   [ 35%]
tests/unit/services/test_config_schema.py .............................. [ 85%]
tests/chaos/test_schema_staleness.py ...s                                [ 91%]
tests/chaos/invariants/test_config_schema.py .....                       [100%]

--------- coverage: platform darwin, python 3.12.11-final-0 ----------
Name                                             Stmts   Miss   Cover   Missing
core/services/config_schema.py                     114      7  93.86%   125, 129, 140, 208, 213-214, 217
TOTAL                                            10844   9151  15.61%

======================== 59 passed, 1 skipped in 28.96s ========================
```

Exit code: `0`. The one skip is the empty candidate-provenance parameter set: `_SCHEMA_PATHS` intentionally contains only the existing fleet schema until the exact candidate is available. Once a candidate is registered, missing bundle/JSON/provenance evidence fails instead of skipping.

### Syntax, diff, scope, and frozen-artifact checks

```sh
git diff --check
bash -n apps/backend/scripts/vendor_openclaw_schema.sh
node --check apps/backend/tests/chaos/_schema/gen_json_schema.mjs
node --check apps/backend/tests/chaos/_schema/validate.mjs
shasum -a 256 apps/backend/tests/chaos/_schema/openclaw-config-schema.mjs apps/backend/core/services/openclaw_config.schema.json apps/backend/tests/chaos/_schema/PINNED_TAG
git diff --name-only -- apps/infra .github openclaw-version.json
```

```text
6e194e95823db3a58af8ca6708291ca1a9cf45149806de60851f006f20c71fc6  apps/backend/tests/chaos/_schema/openclaw-config-schema.mjs
ce18b8954fdc14687c83925bc5157b70ef40e3a9fd28fd049e70cbc5bd0d31a1  apps/backend/core/services/openclaw_config.schema.json
f11970c87b9a1ef73d8a0577ff24103aeea637993ee2f3655f396474fded58dd  apps/backend/tests/chaos/_schema/PINNED_TAG
```

Exit code: `0`. The empty final output from `git diff --name-only` confirms no forbidden Task 1/workflow/manifest surfaces changed.

## Self-review

- Confirmed the default `_SCHEMA_PATH` and the only registered schema remain the existing 8.1 fleet artifact.
- Confirmed no candidate release/JSON files exist in the checkout.
- Confirmed the vendor output directories are hardcoded and schema ids cannot become paths.
- Confirmed no skip-build flag, `npx`, dependency addition, source-checkout write for the synthetic entry, or output truncation remains.
- Confirmed validation errors do not print config values; the zod invariant failure now prints only the selected schema id and zod issues.
- Confirmed existing 8.1 bundle, JSON, and `PINNED_TAG` hashes are unchanged and enforced in the drift test.

## Concerns / missing final inputs

1. Exact adapted OpenClaw 9.3 annotated tag, peeled commit, and controller-released source checkout are still required.
2. The final source must be built once with its supported Node runtime and exact pinned pnpm; this preparation did not touch the active writer-owned fork checkout.
3. Candidate bundle, JSON, provenance, `_SCHEMA_PATHS` registration, and candidate-specific checks remain pending until those exact inputs exist.
4. Backend CI's actual Node20 consumer run over the committed candidate remains mandatory. Local tooling/fixture tests ran with Node `v26.3.0` and are not that proof.
5. No image digest, container/plugin proof, AWS action, workflow dispatch, PR, push, merge, or deployment was performed.

## Review fix round 1

Base: `765ece6865c7460b2fefb2480bcd09013bf2d7f4`.

The shared `verify_source` check now rejects every non-ignored untracked file instead of using a build-input path allowlist. This closes implicit pnpm inputs such as root `.npmrc` and `.pnpmfile.cjs`; ignored `node_modules/` and `src/config/generated.ts` remain allowed through `git ls-files --exclude-standard`. A focused `.npmrc` negative proves rejection happens before dependency installation and preserves the legacy artifacts.

### Exact affected batch

```sh
cd apps/backend
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest -q tests/unit/test_vendor_openclaw_schema_source.py
```

```text
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.0.2, pluggy-1.6.0
rootdir: /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-openclaw93-image/apps/backend
configfile: pytest.ini
plugins: anyio-4.12.1, asyncio-0.23.5, respx-0.20.2, schemathesis-4.10.2, cov-4.1.0, hypothesis-6.151.9, mock-3.12.0, Faker-40.11.0, subtests-0.15.0
asyncio: mode=Mode.AUTO
collected 22 items

tests/unit/test_vendor_openclaw_schema_source.py ......................  [100%]

--------- coverage: platform darwin, python 3.12.11-final-0 ----------
Name                                             Stmts   Miss   Cover   Missing
TOTAL                                             4454   4056   8.94%

============================== 22 passed in 7.40s ==============================
```

Exit code: `0`.

### Review-fix syntax, diff, and preservation checks

```sh
git diff --check
bash -n apps/backend/scripts/vendor_openclaw_schema.sh
shasum -a 256 apps/backend/tests/chaos/_schema/openclaw-config-schema.mjs apps/backend/core/services/openclaw_config.schema.json apps/backend/tests/chaos/_schema/PINNED_TAG
```

```text
6e194e95823db3a58af8ca6708291ca1a9cf45149806de60851f006f20c71fc6  apps/backend/tests/chaos/_schema/openclaw-config-schema.mjs
ce18b8954fdc14687c83925bc5157b70ef40e3a9fd28fd049e70cbc5bd0d31a1  apps/backend/core/services/openclaw_config.schema.json
f11970c87b9a1ef73d8a0577ff24103aeea637993ee2f3655f396474fded58dd  apps/backend/tests/chaos/_schema/PINNED_TAG
```

Exit code: `0`.

Self-review: the change is confined to the existing shared verifier and one focused test; no-argument behavior is unchanged, ignored generated outputs remain accepted, and no fork build/install, candidate, registry, dependency, AWS, or sibling change was made. The recorded minor existing-candidate second-invocation coverage item remains deferred to final review as directed.
