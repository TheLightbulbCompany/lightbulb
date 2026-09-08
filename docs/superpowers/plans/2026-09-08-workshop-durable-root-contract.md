# Workshop durable-root contract

Date: 2026-09-08. Proposed fork adaptation, not implemented or runtime-qualified. Frozen source: `v2026.9.3`, `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`, in the sibling `founder-openclaw-9x-fork` checkout.

## Smallest source-supported change

Reuse the two native Workshop root functions, backed by one Workshop-owned per-agent root beneath the durable state-root spine. Do not move agentDir, SQLite, or the whole agents mount onto EFS. Do not add per-caller path overrides, another storage service, or another proposal database.

Proposed layout beneath `resolveStateDir(env)`:

```text
skill-workshop/
  proposals/<proposal-id>/generations/...  existing durable proposal bundles
  agents/<canonical-agent-id>/
    skills/                              applied native Workshop collection
    collection-backups/                  native review/restore backups
state/openclaw.sqlite                    unchanged task-local DB, Litestream
agents/<id>/agent/openclaw-agent.sqlite   unchanged task-local DB, Litestream
```

The final path must be derived from the same native canonical agent identifier policy, not an arbitrary path or workspace name. Different configured agents must never share their Workshop root merely because they share a workspace. A configured external agentDir must not redirect the fork's Workshop files out of its owner's durable state root. No new user-facing storage option is needed for this Lightbulb-specific fork behavior.

## Inspected native seams

- `src/skills/workshop/skills-root.ts:5` currently joins `resolveAgentDir(config, agentId, env)` with `workshop-skills`. This also supplies `resolveWorkshopWatchRoots` and the downstream skill loader/discovery, execution root, proposal target, security-audit and migration consumers.
- `src/skills/workshop/collection-paths.ts:7` independently joins agentDir with `skill-workshop/collection-backups`. Both functions must derive from the same proposed `resolveWorkshopAgentRoot`; moving only active skills leaves rollback files ephemeral.
- `src/agents/agent-scope-config.ts:597` uses native `normalizeAgentId`, supports a configured agentDir, and otherwise returns `<state-root>/agents/<id>/agent`. Preserve its SQLite/auth behavior; do not change this resolver to solve a Workshop-only problem.
- `src/config/types.skills.ts:57` exposes autonomous mode, approval policy and size/queue limits, but no native Workshop root option. `skills.load.extraDirs` is discovery, not a way to relocate authoritative Workshop writes/backups.
- `src/skills/workshop/proposal-generation.ts:21` already resolves proposal files through the state root. Its immutable generation staging uses the existing safe-root operations, file sync and directory sync. Keep this path and safety behavior.
- `src/skills/workshop/store-sqlite-schema.ts:111` uses the existing central state DB, including `<stateDir>/state/openclaw.sqlite` when explicitly supplied. Keep that database local; owner_agent_id in the existing tables retains proposal ownership. Durable proposal bundles do not imply moving these tables to a new store.
- `src/skills/workshop/store.ts:149` constructs targets through the active collection resolver and checks containment; `collection-restore.ts:32` uses both canonical roots and rejects restore over post-review edits. Preserve these controls and existing symlink/hardlink/atomic-write protections.
- `src/commands/doctor-skill-workshop-collection-backups.ts:35` migrates the old workspace-keyed backup roots and requires exactly one configured agent to own a workspace. It already obtains the destination from the native backup resolver. Keep ambiguous roots as reviewable history; do not guess ownership.
- `src/commands/doctor-skill-workshop-sqlite.ts:216` uses the canonical active root during interrupted-apply recovery and relocation. Replacing destinations must not bypass the final source/rollback checks.

The caller scan found production uses of active/backup roots consistently routed through these resolvers. One concrete test still hardcodes `<custom-agentDir>/workshop-skills`: `src/infra/state-migrations.skill-workshop.test.ts:108`; its expected destination must change deliberately and still demonstrate readable migrated pending proposals. Existing tests elsewhere mostly call the canonical resolvers and should follow the new root automatically. This is source evidence, not proof all migration behavior will pass unchanged.

## Migration and validation boundaries

The intended production migration starts from the shipped 8.1 fork, not an already-used stock9.3 runtime. Test a representative 8.1 state copy and the exact native Doctor conversion into the fork's new canonical destinations. Do not assert that changing a resolver alone migrates arbitrary pre-existing stock9.3 agentDir collections; that source-state combination is unqualified and must be rejected or separately migrated if encountered.

Required fixture cases before candidate publication:

1. Two configured agents sharing a workspace resolve to separate collection and backup roots; an agentDir override cannot move them into the ephemeral agents/state mounts. Canonical ID handling matches native policy, and traversal-like inputs cannot escape or alias a different legitimate owner.
2. Old pending proposals remain inspectable and actionable; old applied proposals relocate with their supporting files and retained lineage. Interrupted create/update recovery and ownership conflicts remain conservative and reviewable.
3. Approve/update/reject and native collection review/restore use the same durable roots. Restore must still refuse to overwrite later user edits. Symlink/hardlink/path-containment negative cases remain green.
4. Native loader, watcher and security audit discover only the correct agent's applied collection after the move. No duplicate workspace/agent discovery exposes another agent's skills.
5. Simulate the actual mount boundary: retain the EFS spine, discard only synthetic task-local agents/state directories, restore synthetic databases through the real Litestream format, and repeat replacement twice. Collection, backup, proposal content, owner attribution and final state must survive. Do not label a plain local-directory rename test as ECS replacement or Litestream qualification.
6. Verify no SQLite file is created beneath the Workshop root and the actual final task definition still uses local agents/state mounts. Deletion/rename behavior must not reassign another agent's collection or silently erase retained proposal history.

Reuse the existing focused suites for Workshop store/service/restore, Doctor relocation/backup conflicts/atomicity, workspace loading/containment, and Workshop security audit. New tests should establish the changed root and persistence boundary, not duplicate all native lifecycle tests. Exact executable commands and additional files will be frozen in the implementation task after the pinned fork toolchain is usable; no package-age override has been made.

## Related backup warning

Lightbulb's `core/services/openclaw_backup_history.py` is a sanitized marketplace/import path: it rewrites owner/session identifiers and deletes imported auth state. It is not a founder rollback backup/restore mechanism and must not be reused as one. The release backup must retain the coherent founder config/files and complete state/agent databases separately from candidate replica history, under the goal's secret-handling and state-aware restore approval rules.
