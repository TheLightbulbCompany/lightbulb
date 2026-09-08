# Restart Config Failure Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stopped service must not start or register a replacement after its required configuration write fails.

**Architecture:** Correct the existing common stopped-service provision branch. Preserve the original exception so the existing outer fenced failure handler can classify it and release only its own lease. Reuse the current restart test helpers and real mocked-DynamoDB lease tests; add no new runtime abstraction or rollout API. This independently closes one confirmed R2 defect while the fork toolchain decision is pending; it does not implement release assignment or staged activation.

**Tech Stack:** Python 3.12, existing pytest/moto, mocked ECS/EFS boundaries.

**Spec:** Approved founder delivery R2 and submitted goal section 6; lifecycle reconciliation report.

## Global Constraints

- Do not upgrade other production owners or change the new-user production image default under this goal.
- Never bypass sandbox approvals, branch protection, identity checks, billing limits or failing release gates.
- Preserve provision claim/generation/attempt fencing and existing owner-isolated ECS services.
- Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion.

## Source evidence and scope

At baseline `a11402079ea61c8c8bd2a23fcb631cb105b16d48`, stopped provision in `core/containers/ecs_manager.py:3265-3293` catches config errors and logs “continuing anyway,” then registers a new task and scales up. The no-access-point fallback also starts after that swallowed error. Silent wake, recovery, provider switches and admin reprovision converge here via the existing provision runner. The fresh-service branch already stops on config failure.

The outer `provision_user_container` at lines 2821-2916 owns error classification and conditional lease release. `container_repo.classify_provision_error` classifies the immediate exception: wrapping a `ValueError` in `EcsManagerError` would lose deterministic classification. Bare re-raise is intentional. `ProvisionSuperseded` must continue to bypass ordinary error release.

This plan does not change fresh-provision classification, active config writing, best-effort Paperclip identity seeding, desired-release policy, access-point recovery, or any deployment.

### Task 1: Fail stopped-service provision before activation when config fails

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py`
- Modify: `apps/backend/tests/unit/containers/test_ecs_manager.py`

**Interfaces:**
- Consumes: existing stopped ACTIVE service, provision claim/generation/attempt, required `write_user_configs` result.
- Produces on failure: original exception reaches the existing outer handler; old task-definition pointer remains; no register/update/start/deregister/poller side effect follows the failed config write.
- Produces on success: unchanged current restart behavior.

- [ ] **Step 1: Add focused behavior tests and prove RED.** Extend the existing `TestProvisionLeaseFencing` tests using `_real_containers_table`, `_seed_restart_row`, and `_stub_restart_branch`. Exercise the actual public `provision_user_container` entry point with caller-supplied claim and mocked external boundaries. Inject a synthetic `ValueError("invalid config shape")` and an `OSError("EFS unavailable")` at `write_user_configs`; cover both access-point-present and absent rows. A small parametrized test is sufficient; do not add a fixture framework or production test hook.

Required assertions: original exception propagates; `write_user_configs` was awaited; no task-definition register, ECS update, simple start, deregister, or detached running poller occurs afterward; existing task-definition pointer is unchanged; the real lease release transitions the row to error and records `retryable=False` for ValueError and `True` for OSError. No claim from a newer provision may be released. Add a focused supersession case at the config boundary that demonstrates no ordinary error release and no downstream activation; reuse an existing equivalent if it already covers this exact boundary. Preserve the existing successful restart test as the positive control.

Use monkeypatch/AsyncMock only for external work and failure injection. Do not patch the production branch or classifier under test. Use the existing source-module lease spy conventions.

Run from `apps/backend`:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/containers/test_ecs_manager.py -k 'restart_config' -q -o addopts=''
```

Expected RED: the current handler swallows ordinary config failures and reaches activation instead of raising. Fixture/setup failures are not RED evidence.

- [ ] **Step 2: Correct the shared stopped-provision handler.** Replace the “continuing anyway” ordinary exception branch with failure metrics and a bare `raise`. Keep `except ProvisionSuperseded: raise` ahead of it. Reuse the two metric calls already used by sibling provision failure branches. Do not write status/error rows here: the outer claim-fenced handler is the sole owner. Do not include configuration values in new logs or wrap exceptions into a less precise type. No new helper or lifecycle-level caller edits are necessary.

- [ ] **Step 3: Prove GREEN and preserved leases/success.** Run the new tests and the whole existing `TestProvisionLeaseFencing` class, plus focused restart/provision sizing tests. Use the existing Python environment and dummy AWS credentials above. Avoid the known long ECS poller suite for the tight feedback loop; the controller owns the separate full baseline verification.

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/containers/test_ecs_manager.py::TestProvisionLeaseFencing tests/unit/containers/test_provision_plan_sizing.py -q -o addopts=''
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff check core/containers/ecs_manager.py tests/unit/containers/test_ecs_manager.py
/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff format --check core/containers/ecs_manager.py tests/unit/containers/test_ecs_manager.py
git diff --check
```

If existing lint/format debt is encountered, prove it at the task base and report it rather than bulk-format unrelated code. Required GREEN includes the unchanged positive restart case, claimed error classification, supersession, and no-access-point fallback refusal.

- [ ] **Step 4: Self-review and commit only the two task files.** Record exact RED/GREEN results, test counts, diff scope and any concerns in the implementer report. No push or deployment. Independent task review must approve both spec and quality before acceptance.

```sh
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_ecs_manager.py
git commit -m "fix(containers): stop restart when required config write fails"
```
