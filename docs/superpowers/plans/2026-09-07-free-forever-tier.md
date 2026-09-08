# Free-forever Lightbulb — working plan

**Status:** research complete, design not yet approved. Living doc — update as work lands.
**Owner ask (2026-09-07):** give Lightbulb away free to start; remove the free-tier limit;
let people start a business without signing in; containers spin up/down on use plus one
nightly CEO run; power inference from a free LLM tier so we don't take a loss.

---

## 1. Verdict up front

Three of the four premises survive contact with the code. One does not.

| premise | verdict |
|---|---|
| Containers are ephemeral, so free is cheap | **TRUE, and stronger than assumed.** A dormant owner costs **$0.86/mo**, and $0.80 of that is one fixable line. |
| Remove the free-tier limit | **YES — by moving off our inference.** The cap is only needed while we pay for tokens. On the user's own account the worst case is ~$18/mo/owner and there is nothing to cap. |
| No signup required | **POSSIBLE**, but the container-per-visitor part is what costs, not the identity part. |
| Free LLM provider tier powers inference | **DEAD.** No free tier is sized for a fleet, most forbid it, and going non-Bedrock loses all metering. |

---

## 2. Measured facts (2026-08-08 → 09-07, AWS 877352799272)

### Per-owner cost

| state | $/month | note |
|---|---|---|
| **Dormant** (container at desiredCount 0) | **$0.86** | 93% is Secrets Manager |
| Light (8.2% duty cycle, 2 Bedrock-active days) | $4.73 | 8.2% is the *measured* fleet duty cycle |
| Daily-active (always-on, 20 Bedrock days) | $42.68 | |

Dormant breakdown: Secrets Manager $0.80 · EFS+DDB+S3 storage $0.06 · ECS at zero $0.00 ·
Bedrock inference profile $0.00 · CloudWatch $0.00.

**The $0.80 is `lightbulb/{env}/paperclip-agent-keys/{owner}/{agent_uuid}` — one secret PER AGENT.**
A fresh owner gets Friday + Atlas = 2 secrets. 34 of 37 prod owners sit at exactly 2.

### Unit prices (measured, CE cost ÷ CE quantity)

- Fargate **Spot**: $0.01264/vCPU-hr, $0.001388/GB-hr → **$0.018187/live-container-hour = $13.28/mo always-on**
- Fargate on-demand: $0.04048 / $0.004445 (Spot is 31% of on-demand)
- Secrets Manager: $0.40/secret/month
- Litestream S3 requests: $3.94/live-container-month at 3 DBs; $21.24 at the fleet-average 16 DBs

### Bedrock spend per trial owner per ACTIVE day (n=30 owner-days)

| p0 | p25 | **p50** | p75 | p90 | max |
|---|---|---|---|---|---|
| $0.045 | $0.62 | **$1.01** | $1.68 | $2.78 | $2.90 |

Cap is $3.00. Zero days overshoot — the gate's reservation holds. 17% of active days reach ≥90% of cap.

### Account totals, 30 days: $1,692.13

| bucket | $ | |
|---|---|---|
| Amazon Registrar | 313.00 | one-off domain purchases, not run-rate |
| **CI/CD deploy overhead** | **335.84** | see §4 |
| goosetown (not Lightbulb) | ~244 | incl. the entire $35 WAF line |
| Lightbulb fixed platform | ~425 | Fargate $144, Aurora $92, NAT $67, ALB $33, CW $45, ECR $18… |
| **Lightbulb marginal (40 owners)** | **~312** | Secrets $95, litestream S3 $70, Bedrock $100, Fargate Spot $43 |

### Scaling, all-dormant

| free owners | today | after the secrets fix |
|---|---|---|
| 1,000 | $860 | ~$60 |
| 10,000 | $8,600 | ~$600 |
| 100,000 | $86,000 | ~$6,000 |

### Capacity walls, in the order they bind

| # | limit | value | adjustable | binds at |
|---|---|---|---|---|
| 1 | Private-subnet IPs, prod VPC (1 ENI/task) | 727 | yes — add subnets | ~727 **concurrent** containers (≈8,800 owners at 8.2% duty) |
| 2 | Bedrock inference profiles/account | 1,000 | yes | ~1,000 owners |
| 3 | IAM roles/account | 1,000 applied (AWS max 5,000) | to 5,000 only | 5,000 owners |
| 4 | **ECS services per cluster** | **5,000** | **NO** | 5,000 owners — needs cluster sharding |
| 5 | EFS access points per FS | 10,000 | NO | 10,000 owners |

Today: 40 owners, 6 live containers, 86 AIPs, 159 IAM roles (41 per-owner), 74 EFS access points.

---

## 3. Why the free-LLM-tier plan is dead

Read from primary terms, not roundups. **Cerebras removed its free tier ~2026-08-17 (card now required).
GitHub Models was fully retired 2026-07-30.** Any comparison still listing those is stale.

| provider | one key → many users? | free ceiling | trains on tenant prompts? |
|---|---|---|---|
| **Groq** | **Yes** (§3.1 grants it) — but §6.3(d)(iii) bars use "intended to avoid incurring Fees" | **8,000 TPM per org, fleet-wide** | No |
| OpenRouter | Yes, but per-tenant keys is the documented shape | 20 RPM / 1,000 RPD globally | varies upstream |
| Google Gemini | **No** for EEA/CH/UK — Paid Services only | undisclosed | **Yes, with human reviewers** |
| Mistral | **No** — accounts may not be used outside your entity | ~1 RPS | **Yes, default-on** |
| NVIDIA NIM | **No** — trial bars "serving real end-users" | ~1,000 credits | **Yes, mandatory** |
| Cerebras / Together / HF | no free tier / $0 capacity | — | — |

Groq's 8,000 TPM is roughly **one long agent turn per minute for the entire fleet**. Our own data:
a first agent run burns 42% of a $3 Bedrock day. These tiers are sized for one developer.

**Structural blocker:** a non-Bedrock provider bypasses the bedrock-gate entirely. `config.py:166-168`:
*"BYO-key and ChatGPT-OAuth: no gate at all… these bounds are the ONLY thing between a stuck loop
and their bill."* Moving free users off Bedrock trades a metered fence for no fence, on the exact
cohort least deserving of trust.

### What replaces it

**The user's own account.** `chatgpt_oauth` is already shipped, is 3 user actions (click → sign in →
type an 8-char code), costs us **zero**, has no fleet rate limit, no ToS problem, no training exposure
we own. **All 4 paying Lightbulb customers already use it. Zero paid owners use Bedrock.**

It is blocked for free users by three hard-codes, not by any missing capability:
- `container_provision.provider_choice_from_row:27-38` — `trial_local` returns `BEDROCK_TRIAL_CHOICE` unconditionally
- `provision_gate._evaluate_provision_gate_inner:592-604` — trial rows short-circuit before the provider check
- `billing_repo._VALID_PROVIDER_CHOICES:1007` — won't store anything outside `{bedrock_claude, byo_key, chatgpt_oauth}`

Also proven: a **custom OpenAI-compatible provider needs no image change** — a patched
`build_provider_block` emitting a custom `baseUrl` + `api: openai-completions` + `models[]`
validated against the vendored upstream zod schema, exit 0.

**OPEN:** per-tenant free rails beyond ChatGPT OAuth — GitHub Copilot OAuth (schema lists
`github-copilot` as a transport; large installed base), OpenRouter provisioning keys, Gemini
free key. Agent investigating.

---

## 4. Cost fixes independent of the free tier

| # | fix | saving | risk | status |
|---|---|---|---|---|
| **C2** | **`cdk.out` handoff as ONE tarball instead of 6,376 loose objects.** `Build-Synth` uploaded with `aws s3 cp --recursive`; each of 39 `Assets-*` jobs pulled the whole tree back. Bucket keys off ⇒ every GET is also a KMS Decrypt. 89 MB/6,376 objects → 33 MB/1 object. | **~$309/mo** | low, CI-confined | **PR #2356 open, not merged** |
| C4 | Collapse the 2 paperclip-agent-key secrets into 1, or move them off Secrets Manager | $0.40/owner/mo → **$80k/yr at 100k owners** | low | not started |
| ~~C1~~ | ~~S3 Bucket Keys on the CDK assets bucket~~ | ~~$224/mo~~ → **$0.04 after C2** | **would silently drift** | **REJECTED** |
| ~~C3~~ | ~~Lifecycle policy on `gha-cdk-out/`~~ | ~~small~~ → $0.53/mo, ~$0 after C2 | same drift | **REJECTED** |
| ~~C5~~ | ~~DynamoDB CMK → AWS-managed~~ | ~~$290/mo~~ → **$0.65/mo** | 58 prod tables | **REJECTED**, hypothesis disproved |

**Why C1/C3 are rejected (verified against the deployed template):** the `CDKToolkit` stack hardcodes
`BucketEncryption` with **no `BucketKeyEnabled` property and no parameter for it**, and
`LifecycleConfiguration` likewise. A manual `put-bucket-encryption` or lifecycle rule is drift — the
next `cdk bootstrap` silently resets it, no error, and the cost quietly returns. SSE-S3 is worse:
`SSEAlgorithm: aws:kms` sits outside any condition. The only clean route is forking the bootstrap
template forever. After C2 the line is **four cents**, so the risky prod-infra edit buys nothing.

**Deploy cadence — checked, nothing wrong.** 400-run sample since 08-08: 318 push / 82 dispatch,
150 cancelled (38%), but **19 of 20 sampled cancellations executed ZERO jobs** — superseded while
pending in the `lightbulb-deploy` concurrency queue, which costs nothing.

C5 is recorded becauseC5 is recorded because it was greenlit on my wrong hypothesis and stopped by its own gate.
DynamoDB's CMK is 0.31% of KMS volume; the CDK bucket is 99.6%.

---

## 5. Correctness / security findings surfaced en route

Independent of the free tier. Ranked.

| # | finding | evidence |
|---|---|---|
| S1 | **Cross-tenant EFS read is open.** Task role has `elasticfilesystem:ClientRootAccess` on `Resource: "*"` with no `AccessPointArn` condition; no `FileSystemPolicy` exists anywhere in the repo; every container shares subnets with the mount targets, 2049 open. The access point chroots the *mount*, not a userspace NFS client. | `container-stack.ts:267-276, 323-331, 372-380` |
| S2 | **The free-tier nightly CEO run has never fired.** 14 days of prod: `operator.connect.stopped_container_wake` = **305 blocked, 0 woken**. `nightly_ceo.arm` = armed 9, not_entitled 5,595. | CloudWatch, measured |
| S3 | **There is no reaper.** TTL on 4 ephemeral tables only. `account_wipe` is manual, admin-only, personal-only, and its own docstring lists what it fails to clean. Slugs are reserved permanently with no delete path. | `account_wipe.py:57-77`, `database-stack.ts` |
| S4 | **There is no WAF on Lightbulb.** `list-web-acls` returns only goosetown ACLs in both scopes. | measured |
| S5 | Container egress is unrestricted (`allowAllOutbound: true`), one NAT, one shared egress IP for all tenants incl. paying ones. `exec: mode=full` for every tier with no branch. | `container-stack.ts:195-203`, `network-stack.ts:39`, `config.py:516-554` |
| S6 | `routers/teams/_guards.py:36-40` is stale — asserts no operator path wakes a container; #2341 falsified it 2026-09-06. | |
| S7 | `usage_service` prices only `amazon-bedrock/*`, so every non-Bedrock owner records `spend 0` — which is now *all four paying customers*. | `usage_service.py:86-115` |

---

## 6. What removing each limit actually costs

### The 3-day window (`trial_end`)
Removing it is **cheap and high-value**: it is what currently blocks all 305 nightly-CEO wakes.
Two independent implementations must move together — `trial_cap._is_expired` (Python, backend) and
`preflight.isTrialExpired` (JS, in the gate Lambda). Removing one side only = split brain.

**Watch:** `should_arm_nightly_ceo` is the one reader that *starts* spend rather than stopping it.
Its code comment justifies arming free owners because "the run is still cap-gated." Remove the cap
too and that justification is void — every dead account gets a daily autonomous run forever.

### The $3/day cap (`CAP_MICROCENTS`)

**The cap only matters if WE pay for inference.** This is the hinge of the whole design
(owner's observation, 2026-09-07 — it collapses the problem):

| free tier runs on… | our worst case per owner | needs a cap? |
|---|---|---|
| **our Bedrock** (today's `bedrock_trial`) | **$1,700-3,400/day** | **yes — it is the only fence** |
| **their own account** (`chatgpt_oauth`) | **~$18/month always-on** | **no** |

$18/mo = Fargate Spot $13.28 + litestream S3 ~$4 + secrets $0.80. Bounded by construction:
`maximumPercent: 100` means one task per owner, so it cannot be multiplied. Upper bound $42/mo
if Spot capacity forces on-demand.

Derivation of the $1,700-3,400 (so it is auditable): Sonnet at $3.3/$16.5/$0.33 per Mtok; a
representative turn at the 200k context bound = 200k cache-read + 20k input + 4k output = **$0.198**;
concurrency 4 main + 2 subagent = 6 lanes; 6 x 2,880 turns/day at 30s/turn x $0.198 = $3,421/day
($1,710 at 60s/turn). Every dollar is Bedrock tokens billed to us. On the user's own account our
share is $0 and the abuser exhausts their own provider quota.

**So: the answer to "remove the limit" is not a bigger cap. It is to move the free tier off our
inference.** Then there is nothing to cap. That is three hard-codes (see §3), not a new fence.

If the free tier DOES stay on Bedrock, the cap must stay. For reference: it is the only fence —
`ledger.mjs:60` is the sole denial in the gate's reservation, `index.py:941` the sole trigger for
the meter kill, trial owners are never ingested to Metronome, and `build_owner_bedrock_policy` is
deny-only so the gate is the only door. `CAP_MICROCENTS` lives in **three** places
(`trial_cap.py:45`, meter `index.py:123`, `bedrock-gate-stack.ts:178`).
**`RESERVE_ESTIMATE_MICROCENTS` is NOT trial-only** — `metronome_gate.py:47,66,143` and
`routers/billing.py:939-943` use it for the paid low-balance threshold. Do not delete it with the cap.

### The axis that stays unbounded either way: NAT egress

$0.045/GB processed + $0.09/GB out, **no cap anywhere in the system**. A container has
`exec: mode=full` and `allowAllOutbound: true`. A 1 vCPU task sustaining ~100 Mbps is ~1 TB/day
= **~$135/day**, on the single shared NAT whose IP every paying customer also egresses through
(`network-stack.ts:39`, `natGateways: 1`). Free inference does not touch this. It is the real
remaining cost risk of an unlimited free container, and it is a bandwidth problem, not an
inference one. Blocklisting of the shared IP is the collateral risk.

## 7. No-signup: what it actually requires

### Identity — easier than expected
A self-minted guest JWT cannot pass `get_current_user` (Clerk JWKS only, no HMAC branch). **But an
identical mechanism already exists in production**: `paperclip_proxy._verify_paperclip_session:782-798`
verifies a self-minted HS256 JWT against `PAPERCLIP_SERVICE_TOKEN_KEY`, discriminated by a `kind`
claim, and builds an `AuthContext` from its claims. The WS Lambda authorizer already accepts three
token families, two of them non-Clerk. `hook_token` (`agent_builder.py:103-121`) is the cleanest
template: owner named in the path, HMAC-compared against that owner's row.

### Migration — the trap
Guest→Clerk "claim" cannot rename: ECS service name, EFS access point, Bedrock AIP (no rename op),
Secrets Manager, S3 litestream prefix, IAM role, and — worst — **Paperclip's Better Auth email, which
is written at signup and never updated**. A literal migration is a copy of ~20 DynamoDB tables plus
recreation of every AWS resource.

**Avoid it entirely:** make `owner_id` stable and have Clerk *bind* to it rather than replace it.
One indirection in `resolve_owner_id` instead of rewriting every store.

### Blockers
- **No email → no Paperclip company → no Friday/Atlas.** `user_provision.py:758-770` hard-returns.
  A guest needs a synthetic email.
- **DANGER:** `is_platform_admin_email` (`core/auth.py:280-303`) grants platform admin to any JWT
  whose email ends `@isol8.co` or `@thelightbulb.company`. A synthetic guest email on those domains
  is a prod-admin grant.
- IAM role name: `lightbulb-prod-owner-bedrock-` (29 chars) + `guest_<uuid4>` (42) = 71 > the 64-char limit.
- Push notifications silently drop for any owner id not starting `user_` (`connection_pool.py:1526-1529`).
- No abuse controls exist (S4, S5) and no reaper exists (S3).

---

## 8. Open decisions (owner)

1. **When does an anonymous visitor get a real container?** On first real intent, on page load, or
   keep a one-tap signup? (Asked; owner wanted to clarify first.)
2. ~~What replaces the $3/day cap?~~ **RESOLVED 2026-09-07:** nothing — move the free tier to the
   user's own inference account and the cap becomes unnecessary. Keep it only for any owner who
   remains on our Bedrock.
3. **Which per-tenant free inference rail?** Pending the provider audit.
4. ~~C1 bucket key~~ **RESOLVED: rejected.** C2 (PR #2356) supersedes it. Decision needed instead:
   **merge PR #2356?**

## 9. Work not yet started

- C4 — collapse the per-agent secrets
- S1 — scope the EFS IAM grant to the owner's own access point + add a FileSystemPolicy
- S3 — build a reaper (the prerequisite for any free tier at scale)
- Delete the 3-day window (both implementations)
- Unblock `chatgpt_oauth` for `trial_local` (3 hard-codes)
