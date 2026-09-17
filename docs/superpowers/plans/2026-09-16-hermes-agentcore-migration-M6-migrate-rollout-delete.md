# Hermes on AgentCore — M6 Migrate, roll out, delete (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Master plan:** `docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md` — read its Goal, Architecture, Global Constraints and File Structure first; every task here inherits them. **Spec:** `docs/superpowers/specs/2026-09-16-hermes-agentcore-migration-design.md`.

**Milestone:** M6 Migrate, roll out, delete. Pass criteria: spec §6. Task numbering is local to this file.

---

## Tasks

### Task 1: `scripts/migrate_workspace_to_hermes.py` — the one-shot migration job

**Files:** Create: `apps/backend/scripts/migrate_workspace_to_hermes.py`,
`apps/backend/tests/unit/scripts/test_migrate_workspace_to_hermes.py`.
**Interfaces:** Consumes: `core.containers.get_workspace()` (EFS reads, existing),
`core.repositories.paperclip_repo.PaperclipRepo(table_name="paperclip-companies").get(owner_id)`
(existing, for `starter_agent_ids` role mapping), `core.services.hermes_profiles.write_agent_profile`
(M4, assumed signature — see header note), `core.services.hermes_provisioning.provision_hermes_runtime`
(M4, assumed signature), `core.containers.get_ecs_manager().stop_user_service` /
`.start_user_service` (existing), `core.repositories.container_repo.update_fields` (existing).
Produces: a CLI with `--owner-id`, `--dry-run`, `--rollback`; idempotent re-run.

- [ ] **Step 1: Write the failing test** (fake EFS tree via `tmp_path`, fake Paperclip/AgentCore via
  monkeypatch — no live AWS, no live Paperclip)
  ```python
  # apps/backend/tests/unit/scripts/test_migrate_workspace_to_hermes.py
  import json
  from unittest.mock import AsyncMock, patch

  import pytest

  from scripts import migrate_workspace_to_hermes as mod

  OWNER = "org_test1"
  CEO_ID = "agent_ceo"
  ENG_ID = "agent_eng"


  def _fake_workspace(tmp_path):
      root = tmp_path / OWNER
      for agent_id, soul in [(CEO_ID, "I am the CEO"), (ENG_ID, "I am Engineering")]:
          ws = root / f"workspace-{agent_id}"
          (ws / "memory").mkdir(parents=True)
          (ws / "skills" / "example").mkdir(parents=True)
          (ws / "SOUL.md").write_text(soul)
          (ws / "AGENTS.md").write_text("playbook")
          (ws / "MEMORY.md").write_text("remembered things")
          (ws / "memory" / "2026-09-15.md").write_text("daily note")
          (ws / "skills" / "example" / "SKILL.md").write_text("# example skill")
      return root


  @pytest.mark.asyncio
  async def test_dry_run_lists_agents_without_writing(tmp_path, capsys):
      _fake_workspace(tmp_path)
      with (
          patch.object(mod.settings, "EFS_MOUNT_PATH", str(tmp_path)),
          patch.object(mod, "_starter_roles", AsyncMock(return_value={CEO_ID: "ceo", ENG_ID: "engineering"})),
      ):
          await mod.migrate(OWNER, dry_run=True)
      out = capsys.readouterr().out
      assert CEO_ID in out and ENG_ID in out
      assert "DRY RUN" in out


  @pytest.mark.asyncio
  async def test_migrate_uploads_every_agent_and_flips_the_row(tmp_path):
      _fake_workspace(tmp_path)
      uploaded = []

      def fake_upload_file(path, bucket, key):
          uploaded.append(key)

      fake_s3 = type("S3", (), {"upload_file": staticmethod(fake_upload_file)})()
      with (
          patch.object(mod.settings, "EFS_MOUNT_PATH", str(tmp_path)),
          patch.object(mod.settings, "LITESTREAM_BUCKET", "test-replicas"),
          patch.object(mod, "_starter_roles", AsyncMock(return_value={CEO_ID: "ceo", ENG_ID: "engineering"})),
          patch.object(mod, "_s3_client", return_value=fake_s3),
          patch.object(mod, "write_agent_profile", lambda **kw: None),
          patch.object(mod, "provision_hermes_runtime", AsyncMock(return_value={"agentcore_runtime_arn": "arn:x"})),
          patch("core.containers.get_ecs_manager") as get_mgr,
          patch("core.repositories.container_repo.update_fields", new=AsyncMock(return_value={})),
      ):
          get_mgr.return_value.stop_user_service = AsyncMock()
          await mod.migrate(OWNER, dry_run=False)

      assert any(k.startswith(f"{OWNER}/hermes/{CEO_ID}/") for k in uploaded)
      assert any(k.endswith("SOUL.md") for k in uploaded)
      get_mgr.return_value.stop_user_service.assert_awaited_once_with(OWNER, reason="hermes_migration")


  @pytest.mark.asyncio
  async def test_rollback_restores_ecs():
      with (
          patch("core.containers.get_ecs_manager") as get_mgr,
          patch("core.repositories.container_repo.update_fields", new=AsyncMock(return_value={})) as upd,
      ):
          get_mgr.return_value.start_user_service = AsyncMock()
          await mod.rollback(OWNER)
      get_mgr.return_value.start_user_service.assert_awaited_once()
      assert upd.await_args.args[1]["runtime_host"] == "ecs"
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_migrate_workspace_to_hermes.py -q`
  Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.migrate_workspace_to_hermes'`
- [ ] **Step 3: Write minimal implementation**
  ```python
  # apps/backend/scripts/migrate_workspace_to_hermes.py
  """One-shot per-workspace migration: EFS/OpenClaw -> S3-profile/Hermes-on-AgentCore.

  Re-runnable ops tool (stays in scripts/ per CLAUDE.md's convention — this is not a
  one-time backfill whose data is produced at creation time; a workspace may need to be
  re-migrated after a bad profile write). Reads the EFS home the way agent_export.py
  does today (core.containers.get_workspace()); writes profiles through the M4 config
  writer (core.services.hermes_profiles.write_agent_profile); creates the runtime
  through the M4 fresh-provision path (core.services.hermes_provisioning); uploads to
  the EXISTING replicas bucket under <owner>/hermes/<agentId>/ (spec §4.8, ponytail cut
  2026-09-16 — no new bucket); parks (not deletes) the ECS service so `--rollback` can
  restore it without a re-provision.

  Deliberately NOT carried (spec §4.8): OpenClaw session transcripts/state DB (room
  history replays from DynamoDB as recentHistory on every turn) and exec-approval
  allowlists (Hermes runs unattended behind the config writer's deny backstop, §4.5).

  Exit codes mirror provision_runner.py's ABI where they apply: 0 success, 10
  deterministic failure (bad/missing workspace — do not retry), 11 transient (AWS
  error — retry).
  """

  from __future__ import annotations

  import argparse
  import asyncio
  import logging
  import sys
  from pathlib import Path

  from core.config import settings
  from core.containers import get_ecs_manager, get_workspace
  from core.repositories import container_repo
  from core.repositories.paperclip_repo import PaperclipRepo
  from core.services.hermes_profiles import write_agent_profile
  from core.services.hermes_provisioning import provision_hermes_runtime

  logger = logging.getLogger(__name__)

  # SOUL.md/AGENTS.md/MEMORY.md/USER.md, daily memory, skills — the exact carry list from
  # report 01 §1.2/§6, minus exec-approvals.json (dropped per the 2026-09-16 ponytail cut).
  _TOP_LEVEL_FILES = ("SOUL.md", "AGENTS.md", "MEMORY.md", "USER.md")
  _CARRY_DIRS = ("memory", "skills")


  def _s3_client():
      import boto3

      return boto3.client("s3")


  async def _starter_roles(owner_id: str) -> dict[str, str]:
      """agent_id -> role ("ceo", "engineering", ...), from the PaperclipCompany row's
      starter_agent_ids. An agent with no entry (a hired/custom agent beyond the six
      starters) gets role "standard" — the model-tier default."""
      repo = PaperclipRepo(table_name="paperclip-companies")
      company = await repo.get(owner_id)
      starters = (company.starter_agent_ids if company else None) or {}
      return {agent_id: role for role, agent_id in starters.items()}


  def _collect_agent_files(agent_dir: Path) -> dict[str, Path]:
      """workspace-relative path -> absolute path, for everything this migration carries."""
      out: dict[str, Path] = {}
      for name in _TOP_LEVEL_FILES:
          p = agent_dir / name
          if p.is_file():
              out[name] = p
      for sub in _CARRY_DIRS:
          base = agent_dir / sub
          if not base.is_dir():
              continue
          for f in base.rglob("*"):
              if f.is_file():
                  out[str(f.relative_to(agent_dir))] = f
      return out


  async def migrate(owner_id: str, *, dry_run: bool) -> int:
      workspace = get_workspace()
      agent_ids = workspace.list_workspace_agent_dirs(owner_id)
      if not agent_ids:
          logger.error("migrate: no workspace-* dirs for owner=%s", owner_id)
          return 10
      roles = await _starter_roles(owner_id)

      plan = []
      for agent_id in agent_ids:
          agent_dir = workspace.user_path(owner_id) / f"workspace-{agent_id}"
          files = _collect_agent_files(agent_dir)
          role = roles.get(agent_id, "standard")
          plan.append((agent_id, role, files))
          print(f"{'DRY RUN: ' if dry_run else ''}{agent_id} role={role} files={len(files)}")

      if dry_run:
          return 0

      bucket = settings.LITESTREAM_BUCKET
      if not bucket:
          logger.error("migrate: LITESTREAM_BUCKET is unset — nowhere to write profiles")
          return 10
      s3 = _s3_client()

      for agent_id, role, files in plan:
          # profile_dir is a LOCAL staging dir the M4 writer fills in-place, then this loop
          # uploads it — mirrors the bridge's sync.py restore/save shape (a directory, not a
          # per-key API), so the M4 writer never needs to know about S3 at all.
          import tempfile

          with tempfile.TemporaryDirectory() as tmp:
              profile_dir = Path(tmp)
              write_agent_profile(
                  owner_id=owner_id, agent_id=agent_id, role=role, profile_dir=profile_dir,
                  aip_arns={}, paperclip_key="", env_vars={},
              )
              for rel, src in files.items():
                  dest = profile_dir / rel
                  dest.parent.mkdir(parents=True, exist_ok=True)
                  dest.write_bytes(src.read_bytes())
              for f in profile_dir.rglob("*"):
                  if f.is_file():
                      key = f"{owner_id}/hermes/{agent_id}/{f.relative_to(profile_dir).as_posix()}"
                      s3.upload_file(str(f), bucket, key)

      runtime = await provision_hermes_runtime(owner_id, image_uri="")  # image_uri: M4 resolves the pinned tag
      await get_ecs_manager().stop_user_service(owner_id, reason="hermes_migration")
      await container_repo.update_fields(
          owner_id,
          {
              "runtime_host": "agentcore",
              "agentcore_runtime_arn": runtime.get("agentcore_runtime_arn"),
              "status": "running",
          },
      )
      logger.info("migrate: owner=%s migrated %d agents", owner_id, len(plan))
      return 0


  async def rollback(owner_id: str) -> int:
      await container_repo.update_fields(owner_id, {"runtime_host": "ecs", "status": "running"})
      await get_ecs_manager().start_user_service(owner_id, force=True)
      logger.info("rollback: owner=%s restored to ecs", owner_id)
      return 0


  def main() -> int:
      parser = argparse.ArgumentParser(description=__doc__)
      parser.add_argument("--owner-id", required=True)
      parser.add_argument("--dry-run", action="store_true")
      parser.add_argument("--rollback", action="store_true")
      args = parser.parse_args()
      logging.basicConfig(level=logging.INFO)
      if args.rollback:
          return asyncio.run(rollback(args.owner_id))
      return asyncio.run(migrate(args.owner_id, dry_run=args.dry_run))


  if __name__ == "__main__":
      sys.exit(main())
  ```
  Idempotency: re-running `migrate()` on an already-migrated owner re-uploads the same S3 keys
  (harmless overwrite) and re-provisions — `provision_hermes_runtime` is assumed idempotent per
  M4's contract (mirrors `agentcore_runtime.ensure_runtime`'s existing idempotent-on-existing-row
  shape); `stop_user_service` is already idempotent on an absent/inactive ECS service (its own
  docstring, `ecs_manager.py:1441-1447`). No `--force` flag needed for the MVP —
  ponytail: add a guard against re-migrating a row already on `runtime_host=agentcore` only if a
  real double-run incident happens; today it is a safe no-op-ish overwrite, not a hazard.
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_migrate_workspace_to_hermes.py -q`
  Expected: `3 passed`
- [ ] **Step 5: Commit**
  ```bash
  git add apps/backend/scripts/migrate_workspace_to_hermes.py apps/backend/tests/unit/scripts/test_migrate_workspace_to_hermes.py
  git commit -m "$(cat <<'EOF'
feat(hermes): one-shot workspace migration job (EFS -> Hermes profile -> AgentCore runtime)

Carries SOUL/AGENTS/MEMORY/USER + daily memory + skills per report 01 §1.2/§6; drops
exec-approval allowlists (Hermes runs unattended behind the deny backstop) and session
transcripts (room history replays from DynamoDB). --dry-run lists the plan; --rollback
restores runtime_host=ecs + desired-1 without deleting the uploaded S3 profiles.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 2: Rollout — dev orgs, then the founder org

**Files:** None (an operational runbook, not code) — add a short section to
`docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md` under a "Rollout log" heading (the
plan itself is disposable per CLAUDE.md once M6 ships; this log lives IN it for that reason, not as
a separate doc).
**Interfaces:** Consumes: `scripts/migrate_workspace_to_hermes.py` (Task 1).

- [ ] **Step 1: Write the failing check** — there is no code here, so the "test" is a checklist gate:
  before running Step 3, confirm M1-M5 are merged and green on the target org's environment:
  `cd apps/backend && uv run pytest tests/unit -q` (must pass) and
  `gh pr list --repo TheLightbulbCompany/lightbulb --search "milestone:M1..M5" --state merged` (or
  equivalent tracking) shows all five milestones merged.
- [ ] **Step 2: Confirm the gate fails today** (pre-M1..M5) — trivially true; this step exists so the
  checklist is not skipped once M1-M5 land for real.
- [ ] **Step 3: Run the migration against every non-founder dev org first**
  ```bash
  cd apps/backend
  for owner in $(uv run python -c "
  import asyncio
  from core.repositories import container_repo
  async def main():
      rows = await container_repo.scan_all()
      for r in rows:
          if r.get('runtime_host') != 'agentcore':
              print(r['owner_id'])
  asyncio.run(main())
  "); do
    uv run python scripts/migrate_workspace_to_hermes.py --owner-id "$owner" --dry-run
  done
  ```
  Review every dry-run's agent/role list, THEN re-run each without `--dry-run`. Use dev orgs, never
  the reference OpenClaw workspace `org_3JCpE2dm4G8Dip7z5e6Lod5fqVY` (plan's Global Constraints —
  that org stays on ECS as the comparison baseline until every other dev org is confirmed working).
- [ ] **Step 4: Migrate the founder org, with the owner's explicit go-ahead**
  ```bash
  uv run python scripts/migrate_workspace_to_hermes.py --owner-id "$FOUNDER_ORG_ID"
  ```
  Then use the founder workspace for real work for a day (the M6 pass criterion, spec §6: "founder
  workspace migrated with the migration job and used for a day"). Watch:
  `gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml` for anything unrelated
  breaking, and the run ledger (`lightbulb-{env}-messaging` `RUN#` rows) for `error` outcomes on the
  founder's agents.
- [ ] **Step 5: Record the outcome** — append a dated entry to the plan's rollout log (owner, date,
  agent count, any issue found + its fix commit) and commit:
  ```bash
  git add docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md
  git commit -m "$(cat <<'EOF'
docs(hermes): rollout log — dev orgs + founder org migrated

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 3: Prod dispatch precondition + per-workspace flip tooling

**Files:** Modify: `apps/backend/scripts/migrate_workspace_to_hermes.py` (add a `--all` flag that
migrates every non-founder, non-reference row in one run, for the per-workspace-flip phase after
prod carries the Hermes image). Test: extend
`apps/backend/tests/unit/scripts/test_migrate_workspace_to_hermes.py`.
**Interfaces:** Consumes: the `gh run list` precondition check already documented in CLAUDE.md's
"`queue: max`" section (reused verbatim, not reinvented).

- [ ] **Step 1: Write the failing test**
  ```python
  @pytest.mark.asyncio
  async def test_all_flag_migrates_every_non_agentcore_row(monkeypatch):
      rows = [
          {"owner_id": "a", "runtime_host": "ecs"},
          {"owner_id": "b", "runtime_host": "agentcore"},  # already migrated — skipped
          {"owner_id": "org_3JCpE2dm4G8Dip7z5e6Lod5fqVY", "runtime_host": "ecs"},  # reference org — skipped
      ]
      migrated = []

      async def fake_migrate(owner_id, *, dry_run):
          migrated.append(owner_id)
          return 0

      with (
          patch("core.repositories.container_repo.scan_all", new=AsyncMock(return_value=rows)),
          patch.object(mod, "migrate", fake_migrate),
      ):
          await mod.migrate_all(dry_run=False)
      assert migrated == ["a"]
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_migrate_workspace_to_hermes.py -k all_flag -q`
  Expected: FAIL with `AttributeError: module ... has no attribute 'migrate_all'`
- [ ] **Step 3: Write minimal implementation**
  ```python
  # apps/backend/scripts/migrate_workspace_to_hermes.py — additions
  REFERENCE_ORG_ID = "org_3JCpE2dm4G8Dip7z5e6Lod5fqVY"  # stays on ECS until every other workspace is confirmed


  async def migrate_all(*, dry_run: bool) -> int:
      rows = await container_repo.scan_all()
      worst = 0
      for row in rows:
          owner_id = row["owner_id"]
          if row.get("runtime_host") == "agentcore" or owner_id == REFERENCE_ORG_ID:
              continue
          code = await migrate(owner_id, dry_run=dry_run)
          worst = max(worst, code)
      return worst
  ```
  and in `main()`, add `parser.add_argument("--all", action="store_true")` /
  `if args.all: return asyncio.run(migrate_all(dry_run=args.dry_run))` before the single-owner branch.

  Prod-dispatch precondition (documentation, not new code — CLAUDE.md already specifies the exact
  command under "`queue: max`"):
  ```bash
  gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 \
    --json status --jq '[.[]|select(.status!="completed")]|length'
  # Dispatch the promotion only when this reads 0.
  gh workflow run deploy.yml --ref main
  ```
  This carries the backend (M1-M5's routes/services) and the CDK (Task 3's IAM, M4's runtime env)
  to prod in one dispatch — per CLAUDE.md, prod jobs `needs:` the full dev stage, so this cannot skip
  dev. Only after this dispatch completes does `migrate_all` target prod rows.
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_migrate_workspace_to_hermes.py -q`
  Expected: all tests pass (including the new one)
- [ ] **Step 5: Commit**
  ```bash
  git add apps/backend/scripts/migrate_workspace_to_hermes.py apps/backend/tests/unit/scripts/test_migrate_workspace_to_hermes.py
  git commit -m "$(cat <<'EOF'
feat(hermes): --all flag for fleet-wide per-workspace flips, skips the reference org

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 4: Soak checklist + monitoring

**Files:** None (operational — a checklist appended to the plan's rollout log, same file as Task 2).
**Interfaces:** Consumes: existing observability (CLAUDE.md's Observability audit 2026-09-12 —
messaging metrics/alarms/dashboard already exist; reuse, do not build new ones for this milestone).

- [ ] **Step 1: Write the failing check** — the checklist itself, unchecked, is the "failing" state:
  ```markdown
  ## Soak checklist (per migrated workspace, before calling it done)
  - [ ] Room turn: post a message in #general, agent replies threaded, run frames visible mid-turn
  - [ ] Node exec: `system.run` from an agent turn reaches the paired Mac and returns output
  - [ ] Browser: panel shows the AgentCore Browser live view, agent navigates a page
  - [ ] Paperclip run: trigger a routine/issue run from Teams, it completes
  - [ ] Slack: a bound Slack channel round-trips a message
  - [ ] Agent mail: send to `{agent}@{slug}...`, it lands in the room
  - [ ] Marketplace: deploy a listing into the migrated workspace, the new agent responds
  ```
- [ ] **Step 2: N/A** — this is a checklist, not code; "failing" is simply "unchecked."
- [ ] **Step 3: Run each item against the founder workspace** (Task 2's day-long soak) and against
  the first `--all`-migrated prod cohort, checking off each line as it passes. A failed line blocks
  the NEXT cohort's flip until fixed and re-verified — do not proceed to `migrate_all` on more
  workspaces with a known-red line.
- [ ] **Step 4: Monitor** (existing dashboards, not new metrics for this milestone):
  run ledger error rate (`RUN#` rows with `outcome=error` per the sweeper in "Room turns are
  asynchronous", CLAUDE.md Messaging section) and the bedrock-gate admit/deny rate (existing
  dashboard per the 2026-09-12 Observability audit memory) for an hour after each cohort's flip.
  Expected: no error-rate step-change correlated with the flip timestamp.
- [ ] **Step 5: Commit the completed checklist**
  ```bash
  git add docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md
  git commit -m "$(cat <<'EOF'
docs(hermes): soak checklist + monitoring notes for the rollout

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 5: Deletion PR series (spec §9), each independently green

**Files:** See spec §9's exact lists (already file:line cited in report 04's "Deletion list at
cutover" section — reuse those citations verbatim in each PR description, do not re-derive them).
**Interfaces:** None new — this is subtraction only. Ordered so every PR leaves `main` green.

- [ ] **Step 1: Write the failing check for PR ordering** — before deleting anything, confirm the
  precondition each PR depends on:
  ```bash
  # PR 1 (frontend browser cleanup) needs Task 7 (M5) already merged
  git log --oneline --all | grep "delete the VNC browser-view relay at cutover" | head -1
  # PR 2 (ecs_manager/config.py EFS+task-def machinery) needs every containers row on runtime_host=agentcore
  cd apps/backend && uv run python -c "
  import asyncio
  from core.repositories import container_repo
  async def main():
      rows = await container_repo.scan_all()
      stragglers = [r['owner_id'] for r in rows if r.get('runtime_host') != 'agentcore']
      print('stragglers:', stragglers)
  asyncio.run(main())
  "
  # Expected before PR 2: [] (or only the intentionally-retired reference org, deleted separately)
  ```
- [ ] **Step 2: Confirm each precondition fails until its prerequisite lands** — trivially true pre-M6;
  this step is the gate a reviewer re-runs before approving each deletion PR.
- [ ] **Step 3: Cut the PRs in this order** (each removes exactly what report 04's tables mark
  DELETE for that surface, and each PR's own test suite drops the corresponding test files rather
  than leaving them red):
  1. **Frontend browser cleanup** (already done in M5 Task 7 — no separate PR).
  2. **`apps/backend/core/containers/workspace.py`** (whole file) + its test file(s)
     (`grep -rl workspace.py apps/backend/tests/unit | grep -i workspace`) — nothing outside
     `agent_export.py`/`catalog_service.py` may still import it (Task 6 below must land first if it
     does; grep `from core.containers.workspace\|from core.containers import get_workspace` across
     `apps/backend` before this PR).
  3. **`apps/backend/core/containers/config.py`'s `openclaw.json` writer/reader** (`write_openclaw_config`,
     `read_openclaw_config_from_efs`, `build_openclaw_config_dict`, `build_provider_block`,
     `_build_exec_policy`) — keep the node-pairing helpers (`generate_node_device_identity` et al.,
     report 04 §2) by moving them to `core/services/node_broker.py` (M2's module, per the plan
     skeleton's File Structure) in the SAME PR, not after.
  4. **`config_patcher.py` + `efs_file_lock.py`** (whole files) + **`core/openclaw_version.py`**
     (whole file) + their test files.
  5. **`ecs_manager.py`'s EFS/task-def/Cloud-Map machinery** (report 04 §1's DELETE rows — roughly
     60% of the file) — leave the lease/fencing pattern and the `_hosted_on_agentcore` no-op
     branches (their own docstrings already say "deleted with the ECS path"); THIS PR is where those
     docstring-promised deletions actually happen.
  6. **`provision_dispatch.py`'s SFN-watchdog/subprocess machinery** + **`provision_runner.py`'s
     step-1 ECS leg** (keep step 0's Paperclip seed) + **`update_service.py`** (whole file).
  7. **Infra**: `container-stack.ts`'s EFS filesystem/Cloud Map/task-def/boot-chain/log-group blocks
     (report 04 §15), `service-stack.ts`'s `OPENCLAW_*` env block (§16), `openclaw-version.json`,
     `.github/workflows/build-openclaw-image.yml`, `apps/infra/openclaw/` in its entirety (skills
     move to the Hermes image FIRST — confirm `apps/infra/hermes/Dockerfile` already bakes
     `apps/infra/openclaw/skills/{lightbulb,paperclip}` before deleting the source tree). Run
     `cd apps/infra && npx cdk synth` and commit the regenerated `deploy.yml` in this same PR
     (CLAUDE.md's drift-guard rule) — this PR touches `apps/infra/lib/` more than any other in the
     series.
  8. **CLAUDE.md rewrite** — see Task 7 below (kept separate so a reviewer can diff prose without
     also reviewing code deletions).
  For each PR: `cd apps/backend && uv run pytest tests/unit -q` and
  `cd apps/infra && npx jest` must both pass before merge; `git log` each PR against
  `docs/superpowers/plans/2026-09-07-audit-report.md` §6's "Deliberately NOT recommended" list to
  confirm nothing on that list is touched (`trial_cap.py`, `connection_pool.py` veto comments,
  `config.py`'s seven `provider_choice` switches, `_size_for_owner`'s agent-count branch,
  `discover_ip`'s never-cache-an-IP rule, the bedrock-gate/meter rate-card copies,
  `_revoke_owner_iam`, the bedrock-gate-tokens liveness predicate, `metronome_fast_gate.set_balance`,
  `auth-stack.ts`'s `make()`, `table-names.ts`'s prefix pattern, `efs_file_lock`/`.isol8-guard` — most
  of these are already gone by PR 5/7 above BECAUSE they are OpenClaw/EFS-specific, which is
  consistent with the audit calling them out as load-bearing scar tissue FOR the system being
  deleted, not scar tissue in the system that remains).
- [ ] **Step 4: Run the full test suite after each PR merges**
  Run: `cd apps/backend && uv run pytest tests/unit -q && cd ../infra && npx jest`
  Expected: PASS after every PR in the series, not just the last one.
- [ ] **Step 5: Commit** — one commit per PR (already itemized above); no single "delete everything"
  commit.

---

### Task 6: CLAUDE.md rewrite

**Files:** Modify: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.claude/worktrees/hermes-evaluation/CLAUDE.md`.
**Interfaces:** None — documentation only.

- [ ] **Step 1: Write the failing check** — grep for the sections report 04 §20 names as false
  post-cutover:
  ```bash
  grep -n "ECS Fargate container running the OpenClaw Docker image\|extended image.*OpenClaw fork\|Container Re-provisioning (Dev)\|Config Patching Notes\|EFS deep merge + OpenClaw file watcher" CLAUDE.md
  ```
  Expected (pre-Task-6): multiple matches — these sections still describe the deleted system.
- [ ] **Step 2: Confirm the grep is non-empty today** — it is (CLAUDE.md as read at the start of this
  plan describes ECS/EFS/OpenClaw throughout).
- [ ] **Step 3: Rewrite each section report 04 §20 named**, replacing ECS/EFS/OpenClaw-container
  language with the Hermes-on-AgentCore shape (one bridge process per workspace, S3 profile sync, no
  EFS, `CreateAgentRuntime` instead of ECS service creation). Sections to rewrite (verbatim list from
  report 04 §20, cited so nothing is missed):
  - Project Overview opening paragraph
  - "The OpenClaw container image we run is an extended image..." paragraph
  - "This repo does not vendor OpenClaw source..." paragraph (becomes historical/rollback note only)
  - Container Orchestration (`core/containers/`) table
  - Container Re-provisioning (Dev) recipe
  - Config Patching Notes (entire section)
  - AWS CDK Stacks table rows for `container-stack.ts`/`service-stack.ts`
  - Container Update System (under Billing System)
  - Free tier trial container-sizing/idle-park paragraph
  - Marketplace seller/buyer flow (EFS-tarball language)
  - Legacy containers (pre-Paperclip provisioning) section
  - WebSocket Architecture item 5 (raw WS door) — this one needs the ACTUAL resolved design from M2,
    not a guess; do not rewrite this line until M2's node-relay design has shipped and is verified,
    or the doc will describe an aspiration as fact (CLAUDE.md's own standard — "verify by curling the
    live site," not by reading a plan).
  - Desktop App section's `LIGHTBULB_WS_URL` paragraph (same dependency on M2 as above)
  - Rebrand section's `ISOL8_*` container env / `.isol8-guard` residue tables — these entries become
    genuinely deleted, not "still residue," and should move OUT of the residue table entirely.
- [ ] **Step 4: Run the check again**
  Run: `grep -n "ECS Fargate container running the OpenClaw Docker image" CLAUDE.md`
  Expected: no match (the opening paragraph now describes Hermes/AgentCore).
- [ ] **Step 5: Commit**
  ```bash
  git add CLAUDE.md
  git commit -m "$(cat <<'EOF'
docs: rewrite CLAUDE.md for Hermes-on-AgentCore (post-cutover)

Every section describing ECS/EFS/OpenClaw container orchestration as the runtime is
replaced. WebSocket Architecture item 5 and the Desktop App LIGHTBULB_WS_URL paragraph
are deferred until M2's node-relay design is actually shipped and verified, not merely
planned.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 7: Marketplace export/deploy on S3 profile prefixes

**Files:** Modify: `apps/backend/core/services/agent_export.py` (`_resolve_agent_dir` and
`_build_tarball`'s callers), `apps/backend/core/services/catalog_service.py`
(`deploy_from_artifact`). Test: `apps/backend/tests/unit/services/test_agent_export.py` (extend),
new `apps/backend/tests/unit/services/test_catalog_service_s3_deploy.py`.
**Interfaces:** Changes: `_resolve_agent_dir` returns an S3-key prefix string instead of a `Path`
when `runtime_transport.host_for(container) == "agentcore"`; `deploy_from_artifact` writes the
extracted tarball to S3 instead of calling `workspace.extract_tarball_to_workspace`.

- [ ] **Step 1: Write the failing test**
  ```python
  # apps/backend/tests/unit/services/test_agent_export.py — new test appended
  from unittest.mock import patch

  from core.services import agent_export


  def test_resolve_agent_dir_returns_s3_prefix_for_agentcore_owner():
      container = {"owner_id": "owner_1", "runtime_host": "agentcore"}
      with patch("core.services.agent_export.runtime_transport.host_for", return_value="agentcore"):
          result = agent_export._resolve_agent_location(seller_id="owner_1", agent_id="agent_9", container=container)
      assert result == ("s3", "owner_1/hermes/agent_9")


  def test_resolve_agent_dir_returns_efs_path_for_ecs_owner():
      container = {"owner_id": "owner_1", "runtime_host": "ecs"}
      with patch("core.services.agent_export.runtime_transport.host_for", return_value="ecs"):
          kind, value = agent_export._resolve_agent_location(seller_id="owner_1", agent_id="agent_9", container=container)
      assert kind == "efs"
  ```
  ```python
  # apps/backend/tests/unit/services/test_catalog_service_s3_deploy.py
  from unittest.mock import AsyncMock, patch

  import pytest


  @pytest.mark.asyncio
  async def test_deploy_from_artifact_uploads_to_s3_for_agentcore_owner(tmp_path):
      from core.services import catalog_service

      uploaded = []

      def fake_upload(path, bucket, key):
          uploaded.append(key)

      service = catalog_service.CatalogService.__new__(catalog_service.CatalogService)
      with (
          patch("core.services.catalog_service.runtime_transport.host_for", return_value="agentcore"),
          patch("core.services.catalog_service._s3_client", return_value=type("S3", (), {"upload_file": staticmethod(fake_upload)})()),
          patch.object(catalog_service.settings, "LITESTREAM_BUCKET", "test-replicas"),
      ):
          await catalog_service.deploy_tarball_to_profile(
              owner_id="owner_1", agent_id="agent_new", tar_bytes=b"", container={"runtime_host": "agentcore"},
          )
      assert any(k.startswith("owner_1/hermes/agent_new/") for k in uploaded)
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -k agentcore -q tests/unit/services/test_catalog_service_s3_deploy.py`
  Expected: FAIL — `_resolve_agent_location` and `deploy_tarball_to_profile` do not exist yet.
- [ ] **Step 3: Write minimal implementation**
  In `agent_export.py`, add alongside `_resolve_agent_dir` (line 429) rather than replacing it (EFS
  callers during the migration window still need the Path-returning function):
  ```python
  from core.services import runtime_transport


  def _resolve_agent_location(*, seller_id: str, agent_id: str, container: dict, config: dict | None = None):
      """("efs", Path) or ("s3", key_prefix) depending on the seller's runtime_host.
      During the migration window BOTH kinds of seller exist — this is the one function
      export/deploy call sites must branch on instead of assuming EFS."""
      if runtime_transport.host_for(container) == "agentcore":
          return ("s3", f"{seller_id}/hermes/{agent_id}")
      return ("efs", _resolve_agent_dir(seller_id=seller_id, agent_id=agent_id, config=config))
  ```
  In `catalog_service.py`, add a sibling to `deploy_from_artifact` (do not fold into it yet — the
  ECS path's EFS extract + `pair_with_openclaw` RPC registration is unrelated logic that a Hermes
  agent has no equivalent for until the bridge's `agents.create`-style RPC exists per report 04 §18;
  this task only unblocks the FILE placement, not agent registration):
  ```python
  def _s3_client():
      import boto3

      return boto3.client("s3")


  async def deploy_tarball_to_profile(*, owner_id: str, agent_id: str, tar_bytes: bytes, container: dict) -> None:
      """Extract tar_bytes into the buyer's Hermes profile prefix instead of their EFS
      workspace-{agent_id}/ dir. Agent registration (the Hermes equivalent of
      pair_with_openclaw) is NOT done here — that RPC method does not exist on the
      bridge yet (report 04 §18: agents.update/create hit UNAVAILABLE today)."""
      import io
      import tarfile
      import tempfile

      bucket = settings.LITESTREAM_BUCKET
      s3 = _s3_client()
      with tempfile.TemporaryDirectory() as tmp:
          with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
              tf.extractall(tmp, filter="data")
          tmp_path = Path(tmp)
          for f in tmp_path.rglob("*"):
              if f.is_file():
                  key = f"{owner_id}/hermes/{agent_id}/{f.relative_to(tmp_path).as_posix()}"
                  s3.upload_file(str(f), bucket, key)
  ```
  (`from pathlib import Path` and `from core.config import settings` already imported or added at
  the top of `catalog_service.py`.)
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -k agentcore -q tests/unit/services/test_catalog_service_s3_deploy.py`
  Expected: `3 passed`
- [ ] **Step 5: Commit**
  ```bash
  git add apps/backend/core/services/agent_export.py apps/backend/core/services/catalog_service.py \
    apps/backend/tests/unit/services/test_agent_export.py apps/backend/tests/unit/services/test_catalog_service_s3_deploy.py
  git commit -m "$(cat <<'EOF'
feat(hermes): marketplace export/deploy branch on runtime_host, write to S3 profile prefixes

Additive, not a replacement — EFS sellers/buyers still work during the migration
window. Agent registration on the buyer side (the Hermes equivalent of
pair_with_openclaw) is explicitly out of scope here: the bridge has no agents.create
RPC yet (report 04 §18).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---


### Task 8: Retire `browser_view.py`, `browser_state.py`, `useBrowserToolAutoOpen.ts`

**Files:** Delete: `apps/backend/routers/browser_view.py`,
`apps/backend/tests/unit/routers/test_browser_view.py`,
`apps/backend/core/services/browser_state.py`, `apps/backend/tests/unit/services/test_browser_state.py`,
`apps/frontend/src/components/workspace/panels/browserViewUrl.ts`,
`apps/frontend/src/components/workspace/useBrowserToolAutoOpen.ts`. Modify: `apps/backend/main.py`
(remove the `browser_view` import and `app.include_router(browser_view.router, ...)` at line
794/37), any component importing `useBrowserToolAutoOpen` (grep first),
`apps/backend/routers/container.py` (remove `_browser_state_restore` at line 457 and its call site
if `container_recover.py`/`GET /container/status` calls it — grep `restore_browser_state` per
report 04 §14 `routers/container.py:286,441,460`).
**Interfaces:** Removes the ECS/EFS-Chrome-sidecar surface entirely; nothing new is produced.

**⚠️ Ordering constraint:** this task is safe ONLY once Tasks 2-5 are merged (the panel and backend
route must already work on the new path before the old one is deleted) AND only for workspaces
already on Hermes — per spec §9's "cutover" framing, this whole file is a **cutover-time** deletion,
not a today deletion, because ECS/OpenClaw-hosted workspaces still use `browser_view.py`'s VNC relay
until they migrate (M6). Sequence this task to run in M6 alongside the other cutover deletions
(Task M6-6 below), not immediately after Task 5. Listed here under M5 per the milestone brief's
item (6), but scheduled last.

- [ ] **Step 1: Write the failing test (a negative test — the route and file are gone)**
  ```python
  # apps/backend/tests/unit/test_no_browser_view_route.py
  def test_browser_view_router_is_not_mounted():
      import main

      paths = {route.path for route in main.app.routes}
      assert not any(p.startswith("/api/v1/browser-view") for p in paths)


  def test_browser_view_module_is_gone():
      import importlib

      import pytest

      with pytest.raises(ModuleNotFoundError):
          importlib.import_module("routers.browser_view")
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/test_no_browser_view_route.py -q`
  Expected: FAIL — the route IS mounted and the module DOES import (this repo, pre-cutover).
- [ ] **Step 3: Write minimal implementation (at cutover time, all workspaces on Hermes)**
  ```bash
  git rm apps/backend/routers/browser_view.py apps/backend/tests/unit/routers/test_browser_view.py \
    apps/backend/core/services/browser_state.py apps/backend/tests/unit/services/test_browser_state.py \
    apps/frontend/src/components/workspace/panels/browserViewUrl.ts \
    apps/frontend/src/components/workspace/useBrowserToolAutoOpen.ts
  ```
  `apps/backend/main.py`: remove `browser_view` from the router import block (line 37) and delete the
  `app.include_router(browser_view.router, prefix="/api/v1/browser-view", tags=["browser-view"])`
  line (794).
  `apps/backend/routers/container.py`: remove `_browser_state_restore` (line 457) and its call site
  in `container_status`/wake handling (grep `_browser_state_restore\b` in the file first — it is
  called from the same function that already branches on `_hosted_on_agentcore`, so the removal is a
  straight deletion, not a branch).
  Frontend: grep `useBrowserToolAutoOpen` for its one mount site (likely
  `components/workspace/panelRegistry.ts` or the chat panel shell) and remove the hook call.
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/test_no_browser_view_route.py -q`
  Expected: PASS. Also run the full backend suite to catch any straggler import:
  `cd apps/backend && uv run pytest tests/unit -q`
- [ ] **Step 5: Commit**
  ```bash
  git add -A
  git commit -m "$(cat <<'EOF'
chore(hermes): delete the VNC browser-view relay at cutover

browser_view.py, browser_state.py and useBrowserToolAutoOpen.ts have no meaning once no
workspace runs an OpenClaw Xvfb+Chrome sidecar. Cutover-time deletion (spec §9) — every
workspace is on Hermes/AgentCore Browser by the time this lands.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

