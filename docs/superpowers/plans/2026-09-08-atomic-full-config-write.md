# Atomic Full Config Write Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A full generated config becomes visible as one complete file, and pre-commit failures leave the previous active config intact.

**Architecture:** Replace direct truncation in `write_openclaw_config` with a same-directory tempfile and atomic replacement inside the existing locks. Reuse the existing hot patcher's stdlib pattern and uid-1000 helper; add a file flush/fsync before replacement. No new storage abstraction or lock is needed.

**Tech Stack:** Python 3.12, stdlib os/tempfile/pathlib/json, existing pytest.

**Spec:** Submitted founder goal section 6; approved R2 staged-config foundation; `2026-09-08-release-fencing-contract.md` identifies this direct-write prerequisite. This task is not the release/revision fence or staged activation transaction.

## Global Constraints

- Do not upgrade other production owners or change the new-user production image default under this goal.
- Never bypass sandbox approvals, branch protection, identity checks, billing limits or failing release gates.
- Preserve existing schema audit/enforce behavior, file-lock protocols and provider/config output.
- Preserve the goal's ledgers and release evidence for handoff; do not automatically delete them at completion.
- Do not expose config values, credentials or exception payloads in new logs.

## Source and boundary review

`core/containers/config.py:3027-3039` validates, obtains the current local and transition-aware sidecar locks, then calls `config_path.write_text(json.dumps(...))`. That truncates the active inode. The sole production caller is `EcsManager.write_user_configs`; all full provision/regeneration flows converge here. `config_patcher._locked_rmw:160-170` already uses a temporary file and rename for incremental writes. Workspace's other atomic writes are specific to OAuth/device formats and should not be repurposed.

Scope: active-file visibility and pre-replacement failure safety only. Existing full render happens before locking and has no release-revision fence; the subsequent release-fencing task must address that separately. File fsync is not a claim of a rehearsed backup, cross-file snapshot, directory power-loss durability, or successful ECS replacement. Do not add a directory-fsync policy or change recovery markers in this task.

### Task 1: Replace full config atomically under existing locks

**Files:**
- Modify: `apps/backend/core/containers/config.py`
- Modify: `apps/backend/tests/unit/containers/test_config.py`

**Interfaces:**
- Preserve public `write_openclaw_config` signature, provider generation, schema selection defaults and return type.
- Preserve canonical local lock and `openclaw_config_sidecar_lock`, including both legacy and ephemeral modes.
- Successful writes replace the destination inode only after complete serialization, flush/fsync and ownership handling. Use private mode0600 tempfile with existing `chown_uid_1000_best_effort`.
- Serialization, fsync or replace failure propagates, cleans its own temporary file, and leaves active config and existing `.bak` bytes unchanged.

- [ ] **Step 1: Add behavioral and injected-failure tests.** Extend existing `TestWriteOpenclawConfigAsync`, retaining `_AIPS` and current positive/ephemeral-lock tests. The held-reader case demonstrates the root defect without mocking the implementation:

```python
async def test_atomic_full_write_preserves_open_reader(self, tmp_path):
    out = tmp_path / "openclaw.json"
    previous = '{"sentinel":"previous complete config"}'
    out.write_text(previous)
    with out.open() as held_reader:
        await write_openclaw_config(
            config_path=out, gateway_token="synthetic-new-token",
            provider_choice="bedrock_claude", user_id="u_1", aip_arns=_AIPS,
        )
        assert held_reader.read() == previous
    assert json.loads(out.read_text())["gateway"]["auth"]["token"] == "synthetic-new-token"
    assert out.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".openclaw.json.*.tmp"))
```

Add one parametrized test for `serialize`, `fsync`, and `replace` failures, with an existing config and `.bak` containing distinct synthetic sentinels. Serialization injection must write partial bytes through the actual tempfile handle and then raise `OSError("synthetic write failure")`:

```python
def fail_serialization(_config, stream, **_kwargs):
    stream.write('{"partial":')
    raise OSError("synthetic write failure")
```

Patch `core.containers.config.json.dump` for that case; patch `core.containers.config.os.fsync` or `.os.replace` to raise the same OSError for the other cases. These are external serialization/I/O fault boundaries, not mocks of the production write function. Require the exception, unchanged active and `.bak` bytes, and no remaining `.openclaw.json.*.tmp`. Include a successful first-write/no-prior-config case through the existing tests. Keep lock timeout proof that no active bytes change; do not remove or bypass its sidecar.

- [ ] **Step 2: Prove RED before changing production code.** From `apps/backend`:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/containers/test_config.py::TestWriteOpenclawConfigAsync -k atomic -q -o addopts=''
```

The held-reader case must fail because it reads rewritten bytes on the old inode. Record that behavioral failure independently of the expected absent-import/new-I/O-boundary failures in the fault cases; setup-only failures are insufficient RED evidence.

- [ ] **Step 3: Implement the minimum atomic write.** Add `os`, `tempfile`, and the existing ownership helper imports. Inside the current two locks, replace only the direct `write_text` call with this pattern:

```python
fd, temporary_path = tempfile.mkstemp(
    dir=config_path.parent, prefix=f".{config_path.name}.", suffix=".tmp",
)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    chown_uid_1000_best_effort(temporary_path)
    os.replace(temporary_path, config_path)
finally:
    Path(temporary_path).unlink(missing_ok=True)
```

Keep temp creation and replacement within the locks and on the destination filesystem. Do not delete or overwrite `.bak`, change config contents/defaults, catch-and-continue an I/O error, add a new helper, or move validation policy in this task. Update the write docstring/comment to say atomic replacement; no broader refactor.

- [ ] **Step 4: Verify GREEN and provider/lock regressions.** Run:

```sh
AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_EC2_METADATA_DISABLED=true PYTHONPATH=. /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/containers/test_config.py tests/unit/containers/test_config_provider_routing.py tests/unit/containers/test_config_paperclip_agents.py tests/unit/services/test_config_schema.py -q -o addopts=''
RUFF_CACHE_DIR=/private/tmp/lightbulb-atomic-config-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff check core/containers/config.py tests/unit/containers/test_config.py
RUFF_CACHE_DIR=/private/tmp/lightbulb-atomic-config-ruff /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/ruff format --check core/containers/config.py tests/unit/containers/test_config.py
git diff --check
```

Report pre-existing formatting debt against the task base; do not bulk-format unrelated generator code. No Docker/AWS/browser/dependency work is needed.

- [ ] **Step 5: Self-review and commit only the two files.** Record RED/GREEN commands, counts and failure-safety checks. Independent spec/quality approval is required before downstream release work relies on this primitive.

```sh
git add apps/backend/core/containers/config.py apps/backend/tests/unit/containers/test_config.py
git commit -m "fix(config): atomically replace full generated configs"
```
