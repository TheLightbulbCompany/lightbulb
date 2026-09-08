# Schema Source Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent schema vendoring from labeling an arbitrary or modified checkout as the pinned fork release.

**Architecture:** Add a fail-closed source preflight to the existing schema-vendoring script before dependency installation or artifact writes. Resolve the expected fork Git tag from the existing image pin, peel annotated tags, compare to HEAD, and reject tracked source modifications. This is a bounded first delivery within R2's provenance requirement; dual-schema selection and staged runtime config activation remain separate unfinished requirements in the program ledger.

**Tech Stack:** Bash, Git, Python 3.12, existing pytest.

**Spec:** `docs/superpowers/plans/2026-09-08-openclaw-9x-founder-multiplayer-delivery-plan.md`, R1/R2; submitted execution goal section 6.

## Global Constraints

- Do not upgrade other production owners or change the new-user production image default under this goal.
- Never bypass sandbox approvals, branch protection, identity checks, billing limits or failing release gates.
- Extend schema vendoring to identify the actual checked-out fork revision and preserve artifacts for all supported releases.
- Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion.

## Current evidence and boundary

At base `ba58f69b47514aca8ca9a47d400d3a40a8f91256`, `vendor_openclaw_schema.sh` reads `openclaw-version.json#upstream` into `PINNED_TAG` but bundles whichever `OPENCLAW_FORK` directory it is given. Its final tag stamp can therefore claim the wrong release. `test_schema_staleness.py` compares labels and cannot detect that false attribution. No caller supplies a verified source commit. The script currently has only an existence check before building.

The shipped annotated tag `v2026.8.1-isol8.3` has tag-object SHA `e00896bad5024cf30b298c6e579128eecbf10ecb` and commit SHA `dfb72bf0d187a5d3d6ef35dd9cabcca5049bd31c`; compare peeled commits, never the tag object. Candidate source is separately frozen to upstream `v2026.9.3` commit `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7` and is not yet a published, qualified fork release.

This task does not re-vendor, add candidate artifacts, change release pins, mutate Git refs, weaken dependency-age rules, or claim current committed schema provenance has been retroactively proven.

### Task 1: Refuse schema generation from an unverified fork checkout

**Files:**
- Modify: `apps/backend/scripts/vendor_openclaw_schema.sh`
- Create: `apps/backend/tests/unit/test_vendor_openclaw_schema_source.py`

**Interfaces:**
- Consumes: existing `OPENCLAW_FORK` environment variable and root `openclaw-version.json#upstream` tagged fork image.
- Produces: early nonzero exit with an actionable, non-secret diagnostic on missing/unresolvable tag, mismatched HEAD, or tracked modifications; clean matching source reaches the unchanged install/build path.
- Produces on successful vendoring: `_schema/PINNED_COMMIT` containing the verified full commit plus newline, alongside the existing tag stamp. Do not add that artifact for the existing bundle without actually re-vendoring it.

- [ ] **Step 1: Add behavior tests and prove RED.** Use real temporary Git repositories. Seed a minimal tracked `src/config/zod-schema.ts`, commit, and make an annotated `v2026.8.1-isol8.3` tag. Set local test identity only (`git -c user.name=Test -c user.email=test@example.invalid commit ...`), never global Git config. Create a temporary invocation root with the actual image pin and sentinel versions of the three existing schema artifacts. Prepend a fake `pnpm` executable to PATH that writes a marker and exits 74; this substitutes only the external installation/build boundary. Run the REAL production shell script with `subprocess.run`, `cwd` set to that invocation root, `OPENCLAW_FORK` set to the temporary fork, and a bounded timeout.

```python
def test_wrong_head_is_rejected_before_dependency_install(vendor_case):
    vendor_case.commit_change("src/config/zod-schema.ts", "export const changed = true;\n")
    result = vendor_case.run()
    assert result.returncode not in (0, 74)
    assert not vendor_case.install_marker.exists()
    assert "HEAD" in result.stderr
    vendor_case.assert_artifacts_unchanged()
```

The helper is test-only and may be a small fixture returning paths/functions; no framework or production test hook. Add cases for a clean annotated-tag checkout reaching the installer (exit 74 and marker present), a missing expected tag, unstaged tracked source changes, and staged source changes. Wrong-head/dirty/missing-tag cases must fail against the existing script because it reaches the fake installer. Assertions concern actual exits, marker effects and unchanged artifact bytes, not source-string matching.

Run from `apps/backend`:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/test_vendor_openclaw_schema_source.py -q -o addopts=''
```

Expected RED: rejected-source scenarios reach the fake installer and fail their assertions. Fix fixture errors before calling the result RED.

- [ ] **Step 2: Add the source preflight.** Keep the current image field meanings and existing generator behavior. After checking `SCHEMA_SRC`, before the install step, derive the tag and verify it:

```bash
case "$PINNED_TAG" in
  ghcr.io/thelightbulbcompany/agent:* ) FORK_TAG="v${PINNED_TAG##*:}" ;;
  * ) echo "ERROR: upstream must name a tagged Lightbulb fork image" >&2; exit 1 ;;
esac
if ! SOURCE_COMMIT=$(git -C "$FORK" rev-parse --verify "refs/tags/${FORK_TAG}^{commit}" 2>/dev/null); then
  echo "ERROR: expected fork tag $FORK_TAG is missing; fetch and check out that release" >&2
  exit 1
fi
if ! HEAD_COMMIT=$(git -C "$FORK" rev-parse --verify HEAD 2>/dev/null) || [ "$HEAD_COMMIT" != "$SOURCE_COMMIT" ]; then
  echo "ERROR: fork HEAD does not match $FORK_TAG; refusing to mislabel the schema" >&2
  exit 1
fi
if ! git -C "$FORK" diff --quiet --ignore-submodules -- || ! git -C "$FORK" diff --cached --quiet --ignore-submodules --; then
  echo "ERROR: fork has tracked modifications; use the clean pinned release" >&2
  exit 1
fi
```

Reject a digest-bearing or empty/malformed tag explicitly; `git check-ref-format "refs/tags/$FORK_TAG"` can validate the derived ref. Do not execute tag/image text as shell code. Existing ignored build output is allowed, but reject nonignored untracked files under the actual source inputs `src/` and `packages/` because they could change an imported module without changing HEAD. Add a real temporary-file case for that branch. No network lookup belongs in this preflight: the local expected ref is verified against the frozen release separately during release qualification.

Log the verified tag plus full commit (not credentials), then proceed with the existing install/build flow. After successful JSON conversion, add the commit stamp next to `PINNED_TAG`. Update the script's introductory comments to describe the actual-source check and recorded commit. Do not change the generated bundle, JSON schema, or current pin as part of this task.

- [ ] **Step 3: Prove GREEN and unchanged-schema compatibility.** Run the new test file and the existing schema-staleness tests with the same isolated test environment. The full build is intentionally not run by this guard test; the clean-source marker proves that the existing build path remains reachable. Validate shell syntax and lint only the new Python file:

```sh
bash -n scripts/vendor_openclaw_schema.sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/test_vendor_openclaw_schema_source.py tests/chaos/test_schema_staleness.py -q -o addopts=''
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff check tests/unit/test_vendor_openclaw_schema_source.py
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff format --check tests/unit/test_vendor_openclaw_schema_source.py
```

Expected: all focused tests pass, shell syntax passes, lint/format pass, no artifact or release-pin diff. Test/report the success commit-stamp behavior too by extending the external-command fixtures only as needed to run a controlled complete generation, or report this acceptance gap before finishing; do not claim it from source inspection alone.

- [ ] **Step 4: Self-review and commit.** Check the full diff, ensure no changes to generated artifacts or user work, record RED/GREEN commands/output, and commit only the task's two files. Return commit, report path, concise test summary and any concerns for independent specification and quality review.

```sh
git add apps/backend/scripts/vendor_openclaw_schema.sh apps/backend/tests/unit/test_vendor_openclaw_schema_source.py
git commit -m "fix(openclaw): verify fork source before schema vendoring"
```
