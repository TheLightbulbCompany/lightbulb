# Config Guard Composition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the release executor acquire the existing OpenClaw sidecar while already holding the owner recovery guard, without releasing that guard early.

**Architecture:** Extract the current protocol-selection body into one private guard-held context manager. The existing public wrapper retains its signature, acquires the recovery guard once and delegates. No new lock, bypass flag, runtime caller, or release executor is introduced.

**Tech Stack:** Existing Python contextlib/fcntl/threading, pytest and stdlib subprocess.

**Spec:** `docs/superpowers/plans/2026-09-08-release-fencing-contract.md`, CAS operations and lock order; submitted founder goal section6. This is the lock-composition prerequisite, not cancellation-safe execution or a release-revision check.

## Global Constraints

- Keep one implementation writer at a time in a shared worktree.
- Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion.
- Do not expose secrets in commands, logs, reports, screenshots or transcripts.
- No candidate assignment may be enabled until paired-row transactions and all config/lifecycle effect boundaries participate.
- Lock order is existing process-local lock → existing persistent recovery guard → OpenClaw sidecar when active config is touched. Never unlink, replace or bypass an occupied guard/sidecar to make progress.

## Evidence and scope

Controller inspected all of efs_file_lock.py, its existing tests, and the two current config-writer callers. Both callers already take the process-local lock and use the public sidecar wrapper; neither is changed by this task. The wrapper currently nests `openclaw_config_recovery_guard`, whose unlock/close can release an outer process-scoped fcntl lock on the same guard inode.

The disposable two-process probe at `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/docs/superpowers/plans/2026-09-08-config-guard-characterization.py` confirms that an outer guard excludes another process before/during nested public-sidecar acquisition but not after the inner context exits, in both legacy and ephemeral modes. This is an unsafe proposed composition, not evidence that current production callers nest these contexts.

Ruling: expose the composable implementation only as a private module helper with an explicit already-held-guard precondition. Do not add a public `guard_held=True` option or new token/lock abstraction. Existing private legacy/ephemeral helpers have the same internal discipline. Cost if wrong: the future executor must be corrected during integration review if it violates the documented precondition; no external endpoint can select this helper.

Ruling: retain the existing public timeout budget by calculating its absolute deadline before guard acquisition and passing it unchanged to the inner helper. Do not reset the sidecar timeout after waiting for the guard. Cost if wrong: an owner can wait longer than its established lock timeout, affecting recovery availability.

### Task 1: Extract a guard-held sidecar context with real process-exclusion tests

**Files:**
- Modify: `apps/backend/core/services/efs_file_lock.py`
- Modify: `apps/backend/tests/unit/services/test_efs_file_lock.py`

**Interfaces:**

Preserve every existing public signature and exception/result behavior. Add only:

```python
@contextmanager
def _openclaw_config_sidecar_under_guard(
    lock_dir: str,
    lock_path: str,
    *,
    deadline: float,
    poll_interval_s: float,
) -> Iterator[None]:
    ...
```

Caller must already hold the matching process-local lock and persistent recovery guard. The helper acquires only the protocol-appropriate sidecar; it never opens, locks, unlocks or closes the guard. No new call sites outside this module/tests in this task. No database, AWS, dependency, schema, marker-migration, runtime or deployment change.

- [ ] **Step 1: Add tests using the existing fixture/helper style.** Import the new private helper and `get_file_lock`, plus stdlib subprocess/sys/time and contextlib.nullcontext. Keep all existing protocol/identity tests.

Use a real child process for guard exclusion, not a mocked fcntl or a thread (fcntl locks are process-scoped). A small test-local helper may run this code via `[sys.executable, "-c", code, guard_path]` with captured output and timeout5seconds:

```python
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
else:
    sys.exit(0)
```

Only returncodes0/1 are meaningful; assert other exits with stderr as a test error. Code0 means acquired,1 means excluded. Child process exit closes its fd. Use tmp_path only; no AWS/app subprocess import and no sleeps.

Parameterize legacy versus ephemeral (using the existing `_write_protocol`) and normal body versus raised `RuntimeError("body failed")`. Inside `get_file_lock(lock_path)` and `openclaw_config_recovery_guard(...)`:

```python
assert not child_can_acquire(guard_path)
with (pytest.raises(RuntimeError, match="body failed") if body_error else nullcontext()):
    with _openclaw_config_sidecar_under_guard(
        str(tmp_path), str(lock_path),
        deadline=time.monotonic() + 1, poll_interval_s=0.01,
    ):
        assert not child_can_acquire(guard_path)
        assert lock_path.is_file()
        if body_error:
            raise RuntimeError("body failed")
assert not child_can_acquire(guard_path)
```

After leaving the outer guard, assert the child CAN acquire it. Legacy sidecar persists; ephemeral sidecar is absent. Verify expected body exception propagation explicitly with pytest.raises when testing the error case, so a swallowed error cannot pass. This directly distinguishes the new helper from nesting the old public wrapper.

Also test the public wrapper forwards its pre-guard deadline and poll interval to the new helper: stub `time.monotonic` to42 and use timeout7/poll0.01; a contextmanager stub records deadline49/poll0.01. Existing real protocol/identity tests prove delegation retains actual behavior. Do not add unrelated lock tests or a multiprocessing framework.

- [ ] **Step 2: Prove RED before production changes.** From apps/backend:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_efs_file_lock.py -q -o addopts=''
```

Expected: new helper missing. Record the exact failure, then implement the extraction. The controller's preexisting two-process characterization already demonstrates the unsafe nested behavior independently.

- [ ] **Step 3: Extract the existing protocol selection without rewriting it.** Insert the new helper immediately before the public wrapper:

```python
@contextmanager
def _openclaw_config_sidecar_under_guard(
    lock_dir: str, lock_path: str, *, deadline: float, poll_interval_s: float,
) -> Iterator[None]:
    """Acquire only the sidecar; caller holds the matching local lock and recovery guard."""
    if _uses_ephemeral_openclaw_protocol(lock_path):
        with _ephemeral_openclaw_config_lock(
            lock_path, deadline=deadline, poll_interval_s=poll_interval_s,
        ):
            yield
    else:
        with _legacy_openclaw_config_lock(
            lock_dir, lock_path, deadline=deadline, poll_interval_s=poll_interval_s,
        ):
            yield
```

Keep the public wrapper's deadline calculation and recovery guard acquisition exactly where they are. Replace its duplicated protocol-selection block with:

```python
with _openclaw_config_sidecar_under_guard(
    lock_dir, lock_path, deadline=deadline, poll_interval_s=poll_interval_s,
):
    yield
```

Keep `_legacy_openclaw_config_lock`, `_ephemeral_openclaw_config_lock`, recovery-guard identity checking, migration marker values, ownership, timeout handling and exception propagation unchanged. Do not implement reentrancy or bypass missing/invalid protocol markers. Do not wire a release caller in this task.

- [ ] **Step 4: Verify, self-review, report and commit.** Focused GREEN then the existing relevant writer/lock regressions once:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_efs_file_lock.py tests/unit/services/test_config_patcher.py tests/unit/containers/test_config.py -q -o addopts=''
RUFF_CACHE_DIR=/private/tmp/lightbulb-guard-composition-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff check core/services/efs_file_lock.py tests/unit/services/test_efs_file_lock.py
RUFF_CACHE_DIR=/private/tmp/lightbulb-guard-composition-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff format --check core/services/efs_file_lock.py tests/unit/services/test_efs_file_lock.py
git diff --check
```

Self-review for one protocol implementation, preserved absolute deadline, exception cleanup, exact inode identity behavior, and actual independent-process assertions. Existing pre-task lint debt is not authority for unrelated formatting. Commit only the two files from repository root:

```sh
git add apps/backend/core/services/efs_file_lock.py apps/backend/tests/unit/services/test_efs_file_lock.py
git commit -m "refactor(config): compose sidecar locks under an existing guard"
```

Write exact RED/GREEN/static results, changed files, self-review, commit and concerns to the controller-provided report path. Independent task review and later Linux/EFS/cancellation integration proof remain required.
