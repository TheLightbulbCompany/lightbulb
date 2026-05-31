# Credential Layer Redesign — no DB vault; secrets on container + Secrets Manager

> **For agentic workers:** implement task-by-task; each Phase deploys independently. Steps use `- [ ]` checkboxes. **Do not start until the open decisions (D1, D2) are resolved.**

**Goal:** Stop storing third-party credentials in **any** backend DynamoDB table. Today there are TWO secret-bearing DDB tables — the `connections` integration vault (M4/M5) and the `api-keys` BYOK table (`key_service`). Put secrets where they belong — container EFS (OAuth/CLI keyrings, already there) and Secrets Manager (paste-key + LLM tokens) — keep only an ephemeral OAuth-dance store and a thin, **secret-free** status index, retire the `api-keys` vault, and remove the cross-stack table-ARN export that made the rename impossible and caused the 2026-05-31 incident. **End state: no secrets in any DynamoDB table.**

**Code-grounded findings about BYOK (`key_service` / `api_key_repo`), verified 2026-05-31 (NOT from docstrings):**
- `set_key` always Fernet-encrypts the key into the `api-keys` DDB table (`key_service.py:150-155`). LLM keys (openai/anthropic) *additionally* mirror plaintext to SM `isol8/<env>/user-keys/<user>/<provider>` + store the ARN on the row (`:158-166`).
- **The 4 "tool keys" (`elevenlabs`, `openai_tts`, `perplexity`, `firecrawl`) are DEAD.** `SUPPORTED_TOOLS` is referenced only for validation + the `list_keys` display + the `supported_tools` response; nothing reads them, their `config_path` is never written, and `routers/settings_keys.py:set_key` ends with `# TODO: update openclaw.json on EFS + send config.apply RPC`. Saving one writes an encrypted DDB row that is never delivered or used.
- **The 2 LLM keys are the only working BYOK.** `_build_byo_secrets_for_user` (`ecs_manager.py:1140-1170`) reads the row's `secret_arn` and injects it as the `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` ECS env var; `billing_service.py:587` reads it for the provision gate. The encrypted_key copy in DDB is **redundant** (delivery uses the SM ARN, not the DDB value).
- `api-keys` is also read by `paperclip_api_key.py:95` (a per-agent Paperclip key) — must be accounted for when retiring the table.

**Architecture:**
- **Secrets never live durably in a backend DB.** The backend holds a token only in-flight during the OAuth exchange / paste validation.
- Paste-key tokens → **Secrets Manager** per owner (`isol8/<env>/integrations/<owner>/<provider>`), mirroring `key_service`'s `user-keys/*`. OAuth/CLI (gog) → **container EFS keyring** (already true via M6 non-retention).
- OAuth dance state (PKCE verifier + `state`) → keep the small **`oauth-state`** table (TTL'd, no secrets).
- Integration status ("which providers connected") → **decision D1** (thin index vs derive-from-container).
- IAM: backend gets DynamoDB/Secrets-Manager access via **name-based ARNs** (the WS-table / user-keys pattern), never cross-stack `grantReadWriteData(table)` → no CFN export → tables can be renamed/replaced freely.

**Tech stack:** AWS CDK (TS), DynamoDB, Secrets Manager, FastAPI/boto3.

---

## Background: what exists today + what broke

- `connection_repo` writes a Fernet-encrypted credential to the `isol8-<env>-connections` DDB table (owner_id PK, provider SK). M6 already **scrubs** the value for `refresh_owner=actor` providers (gog/google) — their durable credential is the container EFS keyring. Paste-key providers (linear/stripe) **retain** the encrypted value in DDB.
- Consumers of the vault: `routers/connections.py` (connect/disconnect/list/oauth_callback/`_scrub_if_actor_refresh`/`_remove_materialized_artifacts`), and M8 marketplace (`catalog_service.assert_credentials_satisfied` reads `list_for_owner`; `_fill_buyer_credential_slots` reads `get` + `decrypt_credential`).
- **The incident:** the connections table's ARN is exported cross-stack to `service-stack` (`service-stack.ts:315 props.database.connectionsTable.grantReadWriteData(taskRole)`), so CloudFormation refused to change the in-use export during a logical-id rename: `Cannot delete export ...ConnectionsTable8000B8A1Arn... as it is in use by isol8-dev-service`. The WS table (`service-stack.ts:569 table/${props.connectionsTableName}`) and BYOK secrets (`:298 secret:isol8/${env}/user-keys/*`) grant by **name string**, so they have no such export and no such fragility.
- **State after recovery (Phase 0), verified against live AWS 2026-05-31:**
  - The physical `isol8-dev-connections` and `isol8-prod-connections` tables **do not exist** (`DescribeTable` → ResourceNotFoundException, both envs). They were deleted out-of-band during the incident and never re-created — the revert (#781) restored the *prior* template verbatim, so the database-stack deploy was a no-op and CFN never noticed the table was gone.
  - **This is CloudFormation drift, not a "ghost":** the deployed `isol8-<env>-database` template still contains `ConnectionsTable8000B8A1 → isol8-<env>-connections` and the two ARN exports still resolve to the (now-missing) table ARN; `isol8-<env>-service` still imports them. CFN believes the table exists.
  - `isol8-<env>-oauth-state` **does** exist and is empty (`ItemCount=0`, ACTIVE, both envs).
  - **No data at risk:** both `connections` tables were empty when deleted; the credential layer is not live.
  - **Executing this plan resolves the drift:** Phase 2 Task 2.5 removes the `ConnectionsTable` construct, so CFN stops believing the table exists. In prod the construct's `removalPolicy` is RETAIN, so Pass-2 issues no DeleteTable; in dev it is DESTROY, and CFN treats deleting an already-absent table as a successful delete.

---

## Target end state

```
Acquire (backend, in-flight only)        Deliver + custody (durable)
  paste:  validate -> SM secret  ----->   container openclaw.json mcp env (literal)  + SM (re-inject source)
  oauth:  PKCE dance -> exchange  ----->   container EFS gog keyring (M6, non-retained)
  byo-llm: validate -> SM secret ----->   ECS task secrets:[{valueFrom: SM arn}]      (Phase 3)
  paperclip-key: mint -> SM secret ---->  ECS task secrets:[{valueFrom: SM arn}]      (Phase 3)
Backend durable state:
  - oauth-state table  (TTL'd PKCE/state; NO secrets)
  - integrations status index (owner, provider, status, ts, [secret_arn]; NO secrets)   [D1]
  - NO credential vault, NO api-keys table
IAM: backend -> DDB/SM by name-based ARN (no cross-stack export)
```

---

## Open decisions (resolve before building)

- **D1 — integration status source.**
  - **(a, recommended) thin `integrations` DDB index** — `{owner_id, provider, status, connected_at}`, **no secret**. Fast per-owner query; decoupled from container availability; the marketplace gate already does a per-owner lookup; satisfies "no *secrets* in the DB" (the real objection). Cheapest refactor.
  - (b) **derive from container** — read the buyer's EFS/`openclaw.json` (which mcp env keys are set, which keyrings exist). Single source of truth, zero status table; but more I/O and couples status to container state, awkward for the marketplace gate.
- **D2 — paste-key secret store.**
  - **(a, recommended) Secrets Manager** per owner (`isol8/<env>/integrations/<owner>/<provider>`), mirroring BYOK. Managed secret store; gives the marketplace slot-fill a clean source.
  - (b) **don't retain** — read the buyer's token from their container config on demand. Maximally "container owns it," but the slot-fill must snapshot before the slice merge blanks the slot; fiddlier.

The plan below assumes **D1=(a)** and **D2=(a)**; adjust tasks if you choose (b).

---

## Phase 1 — decouple the cross-stack export (unblocks all later renames)

> **Incident-critical. Verified against live CFN 2026-05-31 (read-only):**
> - The trap is **two auto-generated ARN exports**, each imported by `isol8-<env>-service` (dev + prod confirmed; identical logical-id suffixes across envs):
>   - `isol8-<env>-database:ExportsOutputFnGetAttConnectionsTable8000B8A1Arn6FD6777C` → `arn:…:table/isol8-<env>-connections`
>   - `isol8-<env>-database:ExportsOutputFnGetAttOauthStateTable1D8479B9Arn3B610B82` → `arn:…:table/isol8-<env>-oauth-state`
> - The explicit name outputs `isol8-<env>-database-connections-table` / `-oauth-state-table` are **imported by nobody** — safe to delete anytime (Phase 2).
> - **Why a single-PR grant removal repeats the incident:** `service` still depends on `database` (it grants ~20 other tables), so the deployer updates `database` FIRST. Removing the grant deletes the ARN export in `database` while the still-deployed old `service` template imports it → `Cannot delete export … as it is in use by isol8-<env>-service`. The exact failure from the rename.
> - **Fix = CDK `exportValue` two-pass.** Pass 1 (this phase) drops the import AND pins the export so it is NOT deleted this deploy. Pass 2 (Phase 2 Task 2.5 for connections; a one-line follow-up for oauth-state) removes the pin once nothing imports it.

### Task 1.1: Pass 1 — name-based grant + pin the exports (service stops importing; export retained)
**Files:** Modify `apps/infra/lib/stacks/service-stack.ts` (grant + `ServiceStackProps` interface); Modify `apps/infra/lib/stacks/database-stack.ts` (add the two `exportValue` pins); Modify `apps/infra/lib/stacks/{isol8-stage,local-stage}.ts` (drop the now-unused `connectionsTable`/`oauthStateTable` objects from the `ServiceStack` props block); Test `apps/infra/test/database-stack.test.ts` + a service-stack template test.

- [ ] **service-stack.ts:** replace lines 315-316 (`props.database.connectionsTable.grantReadWriteData(this.taskRole)` + `...oauthStateTable...`) with one name-based statement, mirroring the WS grant at :557-576. **Verify the action set covers everything `connection_repo` + the oauth-state consumers actually call** (get/put/update/delete/query at minimum; add `BatchWriteItem`/`BatchGetItem`/`ConditionCheckItem` only if a caller uses them — `grantReadWriteData` granted those, the narrow statement must not silently drop a used action):
  ```ts
  this.taskRole.addToPolicy(new iam.PolicyStatement({
    sid: "DynamoDbIntegrations",
    actions: ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem","dynamodb:Query","dynamodb:Scan"],
    resources: [
      `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-connections`,
      `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-connections/index/*`,
      `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-oauth-state`,
      `arn:aws:dynamodb:${this.region}:${this.account}:table/isol8-${env}-oauth-state/index/*`,
    ],
  }));
  ```
- [ ] **service-stack.ts:** remove `connectionsTable` + `oauthStateTable` from the `ServiceStackProps.database` interface (lines 65-66). Leave `oauthTokensTable` (ChatGPT, out of scope) and all others.
- [ ] **isol8-stage.ts / local-stage.ts:** delete the `connectionsTable: database.connectionsTable,` + `oauthStateTable: database.oauthStateTable,` lines from the `ServiceStack` props (isol8-stage 95-96, local-stage 100-101). Do **not** touch ObservabilityStack or the `connectionsTableName` (WS) wiring.
- [ ] **database-stack.ts:** pin the two ARN exports so Pass-1 deploy does NOT delete an in-use export. Add right after the existing connections/oauth-state CfnOutputs (~line 321), passing the EXACT live export names so CFN sees an unchanged export (same Name + Value), not a delete:
  ```ts
  // Pass-1 of cross-stack-export retirement (credential-layer-redesign Phase 1).
  // Pins the auto-generated ARN exports that isol8-<env>-service imported via
  // grantReadWriteData, so removing that grant does not try to delete an
  // in-use export. Removed in Phase 2 once nothing imports them.
  this.exportValue(this.connectionsTable.tableArn, {
    name: `${this.stackName}:ExportsOutputFnGetAttConnectionsTable8000B8A1Arn6FD6777C`,
  });
  this.exportValue(this.oauthStateTable.tableArn, {
    name: `${this.stackName}:ExportsOutputFnGetAttOauthStateTable1D8479B9Arn3B610B82`,
  });
  ```
  (`this.stackName` resolves to `isol8-<env>-database`, matching the live names.)
- [ ] **Test (deterministic, no AWS context — use `Template.fromStack`, the repo's infra-test pattern):**
  - database template still has `Outputs` with `Export.Name` = each exact name above, `Value` = the table `Fn::GetAtt … Arn`.
  - service template's task-role policy has the `DynamoDbIntegrations` statement and **no** `Fn::ImportValue` referencing `ConnectionsTable8000B8A1Arn` / `OauthStateTable1D8479B9Arn`.
  - Run for BOTH dev and prod synth.
- [ ] `cd apps/infra && npx jest` green; `npx cdk synth` (or `cdk diff`) shows the database export **unchanged** and the service import **removed**. **DO NOT deploy from here** — deploy happens via the PR→CI pipeline after review.

### Task 1.2 (Pass 2 — deferred to Phase 2): remove the pins
The `exportValue` pins are removed once nothing imports them: the connections pin disappears with the table in **Task 2.5**; the oauth-state pin is removed in the same Phase-2 deploy (the table stays, only its now-orphaned ARN export goes). Do NOT remove either pin in Phase 1 — Pass 1 and Pass 2 must be SEPARATE deploys.

---

## Phase 2 — replace the vault with SM + a status index

### Task 2.1: Paste-key secrets in Secrets Manager
**Files:** Create `apps/backend/core/services/credentials/integration_secret_store.py` (mirror `key_service._put_user_secret`/`_delete_user_secret`); Modify `service-stack.ts` (grant `secret:isol8/<env>/integrations/*`, like `user-keys/*` at :298); Test.

- [ ] `put(owner, provider, token) -> arn` (create/put `isol8/<env>/integrations/<owner>/<provider>`), `delete(owner, provider)`, `get(owner, provider)`. KMS via the same general CMK as BYOK.
- [ ] service-stack: add `secret:isol8/<env>/integrations/*` to the existing `UserKeysSecretsRW`-style statement.
- [ ] Tests (moto/boto stub): put/get/delete round-trip.

### Task 2.2: `integrations_repo` — status-only, no secret  [if D1=(a)]
**Files:** Replace `apps/backend/core/repositories/connection_repo.py` → `integrations_repo.py` (`get_table("integrations")`); item shape `{owner_id, provider, status, connected_at, updated_at}` — **no `credential_enc`**. Drop `decrypt_credential`/`scrub_secret`.
- [ ] `put_status(owner, provider, status)`, `get(owner, provider)`, `list_for_owner(owner)`, `delete(owner, provider)`.
- [ ] Tests.

### Task 2.3: Rework connect / disconnect / list / oauth-callback
**Files:** Modify `apps/backend/routers/connections.py`.
- [ ] paste connect: validate → `integration_secret_store.put` (SM) → `apply_connection` (deliver literal into container mcp env, unchanged) → `integrations_repo.put_status(connected)`. **Never** write the token to DDB. Drop `_scrub_if_actor_refresh` (nothing retained to scrub) and the vault-based rollback; on apply failure, delete the SM secret + the status row + call the materializer remover.
- [ ] oauth callback: exchange → materialize to container EFS (gog, unchanged) → `put_status(connected)`. No secret stored.
- [ ] disconnect: materializer remover (EFS) + `integration_secret_store.delete` (SM) + `integrations_repo.delete` + clear `openclaw_paths`.
- [ ] list: from `integrations_repo.list_for_owner` (D1a) or derived (D1b).
- [ ] Tests (rework the existing `test_connections.py`).

### Task 2.4: Marketplace gate + slot-fill re-sourced
**Files:** Modify `apps/backend/core/services/catalog_service.py`.
- [ ] `assert_credentials_satisfied`: read connected set from `integrations_repo.list_for_owner` (D1a) / derived (D1b) — same logic, new source.
- [ ] `_fill_buyer_credential_slots`: read the buyer's paste token from **`integration_secret_store.get`** (SM) instead of `connection_repo.decrypt_credential`. Keep the existing skip rules (materializer/actor providers skip; canonical-key check; per-provider best-effort). If D2=(b), snapshot the buyer's mcp env before the merge instead.
- [ ] Tests.

### Task 2.5: Pass 2 — drop `connections`, remove both export pins, add `integrations` [D1a]
**Files:** Modify `apps/infra/lib/stacks/database-stack.ts`; Test.
- [ ] Remove the `ConnectionsTable` construct (database-stack.ts:251-260) **and** its explicit `ConnectionsTableName` CfnOutput (:315-318). Physical table is already gone (drift); prod RETAIN → no DeleteTable, dev DESTROY → CFN no-ops the missing-table delete. This clears the drift.
- [ ] Remove **both** Pass-1 `exportValue` pins added in Task 1.1 (the connections ARN pin and the oauth-state ARN pin). After Pass 1 nothing imports either, so CFN deletes them cleanly. **This is the second deploy of the two-pass — it MUST land after Task 1.1 is deployed, never in the same deploy.**
- [ ] Keep the `OauthStateTable` construct + its `OauthStateTableName` output (the table stays; only its orphaned ARN export pin is removed).
- [ ] Add `IntegrationsTable`, mirroring the old `ConnectionsTable` shape minus any secret intent: `isol8-<env>-integrations`, `owner_id` PK + `provider` SK, PAY_PER_REQUEST, CMK (`props.kmsKey`), PITR, `removalPolicy: config.removalPolicy`. **No longer granted cross-stack.**
- [ ] **Extend the Phase-1 `DynamoDbIntegrations` name-based grant** (service-stack.ts) to add `arn:…:table/isol8-<env>-integrations` (+ `/index/*`). The credential `connections` ARN can be dropped from that statement in the same edit (the table is gone). This is a name-based string change — no new cross-stack export.
- [ ] **Test (`Template.fromStack`):** `isol8-<env>-integrations` present with the right key schema; `ConnectionsTable8000B8A1` absent; neither `exportValue` pin present; oauth-state table still present.
- [ ] `npx jest` + `npx cdk synth`/`diff` clean for dev + prod. **Deploy via CI after review.** If the dev deploy errors on the missing-table delete (it should not), the fallback is a one-deploy `removalPolicy: RETAIN` on the construct before removing it. Confirm post-deploy: `isol8-<env>-integrations` ACTIVE; the two ARN exports gone from `list-exports`.

### Task 2.6: Config + cleanup
- [ ] Remove the dead `ENCRYPTION_KEY`-based vault path if no longer used elsewhere (it's still used by channels/other Fernet uses — verify before removing).
- [ ] Frontend `useConnections` / panel labels: rename "connections" → "integrations" where user-facing (optional, cosmetic).

---

## Phase 3 — retire the `api-keys` BYOK table (no secrets in any DDB table)

> **Why this phase exists:** `api-keys` is the *other* secret-bearing DDB table. Verified consumers (2026-05-31, from code):
> - **4 tool keys** (elevenlabs/openai_tts/perplexity/firecrawl) — fully dead: `set_key` writes them, `list_keys` displays them, **nothing reads `encrypted_key`**, no `config_path` is ever written, `settings_keys.py:62` is a literal `# TODO` for delivery.
> - **2 LLM keys** (openai/anthropic) — work, but the DDB `encrypted_key` is also dead: the only readers are `ecs_manager.py:1161` (`_build_byo_secrets_for_user`) and `billing_service.py:587` (change-provider gate), and **both read `secret_arn` only**; `KeyService.get_key` (the decrypt path) has **zero callers**. Real custody is already Secrets Manager.
> - **Paperclip per-agent keys** (`tool_id="paperclip-agent-key:<uuid>"`) — the **only** live secret in DDB: `paperclip_api_key._mint_and_store` Fernet-encrypts a minted Paperclip token into `encrypted_key`; `_read_existing` decrypts it back and it's injected as the `PAPERCLIP_API_KEY` literal in the MCP block (`config.py:916`). This is the load-bearing piece — the table can't be dropped until this moves.
>
> Sequencing: 3.1 → 3.2 → **3.3 (Paperclip — the blocker)** → 3.4 (drop the table). 3.4 must be last.

### Task 3.1: Delete the dead tool-key feature
**Files:** Modify `apps/backend/core/services/key_service.py`; Modify `apps/backend/routers/settings_keys.py`; Test `apps/backend/tests/.../test_settings_keys.py`.
- [ ] Remove the `SUPPORTED_TOOLS` dict (`key_service.py:28-45`) and every reference: `_allowed_ids` (→ becomes just `set(SUPPORTED_LLM_PROVIDERS)`), the `list_keys` display branch (`:218-219`), and the router's `ALLOWED_KEY_IDS`/`supported_tools` response field (`settings_keys.py:21,41`). Keep the `config_path` concept out — if Perplexity/Firecrawl/TTS are wanted later, they are net-new *registry integrations* (Bucket A paste-key), not resurrected here.
- [ ] Remove the `# TODO: update openclaw.json on EFS + send config.apply RPC` (`settings_keys.py:62`) — the delivery it describes is superseded by the integrations panel.
- [ ] Update the `list`/`set`/`delete` router tests: the 4 tool ids now 400 (unsupported); openai/anthropic still 200.

### Task 3.2: BYO-LLM key → SM-only record (drop the encrypted DDB copy)
**Files:** Modify `apps/backend/core/services/key_service.py`; Create/Modify the status-record writer (the `integrations_repo` from Task 2.2, or a thin `byo_keys_repo` — see note); Modify `apps/backend/core/containers/ecs_manager.py:1159-1169`; Modify `apps/backend/core/services/billing_service.py:585-593`; Test.
- [ ] `set_key` for an LLM provider: validate → `_put_user_secret` (SM, unchanged) → write a **metadata-only** record `{owner_id, provider, secret_arn, status:"connected", created_at}` — **no `encrypted` field**. Drop the `encrypt(api_key)` + `api_key_repo.set_key(encrypted_key=…)` call for LLM keys.
- [ ] Repoint the two readers at the new record (both already read only `secret_arn`, so this is a source swap, not a logic change): `_build_byo_secrets_for_user` reads `record["secret_arn"]`; the change-provider gate checks `record and record.get("secret_arn")`.
- [ ] Delete `KeyService.get_key` + `api_key_repo.get_key`'s LLM use (no callers) — keep only what Paperclip needs until 3.3 moves it.
- [ ] **Record home (sub-choice, not a blocking decision):** simplest is to reuse the **`integrations` status index** (D1a) with `provider` like `byo-llm:openai` and an optional `secret_arn` column — it already models "what's connected + where's the SM secret." If D1=(b) chosen, add a tiny dedicated `byo-llm` SM-existence check instead. Pick whichever Task 2.2 landed.
- [ ] Tests: set → SM secret created + metadata record (no ciphertext); ECS secrets block resolves; gate passes/fails on presence; switching away deletes the SM secret (`_delete_user_secret`, unchanged) + the record.

### Task 3.3: Paperclip per-agent key → Secrets Manager (the table-drop blocker)
**Files:** Modify `apps/backend/core/services/paperclip_api_key.py`; Modify `apps/backend/core/containers/config.py:902-916` (MCP block) + `ecs_manager.py:1809-1838` (provision wiring); Modify `service-stack.ts` (grant `secret:isol8/<env>/paperclip-agent-keys/*`); Test `apps/backend/tests/.../test_paperclip_api_key.py`.
- [ ] Replace the `api_key_repo` round-trip with an SM secret `isol8/<env>/paperclip-agent-keys/<owner>/<paperclip_agent_id>`. `_read_existing` → `get_secret_value` (None on `ResourceNotFoundException`); `_mint_and_store` → `create_secret`/`put_secret_value`. Keep the idempotency/self-heal contract (existing secret short-circuits; missing → mint) and the swallow-all-failures behavior verbatim.
- [ ] **Delivery (sub-choice):** (a) keep injecting the plaintext as the `PAPERCLIP_API_KEY` literal in `config.py:916` (read from SM at config-write time — minimal change, but the token sits plaintext in openclaw.json on EFS, same as today); or (b, cleaner) deliver it as an ECS task `secrets:[{name:"PAPERCLIP_API_KEY", valueFrom: arn}]` like the LLM key, and drop it from the openclaw.json env block. Prefer (b) only if the MCP block reliably reads container env; otherwise (a) preserves today's exact behavior. **Verify against OpenClaw config/runtime which one the MCP wrapper honors before choosing.**
- [ ] Teardown: container teardown currently relies on `api_key_repo.delete_all_for_owner` (`debug.py:285`, `paperclip_repo.py:439` doc) to sweep these. Replace with deleting the owner's `paperclip-agent-keys/*` SM secrets (force-delete, no recovery window, matching `_delete_user_secret`).
- [ ] Tests: mint→SM→read-back→deliver; idempotent second call hits SM not Paperclip; decrypt-corrupt path is gone (SM is plaintext-at-rest under KMS); teardown removes the SM secrets.

### Task 3.4: Drop the `api-keys` table + repoint debug/teardown
**Files:** Modify `apps/infra/lib/stacks/database-stack.ts`; Modify `apps/infra/lib/stacks/service-stack.ts` (drop the `api-keys` grant if it was an object grant; the LLM/paperclip SM grants from 3.2/3.3 replace it); Modify `apps/backend/routers/debug.py:285,390`; Delete `apps/backend/core/repositories/api_key_repo.py`; Test.
- [ ] Confirm there are **zero** remaining `api_key_repo` / `KeyService` callers (3.1–3.3 removed them all — re-grep before deleting).
- [ ] `debug.py`: e2e teardown (`:285`) and ddb-rows count (`:390`) drop the `api-keys` entries (their secrets now sweep via the SM deletes in 3.2/3.3).
- [ ] **Pre-drop the cross-stack export, same as Phase 1.** If `api-keys` is granted to the task role via `grantReadWriteData(table)` (cross-stack object), it has the *same export trap* as `connections` — convert that grant to name-based ARN and **deploy that first**, then drop the table in a later deploy. If the grant is already name-based, the table can be removed directly. **Check `service-stack.ts` for an `apiKeysTable.grantReadWriteData` before touching `database-stack.ts`.**
- [ ] Remove the `api-keys` table construct from `database-stack.ts`.
- [ ] `cdk synth`; assert the table is gone and no `Fn::ImportValue` references its ARN. **Deploy.** Confirm `aws dynamodb describe-table isol8-<env>-api-keys` → `ResourceNotFoundException`.

---

## Final verification
- [ ] Backend suite: `cd apps/backend && uv run pytest tests/credentials tests/unit/routers/test_connections.py tests/unit/routers/test_settings_keys.py tests/unit/services/test_catalog_service.py tests/unit/services/test_paperclip_api_key.py -v`.
- [ ] Infra: `cd apps/infra && npx jest`; `npx cdk synth` clean for dev + prod.
- [ ] **Grep gate:** `grep -rn "api_key_repo\|api-keys\|SUPPORTED_TOOLS\|KeyService" apps/backend` returns nothing live; no DDB table holds a ciphertext field (`encrypted_key`, `credential_enc`).
- [ ] Manual dev smoke once Google is registered: paste-connect Linear → token in SM (not DDB), agent uses it; OAuth-connect Google → keyring on EFS, nothing in any backend DB; disconnect clears SM/EFS + status; marketplace deploy gate + slot-fill work from the new sources.
- [ ] **BYOK smoke:** save an OpenAI BYO key → SM secret + metadata record (no ciphertext in DDB); container provisions with `OPENAI_API_KEY` from the SM ARN; provider-switch gate passes; switching away deletes the SM secret. Friday's Paperclip MCP still authenticates after a reprovision (Paperclip key minted → SM → delivered).

---

## Risks / notes
- **Order is load-bearing:** Phase 1 (decouple export) MUST deploy before Phase 2 Task 2.5 (drop the table), or CFN blocks the export removal again — the exact failure that started this. Phase 3 Task 3.4 has the **same trap** for `api-keys` if its grant is a cross-stack object — convert-to-name-based-and-deploy MUST precede the table drop.
- **Phase 3 touches a LIVE path** (Paperclip per-agent keys), unlike the empty `connections` work. `api-keys` is **not** empty in dev/prod — it holds real Paperclip agent tokens (and possibly LLM keys for any owner who set BYOK). Task 3.3 must migrate/re-mint those into SM, not just delete them. Re-mint is safe (the helper is idempotent + self-healing), but **verify a clean reprovision still authenticates Friday's MCP before dropping the table.** Check both dev and prod `api-keys` contents before 3.4.
- **No `connections` data migration:** the `connections` tables were empty and are deleted; that part is a clean slate. `api-keys` is the opposite — see above.
- **The ghost resource** (`ConnectionsTable8000B8A1` in CFN state, physical gone) is removed by Task 2.5 once the export is freed.
- **Don't repeat the incident:** never delete a CFN-managed table out-of-band again; never rename a resource that's cross-stack-exported without first removing the import. Use name-based ARN grants for any new table.
