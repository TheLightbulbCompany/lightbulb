# DynamoDB Dead-Table Retirement (Pass-2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete four DynamoDB tables the backend no longer uses (`api-keys`, `credits`, `credit-transactions`, `connections`) from the CDK database stack, backing up the small amount of prod financial data first, and cleaning up the dead IAM grant and CDK tests they leave behind.

**Architecture:** These four tables reached a "Pass-1" state weeks ago — the backend stopped using them (moved to Secrets Manager / Metronome), the cross-stack CloudFormation exports were pinned so removing the consumer imports wouldn't fail, but the "Pass-2" deploy that actually removes the constructs was deferred and never shipped. Live CloudFormation was verified: all 8 pinned exports are imported by **zero** stacks in both dev and prod, so a **single** database-stack deploy per environment removes them without a rollback. `dev` tables are `DESTROY` (CFN deletes them); `prod` tables are `RETAIN` (CFN releases them and they survive as manual-delete orphans).

**Tech Stack:** AWS CDK (TypeScript), CloudFormation, DynamoDB, Vitest (CDK unit tests), GitHub Actions (`deploy.yml`), AWS CLI (`isol8-admin` SSO profile).

## Global Constraints

- **NEVER modify prod directly.** All CDK changes ship through `deploy.yml` CI, not local `cdk deploy`. Copied verbatim from CLAUDE.md Critical Rules.
- **Always PR, never direct to `main`.** No push without explicit approval.
- **Deploy order is dev → validate → prod**, matching the repo's existing promotion posture.
- **The only two manual AWS operations** in this plan are (a) the pre-flight data backup and (b) the post-deploy `delete-table` of the three prod `RETAIN` orphans. Both require `aws sso login --profile isol8-admin` and are one-off data operations on tables CloudFormation has already released — they are not "modifying prod servers."
- **Exact prod physical state (verified 2026-07-14):** `isol8-prod-api-keys` = 1 item, `isol8-prod-credits` = 1 item, `isol8-prod-credit-transactions` = **921 items** (financial history — back up before deleting), `isol8-prod-connections` = **already does not exist** (deleted out-of-band 2026-05-31).
- **Do NOT touch the look-alike tables:** `isol8-{env}-ws-connections` (WebSocket connection state, `api-stack.ts:272-273`) is a completely different, live, heavily-used table. Every `WS_CONNECTIONS_TABLE` / `connectionsTableName` reference in `service-stack.ts` and `observability-stack.ts` points at *that* table, not the credential vault being deleted.
- **Out of scope (verified, do not include):** `oauth-tokens` is LIVE (a concurrency state machine, not a drop); the `marketplace-payout-accounts` → `marketplace-sellers` merge is unsafe (would expose Stripe Connect IDs on a public GSI). See "Explicitly Out of Scope" at the end.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `apps/infra/lib/stacks/database-stack.ts` | Defines all DynamoDB tables + their pinned CFN exports | Remove 4 table constructs + their exports |
| `apps/infra/lib/stacks/service-stack.ts` | Backend ECS task role IAM | Remove the dead `connections` by-name IAM grant (`:533-534`) |
| `apps/infra/test/apikeys-export-decouple.test.ts` | Asserts api-keys export pins present | Remove pin-present assertions |
| `apps/infra/test/credits-export-decouple.test.ts` | Asserts credits/credit-tx tables + exports present | Remove table/export assertions |
| `apps/infra/test/credential-export-decouple.test.ts` | Asserts connections ARN pin + grant present | Remove connections-pin + grant assertions; KEEP oauth-state assertions |
| `scratchpad backup files` | Pre-flight prod data export | Create (transient) |

---

## Task 0: Pre-flight — back up prod financial data before any destructive change

**Files:** none (AWS data operation). Backup artifacts land in the scratchpad, then S3.

**Interfaces:**
- Consumes: `aws` CLI with `isol8-admin` SSO profile.
- Produces: three JSON backups in S3 confirming `credit-transactions` (921 rows), `credits` (1), `api-keys` (1) are preserved before deletion. This is the safety gate for Task 5.

- [ ] **Step 1: Authenticate**

Run: `aws sso login --profile isol8-admin`
Expected: browser SSO flow completes; `aws sts get-caller-identity --profile isol8-admin` returns account `877352799272`.

- [ ] **Step 2: Confirm current row counts match the plan's assumption**

```bash
for t in isol8-prod-api-keys isol8-prod-credits isol8-prod-credit-transactions; do
  echo -n "$t: "
  aws dynamodb describe-table --table-name "$t" --region us-east-1 --profile isol8-admin \
    --query 'Table.ItemCount' --output text
done
```
Expected: `api-keys: 1`, `credits: 1`, `credit-transactions: 921` (approximately — `ItemCount` is updated ~every 6h, so treat as a sanity check, not exact). If wildly different (e.g. thousands), STOP and re-investigate before proceeding.

- [ ] **Step 3: Scan each table to a local JSON backup (CLI auto-paginates all rows)**

```bash
BK=/private/tmp/claude-501/-Users-prasiddhaparthsarthy-Desktop-isol8-nosync/eba3c2cc-3bdd-44b0-91e9-670d8564bced/scratchpad
for t in isol8-prod-api-keys isol8-prod-credits isol8-prod-credit-transactions; do
  aws dynamodb scan --table-name "$t" --region us-east-1 --profile isol8-admin \
    --output json > "$BK/backup-$t-2026-07-14.json"
  echo -n "$t rows backed up: "; python3 -c "import json,sys; print(len(json.load(open('$BK/backup-$t-2026-07-14.json'))['Items']))"
done
```
Expected: each file's `Items` length matches the counts from Step 2 (exactly, since scan is a live read). Confirm `credit-transactions` shows 921 (or the true live count).

- [ ] **Step 4: Upload backups to S3 with a retention-friendly key**

```bash
aws s3 cp "$BK/backup-isol8-prod-api-keys-2026-07-14.json"          s3://isol8-prod-config/retired-tables/2026-07-14/ --profile isol8-admin
aws s3 cp "$BK/backup-isol8-prod-credits-2026-07-14.json"           s3://isol8-prod-config/retired-tables/2026-07-14/ --profile isol8-admin
aws s3 cp "$BK/backup-isol8-prod-credit-transactions-2026-07-14.json" s3://isol8-prod-config/retired-tables/2026-07-14/ --profile isol8-admin
```
Expected: three `upload:` confirmations. Verify with `aws s3 ls s3://isol8-prod-config/retired-tables/2026-07-14/ --profile isol8-admin`.

> **Note:** Confirm `isol8-prod-config` is the correct config bucket name for prod (`S3_CONFIG_BUCKET`) before running — substitute the actual bucket if different. `connections` needs no backup (already gone).

- [ ] **Step 5: Commit a record of the backup location (no code, doc only)**

```bash
git add docs/superpowers/plans/2026-07-14-dynamodb-dead-table-retirement.md
git commit -m "docs: record pre-flight backup of retiring DDB tables to S3"
```

---

## Task 1: Flip the CDK export-pin tests (they currently assert the pins EXIST)

**Files:**
- Modify: `apps/infra/test/apikeys-export-decouple.test.ts:147,154`
- Modify: `apps/infra/test/credits-export-decouple.test.ts:38,47`
- Modify: `apps/infra/test/credential-export-decouple.test.ts:143` (remove connections-pin case; KEEP `:153` oauth-state)

**Interfaces:**
- Consumes: nothing.
- Produces: a test suite that will pass *after* Task 2/3 remove the constructs. Doing tests first (inverted-TDD for a deletion) means Task 2's `pnpm test` is the pass/fail gate.

- [ ] **Step 1: Read each test file to locate the exact assertions**

Run: `sed -n '130,175p' apps/infra/test/apikeys-export-decouple.test.ts` (and equivalents). Identify the `expect(...).toBe(...)`/`hasOutput`/`Match.objectLike` blocks that assert the pinned export names or table presence.

- [ ] **Step 2: Remove the "pin is present" assertions**

In `apikeys-export-decouple.test.ts`: delete the two assertions that the api-keys ARN pin (`...ApiKeysTable9F4DC7E7ArnE84F3BB0`) and Ref pin (`...RefApiKeysTable9F4DC7E79648F444`) are present in the database stack template. Keep any assertion that the *service stack* no longer imports the ARN (that stays true and is good coverage). If the file's sole purpose was pin-presence, replace its body with a single assertion that the api-keys exports are **absent**:

```typescript
test("database stack no longer exports the retired api-keys table", () => {
  const template = Template.fromStack(dbStack);
  const outputs = template.toJSON().Outputs ?? {};
  const keys = Object.keys(outputs);
  expect(keys.some((k) => k.includes("ApiKeysTable"))).toBe(false);
});
```

- [ ] **Step 3: Same inversion for credits/credit-transactions**

In `credits-export-decouple.test.ts`: remove the assertions that `isol8-dev-credits` / `isol8-dev-credit-transactions` tables exist and that their four export names are pinned. Replace with an absence check:

```typescript
test("database stack no longer defines the retired credit-ledger tables", () => {
  const template = Template.fromStack(dbStack);
  template.resourceCountIs("AWS::DynamoDB::Table", EXPECTED_TABLE_COUNT); // set to the new count
  const outputs = template.toJSON().Outputs ?? {};
  expect(Object.keys(outputs).some((k) => k.includes("CreditsTable") || k.includes("CreditTransactionsTable"))).toBe(false);
});
```

- [ ] **Step 4: Trim the connections case, keep oauth-state**

In `credential-export-decouple.test.ts`: remove the assertion at `:143` that the connections literal-ARN pin is present. **Keep** the oauth-state assertion at `:153` (oauth-state is live and NOT in scope). Also remove `:169` (asserts the service-stack connections grant exists) — that grant is deleted in Task 3.

- [ ] **Step 5: Do NOT run tests yet — they will fail until Task 2/3 land. Commit the test changes together with Task 2 (single reviewable unit).**

No commit here; proceed to Task 2. (This task and Task 2/3 form one atomic "remove the tables" change — a reviewer should see the construct removal and its test updates together.)

---

## Task 2: Remove the four table constructs + their exports from the database stack

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` — remove:
  - `api-keys`: field decl (`:28`), construct (`:116-125`), pins + comment (`:415-427`)
  - `credits`: construct (`:237-245`), pins (`:256-261`); trim the shared "Pass-1" comment (`:227-236`)
  - `credit-transactions`: construct (`:246-255`), pins (`:262-267`)
  - `connections`: field decl (`:37`), construct (`:315-324`), `ConnectionsTableName` output (`:387-390`), `ConnectionsArnExportPin` + comment (`:395-411`)
- **KEEP:** the oauth-state Ref output (`:391-394`) and oauth-state ARN pin (`:412-414`) — oauth-state is live.

**Interfaces:**
- Consumes: the flipped tests from Task 1.
- Produces: a database stack template with 4 fewer `AWS::DynamoDB::Table` resources and 8 fewer Outputs, importable by all existing stacks with no dangling import.

- [ ] **Step 1: Remove the api-keys construct and its class field**

Delete the `public readonly apiKeysTable` field declaration (~`:28`), the `this.apiKeysTable = new dynamodb.Table(...)` block (`:116-125`), and the two `exportValue` pins with their comment (`:415-427`). Search the file for any remaining `apiKeysTable` reference and remove it (there should be none after these).

- [ ] **Step 2: Remove the credits + credit-transactions constructs and pins**

Delete the `creditsTable` const (`:237-245`), the `creditTransactionsTable` const (`:246-255`), and the four `this.exportValue(...)` pins (`:256-267`). Rewrite the "Pass-1" comment block (`:227-236`) — either delete it entirely or replace with a one-line note that the ledger tables were retired 2026-07-14.

- [ ] **Step 3: Remove the connections vault construct + exports**

Delete the `public readonly connectionsTable` field (`:37`), the construct (`:315-324`), the `ConnectionsTableName` CfnOutput (`:387-390`), and the `ConnectionsArnExportPin` CfnOutput with its explanatory comment (`:395-411`). **Verify you did NOT delete** the `OauthStateTableName` output (`:391-394`) or the `this.exportValue(this.oauthStateTable.tableArn, ...)` pin (`:412-414`).

- [ ] **Step 4: Synth and diff to confirm ONLY deletions**

Run:
```bash
cd apps/infra && pnpm cdk synth isol8-dev-database > /dev/null && echo "synth OK"
pnpm cdk diff isol8-dev-database 2>&1 | grep -E "^\[-\]|Table|Output" | head -40
```
Expected: `synth OK`, and the diff shows `[-]` removals for exactly the 4 tables (`ApiKeysTable`, `CreditsTable`, `CreditTransactionsTable`, `ConnectionsTable`) and their outputs, with **no** `[-]` on any other table or on any oauth-state / ws-connections resource. If any unexpected resource is being removed, STOP.

- [ ] **Step 5: Run the CDK unit tests**

Run: `cd apps/infra && pnpm test`
Expected: PASS. The flipped tests from Task 1 now assert absence and match the new template. If `resourceCountIs` fails, update `EXPECTED_TABLE_COUNT` to the actual post-deletion count shown in the failure.

- [ ] **Step 6: Commit constructs + tests together**

```bash
git add apps/infra/lib/stacks/database-stack.ts apps/infra/test/apikeys-export-decouple.test.ts apps/infra/test/credits-export-decouple.test.ts apps/infra/test/credential-export-decouple.test.ts
git commit -m "chore(infra): retire unused api-keys, credits, credit-transactions, connections DDB tables"
```

---

## Task 3: Remove the dead `connections` IAM grant from the service stack

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts:533-534` (the `DynamoDbIntegrations` policy statement referencing `table/isol8-${env}-connections` + `/index/*`) and the flagging comment at `:517-518`.
- Modify: `apps/infra/test/credential-export-decouple.test.ts:169` (already handled in Task 1 Step 4 if you removed it there; otherwise remove here).

**Interfaces:**
- Consumes: nothing (this grant points at a table that no longer exists).
- Produces: a service-stack task role with no dangling ARN reference. This is a normal `isol8-{env}-service` deploy with **no ordering dependency** on the database deploy — it can ship in the same PR.

- [ ] **Step 1: Read the grant in context**

Run: `sed -n '510,540p' apps/infra/lib/stacks/service-stack.ts`
Expected: a policy statement listing the `isol8-${env}-connections` table ARN and its `/index/*`. Confirm it is a template-literal ARN string (NOT an `Fn::ImportValue`) — so removing it does not affect any export.

- [ ] **Step 2: Remove the connections ARN(s) from the statement**

Delete the two resource ARNs (`.../isol8-${env}-connections` and `.../isol8-${env}-connections/index/*`) from the `DynamoDbIntegrations` statement. If the statement's *only* remaining resource was the integrations table, ensure that ARN stays. Remove the `:517-518` "retired — grant removed when the table is dropped" comment.

- [ ] **Step 3: Synth + test**

Run:
```bash
cd apps/infra && pnpm cdk synth isol8-dev-service > /dev/null && echo "synth OK"
pnpm test apps/infra/test/service-stack.test.ts apps/infra/test/credential-export-decouple.test.ts
```
Expected: `synth OK`, tests PASS.

- [ ] **Step 4: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts apps/infra/test/credential-export-decouple.test.ts
git commit -m "chore(infra): drop dead connections-table IAM grant from backend task role"
```

---

## Task 4: Deploy to dev and validate

**Files:** none (CI-driven deploy).

**Interfaces:**
- Consumes: the merged Task 2/3 branch.
- Produces: dev CloudFormation with the 4 tables removed; the physical dev tables (`DESTROY`) deleted by CFN; `connections` no-op (already absent).

- [ ] **Step 1: Pre-deploy guard — confirm the exports are still imported by nobody**

```bash
for e in ApiKeysTable9F4DC7E7ArnE84F3BB0 RefApiKeysTable9F4DC7E79648F444 \
         CreditsTable13AC5EA8ArnB299C72C RefCreditsTable13AC5EA80B87C786 \
         CreditTransactionsTable72EF769AArn60F8FDA5 RefCreditTransactionsTable72EF769A0289C51E; do
  echo -n "$e imported by: "
  aws cloudformation list-imports --export-name "isol8-dev-database:ExportsOutputFnGetAtt$e" \
    --region us-east-1 --profile isol8-admin 2>&1 | grep -o "not imported\|StackName" | head -1 || echo "not imported"
done
```
Expected: every export reports "not imported by any stack" (the API errors with that message when an export has no importers). If ANY returns an importing stack, STOP — a consumer import was reintroduced and this needs the two-deploy sequence instead.

- [ ] **Step 2: Push the branch and open a PR**

```bash
git push -u origin HEAD
gh pr create --repo Isol8AI/isol8 --title "chore(infra): retire 4 unused DynamoDB tables (Pass-2)" \
  --body "Retires api-keys, credits, credit-transactions, connections. Live CFN verified: all pinned exports imported by zero stacks. Prod financial data backed up to s3://.../retired-tables/2026-07-14/. See docs/superpowers/plans/2026-07-14-dynamodb-dead-table-retirement.md."
```

- [ ] **Step 3: Watch the dev deploy run after merge**

Run: `gh run watch <run-id> --repo Isol8AI/isol8 --exit-status`
Expected: `deploy.yml` succeeds for the dev environment. Confirm the `isol8-dev-database` and `isol8-dev-service` stack updates completed (no rollback).

- [ ] **Step 4: Validate dev physical state**

```bash
for t in isol8-dev-api-keys isol8-dev-credits isol8-dev-credit-transactions isol8-dev-connections; do
  echo -n "$t: "
  aws dynamodb describe-table --table-name "$t" --region us-east-1 --profile isol8-admin \
    --query 'Table.TableStatus' --output text 2>&1 | grep -o "ResourceNotFoundException\|ACTIVE\|DELETING" | head -1
done
```
Expected: all four report `ResourceNotFoundException` (dev is `DESTROY`, so CFN deleted them; `connections` was already gone).

- [ ] **Step 5: Backend smoke check on dev**

Confirm the backend still boots and the credential-adjacent paths work: hit `GET /container/status` for a dev test account (exercises the provision gate, which reads oauth-tokens/integrations — the LIVE tables we did NOT touch) and confirm no 500s referencing a missing table. Check backend logs for `ResourceNotFoundException` on any table name.

---

## Task 5: Promote to prod and manually delete the RETAIN orphans

**Files:** none (CI-driven deploy + manual data op).

**Interfaces:**
- Consumes: the validated dev deploy + the Task 0 S3 backups.
- Produces: prod CloudFormation with the 4 tables removed from stack management; the three physical prod tables (`RETAIN`) deleted manually after backup confirmation.

- [ ] **Step 1: Promote to prod via the existing deploy.yml flow**

Follow the repo's current dev→prod promotion (merge/promotion trigger per existing practice — do NOT re-run an old failed deploy). Watch the run: `gh run watch <run-id> --repo Isol8AI/isol8 --exit-status`.
Expected: prod `isol8-prod-database` + `isol8-prod-service` update successfully. Because prod is `RETAIN`, the three physical tables **survive** the deploy (CFN just stops managing them) and `connections` is a no-op.

- [ ] **Step 2: Confirm the tables are now orphaned (out of CFN, still physically present)**

```bash
for t in isol8-prod-api-keys isol8-prod-credits isol8-prod-credit-transactions; do
  echo -n "$t: "; aws dynamodb describe-table --table-name "$t" --region us-east-1 --profile isol8-admin --query 'Table.TableStatus' --output text
done
```
Expected: all three `ACTIVE` (RETAIN kept them). `isol8-prod-connections` → `ResourceNotFoundException` (already gone).

- [ ] **Step 3: RE-CONFIRM the S3 backups exist before any delete**

Run: `aws s3 ls s3://isol8-prod-config/retired-tables/2026-07-14/ --profile isol8-admin`
Expected: all three backup JSON files present. **This is the gate — do not proceed if any backup is missing.**

- [ ] **Step 4: Manually delete the three orphaned prod tables**

> ⚠️ Destructive and irreversible. Requires explicit human go-ahead. Run only after Step 3 confirms backups.

```bash
for t in isol8-prod-api-keys isol8-prod-credits isol8-prod-credit-transactions; do
  aws dynamodb delete-table --table-name "$t" --region us-east-1 --profile isol8-admin --query 'TableDescription.TableStatus' --output text
done
```
Expected: each returns `DELETING`. Re-run `describe-table` after a minute to confirm `ResourceNotFoundException`.

- [ ] **Step 5: Final verification**

```bash
aws dynamodb list-tables --region us-east-1 --profile isol8-admin \
  --query 'TableNames' --output text | tr '\t' '\n' | grep -E "isol8-(dev|prod)-(api-keys|credits|credit-transactions|connections)$" || echo "ALL RETIRED — none remain"
```
Expected: `ALL RETIRED — none remain`.

---

## Self-Review

**Spec coverage:** Every table in the verified "delete" set (api-keys, credits, credit-transactions, connections) has a construct-removal task (Task 2), a test-update task (Task 1), and a deploy+delete task (Task 4/5). The dead IAM grant has Task 3. The prod financial data has a backup task (Task 0) gating the delete (Task 5 Step 3). The `ws-connections` look-alike is called out in Global Constraints so it is never touched.

**Placeholder scan:** No TBD/TODO. `EXPECTED_TABLE_COUNT` in Task 1 Step 3 is the one value to fill from the synth output — flagged explicitly with how to get it (Task 2 Step 5). The prod config bucket name is flagged for confirmation in Task 0 Step 4.

**Type consistency:** All export logical names (`ApiKeysTable9F4DC7E7ArnE84F3BB0`, etc.) are copied verbatim from `database-stack.ts` and match between the removal (Task 2), the test flips (Task 1), and the pre-deploy import guard (Task 4 Step 1).

---

## Explicitly Out of Scope (verified — do NOT include in this plan)

These were candidates in the original review but verification ruled them out:

- **`oauth-tokens` retirement.** LIVE (read + write). It is a concurrency state machine: a conditional `put_item` enforces one-active-OAuth-session (`oauth_service.py:165-168`), it holds transient device-code dance state between `/start` and `/poll`, and it enforces a `bound_owner_id` cross-workspace rejection invariant. It is the last DynamoDB credential table, but retiring it is a real migration (Secrets Manager has no compare-and-swap), not a table drop. Separate spec required.
- **`marketplace-payout-accounts` → `marketplace-sellers` merge.** UNSAFE. The sellers row is written by `upsert_profile` via `put_item` from scratch (`seller_profile.py:212-223`), which would wipe `balance_held_cents` / `lifetime_earned_cents` / `stripe_connect_account_id`. Worse, `handle-index` is `ProjectionType.ALL` and is read by the **unauthenticated** `GET /seller/handle/{handle}` (`marketplace_sellers.py:89-105`) — merging would physically replicate Stripe Connect IDs and seller balances into a public read path. Keep separate.
- **`marketplace-takedowns` fold into `marketplace-listings`.** Blocked by a sort-key type conflict (listings SK is `version:NUMBER`, takedowns SK is `takedown_id:STRING`; DynamoDB allows one SK type per table). Migration cost with no payoff.

## Optional Follow-Up (separate plan, code changes required)

- **`metronome-fast-gate` → `billing-accounts` merge.** Feasible (same PK, same KMS/PITR posture, no cross-table transactions), but requires: (a) rewrite `set_balance` from `put_item` to `update_item` (`metronome_fast_gate.py:139`) — a whole-item `put_item` folded onto the billing row would destroy it; (b) rename the fast-gate `updated_at` staleness clock to `fast_gate_updated_at` to avoid colliding with the billing row's own `updated_at`, updating `is_stale` (`metronome_fast_gate.py:76`) and the webhook staleness guard (`metronome_webhook.py:231`); (c) accept a self-healing lost-update race with the two subscription `put_item` writers (`billing_repo.py:296-315,334-345`). Net: 26 → 21 tables per env if done. Write as its own TDD plan.

## Housekeeping (unrelated orphan, do anytime)

- **`isol8-terraform-locks`** — a Terraform state-lock table holding dev/staging/prod state digests, but there are zero `.tf` files in the repo and CDK is the sole IaC source. Leftover from a pre-CDK era. Verify with the team it's truly dead, then `aws dynamodb delete-table --table-name isol8-terraform-locks`. Not part of the CDK stack, so no code change.
