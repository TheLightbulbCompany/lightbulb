# Plan: AgentCore workspace memory (Track G)

**Status:** approved by the coordinator (owner delegation), 2026-09-22.

**Spec:** `docs/superpowers/specs/2026-09-22-agentcore-memory-code-interpreter.md`. Delete both files in PR 3.

## Goal

Every agent in a workspace shares one long-term memory. Facts said in any room are extracted by AgentCore and recalled by any agent on a later turn.

## Product note for the owner

**Extraction takes 1.5 to 2 minutes** (measured in dev: 96 s and 133 s). A fact told to one agent is recallable by the others only after that window, and not in the same or the very next turn. The built-in `MEMORY.md` and `USER.md` files still cover same-agent, same-turn recall.

## Global design

- **One `AWS::BedrockAgentCore::Memory` per environment.**
  - Semantic strategy only.
  - Namespace `/owners/{actorId}/`.
  - `actorId` = owner id.
  - 30-day event expiry.
- **Isolation is per-owner IAM**, proven in the dev spike:
  - Events are scoped by `actorId`.
  - Records are scoped by an exact `namespace` match.
  - Record get and delete are explicitly denied, because those actions have no condition keys.
- **The runtime learns the memory id from the `AGENTCORE_MEMORY_ID` env var** in its runtime spec.
  - An old image ignores it.
  - A new image enables the provider only when the variable is set.
  - Rolling out needs both pieces: a new image plus a runtime-spec update (the roll). Nothing else has to be kept in lockstep.

## PR 1: infra (CDK)

Files:
- `container-stack.ts`
- `service-stack.ts` (the backend env var and the wipe grants only)
- the regenerated `.github/workflows/deploy.yml`

Message `lightbulb-nosync-a5 [c1fde6]` before opening this PR.

1. **Memory resource.** `container-stack.ts` gets a `CfnMemory`:
   - name `lightbulb_<env>_workspace_memory`
   - **AWS-owned encryption, deliberately.** A CMK needs a service key-policy grant plus kms grants on every owner role, and `EncryptionKeyArn` is create-only. Adding one later means a new resource name plus a data migration.
   - semantic strategy with namespace `/owners/{actorId}/`
   - `eventExpiryDuration` 30
   - The stack exposes the memory id as a prop (one export). The service stack derives the ARN from it.
2. **Boundary.** `OwnerBedrockBoundary` gets `BoundaryAgentCoreMemoryUse` on the memory ARN:
   - `CreateEvent`, `ListEvents`, `GetEvent`, `ListSessions`
   - `RetrieveMemoryRecords`, `ListMemoryRecords`
   - This is a ceiling only. The grant itself is the per-owner inline policy in PR 2.
3. **Backend task role** (`service-stack.ts`), on the memory ARN:
   - wipe grants: `ListMemoryRecords`, `BatchDeleteMemoryRecords`, `ListSessions`, `ListEvents`, `DeleteEvent`
   - env var `AGENTCORE_MEMORY_ID`
4. **Tests.** Jest assertions for the memory resource, the boundary statement and the task-role statement.
5. **Checks.** Run `npx cdk synth` and commit `deploy.yml`.
6. **Dev check after merge.** `aws bedrock-agentcore-control get-memory` returns ACTIVE.

## PR 2: backend

Files:
- `core/config.py`
- `core/billing/bedrock_aip.py`
- `core/services/hermes_provisioning.py` (the env line and the policy call)
- `core/services/workspace_memory.py` (new, the wipe)
- `hermes_provisioning.teardown_hermes_runtime`

Coordinate with c1fde6's T1 on `bedrock_aip.py`.

1. **Config.** `settings.AGENTCORE_MEMORY_ID` defaults to `""`. Empty turns everything off.
2. **Per-owner policy.** `bedrock_aip.ensure_owner_memory_policy(owner_id)` writes an inline `owner-memory` policy on the owner role. It uses the spike-proven document:
   - Allow the event actions when `bedrock-agentcore:actorId` equals the owner.
   - Allow the record reads when `bedrock-agentcore:namespace` equals `/owners/<owner>/`.
   - Deny the record get and delete actions, the Batch actions, `DeleteEvent` and `ListActors`.
   - It is a no-op when the memory id is empty.
3. **Runtime env.** `build_hermes_runtime_env(..., memory_id="")` adds `AGENTCORE_MEMORY_ID` when the id is non-empty.
   - `build_runtime_request_for_owner` passes `settings.AGENTCORE_MEMORY_ID`.
   - Both callers (the provision at line 812 and the roll at line 1209) call `ensure_owner_memory_policy` next to `ensure_browser_profile`.
4. **Wipe.** `workspace_memory.delete_owner_memory(owner_id)`:
   - `ListSessions(actorId)` → `ListEvents` → `DeleteEvent` FIRST (events are what extraction reads).
   - Then list records in the owner's namespace and `BatchDelete` them in chunks of 100.
   - Then one in-process re-sweep of records 180 s later, for extraction that was in flight (96–133 s).
   - It is best-effort and logs on failure.
   - It is called from `teardown_hermes_runtime` **only when `delete_profiles=True`** (account wipe and `/debug/user-data`). A reprovision keeps memory.
5. **Tests (TDD).**
   - The policy document, including an exact-match test against the spike JSON.
   - The env var is present or absent depending on the setting.
   - Wipe pagination and chunking, using a stubbed client.
   - Teardown calls the wipe only with `delete_profiles=True`.
   - Run with `uv run pytest tests/unit/<area> -q`.

## PR 3: image (bridge train, after provisioning Task 11)

Files:
- `apps/infra/hermes/plugins/memory/agentcore/` (provider + `plugin.yaml`)
- `apps/infra/hermes/bridge/sync.py`
- `bridge/server.py` (health)
- skills reference
- tests

1. **Verify provider discovery first.** Check how the pinned base discovers memory providers. The image copies `plugins/` into `/opt/hermes/plugins/`; confirm that path is the Hermes source root's `plugins/memory/` or a `$HERMES_HOME/plugins/` route, against `v2026.9.14` inside the image. Pick the path that loads without forking Hermes.
2. **The provider.** About 150 lines, adapted from `sfalsin/hermes-agentcore-memory` (MIT, credited in the file header):
   - `prefetch(query)`: `RetrieveMemoryRecords(namespace="/owners/<owner>/", maxResults=5)` with a 1.5 s boto timeout and **no retries**. It returns `None` on any error. It never passes `namespacePath`.
   - `sync_turn(user, assistant)`: one `CreateEvent(actorId=owner, sessionId=<hermes session id, sanitized>, payload=[USER, ASSISTANT])`, on a daemon thread so the turn never waits.
   - It exposes no tools in this slice. Recall is passive.
3. **Enable it.** `sync.normalize_profile_backends` sets `memory.provider: agentcore` when `AGENTCORE_MEMORY_ID` is set and the configured provider is `""`. It never overrides a provider someone chose.
   - When the variable is unset, a provider left at `agentcore` is reset to `""`.
   - `health` reports `memoryProtocol: 1` when the variable is set.
4. **Skill.** Add a `lightbulb` skill reference line: memory is shared across the workspace's agents, lags about 2 minutes, and anything said in any room may resurface. Update `MAINTENANCE.md`.
5. **Tests.**
   - Provider unit tests with a stubbed boto client: timeout returns `None`, the namespace is exact, `actorId` is the owner, and `sync_turn` doesn't block.
   - Enable and disable tests on `normalize_profile_backends`.
6. **Rollout.** Build the image → dev pin PR → roll dev runtimes. The roll also applies the new env and policy. Prod pin and roll go through the coordinator and need the owner's go-ahead.

## Dev acceptance (after PR 3 is on dev)

Account setup:
- A fresh `+clerk_test` user and org labelled "memory", on the free tier, after #2894 is on dev.
- Sign in with a Clerk sign-in-token ticket. Keep turns minimal (CROSS-TRACK §1).

Steps:
1. **Store.** Turn 1: tell the CEO in #general "Our launch codename is PERIWINKLE-42".
2. **Recall.** Wait at least 2 minutes. Then, in a later turn, DM another agent (for example Engineering): "What's our launch codename?" It must answer PERIWINKLE-42. As evidence, capture the answer and the provider's recall log line in CloudWatch.
3. **Isolation.** Assume a second dev owner's runtime role and call `RetrieveMemoryRecords` on the first owner's namespace. Expect AccessDenied, and confirm it in CloudTrail.
4. **Wipe.** Run `DELETE /debug/user-data` on the memory org. Within about 30 s, `ListMemoryRecords` on its namespace returns 0.
5. **Report.** Paste the evidence into PR 3. Tell the coordinator the change is merged and dev-verified.

## Out of scope

- Summary, preference and episodic strategies.
- Private per-agent memory.
- A memory UI.
- Per-owner metering. Memory is platform-absorbed at about $0.001 per turn.
- Code execution. That decision waits on the Code Interpreter spike.
