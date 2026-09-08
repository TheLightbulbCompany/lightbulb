# Release Row Ownership Fences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve durable owner release metadata and prevent ordinary repository writers from taking over or terminalizing an owner-reserved release.

**Architecture:** Extend the existing container whole-item CAS and provision conditions, and add one common native DynamoDB condition to ordinary update-row mutations. No assignment endpoint or release transaction is exposed by this prerequisite; the subsequent paired-row transaction owns these fields.

**Tech Stack:** Existing Python, boto3 DynamoDB conditions, pytest/Moto.

**Spec:** Submitted founder goal section 6 and `docs/superpowers/plans/2026-09-08-release-fencing-contract.md`, especially exact storage/ownership and CAS operations. This plan covers repository exclusion only, not remote-effect fencing, release assignment, schema migration, or healthy deployment.

## Global Constraints

- Do not upgrade other production owners or change the new-user production image default under this goal.
- Never bypass sandbox approvals, branch protection, identity checks, billing limits or failing release gates.
- Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion.
- Do not expose secrets in commands, logs, reports, screenshots or transcripts.
- Keep one implementation writer at a time in a shared worktree.
- No candidate assignment may be enabled until paired-row transactions and all config/lifecycle effect boundaries participate.

## Scope and rulings

Four container-owned fields are `desired_release_id`, `active_release_id`, `release_revision`, `release_update_id`. Generic writers may not supply them. A stable assigned row (no reservation pointer) retains them through whole-item upsert, and concurrent changes abort that upsert rather than being adopted by its stale caller. Absence and explicit NULL are different states; a malformed present reservation pointer must still exclude ordinary mutation.

Any update with `update_type="release"`, or with a present `release_revision`, belongs outside the ordinary single-row update helpers. The revision marker also protects reserved size/base operations that have no target release. Guard the write itself, not a preceding read.

Ruling: retain the current no-pointer provision stale/error-reclaim and deliberate-takeover semantics in this prerequisite. Changing those requires coordinated executor/caller recovery work. Cost if wrong: assignment remains unavailable until that later work; this slice is not claimed to close all existing provision races.

Ruling: generic container `update_fields` raises existing `ProvisionSuperseded` on a reservation conflict, rather than returning `None` and letting a caller acknowledge a dropped write. Preserve `None` for a genuinely missing row. Cost if wrong: reserved owners can surface retryable contention until their release completes, which is safer than silently overwriting its state.

No changes to AWS resources, dependencies, schemas/images/defaults, billing/gateway credential writers, account teardown, or filesystem locks. Those other effect boundaries remain explicit integration requirements before assignment is usable.

### Task 1: Fence ordinary writers out of release-owned records

**Files:**
- Modify: `apps/backend/core/repositories/container_repo.py`
- Modify: `apps/backend/core/repositories/update_repo.py`
- Modify: `apps/backend/tests/unit/repositories/test_container_repo.py`
- Modify: `apps/backend/tests/unit/repositories/test_container_repo_provision_claim.py`
- Modify: `apps/backend/tests/unit/repositories/test_update_repo.py`

**Interfaces:**
- Existing public signatures and success values remain unchanged.
- Container `upsert`, `update_fields`, `update_container_leased` reject caller-provided release fields with `ValueError` before I/O. Error text names the owned fields, never their values.
- Existing `ProvisionSuperseded` is the stop signal for a reserved/stale generic container writer; do not introduce an exception class.
- `try_claim_provision` returns `None` if a release pointer is present; `takeover_provision`, `claim_attempt`, and `update_container_leased` raise `ProvisionSuperseded`; `release_provision_to_error` remains a no-op on conditional refusal. These outcomes apply even when the supplied token matches the reserved update's owner claim.
- Ordinary update helpers keep their existing refusal result (`None` for claims, `False` for boolean helpers); they cannot mutate protected records or add terminal TTL.
- `_UPSERT_PRESERVED_FIELDS` and `_UPSERT_CAS_FIELDS` include all four release fields. Whole-item CAS failure due to any changed release field is a stop, not a retry. A gate-only pointer conflict still retries as before.

- [ ] **Step 1: Add behavioral tests using the existing Moto fixtures.** Name new tests with `release` so the RED selection below covers them. Use raw fixture-table writes only to seed release metadata; the ordinary production writers under test must not supply it. Required cases:

  1. Stable assigned container retains desired/active/revision and no pointer through `upsert`; unassigned first write adds none of the fields.
  2. Parameterize each of the four field names and each generic writer; passing either a value or `None` raises `ValueError` and leaves the row unchanged.
  3. Between the real upsert read and put, inject a real Moto update for each release field, including absent-to-present and revision-only changes; the stale upsert raises `ProvisionSuperseded`, does not retry/adopt, and leaves the winner's complete row intact. Include ABA where desired/active return to their prior values but revision advances. Existing gate-only conflict control must still pass.
  4. A present release pointer, including NULL, blocks `upsert`, `update_fields`, every provision claim/takeover/attempt/checkpoint and error-release path. Test stopped, error, and stale provisioning states for acquisition; retain all existing no-pointer controls. Reserved checkpoint with correct claim and attempt still refuses, including `release_lease=True`.
  5. `update_fields` reservation arriving immediately before its conditional update is refused without changing the row; genuinely missing row returns `None`; ordinary scoped updates still work. Unexpected DynamoDB errors propagate.
  6. Every ordinary update mutation refuses both a release-typed row with no revision and a task-definition-refresh row with `release_revision=0` and no target release. Include NULL revision as present. Seed a status/token that would otherwise let the method succeed; assert unchanged complete row and absence of new TTL/target/heartbeat. Methods: `set_status_conditional`, `claim_apply`, `claim_stale_apply`, `heartbeat_apply`, `set_apply_target`, `finish_claimed_apply` for both terminal statuses, `set_scheduled`, `set_snoozed`, `mark_applied`, `mark_failed`. Retain existing legacy no-marker tests, including Paperclip and task-definition refresh.

Representative persistence test (in existing `test_container_repo.py`):

```python
@pytest.mark.asyncio
async def test_upsert_preserves_stable_release(dynamodb_table):
    from core.repositories import container_repo

    row = await container_repo.upsert("release-owner", {"status": "stopped"})
    row.update(desired_release_id="candidate", active_release_id="candidate", release_revision=4)
    dynamodb_table.put_item(Item=row)
    result = await container_repo.upsert("release-owner", {"status": "provisioning"})
    stored = dynamodb_table.get_item(Key={"owner_id": "release-owner"})["Item"]
    assert result["desired_release_id"] == stored["desired_release_id"] == "candidate"
    assert stored["active_release_id"] == "candidate"
    assert stored["release_revision"] == 4
    assert "release_update_id" not in stored
```

Race injection follows the existing `racing_run_in_thread` tests: wrap only the outbound put/update boundary, let the actual Moto conditional operation execute, and assert persisted rows rather than a mocked return. No sleeps/concurrency stress loop are needed to prove the deterministic interleaving.

- [ ] **Step 2: Prove RED before production edits.** From `apps/backend`:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/repositories/test_container_repo.py tests/unit/repositories/test_container_repo_provision_claim.py tests/unit/repositories/test_update_repo.py -k release -q -o addopts=''
```

Record behavioral failures (lost fields, successful forbidden writes/claims, or changed terminal state), not only missing constants/imports.

- [ ] **Step 3: Extend existing container ownership/CAS paths.** Add a small release-owned tuple and rejection helper beside the existing gate-owned restriction; reuse it at all three generic field doors:

```python
_RELEASE_OWNED_FIELDS = (
    "desired_release_id", "active_release_id", "release_revision", "release_update_id",
)

def _reject_release_owned_fields(caller: str, fields: dict) -> None:
    named = sorted(set(_RELEASE_OWNED_FIELDS) & fields.keys())
    if named:
        raise ValueError(f"{caller}: {named} may only be written by release transactions")
```

Include the tuple in preservation and CAS. Extend the existing `_unchanged_since_read`/`_raise_if_superseded` pair so the condition and failure triage agree about release fields. Retain lease expectations unchanged for existing gate-swap callers. For release fields, a `(present, value)` tuple is a sufficient exact presence comparison, including NULL:

```python
snapshot = existing or {}
# Inside the existing CAS-field loop:
if field in _RELEASE_OWNED_FIELDS:
    lease_expected[field] = (field in snapshot, snapshot.get(field))
    # Pin present NULL with equality, not attribute_not_exists.
# In refusal triage:
actual = (field in current, current.get(field)) if field in _RELEASE_OWNED_FIELDS else current.get(field)
if want != actual:
    raise ProvisionSuperseded(f"owner mutation state for {owner_id} moved during {where} ({field})")
```

Use existing condition construction; no new CAS abstraction. Update its internal absent-field test to include the release presence tuples. Before `upsert` constructs an item, refuse any existing row containing `release_update_id`; concurrent reservation remains protected by the same put CAS. Use a field-presence test, not truthiness.

Add `attribute_not_exists(release_update_id)` to the actual conditional write in `try_claim_provision`, `takeover_provision`, `claim_attempt`, `update_container_leased`, and `release_provision_to_error`. Parenthesize the existing OR expression in `try_claim_provision` before AND-ing this guard. Map takeover's conditional refusal to `ProvisionSuperseded`, retaining other errors.

For `update_fields`, use this condition and request the refused row:

```python
"ConditionExpression": "attribute_exists(owner_id) AND attribute_not_exists(release_update_id)",
"ReturnValuesOnConditionCheckFailure": "ALL_OLD",
```

On conditional failure, use the existing `_deserialize_item` to inspect the returned row. A present reserved row raises `ProvisionSuperseded`; a genuinely absent row returns `None`. A nonempty unparseable returned Item must fail closed rather than masquerade as missing. Never reread and then blindly retry the write. Keep success status publishing unchanged.

- [ ] **Step 4: Guard ordinary update-row mutations.** Define one reusable native condition value, not a wrapper repository or second client:

```python
_UNRESERVED_UPDATE = Attr("release_revision").not_exists() & (
    Attr("update_type").not_exists() | Attr("update_type").ne("release")
)
```

AND this condition into every mutation listed in Step 1. Existing Attr conditions can be combined directly. For existing string conditions, use native equivalents below, preserving each old predicate:

```python
# claim_apply
_UNRESERVED_UPDATE & Attr("status").is_in(["pending", "scheduled"])
# claim_stale_apply
_UNRESERVED_UPDATE & Attr("status").eq("applying") & (
    Attr("apply_claimed_at").not_exists() | Attr("apply_claimed_at").lte(stale_before_epoch)
)
# heartbeat_apply and finish_claimed_apply
_UNRESERVED_UPDATE & Attr("status").eq("applying") & Attr("apply_claim_id").eq(claim_id)
# set_apply_target
_UNRESERVED_UPDATE & Attr("status").eq("applying") & Attr("apply_claim_id").eq(claim_id) & (
    Attr("target_task_definition_arn").not_exists() | Attr("target_task_definition_arn").eq(task_definition_arn)
)
```

Remove manual expression values/names that become unused when Attr emits condition bindings; DynamoDB rejects unused bindings. Retain ones still referenced by UpdateExpression. Leave the legacy snooze existence predicate unchanged and just add the guard; release scheduling/recovery will use its dedicated paired-row path. No read-only query, create, deletion, or service behavior changes in this task.

- [ ] **Step 5: Verify, self-review and commit only scoped files.** Run the RED command again for focused GREEN, then:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/repositories/test_container_repo.py tests/unit/repositories/test_container_repo_provision_claim.py tests/unit/repositories/test_update_repo.py tests/unit/services/test_update_service.py -q -o addopts=''
RUFF_CACHE_DIR=/private/tmp/lightbulb-release-fences-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff check core/repositories/container_repo.py core/repositories/update_repo.py tests/unit/repositories/test_container_repo.py tests/unit/repositories/test_container_repo_provision_claim.py tests/unit/repositories/test_update_repo.py
RUFF_CACHE_DIR=/private/tmp/lightbulb-release-fences-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff format --check core/repositories/container_repo.py core/repositories/update_repo.py tests/unit/repositories/test_container_repo.py tests/unit/repositories/test_container_repo_provision_claim.py tests/unit/repositories/test_update_repo.py
git diff --check
```

Report any pre-existing formatting debt against the recorded BASE, without unrelated bulk formatting. Retain exact RED/GREEN commands/results and concerns in the report. Independent spec/quality review is required.

```sh
git add apps/backend/core/repositories/container_repo.py apps/backend/core/repositories/update_repo.py apps/backend/tests/unit/repositories/test_container_repo.py apps/backend/tests/unit/repositories/test_container_repo_provision_claim.py apps/backend/tests/unit/repositories/test_update_repo.py
git commit -m "fix(releases): fence release-owned repository records"
```
