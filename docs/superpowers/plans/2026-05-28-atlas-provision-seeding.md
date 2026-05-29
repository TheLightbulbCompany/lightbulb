# Atlas Provision Seeding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seed the Atlas recruiter's workspace files into every provisioned container at provision time, mirroring Friday, then remove the `agents/` folder.

**Architecture:** Atlas's source files move from the repo-root `agents/atlas/` into a bundled backend resource dir (`apps/backend/core/services/atlas_seed/files/`). A `pre_stage_atlas` seeder — a near-copy of `pre_stage_friday_identity` — walks that dir and writes each file to the container's EFS workspace at `workspaces/{atlas_uuid}/…` via `write_file_if_missing`, keyed by the Paperclip agent named "Atlas". It is invoked from `ecs_manager.write_user_configs` right after the Friday seeder. Atlas's own `openclaw.json` is NOT shipped (the container config is backend-generated); enabling `lobster`/`llm-task`/`cron` in that generated config is out of scope (owned separately).

**Tech Stack:** Python 3.12, FastAPI backend, pytest, EFS via `core.containers.workspace.Workspace`, git.

**Spec:** `docs/superpowers/specs/2026-05-28-atlas-provision-seeding-design.md`

---

## File Structure

- **Create** `apps/backend/core/services/atlas_seed/__init__.py` — the `pre_stage_atlas` seeder + helpers.
- **Create** `apps/backend/core/services/atlas_seed/files/…` — Atlas's seeded workspace tree (markdown, `.lobster`, `.js`, `skills/`), read at runtime.
- **Modify** `apps/backend/core/containers/ecs_manager.py` (~line 1862, immediately after the Friday seed block) — call `pre_stage_atlas`.
- **Create** `apps/backend/tests/unit/services/test_atlas_seed.py` — unit tests for the seeder.
- **Modify** `apps/backend/tests/unit/containers/test_write_user_configs_seeds_friday.py` — add an assertion that `pre_stage_atlas` is also invoked.
- **Delete** `agents/atlas/`, `agents/atlas-v0/` — completing the `agents/` removal.

---

## Task 1: Relocate Atlas source + recover skill bundles into the backend resource dir

**Files:**
- Create: `apps/backend/core/services/atlas_seed/files/` (populated from `agents/atlas/` on `main` + 7 skill bundles from branch `feat/agent-skills-folders`)
- Verify: `apps/backend/.dockerignore` does not exclude the new files

This is a mechanical relocation (no TDD). The kept runtime files are the 6 markdown, 8 `.lobster`, 18 `.js`. Excluded: `agents/atlas/openclaw.json` (never deployed) and `agents/atlas/tests/`. Seven skill bundles that are absent from `main` (`fast-io, taskr, n8n-workflow, capability-evolver, skill-vetter, sona-security-audit, agentgate`) come from PR #740's branch; the other three Atlas references (`gog, slack, summarize`) ship upstream/baked and must NOT be copied (they'd shadow the bundled versions).

- [ ] **Step 1: Create the resource dir and copy the runtime files (excluding openclaw.json + tests/)**

```bash
cd "$(git rev-parse --show-toplevel)"
mkdir -p apps/backend/core/services/atlas_seed/files
# Copy markdown, lobster, and js from the repo-root atlas dir (top level only — skips tests/ and openclaw.json explicitly below)
cp agents/atlas/*.md agents/atlas/*.lobster agents/atlas/*.js \
   apps/backend/core/services/atlas_seed/files/
```

- [ ] **Step 2: Recover the 7 non-upstream skill bundles from PR #740's branch**

```bash
cd "$(git rev-parse --show-toplevel)"
git fetch origin feat/agent-skills-folders
mkdir -p apps/backend/core/services/atlas_seed/files/skills
for s in fast-io taskr n8n-workflow capability-evolver skill-vetter sona-security-audit agentgate; do
  git archive origin/feat/agent-skills-folders "agents/atlas/skills/$s" \
    | tar -x --strip-components=3 -C apps/backend/core/services/atlas_seed/files/skills/ \
    && echo "recovered skill: $s"
done
```

- [ ] **Step 3: Sanity-check the recovered tree — every skill bundle has a SKILL.md, openclaw.json/tests were NOT copied**

```bash
cd "$(git rev-parse --show-toplevel)"
echo "openclaw.json present? (must be ABSENT):"; ls apps/backend/core/services/atlas_seed/files/openclaw.json 2>&1
echo "tests dir present? (must be ABSENT):"; ls -d apps/backend/core/services/atlas_seed/files/tests 2>&1
echo "skill bundles + SKILL.md presence:"
for s in fast-io taskr n8n-workflow capability-evolver skill-vetter sona-security-audit agentgate; do
  test -f "apps/backend/core/services/atlas_seed/files/skills/$s/SKILL.md" \
    && echo "  OK   $s/SKILL.md" || echo "  MISSING $s/SKILL.md"
done
echo "markdown/lobster/js counts (expect 6 / 8 / 18):"
ls apps/backend/core/services/atlas_seed/files/*.md | wc -l
ls apps/backend/core/services/atlas_seed/files/*.lobster | wc -l
ls apps/backend/core/services/atlas_seed/files/*.js | wc -l
```

Expected: openclaw.json ABSENT, tests ABSENT, all 7 `SKILL.md` OK, counts `6 / 8 / 18`.

- [ ] **Step 4: Fix `.dockerignore` — it WILL strip Atlas's markdown otherwise (BLOCKER)**

`apps/backend/.dockerignore` contains `*.md` (with only `!README.md` negated), under
`# Documentation`. The backend image is a CDK `DockerImageAsset`
(`apps/infra/lib/stacks/service-stack.ts:120`, `directory: backend`) whose Dockerfile
does `COPY . .` (`apps/backend/Dockerfile:33`) and **honors `.dockerignore`**. So
without a negation, all 6 of Atlas's markdown files AND every skill `SKILL.md` are
excluded from the image — Atlas would seed a workspace missing its identity + skill
docs. `.dockerignore` is last-match-wins, so the negation must come AFTER `*.md`.
Append this block to the END of `apps/backend/.dockerignore`:

```
# Atlas seed resources — re-include after the *.md / .DS_Store / __pycache__ globs
# above strip them. These ship in the image and are read at provision time.
!core/services/atlas_seed/files/**
```

- [ ] **Step 5 (verify the fix): confirm the markdown survives the build context**

```bash
cd "$(git rev-parse --show-toplevel)/apps/backend"
# Build just the context listing and confirm an IDENTITY.md + a SKILL.md are present.
docker build -f Dockerfile -t atlas-ctx-check --target builder . >/dev/null 2>&1 || true
# Lighter check without a full build — dockerignore honors trailing negations:
python3 - <<'PY'
import pathlib, fnmatch
root = pathlib.Path(".")
ignore = [l.strip() for l in (root/".dockerignore").read_text().splitlines()
          if l.strip() and not l.startswith("#")]
def included(p):
    inc = True
    for pat in ignore:
        neg = pat.startswith("!")
        g = pat[1:] if neg else pat
        if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.rstrip("/")+"/*") or fnmatch.fnmatch(p, g+"**"):
            inc = neg
    return inc
for f in ["core/services/atlas_seed/files/IDENTITY.md",
          "core/services/atlas_seed/files/skills/fast-io/SKILL.md"]:
    print(("INCLUDED " if included(f) else "EXCLUDED!") + f)
PY
```

Expected: both lines print `INCLUDED`. (Renumber the remaining steps in this task accordingly.)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/atlas_seed/files
git commit -m "chore(atlas-seed): vendor Atlas workspace files + 7 skill bundles into backend"
```

---

## Task 2: Implement the `pre_stage_atlas` seeder (TDD)

**Files:**
- Create: `apps/backend/core/services/atlas_seed/__init__.py`
- Test: `apps/backend/tests/unit/services/test_atlas_seed.py`

The seeder mirrors `core/services/friday_identity.py::pre_stage_friday_identity`: resolve the agent (by `name=="Atlas"` — `role` is not unique), write a `workspace-state.json` with `setupCompletedAt` first (so OpenClaw's boot reconcile doesn't delete `BOOTSTRAP.md`), then walk the resource dir writing each file via `write_file_if_missing`. Best-effort: per-file errors log + continue (the tree has 150+ files; one bad file must not strand the rest), and the function never raises.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_atlas_seed.py
"""Tests for core.services.atlas_seed.pre_stage_atlas."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.services import atlas_seed
from core.services.atlas_seed import pre_stage_atlas


@pytest.fixture
def workspace_stub():
    """A Workspace stand-in recording write_file_if_missing(user_id, path, content)."""
    ws = MagicMock()
    ws.calls = []

    def _write(user_id, path, content):
        ws.calls.append((user_id, path))
        return True  # pretend every file was newly written

    ws.write_file_if_missing.side_effect = _write
    return ws


@pytest.fixture
def fake_files_dir(tmp_path, monkeypatch):
    """Point the seeder at a tiny fake resource tree."""
    root = tmp_path / "files"
    (root / "skills" / "fast-io").mkdir(parents=True)
    (root / "IDENTITY.md").write_text("# Atlas", encoding="utf-8")
    (root / "build.lobster").write_text("pipeline {}", encoding="utf-8")
    (root / "skills" / "fast-io" / "SKILL.md").write_text("# fast-io", encoding="utf-8")
    monkeypatch.setattr(atlas_seed, "_FILES_DIR", root)
    return root


@pytest.mark.asyncio
async def test_seeds_atlas_files_keyed_by_uuid(workspace_stub, fake_files_dir):
    agents = [{"id": "uuid-friday", "role": "ceo", "name": "Friday"},
              {"id": "uuid-atlas", "role": "general", "name": "Atlas"}]

    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)

    paths = [p for (_u, p) in workspace_stub.calls]
    assert "workspaces/uuid-atlas/.openclaw/workspace-state.json" in paths
    assert "workspaces/uuid-atlas/IDENTITY.md" in paths
    assert "workspaces/uuid-atlas/build.lobster" in paths
    assert "workspaces/uuid-atlas/skills/fast-io/SKILL.md" in paths
    # never seeds into another agent's workspace
    assert all("uuid-friday" not in p for p in paths)


@pytest.mark.asyncio
async def test_skips_when_no_atlas(workspace_stub, fake_files_dir):
    agents = [{"id": "uuid-friday", "role": "ceo", "name": "Friday"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    workspace_stub.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_per_file_error_does_not_raise_or_abort(workspace_stub, fake_files_dir):
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    # First write raises, the rest succeed — must continue, not raise, not stop.
    calls = {"n": 0}

    def _flaky(user_id, path, content):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("EFS hiccup")
        return True

    workspace_stub.write_file_if_missing.side_effect = _flaky
    # Should not raise.
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    # More than one write attempted => it continued past the failure.
    assert calls["n"] > 1


@pytest.mark.asyncio
async def test_state_file_written_first(workspace_stub, fake_files_dir):
    """workspace-state.json is written BEFORE any workspace file (BOOTSTRAP guard)."""
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    paths = [p for (_u, p) in workspace_stub.calls]
    state = "workspaces/uuid-atlas/.openclaw/workspace-state.json"
    assert paths[0] == state, f"state file must be first, got order: {paths}"


@pytest.mark.asyncio
async def test_does_not_clobber_existing_files(workspace_stub, fake_files_dir):
    """write_file_if_missing returning False (file exists) => counted skipped, no raise."""
    agents = [{"id": "uuid-atlas", "role": "general", "name": "Atlas"}]
    # Simulate every file already present on a reprovision.
    workspace_stub.write_file_if_missing.side_effect = lambda u, p, c: False
    await pre_stage_atlas(owner_id="user_x", paperclip_agents=agents, workspace=workspace_stub)
    # It still attempted each file (idempotent), and did not raise.
    assert workspace_stub.write_file_if_missing.call_count >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && CLERK_ISSUER=https://test.clerk.accounts.dev uv run pytest tests/unit/services/test_atlas_seed.py -v`
Expected: FAIL with `ModuleNotFoundError: core.services.atlas_seed` (or `cannot import name 'pre_stage_atlas'`).

- [ ] **Step 3: Implement the seeder**

```python
# apps/backend/core/services/atlas_seed/__init__.py
"""Pre-stage Atlas's workspace files on EFS at provision time.

Atlas is the seeded agent-recruiter (Paperclip name="Atlas"). Like Friday, its
workspace files are written to EFS before the container boots so OpenClaw's
writeFileIfMissing honors them on first run. Unlike Friday (5 inline markdown
templates), Atlas ships dozens of files — .lobster workflows, .js tools, and
skill bundles — so they live as packaged files under ``files/`` and are walked
at runtime.

Atlas's own openclaw.json is intentionally NOT shipped: the container's config
is generated by core.containers.config. The plugin/cron enablement Atlas needs
(lobster, llm-task, cron) is handled in that generated config separately.

Best-effort: the caller (ecs_manager.write_user_configs) wraps this in
try/except; additionally each file write is guarded so one unreadable file can't
strand the remaining ~150. This function never raises.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.containers.workspace import Workspace
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# Matched by display name: Atlas is seeded with role="general" (shared with
# marketplace hires), so role is not a unique handle. A user-renamed Atlas or a
# name collision is skipped — acceptable for a best-effort seed.
_ATLAS_AGENT_NAME = "Atlas"
_FILES_DIR = Path(__file__).parent / "files"


async def pre_stage_atlas(
    *,
    owner_id: str,
    paperclip_agents: list[dict],
    workspace: Workspace,
) -> None:
    atlas = next(
        (a for a in paperclip_agents if a.get("name") == _ATLAS_AGENT_NAME and a.get("id")),
        None,
    )
    if atlas is None:
        # Mirror Friday's two-branch split: an empty roster means Paperclip
        # isn't ready yet (defer to next provision); a non-empty roster with no
        # Atlas means the Atlas seed failed earlier or the agent was renamed.
        if not paperclip_agents:
            put_metric("atlas_seed.skipped_no_agents")
            logger.info("atlas_seed.skipped: no paperclip agents for owner=%s — deferring", owner_id)
        else:
            put_metric("atlas_seed.skipped_no_atlas")
            logger.info("atlas_seed.skipped: no agent named Atlas for owner=%s", owner_id)
        return

    atlas_uuid = atlas["id"]

    # Pre-mark setup complete first so OpenClaw's boot reconcile doesn't rm
    # BOOTSTRAP.md when it sees Atlas's customized IDENTITY/SOUL/USER files.
    now_iso = datetime.now(timezone.utc).isoformat()
    state_rel = f"workspaces/{atlas_uuid}/.openclaw/workspace-state.json"
    state = {"version": 1, "bootstrapSeededAt": now_iso, "setupCompletedAt": now_iso}
    try:
        workspace.write_file_if_missing(owner_id, state_rel, json.dumps(state))
    except Exception as exc:  # noqa: BLE001 — best-effort, log + continue
        put_metric("atlas_seed.state_error")
        logger.warning("atlas_seed: workspace-state write failed owner=%s: %s", owner_id, exc)

    # Junk that should never be seeded into a user's workspace, even if it
    # sneaks into the vendored tree (mirrors agent_export's hygiene filter).
    _SKIP = {".DS_Store", "__pycache__", ".git"}

    written = 0
    skipped = 0
    errored = 0
    for abs_path in sorted(_FILES_DIR.rglob("*")):
        if not abs_path.is_file():
            continue
        rel = abs_path.relative_to(_FILES_DIR).as_posix()
        if any(part in _SKIP for part in abs_path.relative_to(_FILES_DIR).parts):
            continue
        dest = f"workspaces/{atlas_uuid}/{rel}"
        try:
            # Text-only: Atlas's vendored tree is all text (.md/.js/.lobster/.json/
            # .sh/.py/.ts). If a binary asset is ever added, switch to
            # workspace.write_bytes (workspace.py) for that file — read_text would
            # raise UnicodeDecodeError and that file would land in `errored`.
            content = abs_path.read_text(encoding="utf-8")
            wrote = workspace.write_file_if_missing(owner_id, dest, content)
        except Exception as exc:  # noqa: BLE001 — one bad file must not strand the rest
            errored += 1
            put_metric("atlas_seed.error")
            logger.error("atlas_seed: write failed owner=%s file=%s: %s", owner_id, dest, exc)
            continue
        if wrote:
            written += 1
        else:
            skipped += 1

    if written:
        put_metric("atlas_seed.success", value=written)
    if skipped:
        put_metric("atlas_seed.skipped_existing", value=skipped)
    logger.info(
        "atlas_seed: owner=%s atlas=%s wrote=%d skipped=%d errored=%d",
        owner_id, atlas_uuid, written, skipped, errored,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && CLERK_ISSUER=https://test.clerk.accounts.dev uv run pytest tests/unit/services/test_atlas_seed.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/atlas_seed/__init__.py apps/backend/tests/unit/services/test_atlas_seed.py
git commit -m "feat(atlas-seed): pre_stage_atlas seeds Atlas workspace files at provision"
```

---

## Task 3: Wire `pre_stage_atlas` into `write_user_configs` (TDD)

**Files:**
- Modify: `apps/backend/core/containers/ecs_manager.py` (immediately after the existing Friday-seed try/except, ~line 1862)
- Test: `apps/backend/tests/unit/containers/test_write_user_configs_seeds_friday.py` (extend the existing "invokes seeder" test)

- [ ] **Step 1: Add the failing assertion to the existing wire-up test**

In `apps/backend/tests/unit/containers/test_write_user_configs_seeds_friday.py`, inside
`test_invokes_seeder_after_write_openclaw_config`, add an Atlas seeder mock and assertion.
Add this patch alongside the existing `pre_stage_friday_identity` patch:

```python
        patch(
            "core.services.atlas_seed.pre_stage_atlas",
            atlas_seeder_mock,
        ),
```

Define `atlas_seeder_mock = AsyncMock()` next to `seeder_mock = AsyncMock()`, and add after the
existing `seeder_mock.assert_awaited_once()`:

```python
    atlas_seeder_mock.assert_awaited_once()
    atlas_kwargs = atlas_seeder_mock.call_args.kwargs
    assert atlas_kwargs["owner_id"] == "user_abc"
    assert atlas_kwargs["paperclip_agents"] == paperclip_agents
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/backend && CLERK_ISSUER=https://test.clerk.accounts.dev uv run pytest tests/unit/containers/test_write_user_configs_seeds_friday.py::test_invokes_seeder_after_write_openclaw_config -v`
Expected: FAIL — `atlas_seeder_mock` is never awaited (call count 0).

- [ ] **Step 3: Add the Atlas seed call after the Friday seed block in `ecs_manager.py`**

Immediately after the existing Friday `try/except` block (the one ending at the
`logger.warning("write_user_configs: pre_stage_friday_identity failed ...")`), insert:

```python
        # Seed Atlas's recruiter workspace (files + skill bundles) on EFS,
        # mirroring Friday. Best-effort — an Atlas failure MUST NOT block
        # provision; Atlas boots with an empty workspace if this fails.
        from core.services.atlas_seed import pre_stage_atlas

        try:
            await pre_stage_atlas(
                owner_id=owner_id,
                paperclip_agents=paperclip_agents,
                workspace=get_workspace(),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort.
            # Distinct from the seeder's per-file `atlas_seed.error`: this fires
            # only on a catastrophic failure (e.g. import error) where the seeder
            # never ran, vs a single bad file the seeder logged and skipped.
            put_metric("atlas_seed.invoke_error")
            logger.warning(
                "write_user_configs: pre_stage_atlas failed for owner=%s: %s",
                owner_id,
                exc,
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/backend && CLERK_ISSUER=https://test.clerk.accounts.dev uv run pytest tests/unit/containers/test_write_user_configs_seeds_friday.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/containers/test_write_user_configs_seeds_friday.py
git commit -m "feat(atlas-seed): invoke pre_stage_atlas from write_user_configs"
```

---

## Task 4: Delete `agents/atlas/` and `agents/atlas-v0/` (complete the `agents/` removal)

**Files:**
- Delete: `agents/atlas/`, `agents/atlas-v0/`

The runtime files now live in `apps/backend/core/services/atlas_seed/files/`, so the repo-root copies are redundant. `agents/atlas-v0/` is the frozen, superseded predecessor.

- [ ] **Step 1: Remove the directories**

```bash
cd "$(git rev-parse --show-toplevel)"
git rm -r agents/atlas agents/atlas-v0
```

- [ ] **Step 2: Verify the only remaining backend reference to Atlas's files is the new resource dir**

```bash
cd "$(git rev-parse --show-toplevel)"
# Should return NO hits referencing the repo-root agents/atlas path from runtime code:
grep -rn "agents/atlas" apps/backend/core apps/backend/routers 2>/dev/null | grep -v ".venv" | grep -v "atlas_seed" || echo "clean — no stale references"
```

Expected: `clean — no stale references`.

- [ ] **Step 3: Commit**

```bash
git commit -m "chore(atlas-seed): remove repo-root agents/atlas + atlas-v0 (vendored into backend)"
```

---

## Final Verification

- [ ] **Step 1: Run the full backend unit suite for the touched areas**

Run: `cd apps/backend && CLERK_ISSUER=https://test.clerk.accounts.dev uv run pytest tests/unit/services/test_atlas_seed.py tests/unit/containers/test_write_user_configs_seeds_friday.py -v`
Expected: all PASS.

- [ ] **Step 2: Lint/format gate (matches CI)**

Run: `cd apps/backend && uv run ruff check core/services/atlas_seed && uv run ruff format --check core/services/atlas_seed`
Expected: no errors.

- [ ] **Step 3: Open the PR** (per repo rules — never push to main directly; this branch carries the spec doc too)

```bash
git add docs/superpowers/specs/2026-05-28-atlas-provision-seeding-design.md docs/superpowers/plans/2026-05-28-atlas-provision-seeding.md
git commit -m "docs(atlas-seed): design + implementation plan"
gh pr create --base main --title "feat(atlas-seed): seed Atlas at provision + finish agents/ removal" --body "$(cat <<'BODY'
## Summary
Seeds the Atlas recruiter's workspace files (markdown + .lobster + .js + 7 skill bundles) into every provisioned container at provision time, mirroring Friday's pre-stage. Relocates Atlas's source from repo-root `agents/atlas/` into `apps/backend/core/services/atlas_seed/files/`, adds `pre_stage_atlas` (invoked from `write_user_configs`), and deletes `agents/atlas` + `agents/atlas-v0` to finish the `agents/` removal.

## Out of scope
- Enabling `lobster`/`llm-task` plugins + `cron` in the generated `openclaw.json` (owned separately).
- Atlas's `ISOL8_AGENTS_DIR` agents-root build-output integration (deferred — needs product decision).

## Design + plan
- Spec: `docs/superpowers/specs/2026-05-28-atlas-provision-seeding-design.md`
- Plan: `docs/superpowers/plans/2026-05-28-atlas-provision-seeding.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

---

## Notes for the implementer

- **Out of scope (do NOT add):** enabling `lobster`/`llm-task` plugins or a `cron` block in `core/containers/config.py`. The repo owner is handling that separately. Atlas's seeded files are inert until that lands — that's expected and not a bug in this change.
- **Deferred (do NOT attempt):** the `ISOL8_AGENTS_DIR` / agents-root data-plane (Atlas's *build-other-agents* output). It needs a product decision and is tracked separately.
- **Interaction with PR #744:** #744 deletes the other static `agents/` dirs and keeps `atlas`/`atlas-v0`. This plan deletes those two. They touch disjoint paths, so no merge conflict; order of merge doesn't matter.
- **Worktree:** create an isolated worktree via `superpowers:using-git-worktrees` before executing (per repo worktree workflow).
