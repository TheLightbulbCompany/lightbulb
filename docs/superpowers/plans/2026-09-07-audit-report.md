# Complexity & Cost Audit — Final Report

**Scope:** whole repo, 11 dimensions, 62 proposed findings → 13 refuted by adversarial review → **49 survive**. Every claim below was checked against files on disk, not CLAUDE.md.

---

## 1. Verdict

**Mostly justified.** Nine of eleven dimensions came back "this is scar tissue, leave it": the gateway veto ladder, `trial_cap.py`, `provision_gate`, `config_patcher`, the two money Lambdas, and `containers/config.py` (2,828 lines of which only ~784 are executable — 66% is dated incident prose) are all correctly built for what they survived. Frontend duplication measures **0.54%** (731 of 134,440 lines). The premise of an over-abstracted codebase is a false positive.

What is actually wrong is narrower and more specific: **second copies of things that already exist elsewhere**, made across a process or module boundary where no test can see both. Four of them have already drifted in production, and one — `MetronomeFastGate.is_blocked` — is a live trap sitting one import away from the correct predicate at the wrong threshold. The other real problem is **$/month**, and it is not where anyone was looking: a 1000× KMS step-change on 2026-08-18 that nobody noticed now runs ~11–18% of the entire AWS bill.

Net: roughly **1,900 lines to delete**, **~$400/month to recover**, and **~12 lines of test** to close the class of bug that already cost you ten days of wrong billing. Almost nothing here is "refactor the architecture."

---

## 2. The structural causes

Five root patterns account for 45 of the 49 findings.

### P1 — The second copy across a boundary no test spans (14 findings)
The dominant pattern. A shape gets copied because the two sites are in different languages, different Lambda assets, or different modules — and Python's test suite can only see one side.

- Money constants: `rates.mjs` (4th copy of the rate card, **on the settle path**), `CAP_MICROCENTS` ×3, `RESERVE_ESTIMATE_MICROCENTS` ×2.
- Onboarding defaults: `readiness_state.py:141` hand-copies `paperclip_repo`'s 25-key dict and is **missing 4 keys today**.
- Paperclip client construction: `update_service._build_paperclip_provisioning` is line-for-line `paperclip_provisioning.build_paperclip_provisioning`, trap comment included.
- The three WS Lambdas: the same 26-line secret fetcher three times, 234 lines total.
- Five hand-rolled inline-edit state machines in `components/teams/`, one of which already carries a stale-prop guard the other four lack.

The tell: **every instance that has drifted, drifted at a boundary a test could not cross.** Where Python can see both sides (meter ↔ backend rate table, gate-token single-writer, START_REFUSED ↔ trial_cap codes), the lockstep tests exist and the copies agree.

### P2 — The guard that was copied into 3 of 4 callers (4 findings)
A protocol is invented once, hand-inlined at each site, and the newest site is missing it.

- `billing_service`: the per-owner claim + pending-marker double-charge protocol is inlined into flat, mobile-web and ultra checkout, and **entirely absent from team checkout** (L746) — the only path that mints a two-line-item subscription.
- `websocket_chat.ws_message`: the session-id validation is byte-identical in all three message branches; a fourth branch shipping without it is the actual failure mode.
- The hook-token constant-time compare exists three times (`agent_builder`, `agent_mail`, `hooks_proxy` inline).

### P3 — The leftover whose sunset condition was already met (11 findings)
Migration scaffolding that outlived the migration. The rename shipped; the widening did not get narrowed back.

- `table-names.ts` `table/*-{env}-*` — the leading `*` was for the isol8→lightbulb window. That window closed (0 tables remain on `isol8-*`), the code's own comment says "narrow at step D", and I confirmed live that the wildcard **grants the Lightbulb backend full CRUD on 28 GooseTown tables**.
- Two DynamoDB IAM statements in `service-stack.ts` fully subsumed by a wildcard added in August — #2103 deleted the statements it subsumed and left these two only because tests assert on the sid.
- `legacyTablePrefix` — zero call sites, imported by nine files.
- `x-isol8-ws-internal` header shim, whose docstring carries its own deletion precondition (met: five prod promotions since 2026-08-31, zero senders remain).
- `usage_event.py` + the AIP reverse-resolvers, orphaned by PR #846 in June.
- 194 lines of HTML-stub code in `paperclip_proxy.py` superseded by living siblings.

### P4 — The comment that outlived the code beneath it (7 findings)
Distinct from P3: the code moved, the comment did not, and the comment is now *actively misleading about a money or safety path*. Section 5 treats these individually — this class already caused a ten-day billing bug.

### P5 — Env-blind account-level resources duplicated per stage (3 findings)
Dev and prod are one AWS account. Anything account-scoped and instantiated per-stage runs twice and mails the same inbox twice: a $1,500 account-wide Budget with no CostFilters, and the GuardDuty + Access Analyzer EventBridge rules. The file's own comments three lines away establish the ENV-BLIND → PROD-GATED rule; these three just missed the sweep.

---

## 3. Do these first

Ordered best value-to-risk. Everything above the line is same-day.

| # | Where | Change | Payoff | Risk |
|---|---|---|---|---|
| 1 | AWS console / Cost Explorer | **Investigate the 2026-08-18 KMS step-change** — $0.30/mo → $180/mo, now trending ~$290/mo. Find what started calling `Decrypt`/`GenerateDataKey` per-request rather than per-boot. | **~$290/mo**, 11–18% of the bill | none (read-only) |
| 2 | `core/billing/metronome_fast_gate.py:89-122` | Delete `is_blocked` + `get_remaining` (zero production callers — verified) and rewrite the module docstring. `is_blocked` returns `remaining <= 0`; the *live* predicate `metronome_gate._row_blocked` returns `remaining <= RESERVE_ESTIMATE_MICROCENTS`. | -35 lines; **removes a live trap** — any future door reaching for the obvious-looking method admits an owner the gate will 429 | low |
| 3 | `apps/infra/lambda/bedrock-usage-meter/test_index.py` | Add `assert index.CAP_MICROCENTS == trial_cap.CAP_MICROCENTS`, and regex `CAP_MICROCENTS: "(\d+)"` + `RESERVE_ESTIMATE_MICROCENTS` out of `bedrock-gate-stack.ts`. Add `apps/infra/lib/stacks/bedrock-gate-stack.ts` to `backend.yml` paths. Delete the stale `2_600_000` comment in `bedrock-gate-stack.test.ts:75`. | ~6 lines; closes the **kill/restore/admission desync** class | low |
| 4 | same test file | Parse `apps/infra/lambda/bedrock-gate/rates.mjs` and assert `RATES == bedrock_pricing._RATES`, `TIER_TO_FM == bedrock_aip.TIER_TO_FM`. Add `apps/infra/lambda/bedrock-gate/**` to `backend.yml` paths. **Do not** put this in vitest — `infra-ci.yml` does not run on backend PRs, so it would fire after the drift merged and dev auto-deployed. | ~20 lines; the **4th rate-card copy is the one on the settle path** (`debit.mjs:41` → `trialSettle`/`paidSettle`) and the only unguarded one | low |
| 5 | `CLAUDE.md:481, 770, 1036` + the diagram at 425-433 | The header is **`X-Isol8-Public-Host`** (verified: `api-stack.ts:207`, `main.py:148`, `paperclip_proxy.py:13`). CLAUDE.md names `X-Lightbulb-Public-Host`, which exists in no file. Fix all four and add it to the residue list. | 4 lines; stops every future agent grepping a header that does not exist | none |
| 6 | `core/billing/usage_event.py` (whole file), `bedrock_pricing.price_from_aip_arn`, `bedrock_aip.resolve_*_from_aip_arn` ×3 | Delete. `UsageEvent`'s docstring cites `LedgerBackend`, which does not exist in the repo. **Keep** `persist_owner_aips`/`_put_reverse_row` (the meter reads those rows) and **keep** `price_from_tier` (the lockstep oracle). | -150 prod, -200 test lines | low |
| 7 | `apps/infra/lambda/bedrock-gate/spike/` (6 tracked files) | Delete `server.mjs`, `client.mjs`, `invoke-client.mjs`, `package.json`, `package-lock.json`. Keep `SPIKE-RESULTS.md` — `proxy.mjs:3-6` cites it. | -950 lines | none |
| 8 | `routers/paperclip_proxy.py:266-342, 800-916, 1126-1194` | Delete `_provisioning_response`, `_bootstrap_html`, `_decode_clerk_publishable_key` + its 2 tests, and `POST /__provision__`. The route is *live* but has zero consumers, ignores `resolve_owner_id`, and creates a personal company keyed on `auth.user_id` for an org caller — **the exact org-scoping bug `ea66ac447` deleted `_autoprovision` for.** It was missed by that sweep. | -285 lines; closes an un-gated, non-single-flight door onto `provision_org` | low |
| 9 | `service-stack.ts:806-849, 1154-1174` + `table-names.ts:24, 55-81` | **Two commits.** (a) Delete `DynamoDbIntegrations` + `DynamoDbConnections` (exactly subsumed by `DynamoDbOwnedTables`, wider actions, same role) and `legacyTablePrefix` + its 9 imports — zero-risk, no AWS state. (b) Separately, drop the leading `*` from `allTableArns`/`bothTableArns`. Leave the five suffixes behind as a comment. | ~1.1KB of the 10,240-byte policy budget; **revokes backend CRUD on 28 GooseTown tables** | (a) low, (b) **medium** — live prod IAM narrowing, failure mode is a runtime AccessDenied found by a customer. Verify `list-tables` at *promotion* time, not merge time. 3 test assertions break: `integrations-resources.test.ts:168/211`, `credential-export-decouple.test.ts:176`, `service-stack.test.ts:265` |
| 10 | `core/services/billing_service.py:296-419, 692-742, 993-1046` (and `746` where missing) | Move claim → build → release-on-failure → pending-marker → expire-on-marker-failure **inside** `_build_subscription_checkout` with one `pending_provider_choice` kwarg. | -75 lines; **team checkout gets double-charge protection by construction** rather than by remembering | medium — it is the checkout path; land behind the full test suite |
| 11 | `observability-stack.ts:2243-2283` and `3040-3075` | Wrap `MonthlyBudget` in `if (this.envName === "prod")`; early-return `createAccountHardening` unless prod. Both are account-wide, un-filtered, and mail the same `admin@` inbox twice. Update `observability-stack.test.ts:283` (`Budgets::Budget` count) and `:279` (`Events::Rule` 5 → 3 dev, add a prod assertion — that enumeration is a deliberate guard, not a stale count). | halves every budget-threshold and GuardDuty email | low |
| 12 | `core/services/readiness_state.py:141-163` | Hoist the defaults to `ONBOARDING_STATE_DEFAULTS` in `paperclip_repo`. The copy is **already missing 4 keys** (`first_contact_state`/`_attempts`/`_reason`, `friday_agent_id`) and `6b42f427e` (#1820) already paid the lockstep tax once. Every extra key is provably inert on the wrapper's consumers. | -22 lines; ends the drift. Note the row-*present* return at `paperclip_repo.py:660` is a third copy this does not unify | low |

Deferred but real (13–20, all low-risk, ~500 more lines): the WS-Lambda triple, `_res_error`/`_ws_error` helpers in `websocket_chat.py` (-85), the `useInlineEdit` hook (-100), `update_service._build_paperclip_provisioning` (-30), the two dead admin Server Action files (-187), `user_provision`'s doubled member-add (-13), `build_provider_block`'s dead `user_id` param (which today receives **two different identities** from its two callers — `user_id` vs `clerk_user_id` — a silent org/user mixup waiting for whoever reaches for it).

---

## 4. Unnecessary cost

| Item | $/month | Assumption | Provenance |
|---|---:|---|---|
| **KMS request volume** — 1000× step-change on 2026-08-18, $0.30 → $180, now trending up | **~$290** | Cost Explorer actuals; cause not yet identified. Likely a per-request `Decrypt` where a per-boot one would do. | Cost-dimension AWS pull; **no root-cause finding yet — investigate before acting** |
| **219 per-agent Secrets Manager secrets** ($0.40/secret/mo) | **~$87** | These are a re-mintable cache whose plaintext already sits unencrypted on EFS, so the secret buys no confidentiality it does not already lack. | Cost-dimension AWS pull |
| **litestream sync-interval** S3 PUT volume | **~$68** | Grows **per agent hired**, not per owner — so it compounds with exactly the metric you want to grow. The 1s→10s fix already landed; this is the residue. | Cost-dimension AWS pull |
| **CloudWatch custom metrics**, 679 identities | **$14.23** | Metrics are **prorated by the hour they receive data** — a naive $0.30/metric gives $204 and is wrong. Reported at true value. | Verified against the bill |
| ↳ of which `admin_metrics.py` middleware, 206 identities, **read by nothing** | ~$4 | Delete the file (76), its `main.py:739` registration, and its 281-line test = 357 lines. Or keep `admin_api.errors` and drop the `endpoint` dimension (206 → 4 identities). | Finding, conf 0.93 |
| `update_service.run_scheduled_worker` — 2 full-table scans + a GSI query every 60s, both envs, forever | **<$5 today** | `pending-updates` is small now. The cost is the growth curve, not the bill: 5,760 scans/day scaling with table size. `status-index` already exists (`get_due_scheduled` uses it) — query it for `applying`/`pending` and keep the staleness check in Python. Two lines per call site. | Finding, conf 0.85 |

**Total recoverable: ~$450/month**, of which ~$290 needs a root-cause investigation first and ~$155 is straightforward. Against an $1,600–2,600/mo bill, that is 17–28%.

Two things I want to flag as *not* costing what people assume: log volume is fine (total CloudWatch Logs ingestion ~0.3 GB/month) and polling intervals and retry loops are already well-tuned. Do not go hunting there.

---

## 5. Lying comments

Highest priority class — a comment in this repo is load-bearing documentation, and a wrong one is worse than none. Ordered by blast radius.

| # | Promise | Holds? | The one test that would enforce it |
|---|---|---|---|
| 1 | `trial_cap.py:36-56`: "Lockstep with the meter Lambda's copy **and** the gate stack's `CAP_MICROCENTS` env — moving one alone desynchronizes kill/restore/admission." Echoed in `bedrock-usage-meter/index.py:117-123` and `bedrock-gate-stack.ts:176-181`. | **Values agree today; nothing enforces it.** `preflight.mjs:51,54` read both from env with **no fallback and no tie to the backend**. | `assert index.CAP_MICROCENTS == trial_cap.CAP_MICROCENTS` in `test_index.py` (already hard-imports the backend, already runs on backend PRs) + regex the two literals out of `bedrock-gate-stack.ts`. |
| 2 | `bedrock-gate/rates.mjs:4-6`: "`rates.test.mjs` asserts this table is byte-equal to the meter's by parsing `index.py`." | **False — `rates.test.mjs` has never existed** (`git log --diff-filter=AD` returns nothing). Worse: `vitest.config.ts` only includes `test/**/*.test.ts` and no workflow runs `node --test` there, so `core.test.mjs` and `gate.test.mjs` next to it are also un-run in CI. A file placed where the comment says would have been dead on arrival. | Parse `rates.mjs` from `test_index.py`; `bedrock_pricing._RATES` has inline `#` comments so strip them before parsing, or shell out to `node -e`. |
| 3 | `billing_repo.py:1245-1250`: "The Bedrock invocation-log meter Lambda is the **sole writer** of this counter… two writers would double-count." Mirrored at `connection_pool.py:1714-1719`. | **False.** The bedrock-gate settles first; the meter counts only gateway misses; the shared `requestId` dedup table is what makes exactly-once hold. Confirmed: `add_trial_daily_spend` has **zero production callers** — the meter reimplements it in raw boto3 at `index.py:596`, and the gate writes it from JS. | Rewrite to name both writers and the dedup table. Then delete the uncalled function — three test files patch it, two to assert it is never called, so with the function gone that guard becomes structural. |
| 4 | `connection_pool.py:3686-3712`: "Direct callers (`trigger != 'grace'`) deliberately stop despite a live connection, fenced by the `has_active_runs`/`is_parked` vetoes." | **Reversed.** Commit `9213918ad` (#1670) added an unconditional locked re-check at L3742 that fires for *every* trigger on a superset condition, silently making the earlier rationale false and vetoes 4–5 outcome-redundant. Both `has_active_runs` and `is_parked` can only fire when a local connection exists, so they were never the fence. The two tests named for them (`test_connection_pool_scale_to_zero.py:161-187`) **pass for the wrong reason** and never reach the lines they are named for. | Correct the docstring and the L3696-3703 block. **Do not change the code** — the skeptic refuted that half. |
| 5 | `bedrock_aip.py:714-756` + `bedrock_enforcement.py` header: `revoke_owner_bedrock` "removes the per-owner role's scoped Bedrock inline policy so the container can no longer invoke any model"; the gate-token deny is described as a "best-effort backstop" that "must NOT break the IAM revoke, which is the primary fence." | **Inverted.** `build_owner_bedrock_policy` is now deny-only (`del aip_arns, regions`; a single Deny on `CreateInferenceProfile`, no Invoke grant), so `delete_role_policy` removes an escalation Deny and no grant. `deny_gate_token` is the actual fence. | Comments only. Correct three docstrings; rename `skip_bedrock_grant` → `skip_bedrock_policy`. **Keep the IAM half** — it still bites in break-glass direct mode. |
| 6 | `CLAUDE.md` ×4: the host-dispatch header is `X-Lightbulb-Public-Host`. | **False** — verified: it is `X-Isol8-Public-Host` in `api-stack.ts:207`, `main.py`, `paperclip_proxy.py` and their tests. | Zero code change. Fix the four occurrences and add it to the residue list. |
| 7 | `routers/teams/schemas.py:19-40`: "every router that forwards a body to Paperclip **must** call `model_dump(..., by_alias=True)`." | Holds — 36/36 sites comply. But it is an obligation nothing enforces. | Add `serialize_by_alias=True` to `_Strict`'s `ConfigDict` (verified against installed pydantic 2.12.5). **Keep the 36 `by_alias=True` kwargs** — deleting them makes the wire shape implicit and opens a failure mode that does not exist today. Rewrite the paragraph rather than deleting it. |
| 8 | `routers/container.py:82-85`: `_TRIAL_REFUSAL_GATES` maps refusal reason → Gate. | Holds, but it is a one-use dict duplicating `provision_gate.gate_for_trial_reason`, whose key sets are provably identical (pinned by `test_ecs_manager.py:4263`). | Delete the dict + 4 imports, call `gate_for_trial_reason`. Note there is **no test covering `START_REFUSED_EXPIRED` at this door** — add one line to `test_container_status.py`. |

Also flagged and worth repeating: the repo **already corrected** the metronome-retry lie at `billing_service.py:1108-1113` ("which is FALSE for a personal owner with a running container") while leaving the sibling at `user_provision.py:266` uncorrected. Two comments disagree about the same mechanism, and the stale one sits on the path that broke.

---

## 6. Deliberately NOT recommended

Load-bearing. If a future audit flags any of these, this section is the answer.

**`core/services/trial_cap.py` — the whole module.** There is deliberately **no cap-only check primitive**. One existed; four separate doors reached for it and checked only half the predicate, waking EXPIRED trials. All 8 production doors were traced (`ses_inbound`, `container` ×2, `websocket_chat` steer, `container_recover` ×2, `teams/_guards`, `ecs_manager.start_user_service`) — every one calls the whole predicate, and the single expiry-only caller documents why. **This is a success case. Leave it.**

**`connection_pool.py` vetoes 7–9.** They look like a redundant re-check of vetoes already passed. They are deliberate **re-evaluations at a different point in time** — under `_lock`, after the awaited liveness read. `all(...)` evaluates once, at one instant, and would reopen exactly the TOCTOU windows those lines close. The comments around them are wrong (finding #4 above); the code is right.

**`containers/config.py`'s seven `provider_choice` switches.** Not duplication. Each answers a different question with a deliberately **non-symmetric** answer — `chatgpt_oauth` returns `None` from `cheap_model_ref` but a real model from `heartbeat_model_ref`, and that asymmetry *is* the fix for Codex native compaction. `heartbeat_model_ref` already delegates. A provider table would move ~10 lines of code and 300 lines of rationale, and destroy the per-branch anchoring of that rationale.

**`_size_for_owner`'s agent-count branch** (`config.py`). Looks like a billing signal leaking into sizing. It is a **reliability floor** — an 11-agent prod owner crash-looped on boot 2026-08-14 when 24 SQLite DBs restored concurrently on one vCPU.

**Never cache a container IP** (`ecs_manager.discover_ip`). A cached dead IP caused a prod outage where the pool showed "connected" and every RPC timed out.

**The bedrock-gate/meter/backend overlap.** Rate card ×4, `CAP_MICROCENTS` ×3, `TIER_TO_FM`, the kill path, the `connectionType` derivation. This is **forced by process boundaries** — the gate has no Python import path, the meter has no VPC. Add lockstep *tests* (§3 items 3–4); do **not** consolidate the copies, and do **not** build a shared `rates.json` artifact — it would have to be copied into three separate deployment images.

**`_revoke_owner_iam` in the meter Lambda.** Vestigial in normal mode (`build_owner_bedrock_policy` is deny-only, so the "revoke" deletes a Deny). Its comment documents that it still bites in **break-glass direct mode**, and that mode cannot be proved impossible. Leave it.

**The bedrock-gate-tokens liveness predicate.** Expensive and self-described as "inert". It was already wrongly deleted once. It stays.

**`metronome_fast_gate.set_balance`'s UPDATE-not-`put_item`.** Protects the bedrock-gate's live `reserved_microcents` hold from being clobbered. Genuine scar tissue — delete `is_blocked`/`get_remaining` around it, not this.

**`auth-stack.ts`'s `make(id, prefix)` and its `${logicalId}Lightbulb` suffixes.** They look exactly as vestigial as the dead `AuthSecrets` interface next to them. They are the opposite: `secretName` is a **replacement** property, so the isol8→lightbulb move had to be add-before-remove. Collapsing them destroys and regenerates **all seventeen live secrets.** Delete the interface; do not let the edit wander into `make()`.

**`table-names.ts`'s `logGroupPath`/`tablePrefix` pattern.** Adding `secretPath` alongside them is safe, but it dedupes the prefix (which has never drifted and fails loudly at boot) rather than the secret *name* (which can drift silently). And a slip inside `auth-stack.createSecret` is **destructive**, not merely broken. Low value; if done, scope it to the ~12 real Secrets Manager paths and never to the ECR repo names, the SSM path (leading slash is load-bearing), or the IAM ARN wildcards.

**`efs_file_lock` / `.isol8-guard` / `.isol8-protocol`.** Renaming them is a once-only migration across every EFS volume with a fatal failure mode and zero benefit. The `fcntl.lockf`-not-`flock` choice is NFS compatibility, not preference.

**`_extract_thinking_text` / `_extract_assistant_text`.** Near-duplicates. Merging saves ~8 lines behind 3 new parameters. Not a win.

**Frontend `@lightbulb/shared` wrapper hooks** (`useAgents`, `useGatewayRpc`, `useApi`, `useChatGPTOAuth`) and the provider-choice UI. Already consolidated; the thin wrappers are real platform bindings for the mobile split, not wrap-for-nothing.

**`statusContract.ts`/`BootstrapState` type mirrors, `PER_USER_CPU` vs `PER_USER_TASK_RESOURCES`, `MODEL_MAX_TOKENS`, `RESERVE_TTL_SECONDS`, and the six "$3/day" frontend strings.** Checked explicitly and found fine — drift is loud or cosmetic, and centralizing marketing prose is the abstraction, not the fix.