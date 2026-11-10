# Legacy per-owner runtime inventory (2026-11-10)

Read-only. Taken 2026-11-10 02:56Z with `--profile isol8-admin --region us-east-1`:
`bedrock-agentcore-control list-agent-runtimes` / `get-agent-runtime`, a strongly consistent
`dynamodb get-item` of each owner row and `__session__#<owner>` row (plus a scan for
`__session__#<owner>#task#*` rows), and CloudWatch `AWS/Bedrock-AgentCore` `Invocations` and
`MemoryUsed-GBHours` over 2026-11-03 02:56Z to 2026-11-10 02:56Z. Nothing was changed.

## Summary

- **29 runtimes**: 2 pools, 25 legacy prod, 2 legacy dev. All 29 are `READY`.
- **Every owner row still names its own legacy runtime** in `agentcore_runtime_arn`. None has
  `agentcore_shared_runtime_arn`. Since #5170, invocation ignores that field and goes to the pool
  (`runtime_transport.runtime_arn`), but teardown, roll and `provision_runner` still read it.
- **Every legacy runtime was invoked in the last 7 days.** Most had their last invocation in the
  15:56Z hour on 2026-11-08, which is the fleet roll (16:11Z to 16:41Z). Four were invoked later.
- Session rows: 4 owners have a session estimate on a pool. The rest have one on their legacy
  runtime, about 34 h stale. There are no live wake leases and no task session rows.
- **The allowlist starts empty** (`apps/backend/scripts/legacy_runtime_retirement_allowlist.json`).
  No runtime passes both rules: "the owner row has moved to the pool" and "no activity in 7 days".

## Why each runtime is excluded

All 27 fail the same two rules:

1. **The owner row still points at the runtime.** The script refuses this. A separate,
   reviewed change must first move the saved route (SET `agentcore_shared_runtime_arn` to the
   pool, REMOVE `agentcore_runtime_arn`). That change is not trivial, because
   `provision_runner` calls `prepare_hermes_workspace` again when `agentcore_runtime_arn` is
   absent. Teardown (`hermes_provisioning.py`, `workspace_wipe.py`, `account_wipe.py`) and the
   roll also key off that field. Codex's proposal designs this change as a CAS on the
   captured owner id and old ARN.
2. **There were invocations in the last 7 days.** On activity alone, the earliest dates a
   runtime can qualify are: most on 2026-11-15 ~17:00Z; `org_3LLrip…`, `org_3JeBYN…` on
   2026-11-15 ~19:00Z; `org_3LkIi…`, `org_3H3vi…` on 2026-11-16 ~00:00Z; dev `org_3LgB9…` on
   2026-11-16 ~22:00Z.

## Per-owner IAM roles: kept

Each legacy runtime runs as `lightbulb-{env}-owner-bedrock-{owner}`. The pools have their own
roles (`lightbulb-{env}-hermes-pool`). The per-owner role is still used after its runtime is
gone. The AppSync handler (`api-stack.ts` `OWNER_ROLE_PREFIX`) and an owner's AWS-connector
trust are pinned to it. The roll and provisioning re-ensure it (`ensure_owner_bedrock_role`),
and its memory and browser policies are attached to it. The retirement tool never deletes it,
and the retirement role has no `iam:` actions.

## Also kept

All S3 profiles, memory and skills; the 28 Hermes boot identity seeds in Secrets Manager
(network identity custody has no replacement yet); gate tokens; and every owner row field.
The tool never calls the workspace teardown seam.

## Inventory

Times are UTC. "Last" is the start of the last non-zero hourly bucket.

| Runtime id | Owner | Owner row route | Status | Last updated | Session estimate on | Invocations 7 d | MemoryUsed GB-h 7 d | Verdict |
|---|---|---|---|---|---|---|---|---|
| `lightbulb_prod_hermes_pool-4EZsNl7K5Z` | POOL (never touched) | - | READY | 2026-11-09T23:12Z | - | 341 (last 11-09 03:56Z) | 15.41 | - |
| `lightbulb_dev_hermes_pool-Fou7GHFIx9` | POOL (never touched) | - | READY | 2026-11-09T18:51Z | - | 40 (last 11-09 20:56Z) | 4.61 | - |
| `lb_prod_org_3LkIiMhS9Z6WTMLsjY3oDTS_c8340730b745-R3nTavAjpo` | `org_3LkIiMhS9Z6WTMLsjY3oDTSFA76` | own legacy ARN; shared field absent | READY | 2026-11-08T16:41Z | none | 358 (last 11-08 22:56Z) | 79.53 (last 11-08 22:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3LLripXRHvPP7kVkUw0gfg1_6eaac8781fba-FO8IAI6RJ8` | `org_3LLripXRHvPP7kVkUw0gfg12EXa` | own legacy ARN; shared field absent | READY | 2026-11-08T16:40Z | own legacy | 576 (last 11-08 17:56Z) | 153.84 (last 11-08 17:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3L7DwtWzR0czxV3x6Puie68_f2b983243520-rn8Hjv5F3D` | `org_3L7DwtWzR0czxV3x6Puie68uF5g` | own legacy ARN; shared field absent | READY | 2026-11-08T16:39Z | own legacy | 991 (last 11-08 15:56Z) | 282.94 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3L6VCxVc7xKDtqMYWBeMTy3_a4761f84486c-gYlaKV7l27` | `org_3L6VCxVc7xKDtqMYWBeMTy3GIo3` | own legacy ARN; shared field absent | READY | 2026-11-08T16:38Z | own legacy | 903 (last 11-08 15:56Z) | 329.63 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3Ky71K6irEjbVXn7DsXS6fB_0268d1c52774-Xk0Pz7Fo9f` | `org_3Ky71K6irEjbVXn7DsXS6fB4vem` | own legacy ARN; shared field absent | READY | 2026-11-08T16:37Z | own legacy | 873 (last 11-08 15:56Z) | 349.38 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3Kxx5eskDOyfl8aDhCGZGDN_47d989d62cc8-AegL36DVCp` | `org_3Kxx5eskDOyfl8aDhCGZGDNa2iw` | own legacy ARN; shared field absent | READY | 2026-11-08T16:35Z | pool | 1454 (last 11-08 15:56Z) | 432.58 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3KwRO97imbvPHkuSAg2QS64_5665b7aa8779-O0KvvzEA8s` | `org_3KwRO97imbvPHkuSAg2QS64UduF` | own legacy ARN; shared field absent | READY | 2026-11-08T16:34Z | own legacy | 1700 (last 11-08 15:56Z) | 494.19 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3KvWVXPfCtZccRBATik2zba_f96c95cf68e1-1i4V419ZuX` | `org_3KvWVXPfCtZccRBATik2zba75pS` | own legacy ARN; shared field absent | READY | 2026-11-08T16:33Z | own legacy | 826 (last 11-08 15:56Z) | 463.97 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3KtNll0g8R2JPnJ0yUrxAb8_06390d2a9cf1-H6sUcb20Wd` | `org_3KtNll0g8R2JPnJ0yUrxAb8Exac` | own legacy ARN; shared field absent | READY | 2026-11-08T16:32Z | own legacy | 1916 (last 11-08 15:56Z) | 500.76 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3KKzCl2TJ26hOVz3LxbPS1K_6641266ca21f-Kh8j3G8Poh` | `org_3KKzCl2TJ26hOVz3LxbPS1KU2Zp` | own legacy ARN; shared field absent | READY | 2026-11-08T16:31Z | own legacy | 1069 (last 11-08 15:56Z) | 346.90 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3KIYf0bMfMZdXquw98gVLvo_2668ac174e1f-IDRQuMH45V` | `org_3KIYf0bMfMZdXquw98gVLvoWWkC` | own legacy ARN; shared field absent | READY | 2026-11-08T16:29Z | own legacy | 317 (last 11-08 15:56Z) | 189.73 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3KF6EmGn0neBg2sjKKzvClB_1bc6e40aff35-9fXXPW6vH8` | `org_3KF6EmGn0neBg2sjKKzvClB01jQ` | own legacy ARN; shared field absent | READY | 2026-11-08T16:28Z | own legacy | 335 (last 11-08 15:56Z) | 107.11 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3KB1sRmSYg4lOdpeAyQVqR7_0d89f04e20e3-91OrmBDn90` | `org_3KB1sRmSYg4lOdpeAyQVqR7hLYY` | own legacy ARN; shared field absent | READY | 2026-11-08T16:27Z | own legacy | 22 (last 11-08 15:56Z) | 2.11 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3K1uYfqun7Mx6TYNhFn4V1S_4f70ef2d9811-gUD14o8N40` | `org_3K1uYfqun7Mx6TYNhFn4V1S1xxm` | own legacy ARN; shared field absent | READY | 2026-11-08T16:26Z | own legacy | 21 (last 11-08 15:56Z) | 1.66 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3Jshao85twVsEky1nB16fVW_39163e4caf9e-Y3k3FDHzim` | `org_3Jshao85twVsEky1nB16fVWqFeW` | own legacy ARN; shared field absent | READY | 2026-11-08T16:24Z | own legacy | 864 (last 11-08 15:56Z) | 341.46 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JqJGTgPBLK6k4811qXsni2_d51e191485f7-Ufuz6xAfwB` | `org_3JqJGTgPBLK6k4811qXsni2PBxh` | own legacy ARN; shared field absent | READY | 2026-11-08T16:23Z | own legacy | 314 (last 11-08 15:56Z) | 158.75 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JeBYNcluAbDg7irxRzGd7L_74b308e61499-m2lzyOEoYc` | `org_3JeBYNcluAbDg7irxRzGd7L2kK4` | own legacy ARN; shared field absent | READY | 2026-11-08T16:22Z | own legacy | 951 (last 11-08 17:56Z) | 274.71 (last 11-08 17:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JQzwxxXWvUVaz8DkFfd3WB_6420fd46ba45-UgSVMCFI3X` | `org_3JQzwxxXWvUVaz8DkFfd3WBdYqb` | own legacy ARN; shared field absent | READY | 2026-11-08T16:21Z | own legacy | 336 (last 11-08 15:56Z) | 96.41 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JPJXXmFmAG6c2jmE9ixQPH_6763149e7233-rJ1E6xAAOF` | `org_3JPJXXmFmAG6c2jmE9ixQPH6DtQ` | own legacy ARN; shared field absent | READY | 2026-11-08T16:20Z | own legacy | 23 (last 11-08 15:56Z) | 1.76 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JL4yREh34gkakAVsZ2GKsp_8ee5630fb92a-bRqmElGJ7m` | `org_3JL4yREh34gkakAVsZ2GKspTdr0` | own legacy ARN; shared field absent | READY | 2026-11-08T16:18Z | own legacy | 113 (last 11-08 15:56Z) | 63.52 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JL4wv7ZyE2dH3mXMu1hAao_735b3d312084-mEcB3x8GoK` | `org_3JL4wv7ZyE2dH3mXMu1hAaoY3AI` | own legacy ARN; shared field absent | READY | 2026-11-08T16:17Z | own legacy | 23 (last 11-08 15:56Z) | 2.02 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JL4wBfaU6dBQJ91aJgs9wm_a814b988eb52-azar8oB8JG` | `org_3JL4wBfaU6dBQJ91aJgs9wm0PHr` | own legacy ARN; shared field absent | READY | 2026-11-08T16:16Z | own legacy | 1982 (last 11-08 15:56Z) | 507.56 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JKSgGBDHBKMpj1RDCRJgld_fc8fe8fd3a96-srV0YZDY3M` | `org_3JKSgGBDHBKMpj1RDCRJgldnh0h` | own legacy ARN; shared field absent | READY | 2026-11-08T16:15Z | own legacy | 1547 (last 11-08 15:56Z) | 511.60 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3JCsS1jjkMfABApmwvSwN3b_7e8cf9fdfcd4-LMmcth5Rf2` | `org_3JCsS1jjkMfABApmwvSwN3b112c` | own legacy ARN; shared field absent | READY | 2026-11-08T16:14Z | own legacy | 23 (last 11-08 15:56Z) | 1.76 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_prod_org_3H3viGAfq2SXc9dIdcatmGX_c42489f74b24-YAXCVADapT` | `org_3H3viGAfq2SXc9dIdcatmGXg8Td` | own legacy ARN; shared field absent | READY | 2026-11-08T16:12Z | pool | 10621 (last 11-08 22:56Z) | 716.61 (last 11-08 22:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_dev_org_3LgB9twuJbYCjGILfOL5JjgQ_f8f05093f713-kgLk4H85tO` | `org_3LgB9twuJbYCjGILfOL5JjgQV0p` | own legacy ARN; shared field absent | READY | 2026-11-08T16:12Z | pool | 4503 (last 11-09 20:56Z) | 447.71 (last 11-09 20:56Z) | excluded: row points at it; invoked in 7 d |
| `lb_dev_org_3L8FQUPIdpUFHcptlCH3a4iS_9165151be497-qTKeqe4l1B` | `org_3L8FQUPIdpUFHcptlCH3a4iScaO` | own legacy ARN; shared field absent | READY | 2026-11-08T16:11Z | pool | 32 (last 11-08 15:56Z) | 2.48 (last 11-08 15:56Z) | excluded: row points at it; invoked in 7 d |

## Running the retirement once a runtime qualifies

1. Land a reviewed PR that adds its exact ARN to the allowlist under its environment.
2. One time only: Actions, "Retire legacy Hermes runtimes", `mode: bootstrap`. This deploys the
   scoped role `lightbulb-retire-legacy-runtimes`. Its IAM can reach only `runtime/lb_{env}_org_*`,
   never a pool.
3. `mode: plan` (the default) is a read-only dry run. It uploads a receipt with a verdict per ARN.
4. `mode: apply` deletes only if every ARN in the list passes every refusal. Otherwise it
   deletes nothing and exits 2.
