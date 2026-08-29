# Open-Issue Reconciliation — 2026-08-15

**What this is:** every one of the 47 open GitHub issues reconciled against the current codebase.
The codebase was treated as authoritative; issue text as historical claims.

**Verification basis:**
- `origin/main` @ `e2d69ebee` (all backend/frontend/infra claims)
- Paperclip fork `~/dev/paperclip-fork` @ the deployed pin `57d4d68e` (dev.tag == prod.tag in `paperclip-version.json`)
- OpenClaw fork `~/Desktop/agent` @ `v2026.7.2-isol8.4`, the pinned base in `openclaw-version.json`
- Method: 10 domain agents (one per subsystem, ~5 issues each), each reading the full issue +
  comments and grepping the code; every "fixed"/"superseded" verdict then attacked by an
  independent adversarial agent instructed to refute it. 15 agents total. **2 of 5 fixed-claims
  were refuted and are reported as still-open below.**
- No issue was closed, commented on, or edited. This document is the only output.

---

## Executive summary

| Verdict | Count | Issues |
|---|---|---|
| **Fixed / superseded — close** | 4 | #1203, #1481, #312, (#1498 mostly — see its entry) |
| **Refuted "fixed" — still open in a new shape** | 2 | #1241, #1498 (remainder) |
| **Still valid — small fix, do now** | 9 | #1486+#1483, #1619, #1734, #1778, #1788, #1646, #1482, #1468 |
| **Still valid — schedule** | 10 | #1735, #941, #959, #974, #1474, #1677, #1645, #1776, #743, #1064 |
| **Partially fixed — precise remainder recorded** | 7 | #1500, #1593, #1497, #1786, #1272, #1469, #970 |
| **Product decision — needs your call** | 9 | #944, #966, #987, #976, #1276, #1470, #1204, #1205, #1206 |
| **Valid backlog, not urgent** | 5 | #751, #979, #1478, #1113, #1196 |
| **Needs a live check code can't settle** | 2 | #964, #1795 |

Headline answers to the questions that prompted this:
- **#1276 (`company.isol8.co`) is NOT done.** Nothing is broken (the adapter backfill closed the
  live impact), but the hostname is still the Better Auth origin identity in both CDK stacks and
  every coupling point in the issue is intact. It's a deliberate-swap decision, not drift. REVIEW COMMENT: We tried removing this but it broke auth, so maybe this isnt as easy as you think it is? 
- **#1474 (orgs always Bedrock) is NOT done.** The decision was recorded and never implemented:
  an org admin can still switch to ChatGPT OAuth or BYO from Settings and `POST /billing/provider`;
  `routers/oauth.py:11` still documents org ChatGPT support; disconnecting ChatGPT even drops an
  org into a three-way provider picker.

---

## A. Close — evidence-verified

### #1203 — Onboarding "aha" demo + How-It-Works rewrite → **FIXED**
Both workstreams landed in the redirected form your own comment specified (Polsia-style autonomous
first-run, not a guided demo): the two-path funnel IS the landing hero (`HeroFunnel.tsx:24-32`),
the brief survives signup (`useLandingBriefReplay.ts`), `BRIEF.md` is staged into Friday's
workspace pre-boot (`onboarding_brief.py`), the kickoff is server-owned
(`bootstrap_dispatcher.py`), and "How It Works" no longer exists anywhere in the frontend — the
funnel-first landing replaced it. Survived adversarial re-check. One soft nit: hero copy still
says "Integrated Management Environment", the technical register the issue disliked. 

### #1481 — Routine auto-pause survives budget reset → **FIXED in the deployed fork**
Fork commit `a0fdf3f07` names this exact defect in its message. A wake-gate refusal is now
classified `provider_quota` (`execute.ts:206-229` matches `details.reason == "wake_admission_gate"`
first), the run parks with a retry at the next UTC midnight instead of finalizing failed, the
agent isn't flipped to error, and the circuit breaker only counts issues reaching
blocked/cancelled/done — which a parked run never does. The fix commit is an ancestor of the
deployed pin (`57d4d68e`, both envs). Survived adversarial re-check, including the
"does the breaker actually stop counting two layers away" attack.
Caveat worth one line in the close comment: the fix is classification-based, not the
`budgets.getInvocationBlock` fence the issue proposed, and `websocket_chat.py`'s docstring still
describes the old behavior.

### #312 — gstack eng-agent in per-container isolation → **SUPERSEDED**
Nothing from Phase 1 was ever built (zero `eng-agent` hits repo-wide), and most premises are
stale (wrong base image, dead modules, renamed methods; its org-wide `ANTHROPIC_API_KEY` shared
container directly conflicts with the bedrock-gate deny-only enforcement model). Meanwhile gstack
itself shipped by a different route: a 5-agent `agentcompanies/v1` company package deployable
through the marketplace company-deploy engine (`company_deploy_service.py`, `format="company"`
listings), with per-agent budgets and per-owner isolation — most of what Phase 1 wanted.
Close with a pointer at the company-package rail.

---

## B. Refuted "fixed" claims — still open, reframed

### #1241 — First-run kickoff can persist "started" without delivery → **DEFECT SURVIVES, WORSE SHAPE**
The client-side hook the issue names was deleted (server-dispatched kickoff replaced it), and the
first-pass agent judged it superseded. The adversarial pass overturned that: **the server
inherited the same defect class with less recovery than the client had.**
- The DDB claim `bootstrap_state: not_started → dispatched` is latched **before** delivery is
  confirmed (`bootstrap_dispatcher.py:459`), then held across a ~3-minute connect-retry ladder
  and the whole first run, with completion observed only by an **in-process** callback.
- If the backend process dies in that window (ECS rolling deploy, OOM, scale-in — no
  `CancelledError` handler, 20s lifespan budget), **no failure is recorded**: the row sticks at
  `dispatched`, nothing reads `bootstrap_dispatched_at` for staleness, there's no lease TTL or
  sweeper, and the admin reset lever 409s because it's fenced on `state == "failed"`. Recovery
  requires a hand-written DDB item.
- Severity is arguably *higher* than the issue rated itself: the kickoff message is now the
  user's own brief; `funnel_state` never completes; `delete_pending_brief` never runs.
**Action:** re-title #1241 to "stuck-`dispatched` bootstrap claim has no staleness recovery" (or
close it and file fresh). Fix shape: a staleness sweep on `bootstrap_dispatched_at` + let the
admin reset lever accept stuck-`dispatched` rows.

### #1498 — Provider switch collides with onboarding save → **ROOT CAUSE FIXED, TWO REMAINDERS**
The collision the issue names is genuinely gone — #1511 made `POST /onboarding/brief` intake-only
(put-if-absent DDB write, always 200s, server-owned retried fulfilment; every client posts to that
one endpoint; reprovision is generation-fenced downstream). But the refuter found two real leftovers:
1. The issue's second explicit ask — split the undifferentiated "Couldn't save that just yet" —
   shipped on web (`PERSISTENT_FAILURE_THRESHOLD = 3` escalates to upstream detail + support link)
   but **not on mobile** (`apps/mobile/.../FirstRunFunnel.tsx:255` still generic, still running a
   deploy-skew retry loop marked for deletion).
2. A provider switch during onboarding can still kill the *kickoff* one stage later, silently —
   the same stuck-`dispatched`/terminal-`failed` hole as #1241, with `bootstrap_state` unread by
   any user-facing surface.
**Action:** close the collision half; keep a small mobile-parity item; fold remainder 2 into the
#1241 reframe (same fix).

---

## C. Fix now — small, verified, live

### #1486 + #1483 — chat-error alarming is decorative → one PR fixes both
Verified mechanism: `put_metric` emits exactly ONE EMF dimension set (`metrics.py:62-71`), every
`chat.error` emitter adds `reason` (13 sites), the gateway's main branch also adds `reclassified`
— while W10 and both ORR widgets bind bare `{env, service}` (`observability-stack.ts:565,738-766`).
The metric identity W10 queries **has never existed**; with `NOT_BREACHING` it can never fire.
The denominator (`chat.message.count`) IS dimensionless, so the math expression half-works,
deceptively. **The requested audit also found two MORE dead alarms the issues didn't know about:**
- **W8** `gateway-rpc-error-count` binds `{env,service}`; sole emitter adds `method`. Dead.
- **W2** `container-lifecycle-latency-p99` binds `{env,service}`; both emitters add `op`. Dead.
Fix shape (one PR): uniform dimension set on every `chat.error` emitter, rebind W10/W8/W2 and the
widgets to match (or emit a dimensionless aggregate in `put_metric`), and add an infra test that
ties alarm `dimensionsMap` to emitter dimensions — no such test exists today.

### #1619 — Admin type-to-confirm dead under autofill
Unchanged since before the incident; `autoComplete="off"` was already present in April and
demonstrably didn't prevent it. `typed === confirmText` compares React state Chrome never updates;
button stays disabled; a click dispatches nothing. **One component gates nine destructive
surfaces** (4 container actions + billing, provisioning, agents, moderation, takedown), so a
single fix (read the DOM value on submit / `onAnimationStart` autofill detection) covers all.

### #1734 — Meta credentials into CloudWatch via httpx URLs
Both call sites still pass secrets as GET query params: the per-owner Instagram token
(`instagram_cli.py:59-63`, every connect) and the platform `client_secret`
(`oauth_dance.py:49-65,234`). No httpx logger quieting, no redaction filter anywhere in the
backend. The in-repo Google materializer already does it right (Bearer header, `gog_cli.py:65-68`)
— copy that pattern, plus `getLogger("httpx").setLevel(WARNING)`. Rotation of the already-logged
secrets is the live remainder.

### #1778 — Marketplace safety-scan snippets, unauthenticated
Chain verified end-to-end: 80-char raw source snippets → manifest → S3 **and** DDB `manifest_json`
→ returned verbatim by unauthenticated `GET /listings/{id}` with `Cache-Control: public`, **and**
by the default (non-compact) anonymous search which returns full DDB rows. The fix's premise
holds: seller-preview and admin surfaces build flags from a live scan, so dropping `snippet` from
the persisted copy blinds nobody entitled.

### #1788 — Assignee picker assigns the wrong agent
Claim A confirmed outright: zero keyboard handling — no `onKeyDown` anywhere, so ArrowDown/Enter
literally cannot reach `handleSelectAgent` (also inaccessible: no combobox role). Claim B: the
issue's guessed mechanism (blur resets filter) is NOT in the code; the real code-visible hazards
are (i) unstable list order — `orderedAgents` memo calls impure `getRecentAssignees()`
(localStorage) keyed only on `[agents]`, so an SWR revalidation reorders rows under the pointer;
(ii) stale `query` survives close-without-select; (iii) popover portalled to body from inside a
modal dialog. Fix keyboard nav + stable ordering + query reset together.

### #1646 — Mobile marketplace, free hires only
Nothing landed since #1643's blunt removal. The server-side entitlement gate exists
(`marketplace_listings.py:779-789`), so a client-side free-only filter can't be abused. Open
design point from the issue is still open: search payload has no entitlement flag, so a price-only
filter hides paid listings the user already owns.

### #1482 — Budget-exhaustion leftovers → half fixed, one line left
CreditsStep half is done (shared `isCreditsExhausted` predicate both sides). The org settings chip
still renders raw `subscription_status` ("Past_due") at `settings/page.tsx:463-467`;
`statusChipLabel` is already computed at :373 and consumed only by the personal card. **The fix is
a one-line swap.**

### #1468 — Runtime provisioning failure invisible to the owner (P1)
Root-cause half landed (#1669 persists `expires_at`, with the workspace write still conditional so
pre-#1669 token rows keep the old behavior). The actual ask is untouched: `bootstrap_last_error`
is composed into the status contract, shipped on the wire, typed in shared hooks — **and rendered
by exactly one surface: the admin page.** Nothing owner-facing branches on
`bootstrap.state === "failed"`. The stepper now has an honest terminal-error contract for
container-level failures, so the remaining gap is precisely the bootstrap/kickoff class. Fix:
render `bootstrap.last_error` in the owner UI. Pairs naturally with the #1241 reframe.

---

## D. Security — schedule

### #1735 — `CLERK_AUDIENCE` unset; `aud` verification off
All three facts reproduce: default `None`, zero CDK wiring, decode passes it straight through so
PyJWT skips the check; the boot check warns instead of raising (its neighbor raises — asymmetry
intact, and a unit test *pins the warn behavior as intended*). **Adjacent surface the issue didn't
name:** the WS Lambda authorizer sets `"verify_aud": False` ("Clerk doesn't use audience"), so an
aud rollout must touch it too or the WS door stays open. Mitigant: `issuer` IS enforced per-env.

### #743 — Admin re-harden remainder
(b) cross-tenant patch is fixed (`require_platform_admin` on both patch routes). Sharpest open
piece: `@audit_admin_action` infrastructure now exists and is used across `routers/admin.py` —
but `routers/updates.py`, home of the **fleet-wide config patch**, has zero audit decorators and
no confirmation header. (c) debug-env allow-list is a latent fail-open (staging is a legal env
value with no staging stage deployed). (d) no app-layer rate limiting; blunted by the API-GW stage
throttle and a now-local-only `/health`.

### #970 — DCR race
Now a **single-site fix**: state binds `client_id` (landed) but `exchange()` still re-reads
`client_information` from the last-writer-wins `oauth_app_store` (`oauth_dance.py:268-270`), and
`oauth_state_repo.put` has no field to bind it to. Materializers no longer read the store
themselves, so binding the JSON at `start()` closes it entirely. Still P2/self-healing.

### #959 — mcp.servers bypass
API-side largely closed (strict per-entry validation, org-admin gate, reconcile-after-write so
curated entries always win). Remainder: agents can still hand-write `mcp.servers` via exec
(`exec.mode = "full"` by design), nothing prunes or caps non-integration entries (a 10-server
hand-written set survives every reconcile and reprovision), and MCP health still isn't surfaced. REVIEW COMMENT: I think its good that agents can do this themselves, is this a real issue? 

### #941 — Unvalidated openclaw.json
Core defect unchanged: `_locked_rmw`'s only pre-write check is JSON serializability. The zod
schema **exists in-repo but only validates the generator output in CI** — nothing validates the
merged runtime config, including `apply_deploy_aux_mutations`, which deep-merges **untrusted
marketplace seller slices** through the same unvalidated path (that's the sharpest angle). Boot
`doctor --fix` landed but is non-fatal and not a schema scrubber. Mitigations since: patch
key-validation (prototype-pollution/control chars), strict manual-MCP API. REVIEW COMMENT: is this in our backend or the contianer, Im not sure what this means

---

## E. Partially fixed — the precise remainder

### #1500 — ChatGPT OAuth seed single-use on reprovision
The necessary-but-insufficient half landed (`expires_at` persisted for post-#1669 grants; legacy
rows still refresh-on-import). Everything the issue called the actual fix is absent: **no
consumed-seed stamp, no container→backend token write-back, and Connected still means "a row
exists"** (`has_active_oauth` deliberately never touches the secret; `/oauth/chatgpt/status`
returns row-decryptable). Net behavior today: a wipe-and-reprovision more than ~an hour after
connect still yields a dead container under a green UI. No Reconnect affordance exists. REVIEW COMMENT: Ideally our backend doesnt really worry about this as this is all openclaw driven right? 

### #1593 — Desktop Mac node auth
The forever-403 loop is fixed (#1623: per-attempt self-minted EdDSA device token, the exact
pattern the issue asked for). Three named failure paths survive: node **startup** is still
webview-gated (`start_node_host` only reachable from `send_auth_token`'s success branch — no
Clerk resolution ⇒ the whole desktop-node surface never starts, just quietly now); the
webview-token fallback path still exists (`"no pair response yet"`); and repeated connect failures
are still invisible — the UI renders the pill only for the positive case, nothing reports to the
backend. REVIEW COMMENT: Is this still true? I thought we fixed the node stuff recently

### #1497 — SQLite locked on EFS
The issue body's mechanism is superseded (O6 moved state+agent DBs onto ephemeral ext4; WAL
restored; only workspace remains on EFS). The 08-14 comment's revised root cause — CPU starvation
on over-limit tenants — got its fix in #1791 (box sized by live agent roster; docstring cites this
incident). The wasted-litestream contributor was also fixed (quarantine DB unlisted). Remainder:
`OPENCLAW_SQLITE_BUSY_TIMEOUT_MS` was never raised (5s hardcoded upstream), and the affected
tenant needs a live retest before closing. REVIEW COMMENT: We dont want to raise this, we are waiting for an upstream fix for this right? Is this still recent I thought we did the litestream stuff

### #1786 — Container Chromium never starts
**Defect 1 was fixed and deployed nine days before the issue was filed** (#1622:
`ensure_xdg_dirs` creates+chowns `.config` etc. on every config write — that placement IS the
fleet backfill; the crashpad-flag revert is guarded by a test). Defect 2 (`tools.browser: {}`) is
per-owner on-disk drift from an old generator — current code unconditionally emits the full block,
and the operator fix already exists (`reapply_owner_config`). Action: run reapply for the affected
owner(s), live-verify Chromium, then close. 

### #1272 — Meta umbrella
Instagram publishing + Meta Ads hosted-MCP shipped on the shared-app rail; WhatsApp was never
built (dead QR scaffolding still present); the ad-approval gate doesn't exist in code (only
Meta's own campaigns-land-paused). The code-side remainder is literally two gate lines per
provider (`environments: [local, dev]` + allowlist) that App Review approval unlocks — so this
umbrella stays open exactly as long as #1795 does.

### #1776 — meta-webhooks rebase
Branch is one commit, now 265 behind. **Not a duplicate of what landed meanwhile:** main's new
`meta_webhook.py` (singular, #1767) is event ingress; the branch's `meta_webhooks.py` (plural) is
the deauthorize + data-deletion `signed_request` callbacks — complementary, collision-prone
naming. Main also already serves data-deletion *instructions* (#1502), which Meta accepts as an
alternative to the callback — so the branch's unique irreplaceable piece is the **deauthorize
callback**. Measured rebase cost: 3 conflicting files, all small (one is a two-sided signature
addition to `put_status`).

### #1469 — Onboarding-campaign cleanup list
Items 1, 2, 4, 5 unchanged (BOOTSTRAP_EMAILS over-protection — now duplicated in THREE places;
Aurora-vs-DDB wipe skip; stale backlog-default comments; layout sanitizer roster check). Item 3
is down to doc-vs-code hygiene (the unsafeMetadata reads no longer gate anything). Item 6 appears
fixed (org path renders the full styled stepper).

### #1064 — Paperclip signup idempotency
The orphan-creation vector is closed (adopt-existing via stored credential, persisted *before*
the chain so mid-failure retries adopt; member path mirrors it) and a `duplicate_email` metric
fires and records failure durably. Remainder: **no alarm on that metric** (zero infra hits), no
scan for >1 Aurora row per email, recovery is still the manual script.

---

## F. Product decisions to resurface (nothing to fix until you decide)

| # | Decision on the table | Sharpest new fact |
|---|---|---|
| **#1474** | You already decided (orgs = Bedrock, full stop). Implement it or reverse it — today all three provider cards show for orgs, `POST /billing/provider` has no org restriction, and a ChatGPT disconnect drops an org into a 3-way picker. No admin override for the Isol8 org exists, so implementing as-written needs that escape hatch first. | The one unnamed path: `clear_provider_choice` on disconnect → org-facing picker. |
| **#1276** | Swap the Better Auth origin off `company.isol8.co` (lockstep CDK pair + backfill), or accept it as a vestigial identifier. | `NEXT_PUBLIC_COMPANY_URL` has zero references — the sidebar link it existed for is gone, so part of the proxy host route is already dead weight. Dispatch hosts are derived from the env var (not hardcoded), making the swap cheaper than the issue assumed. |
| **#944** | Accept impact-only Paperclip alarming (and write the blind spot down) vs add an availability signal. | The FIS experiment that produced the original evidence was deleted (#1740); cheapest real signal is a Route53 check in the existing `createUptimeChecks`. |
| **#1470** | Move provisioning trigger off the frontend poll? | The claimed in-flight branch doesn't exist; design docs survive only on `docs/event-triggered-provisioning-design` @ `29fe82daa`. `try_claim_provision` already makes duplicate triggers safe, weakening the urgency. `CLERK_WEBHOOK_SECRET` is wired end-to-end, so the receiving side is deployable today. |
| **#966** | Owner-facing debug surface: build or drop. | The motivating MCP-OAuth gap was solved another way (`mcp_oauth` materializer), and the allowlisted-RPC pattern is already proven by the owner-facing `doctor.memory.*` Dreams panel — if built, it's more `doctor.*` RPCs, not a terminal. |
| **#987** | OSS-extract the credential layer? | The layer grew to 11 materializers + 3 new rails since filing, several deeply Isol8-specific — extraction is a bigger lift than when proposed. Pure strategy call. |
| **#976** | Paperclip plugins as primitive? | The outcomes it wanted (governance, marketplace bundles, budgets) all shipped via Isol8's own rails + `agentcompanies/v1`; the plugin-specific blocker is still a literal no-op in the fork (`ensurePluginAvailableForCompany = async () => {}`). Mostly answered by events. |
| **#1204** | Multi-key failover. | Cross-provider failover is structurally impossible as wired (exactly one credential env var reaches the container). Storage already supports multiple keys; the gap is scalar `provider_choice` + injection + switch logic. Model-level same-provider fallbacks exist and are error-only. |
| **#1205** | Inference-cost strategy. | The issue's framing is overtaken: the $50 no longer bundles inference at all; routing/caching/overhead levers shipped in July. The only live remainder is "BYO as the default funnel path" — pure product call. |
| **#1206** | Agent-roster folders/sort. | The 10-agent plan cap (#1791) materially weakens the "doesn't scale" premise; the named surface moved (`WorkspaceSidebar`, not `ChatLayout`); the "Ben's glitches" half is unverifiable and the hiring flow has been rewritten 5 times since. |

## G. Valid backlog, not urgent

- **#751** chat activity trace — fully specced, zero built; note the checked-in test that asserts
  the exact behavior to be removed lives in the parallel `tests/` tree (#979's trap).
- **#979** dual test trees — every condition intact; 354 vs 25 files, five duplicated basenames,
  `tests/` still untypechecked, **frontend CI still runs no vitest at all**.
- **#974** multi-CRM — prerequisite `platform_app` rail has since SHIPPED (proven on 3 providers),
  so the blocker is gone; but the issue's BOOTSTRAP.md delivery leg is dead (deliberately disabled
  fleet-wide + the seeded file was verified never-read and deleted). Needs a redesign of the
  delivery half. **Live defect found:** `atlas_seed/files/AGENTS.md:69` advertises
  hubspot/salesforce/pipedrive click-to-connect skills that don't exist — Atlas will
  confidently send owners to connect phantom CRMs. Worth a one-line seed fix regardless.
- **#1478** host X ourselves — untouched; the xurl `$HOME`/pin findings were discharged into
  infra. Gate on the live check: does any owner have X connected at all?
- **#1113** cdk.out artifact quota — fully intact (38 download steps, no retention cap); the org
  budget/retention halves are GitHub settings, checkable via `gh api`.
- **#1196** multi-instance piece 2 — entirely unbuilt; `desiredCount: 1` with in-process RPC/run
  state; the design doc still exists in-tree.
- **#1645** Teams hidden tasks — every blocking premise byte-identical in the deployed fork pin;
  the written plan still applies verbatim, fork-first deploy order included.
- **#1677** wake staleness — marker still has exactly one writer; `start_user_service` is
  explicitly no-forceNewDeployment; agent-mail and restart doors both blind. The status-poll door
  DOES heal (routes through `dispatch_provision`), which is why drift is intermittent.

## H. Needs a live check code cannot settle

- **#964** Lobster cron — the tool contract still hard-errors on all three inputs and the flat
  undiscriminated schema (the blamed cause) is unchanged in the pinned fork, so the *trap* is
  real; whether that specific prod cron still exists/fails needs the owner's container logs.
  Runtime has changed massively since (2026.5 → 2026.7.2).
- **#1795** Meta App Review — the render logic **proves** "No Pages found" means Meta answered
  200 with an empty list (error and loading states render differently; the backend lister cannot
  silently drop entries), so the lister-bug hypothesis is effectively ruled out; the remaining
  diagnosis is one live `/me/accounts` call with the stored token. Reviewer Clerk id is verified
  allowlisted on both providers. Minor split found: trigger discovery pins Graph `v23.0` while
  OAuth pins `v25.0` (not causal).

---

## I. New defects surfaced by this audit (tracked by no issue)

1. **W8 and W2 alarms are dead** for the same emitter/alarm dimension mismatch as W10 — found by
   running the audit #1486 asked for. Fold into the #1486 PR.
2. **`bootstrap_dispatcher` stuck-`dispatched` hole** — no staleness recovery, admin lever fenced
   wrong; a backend deploy mid-kickoff permanently kills a new owner's first run, invisibly
   (details under #1241). Arguably the most user-hurting find in this audit.
3. **Atlas advertises phantom CRM integrations** (`atlas_seed/files/AGENTS.md:69`).
4. **Mobile funnel parity** — generic error + deploy-skew retry loop marked-for-deletion still
   live (`apps/mobile/.../FirstRunFunnel.tsx`).
5. **`BOOTSTRAP_EMAILS` triplicated** (`paperclip_user_delete.py`, `account_wipe` pre-check,
   `recover_orphan_paperclip_member.py:32`) — narrowing it touches three sites.
6. **Graph API version split** — `v23.0` (trigger discovery) vs `v25.0` (OAuth).

---

## Suggested sequencing (if you want a starting point)

1. **One observability PR:** #1486 + #1483 + W2/W8 + the missing infra test.
2. **One onboarding-resilience PR:** #1241 reframe (staleness sweep + admin lever) + #1468
   (render `bootstrap.last_error`) + mobile parity from #1498.
3. **Security batch:** #1778 (snippet strip), #1734 (Bearer + httpx quiet + rotate), #1735
   (aud end-to-end incl. WS authorizer), fleet-patch audit gap from #743.
4. **Two tiny UI fixes:** #1619 (one component, nine surfaces), #1482 (one line). #1788 alongside.
5. **Decisions pass on section F** — ten decisions, most of which close issues either way.
6. **Closes:** #1203, #1481, #312 now; #1786 and #1497 after their live retests; #1498 after the
   mobile parity line ships (or close and track parity in the #1241 reframe).
