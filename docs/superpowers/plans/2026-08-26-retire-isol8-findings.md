# Retirement sweep findings (consolidated)

> Synthesis of 14 inventory sweeps + 9 adversarial verifications over the
> `2026-08-26-retire-isol8-rebrand.md` program plan. Where a verification
> contradicted its sweep, the verification wins and the correction is marked
> **[V]**. Line numbers are as of `3adc25f2c`; §1.C.9 explains why they must be
> re-grepped at execution time.
>
> Working doc. Delete with the program plan when the retirement ships.

---

## 1. Plan corrections — things the program plan gets WRONG

### C1 — L157 "purge `cdk.context.json:18-20`" is backwards, and the range is wrong

> *"Flip `dev` stage `rootDomain` → `flickmeon.ai`; purge `cdk.context.json:18-20`"*

**Truth:** the entry is `cdk.context.json:18-21` (the closing `}` is line 21), and the P2
action is an **ADD**, not a purge. `dns-stack.ts:17-19` is the single
`HostedZone.fromLookup`, and `deploy.yml:34-38` runs `npx cdk synth` **before**
`Authenticate Via OIDC Role` (`:39-46`) — synth has **no AWS credentials**. Purging the
`isol8.co` key at P2 breaks the *prod* stage's lookup inside the same synth → `Build-Synth`
fails → all 52 dev jobs skip. `infra-ci.yml:41-42` (`permissions: contents: read`, no
`id-token: write`) has the same shape on every PR.

**[V]** The new entry does **not** require a credentialed local synth — `cdk.context.json`
is a committed JSON file whose value is `{Id, Name}`, two strings the plan already knows
(`Z03529981ZCUW1R3FM806`, L45). Hand-add it.

**Two more copies must move in the same commit** (neither is in the plan):
`apps/infra/test/ascii-descriptions.test.ts:77-80` and
`apps/infra/test/ses-inbound-stack.test.ts:21-24`. A partial change makes `fromLookup`
fall through to CDK's dummy zone and every Route53 assertion in
`ses-inbound-stack.test.ts` (`:89,146,154,165,176,361,366`) compares against `example.com`.

**Phase:** P2 add, P3-step-6 purge.

### C2 — L157 "dev deploy creates SES `flickmeon.ai` identity + `*.dev.flickmeon.ai` MX/SPF" is impossible

Three independent reasons, each proven:

1. `isol8-stage.ts:52` — `if (env === "prod") { sesInbound = new SesInboundStack(…) }`. The
   dev stage never instantiates it.
2. Prod stack jobs are `workflow_dispatch`-gated (`app.ts:203` `PROD_PROMOTE_IF`, `:218`
   `jobSettings`, `:313` the `JsonPatch` for `prod-DeployVercelProd`). A push to `main`
   skips all 14.
3. The identity is **zone-derived**, not `mailDomain`-derived:
   `ses-inbound-stack.ts:109-110` `identity: ses.Identity.publicHostedZone(hostedZone)`,
   and that zone is the prod stage's `isol8.co`. No prop flip can produce a
   `flickmeon.ai` identity while prod's `rootDomain` is `isol8.co`.

And *"the rule-set recipients list carries both until P3"* is **not a config value**:
`ses-inbound-stack.ts:413` is `recipients: [mailDomain, \`.${mailDomain}\`]` against a
single `mailDomain: string` prop (`:57`). Carrying both needs an array prop or an
`additionalMailDomains` prop — a code change.

**Decision required before P2 starts:** either (a) a prod dispatch during P2 landing a
`mailHostedZone` prop + widened recipients, or (b) dev agent-email dark for the window —
in which case delete `friday@{slug}.dev.flickmeon.ai` from the P2 E2E list (L162).

**[V] Half-correction to the sweep:** only *inbound* is prod-pinned. The **outbound** side
is per-env (`isol8-stage.ts:73-76`) and live state proves it — `isol8-dev-agent-mail`
config set exists, `isol8-dev-agent-mail-outbound` stack is CREATE_COMPLETE, the
`api-dev` EventBridge API destination is ACTIVE, and 2 of 3 SES tenants are associated to
the **dev** config set. Dev outbound has to move at P2 too.

### C3 — L178 "`isol8/prod/clerk_issuer` = `https://clerk.flickmeon.ai`" is necessary but not sufficient

The WS Lambda authorizer's issuer is a **CDK literal**, not the secret:
`api-stack.ts:353-358` `CLERK_ISSUER: isProd ? "https://clerk.isol8.co" : …`,
`CLERK_JWKS_URL: …`, read at cold start by
`lambda/websocket-authorizer/index.py:37-38` and consumed at `:376`. Two sources of truth.
If only the secret moves, every WebSocket `$connect` 401s → **chat is dead fleet-wide while
REST still works**.

**Phase:** add `api-stack.ts:353-358` to P3 step 1 (the flip PR), same dispatch as the
secret.

### C4 — L149 "already patched there for prod — extend to dev" is wrong on both clauses

Both desktop jobs run the **identical** `jq` filter — dev at
`desktop-build.yml:69-83`, prod at `:256-271` — patching exactly six keys: `.version`,
`.bundle.targets`, `.build.devUrl`, `.build.frontendDist`, `.app.windows[0].url`,
`.plugins.updater.endpoints`. There is nothing to "extend to dev".
`capabilities/default.json:7` is patched by **neither** (`grep -n "jq " desktop-build.yml`
→ only 71, 83, 259, 271, all writing `tauri.conf.json`).

Also drop `pair_state.rs:142` from the `option_env!` list — it is a `#[cfg(test)]` fixture
(`gateway_url: "wss://ws-dev.isol8.co".into()`), not config-driven.

**Restate as:** make `capabilities/default.json:7` injectable (new work), and re-point the
four already-injected keys (existing).

### C5 — L139 "write it in `paperclip_provisioning.py` (owner :462, member :1024)" points at the wrong sites

`:462` is an `email=owner_email` kwarg inside the **`sign_up_user`** call (`:461-465`), not
a row write. **[V]** `:1024` *is* inside a `PaperclipCompany(…)` construction (`:1019-1029`)
— only the owner pointer is wrong.

The four real row constructions are **`:497, :810, :1019, :1091`**, and the repo needs
**three** edits, not one: dataclass field (`paperclip_repo.py` ~`:194`), the `put`
serializer (`:372`), the `_from_item` reader (`:467`).

This matters because `PaperclipRepo.put` is a raw `PutItem`, and the module already carries
a defensive note about exactly this bug class at `paperclip_provisioning.py:140-155`:
*"any onboarding field not set on the `PaperclipCompany` passed to it is simply ABSENT from
the new item, silently wiping a previously-written value."*

The **adopt** branches (`:457` owner, `:981` member) never write the email at all — an
adopted row would get no `paperclip_email` under the plan as written.

### C6 — L139 "read it in `paperclip_user_session.py:58-62`" covers 1 of 6 sign-in doors

`paperclip_user_session.py:59` (`email = await clerk_email_resolver(user_id)`) is the Teams
BFF cold mint only. The other five sign in to Better Auth with a **live Clerk** address:

| site | what it does |
|---|---|
| `routers/paperclip_proxy.py:1480` → `:1500` | HTTP proxy path (**[V]** `:1480`, not `:1479`) |
| `routers/paperclip_proxy.py:1803` → `:1852` | WS relay path (**[V]** `:1803`, not `:1802`) |
| `routers/paperclip_proxy.py:1786` | **[V] MISSED by both** — `email = (claims.get("email")…)` out of the `isol8_paperclip` cookie JWT; the WS relay *prefers* this over the Clerk cookie (`:1771-1789`) and `:1773` says the cookie is "always there" by the time live-events connects |
| `paperclip_provisioning.py:457` | owner ADOPT branch |
| `paperclip_provisioning.py:981` | member ADOPT branch |
| `paperclip_provisioning.py:1034-1038` | owner re-signin inside `provision_member` (invite minting) |

Plus `paperclip_admin_session.py:136` — a **sixth** `sign_in_user`, but for the bootstrap
admin using the Secrets-Manager-stored `{email,password}`; **not** Clerk-bound, so it
survives the human email move, and it is the reason `BOOTSTRAP_EMAILS` must keep the old
addresses forever.

**What breaks if only the plan's one line lands:** an admin whose Clerk primary email moves
can open Teams but **every org member-add for their org fails** at
`paperclip_provisioning.py:1035`, and their own re-provision 401s at `:457`. Better Auth's
`user.email` has no unique constraint (`paperclip_provisioning.py:932-936`), so a
fall-through to sign-UP creates a duplicate user — the
`project_member_join_race_duplicate_better_auth_users` failure, whose sign-in rate limit is
**per-IP** and therefore fleet-wide.

### C7 — L139 "one-shot backfill for existing rows from Clerk" seeds from the wrong source

The Aurora Better Auth `user.email` (`~/dev/paperclip-fork/packages/db/src/schema/auth.ts:6`)
is the actual sign-in truth. It is written at provision time
(`paperclip_provisioning.py:462` owner, `:986` member) and **never updated**. Seeding
`paperclip_email` from Clerk *after* a mailbox move writes an address Aurora does not have
and **cements** the 401.

**Backfill from Aurora:** `SELECT email FROM "user" WHERE id = :paperclip_user_id`.
And it must run **while Clerk still holds the old addresses** — see B2 in §3.

### C8 — L132 "`agent_mail_tenant.py:58` ← `AGENT_EMAIL_DOMAIN`" is the wrong shape

`:57` **already** reads `AGENT_EMAIL_DOMAIN`. `:52-58` is a deliberate **apex-collapse**:
`return "isol8.co" if domain.endswith("isol8.co") else domain`, because dev's
`AGENT_EMAIL_DOMAIN` is `dev.isol8.co` (`isol8-stage.ts:72`) and SES authorizes subdomain
sends via the parent identity. Substituting the setting verbatim makes dev target
`identity/dev.flickmeon.ai`, which will not exist → `create_tenant_resource_association`
gets a non-existent ARN, `_ignore_exists` only swallows `AlreadyExistsException` → **every
agent send fails**.

**Derive apex-of-mail-domain**, and re-decide the rule under the two-identity split.
Consumer at `:84`/`:87` belongs in the same diff.

### C9 — L152's P1 verify grep is unachievable, and aiming at it breaks a safety guard

> *"`git grep -n 'isol8\.co' apps/backend/core apps/backend/routers` returns only `config.py` defaults"*

Measured: **105** `isol8.co` lines in `core`+`routers`, of which **34** are `#`-comments and
most of the rest docstrings. Surviving **code** literals after every listed P1 item:
`billing_service.py:1093`, `routers/users.py:151`. (**[V]**
`routers/marketplace_purchases.py:43` *is* in scope — the plan's bullet says `39-43`.)

Worse, five literals **must stay** `isol8.co` forever and the grep pushes toward "fixing"
them: `paperclip_user_delete.py:49` (`BOOTSTRAP_EMAILS`), `account_wipe.py:143`,
`paperclip_admin_session.py:4`, `paperclip_api_key.py:16`, `paperclip_provisioning.py:295`.
Renaming `BOOTSTRAP_EMAILS` **silently disarms the wipe guard** — see §3 B6.

Also `apps/backend/core/services/atlas_seed/files/AGENTS.md:82` sits under
`apps/backend/core/` and hits the grep.

**Rewrite as:** grep minus comment lines, with an explicit five-entry allowlist; add
`billing_service.py:1093` + `routers/users.py:151` to P1; and add a second grep over
`.github/workflows` (15 hostname literals) plus a base64 decode of `mobile-ota.yml:64` and
`eas.json:37`, which no `isol8.co` grep can see.

**Related [V]:** the whole inventory must be pinned to a **commit SHA**. During one
verification, `apps/frontend/src/app/admin/layout.tsx` changed *between two tool calls* — a
concurrent session introduced an uncommitted admin-auth bypass
(`const token = "LOCAL_PREVIEW"; const me = { is_admin: true as const, email: "admin@isol8.co" … }`
at `:30-32`), shifting `isol8 admin` from `:57` to `:52`. Appendix B's inventories rot
mid-execution in this repo. *(Someone should be told about that working-tree bypass.)*

### C10 — L150 "Tests: leave hostname literals" is wrong in three different directions

(a) **Two infra tests break at P1, not P3** — `ses-inbound-stack.test.ts:274` and
`ascii-descriptions.test.ts:83` both `new Isol8Stage(app, environment, {…})`. Adding a
*required* `rootDomain` prop is a TypeScript compile error in both. Give it a
`"isol8.co"` default in P1 and flip the default in P2/P3.

(b) **~9 frontend files assert product copy** and go red the moment the P3 swap lands
(**[V]** — the sweep's "only 3 load-bearing" undercounts ~6×):
`BillingPanel.billing.test.tsx:95,127,151,161,274,341,410`, `BillingPanel.ultra.test.tsx:89`,
`ProfilePanel.test.tsx:168,178`, `marketplace/listing/[listingId]/__tests__/page.test.tsx:90`,
`DesktopUpdater.test.tsx:33` (`/isol8 v9\.9/i` pins `DesktopUpdater.tsx:160,165`),
`SupportTicketDialog.test.tsx:55`, `ProvisioningStepper.test.tsx:1005`,
`FirstRunFunnel.test.tsx:252`, `RoutinesPanel.test.tsx:1562`.

(c) **Three files are pure domain-agnostic fixtures and need nothing ever** —
`packages/shared/src/api/url.test.ts`, `src/lib/__tests__/api.test.ts`,
`src/__tests__/middleware.test.ts` (the last builds its own `ADMIN_HOSTS` and passes it
explicitly at `middleware.ts:59`'s third param).

**[V] correction to the sweep:** `api/download/desktop/__tests__/route.test.ts` is **not**
a P1 host-allowlist test — `download/desktop/route.ts` has no allowlist; its only host use
is `tagPrefixFromHost` (`githubReleases.ts:63-73`), which keys on `dev.`/`localhost`/
`.vercel.app` and stays true for `dev.flickmeon.ai`. Only the sibling
`desktop/update/…/route.test.ts:103,127` is load-bearing.

### C11 — L125/L126 contradicts L150

L125 says *"infra tests use the stage constant"*; L150 says *"leave hostname literals"*.
Both are in the P1 section about the same files. **Resolve toward literals** for
`ses-inbound-stack.test.ts` (43 lines) and `service-stack.test.ts:698-723` — a test that
imports the constant asserts nothing. (**[V]** the sweep cited "line 126"; it is **125**.)

The one exception where the constant is mandatory is the hosted-zone context stub (C1).

### C12 — L122's `service-stack.ts:…,1519` is a pass-through; the real sites are elsewhere

`service-stack.ts:1519` is `AGENT_EMAIL_DOMAIN: props.agentEmailDomain ?? ""` — a pure
pass-through of `isol8-stage.ts:72`. Replace it with `isol8-stage.ts:72`, and **add**
`service-stack.ts:1370,1374,1378` (Apple IAP — see C13) and `:495,523` (SES identity ARNs,
only partly covered by L124).

### C13 — Apple IAP is NOT inert; three live prod env vars are missing from every P3 step

**[V]** CLAUDE.md's *"disabled until `APPLE_IAP_BUNDLE_ID` is set — `routers/apple_iap.py`
503s"* is **stale**. CDK sets all three gating settings unconditionally in both envs:
`service-stack.ts:1370` `APPLE_IAP_BUNDLE_ID: "co.isol8.mobile"`, `:1374`
`APPLE_IAP_PRODUCT_ID`, `:1387` `APPLE_IAP_ROOT_CERTS_DIR`, plus `:1383`
`APPLE_IAP_ENVIRONMENT`. The gate is `core/services/apple_iap.py:210`, which needs all
three — and it has them. Live prod task-def `isol8-prod-backend:712` confirms
`APPLE_IAP_BUNDLE_ID = co.isol8.mobile`.

**[V] and the sweep stopped one line short:** `service-stack.ts:1378`
`APPLE_IAP_APP_APPLE_ID: "6788944100"`. `apple_iap.py:235-244` parses it and passes it to
`SignedDataVerifier(app_apple_id=…)`, **required in production**, and the comment at
`:225-228` states the failure mode: *"SignedDataVerifier compares this against Apple's
NUMERIC appAppleId, so a string never matches and every production notification fails
verification — silently, as an ordinary rejection."* Leave it stale after the new App Store
record and every renewal notification is dropped with no error.

Same numeric id in `eas.json:45` `"ascAppId": "6788944100"`.

**Correct CLAUDE.md as part of this program.**

### C14 — L177's flip-PR list is missing `apps/mobile/app.json`

`grep -n 'app\.json'` over the plan returns **zero** hits. It holds every native
identifier: `:3` `"name": "Isol8"`, `:4` `"slug": "isol8-mobile"`, `:5` `"owner": "isol8"`,
`:9` `"scheme": "isol8"`, `:18` `"bundleIdentifier": "co.isol8.mobile"`, `:23`/`:48` photo
prompts, `:26` `"aps-environment"`, `:29` `"applinks:isol8.co"` (single entry). `owner` +
`slug` are what `eas update` (`mobile-ota.yml:121`) and `eas build` resolve against.

Without it the flip ships an app whose env points at flickmeon and whose *identity* is
still `co.isol8.mobile` / `applinks:isol8.co`. **Highest-risk single omission in the
mobile/desktop slice.**

Also unlisted: `apps/mobile/package.json:8-9`, which hardcodes both API hosts and both
publishable keys for `start:dev` / `start:prod`.

### C15 — L149 lists only 4 of the 10 brand-bound `tauri.conf.json` lines

Unpatched by CI and shipped verbatim into both DMGs: `:3` `"productName": "Isol8"`, `:5`
`"identifier": "co.isol8.desktop"`, `:17` `"title": "Isol8"`, `:41`
`"bin/isol8-browser-service"`, `:50` `signingIdentity` (a **person's** name — P5), `:57`
`"schemes": ["isol8"]`. `:64` updater pubkey must **not** change or every future update
breaks.

### C16 — L53 "Old installs die with the domain (updater endpoint)" understates it

`tauri.conf.json:10` `frontendDist` is a **remote URL** — there is no bundled HTML at all.
Once `isol8.co` has no records, old installs open a **blank WKWebView load-failure page**,
silently, with no reinstall affordance. Separately, the Rust host keeps
`ISOL8_API_BASE`/`ISOL8_WS_URL` compiled in (`lib.rs:29-31,205,534,971-973`), and
`start_node_host_from_persisted` is spawned unconditionally in `setup` (`lib.rs:1205-1217`),
so every launch logs a node-start error forever. Old installs also never revoke their pair
(`DELETE /desktop/devices/{id}` can't reach a dead host).

**The plan records no decision here, and one option expires.** Either (a) accept it and tell
the team in Slack, or (b) before P3 step 5, publish one last `desktop-v0.4.N` to the OLD
identity whose window URL is a static "install Lightbulb" page. **(b) is impossible
after step 5 runs.**

### C17 — L174's brand-swap list is missing the highest-blast-radius frontend surfaces

- `Logo.tsx:36` (`ariaLabel = "isol8 home"`) and `:46` (`ISOL8`) — **18** render call sites
  (**[V]** 18, not 21). Coupled trap: `LogoMark` (`Logo.tsx:20-29`) renders a bare Didone
  `8` — *that glyph IS the mark*. A text-only swap leaves an orphan `8` beside the new name.
- `Footer.tsx:52` — the giant decorative wordmark. **[V]** styled at
  `landing.css:1767-1784` `clamp(110px, 21vw, 320px)`, tuned for 5 characters;
  "Lightbulb" (12) overflows horizontally at every viewport. This is a **layout change**,
  not a text swap.
- `LegalDocument.tsx:35` — `© {year} isol8 Inc.` on **all seven** Termly pages. The plan
  names 2 of 9 © surfaces (`Footer.tsx:47`, `support/page.tsx:119`).
- `[seoSlug]/page.tsx:46` — `page.title.replace(" | isol8", "")`. Must move in **lockstep**
  with `seo.tsx:99-108` or every SEO landing page emits JSON-LD `name` with the new suffix
  glued on.
- The four JSON-LD `url:` overrides that bypass `pageMetadata()` entirely: `docs/page.tsx:32`,
  `:37`, `marketplace/page.tsx:26`, `marketplace/agents/page.tsx:24`.
- **[V]** the CSS ranges the sweep gave are wrong at both ends: the brand block is
  `globals.css:1512-1548` (not 1501-1536; `letter-spacing: 0.14em` at `:1517`, the
  "the og mark IS a Didone" comment at `:1525-1528`), and the GooseTown block is
  `globals.css:1027-1144` (not 1015-1128 — a cut at 1128 leaves 16 orphan lines and a
  dangling `@media`).

### C18 — L174 "`docs/` markdown content pages" points at the wrong tree, and omits the blog

The repo's `docs/` is internal engineering documentation, never served. The user-visible
markdown is `apps/frontend/src/content/docs/pages/*.md` (5 files) + `content/docs/index.ts`
+ **`content/blog/posts/*.md` (6 posts, 25 brand hits)** + `content/__tests__/markdown.test.ts`
= **13 files**. The blog appears nowhere in the plan. Includes three
`github.com/Isol8AI/isol8/blob/main/...` links in a public post
(`webhook-event-reference.md:17,18,19`) that 404 for every reader today.

### C19 — L174 calling the skill-dir rename "text only" contradicts the plan's own P6 row

`Dockerfile:750` `COPY skills/isol8 /app/skills/isol8` makes the directory name the **skill
invocation name**, and that name is written into EFS by write-if-missing seeds with no
rewriting migration: `friday_identity.py:218,221,385` and
`atlas_seed/files/AGENTS.md:82` all say ``run `skill isol8` ``. Friday's SOUL.md/AGENTS.md
are seeded once and only ever **appended** to. Atlas heals (fingerprint force-refresh);
Friday does **not**. Every existing Friday would keep instructing herself to run a skill
that no longer exists.

The plan's own P6 row (L206) says this belongs "With the image roll that carries the brand
swap". L174 contradicts it. Pick one, and budget the fourth surgical migration nobody has
written.

**[V] UNRESOLVED, and it decides the cost:** `Dockerfile:718` says OpenClaw auto-discovers
bundled skills at `/app/skills/<name>/SKILL.md` — but whether the frontmatter `name: isol8`
(`SKILL.md:2`) **overrides** the directory name was never checked in `~/dev/agent-fork`. If
it does, the dir can be renamed while `skill isol8` keeps resolving, and the whole blocker
collapses into a one-line frontmatter decision. **Resolve this in the fork before writing
the phase plan.**

Two backend strings hardcode the same path and must move in the same image roll:
`agent_mail_inbound.py:249` (**[V] missed by both sweeps**) and `onboarding_brief.py:296`.

### C20 — L174 `friday_identity.py:204-385 + migration helper :906` will corrupt every existing SOUL.md

`friday_identity.py:210` `_SELF_GROUNDING_HEADING = "## You are made of Isol8"` is **both**
the writer (embedded in `_SELF_GROUNDING_SECTION` at `:216`, composed `:317`, appended
`:924`) **and** the idempotence matcher (`:922` `if _SELF_GROUNDING_HEADING in content:
return False`). Swap the heading and every already-migrated SOUL.md gets a **second**
grounding section appended, with the old Isol8-branded one still above it, permanently.

**[V] line correction:** the migration is `_migrate_add_soul_grounding` at **`:905`**;
sentinel check `:922`; append `:924`. The plan and the sweep both said `:906/:918/:921`.

**Same class, opposite direction —** `:802` `_LEGACY_PLAN_LINE = "- **Isol8 plan:** Starter
($50/mo flat-fee)"` and `:806` `"Bedrock (Claude, Isol8-managed credits)"` (inside
`_LEGACY_PROVIDER_LABELS`) are **match-the-old-bytes keys** for `_migrate_legacy_user_md`,
which hard-gates at `:837` (`if _LEGACY_PLAN_LINE not in content: return False`). A
mechanical `Isol8`→`Lightbulb` over this file silently kills that migration for every
owner it has not yet reached. `:806` is a byte-identical copy of `owner_facts.py:38`, which
the plan **does** rename — so the two must be split: rename `owner_facts.py:38`, **add** the
new label to `_LEGACY_PROVIDER_LABELS`, never swap it.

**Exclude `friday_identity.py` from bulk replace. Edit by hand.**

### C21 — L180 "credential helper on EFS re-materializes on boot" is FALSE

`credentials/apply_connection.py:45` is the **sole** `MATERIALIZERS` dispatch (grep over
`core/ routers/ scripts/` returns only `:11` import and `:45`), and its only callers are
`routers/connections.py:495,651,998` — all interactive connect/paste flows.
`scripts/rematerialize_instagram_skill.py:8` says it in the negative: *"Nothing
re-materializes on deploy."* Nothing in `write_user_configs`, nothing on the boot path.

### C22 — L180 "Friday `SOUL.md` re-render" is FALSE

`pre_stage_friday_identity` writes with `write_file_if_missing` (`:1254`, `:1327`, `:1413`)
and its docstring at `:1191` says why: *"so subsequent (re)provisions don't clobber Friday's
evolved persona."* The only writes to an existing SOUL.md are heading-anchored appenders in
`_run_heal_migrations` (**[V]** `:1070`, list body `:1080-1100`), none of which rewrite
"Isol8". **A re-roll changes nothing.** Replace this bullet with: *"new heal migration in
`_run_heal_migrations`, modelled on `_migrate_legacy_user_md:816-869`"* — the only existing
migration that rewrites lines rather than appending.

### C23 — L180 "fleet re-roll" must read **reprovision**

`ecs_manager.py:2045-2048`: *"Routing through `provision_user_container` (via
`dispatch_provision`) is the **only** path that re-runs `write_user_configs`"*, and
`:2057-2064`: *"`provision_user_container` only re-registers the task def + rewrites the EFS
config on its scale-FROM-zero branch. Its already-running branch just force-redeploys the
SAME (stale) task-def + config."* A roll delivers **none** of the EFS-side work (Atlas
restage, facts file, credential helper, USER.md migration, task-def `ISOL8_API_URL`).
`admin_container_reprovision` (`routers/admin.py:275`) scales to 0 first.

### C24 — L180 lists only `agents.adapter_config.url`; the UPDATE must be string-replace, never rebuild

The stored value is `f"{gateway_url}?token={service_token}&clientId={PAPERCLIP_OPERATOR_CLIENT_ID}"`
(`paperclip_adapter_config.py:196`). A `jsonb_set` that rebuilds from the host destroys a
live service-token JWT and a live device pairing. The same jsonb also carries
`paperclipApiUrl` (`:219`, the Cloud Map internal host) — the UPDATE must not clobber it.

### C25 — L169 puts the Clerk webhook edit in "pre-stage"; it belongs AFTER step 3, same as Stripe

`routers/webhooks.py:87-106` verifies against a single `settings.CLERK_WEBHOOK_SECRET`, and
the endpoint is `https://api.isol8.co/api/v1/webhooks/clerk` until step 3 makes
`api.flickmeon.ai` resolve. Editing it in pre-stage points Clerk at a host that does not
exist yet: no welcome email, no user/org record-keeping, no Stripe customer email sync, for
the whole pre-stage window. Svix retries help; the rule should still be **nothing points a
vendor at `api.flickmeon.ai` before step 3 completes.**

### C26 — L182 "remove old Stripe/Clerk webhook endpoints" contradicts L168-169

L168-169 correctly says **edit each endpoint's URL in place**. If they are edited in place
there is nothing to remove. Delete the L182 clause.

### C27 — L168's "three Stripe registrations" undercounts the routes

Three registrations across **two** routes, **three** secrets:
`billing.py:1131` `handle_stripe_webhook` (`STRIPE_WEBHOOK_SECRET`, verified at `:1139`)
and `marketplace_purchases.py:110-146`, which serves **two** registrations against **two**
secrets (`:136` iterates `STRIPE_MARKETPLACE_WEBHOOK_SECRET`, `STRIPE_CONNECT_WEBHOOK_SECRET`).
Paths: `/api/v1/billing/webhooks/stripe` and `/api/v1/webhooks/stripe-marketplace`.
`service-stack.ts:1566-1572` spells the failure modes.

### C28 — L168 lists "Connect refresh/return URLs" as dashboard work. They are not.

`marketplace_payouts.py:47-48` reads `settings.STRIPE_CONNECT_REFRESH_URL` /
`STRIPE_CONNECT_RETURN_URL`, set at `service-stack.ts:1487-1494`. Every Checkout
`success_url`/`cancel_url` and the portal `return_url` derive from `FRONTEND_URL` at
runtime. **These ship with the step-3 deploy.** What *is* dashboard work: the three webhook
URLs, Public business info, Branding, Product names, the **Customer Portal configuration's
ToS + Privacy URLs** (`billing_service.py:229` calls `billing_portal.Session.create` with
**no `configuration=`**, so the account default's legal links — pointing at
`isol8.co/terms`, `/privacy` — render for every paying customer), statement descriptor (P5),
Connect platform branding (P5).

### C29 — L76-77 "DMARC rua → `admin@thelightbulb.company`" is a protocol change, not a value swap

`ses-inbound-stack.ts:244` derives it: `const reportMailbox = \`admin@${mailDomain}\``, and
the comment at `:240-243` states the design assumption verbatim: *"A same-domain `rua` needs
no authorization record."* Making it cross-domain requires (RFC 7489 §7.1) a new TXT at the
**receiving** domain: `flickmeon.ai._report._dmarc.thelightbulb.company` → `"v=DMARC1"`.
Without it every conformant receiver silently sends **zero** aggregate reports.

Also **two** `_dmarc` records are needed, not one: `_dmarc.flickmeon.ai` (org domain for
every `{slug}.flickmeon.ai` agent address) and `_dmarc.thelightbulb.company` (platform
sender + humans) — and P0 L108 lists only "3 DKIM CNAMEs + SPF" for the latter.

### C30 — L67's "same prefixes" framing is wrong for the apex SPF/GSV record

`ses-inbound-stack.ts:176-184` is a `deleteExisting: true` TXT set carrying
`"v=spf1 include:_spf.google.com include:amazonses.com ~all"` **and**
`"google-site-verification=4m6AkWYnaePsQsIyDL4eJlPEddQg2RF1_Hxl7EFDHr8"`.

- The GSV token is scoped to the **`isol8.co`** Workspace. Copied to `flickmeon.ai` it
  verifies nothing; a fresh token must be minted for whichever domain is verified.
- `include:_spf.google.com` exists for Workspace user mail on the apex (`:149`). Workspace
  moves to `thelightbulb.company`, so `flickmeon.ai`'s apex should **drop** it (it
  costs ~4 of the 10-lookup SPF budget, `:156`) and the company zone needs its own.
- **`deleteExisting: true` destroys the entire apex TXT set on the new zone.** Any
  verification TXT hand-added to `flickmeon.ai` before that deploy (Google, Termly, PostHog,
  Stripe) is wiped unless appended to this array first. The warning is at `:169-175` and is
  easy to skip past during a rename.

### C31 — L204's header row is wrong on both halves ("one deploy", "brief mismatch window")

It **cannot** be one deploy. `deploy.yml:1454-1473` shows `dev-isol8-dev-service-Deploy`
`needs:` both `dev-isol8-dev-api-Deploy` and `dev-isol8-dev-agent-mail-outbound-Deploy`, so
the header **producers** are structurally guaranteed to flip before the consumer. With
`x-isol8-ws-internal` renamed producer-side and `WS_ENFORCE_INTERNAL_SECRET: "true"`
(`service-stack.ts:1264`), the backend rejects **every** chat message for the full api→
service job gap. Correct shape is **three PRs**: backend accepts old-OR-new → producers
flip → backend drops old.

The row also names one family where there are **four names and eight+ components**:

| header | producers | consumers |
|---|---|---|
| `X-Isol8-Public-Host` | `api-stack.ts:178` param mapping (live: `apigw-int:g6ztdi6e86/p3q6k5u`, `b56khy6cf6/st8ne63`) | `main.py:171`, `paperclip_proxy.py:1537` |
| `x-isol8-ws-internal` | `ws-connect/index.py:89`, `ws-disconnect/index.py:57`, `ws-message/index.py:49` (**[V]** three, not one) | `websocket_chat.py:362,390` |
| `X-Isol8-SES-Secret` | `agent-mail-outbound-stack.ts:58` **EventBridge Connection `ApiKeyName`** (live: `events:connection BackendConnectionB36A7E29-*`), `lambda/ses-inbound/index.py:384` | `ses_inbound.py:170,363` |
| `x-isol8-gate-code` | `lambda/bedrock-gate/errors.mjs:10,24` | **zero repo consumers** — rename unilaterally any time |

Test co-edits the plan omits: `lambda/ws-connect/test_index.py:138,149,159`,
`lambda/ses-inbound/test_index.py:197`, `apps/infra/test/agent-mail-outbound-stack.test.ts:54`.

### C32 — L203's GitHub-org row implies both OIDC trusts are code. Only one is.

`container-stack.ts:492` `"token.actions.githubusercontent.com:sub": "repo:Isol8AI/isol8:*"`
is CDK (dev-stage-only, `:477`). The deploy role
`arn:aws:iam::877352799272:role/isol8-dev-github-actions` is **defined nowhere in this
repo** — `git grep 'githubusercontent.com:sub' -- apps/infra` returns exactly one line, and
`git grep isol8-dev-github-actions` outside `deploy.yml` returns only `app.ts:49`, `app.ts:101`
and a comment. **[V] Live-verified:** `iam:get-role --role-name isol8-dev-github-actions` →
trust `repo:Isol8AI/isol8:*`, created 2026-03-20, policies `isol8-github-cdk-pipeline` +
`CdkOutTransport`, used for dev **and** prod. It must be widened by console/CLI, with no PR
to review and no test to catch it. `deploy.yml` carries the ARN **64** times (verified count).

Also drop `GITHUB_RELEASES_TOKEN` from that row — it appears in **no** workflow; it is a
**Vercel** runtime env var read only by `githubReleases.ts:52`. It does need re-minting
(fine-grained PATs are repo-scoped), just not as an Actions secret.

### C33 — L202 lumps three families that fail by three different mechanisms, and "Data at risk: no" is wrong twice

- **ECR** — `container-stack.ts:474,516` use `fromRepositoryName` (imported-existing;
  rationale at `:458-471`: RETAIN left an orphan that blocks recreation), and the repo has
  tag immutability on. New repo must be created by hand, re-pushed, and the old one must
  survive until every task def is rolled off it.
- **GHCR** — the two forks rename by **opposite** mechanisms. agent-fork
  `.github/workflows/docker-release.yml:25` `IMAGE_NAME: isol8ai/agent` is **hardcoded** (a
  fork patch; upstream uses `${{ github.repository }}`), so an org rename does **not** move
  it. paperclip-fork `docker.yml:176,360` `images: ghcr.io/${{ github.repository }}` is
  **derived**, so an org rename moves it **silently**, instantly invalidating
  `paperclip-version.json:2`'s digest ref (the path is part of the ref; a digest does not
  rescue it).
- **Tag suffix `-isol8.N`** — parsed at runtime by `core/openclaw_version.py:163`
  `_ISOL8_PATCH_RE`, which returns **0** on no-match (`:166-171`, deliberately: *"a value we
  can't read must never satisfy a `>=` gate"*), feeding `deployed_fork_at_least()` →
  fork-gated config keys are **silently omitted** from the next `openclaw.json` write.
  **Data at risk: YES** (a fail-quiet config regression on EFS). Also byte-asserted by
  `tests/chaos/_schema/PINNED_TAG:1` (see §3 B9). **[V]** the paperclip rail carries **no**
  `-isol8.N` at all (`build-paperclip-image.yml:58-61` derives `TAG` from `upstreamCommit`),
  so treating the two rails as one family overstates the paperclip side.

### C34 — L200/L201/L208 name secret families by the slash form only

**[V] live:** `secretsmanager list-secrets` shows two **dash-form** secrets outside the
`isol8/` prefix: `isol8-dev-paperclip-db-credentials` and `isol8-prod-paperclip-db-credentials`
— the Aurora master secrets, i.e. the ones you least want half-renamed. A `isol8/` prefix
sweep misses both.

### C35 — L200's DDB row: `DYNAMODB_TABLE_PREFIX` is not the switch, and a name edit is not a copy

(i) **Five table names are literals in the gate/meter Lambda env blocks** —
`bedrock-gate-stack.ts:162,163,166`, `bedrock-meter-stack.ts:237,243,244,249` — plus
`ECS_CLUSTER_PREFIX` (`bedrock-meter-stack.ts:256`, consumed at
`bedrock-usage-meter/index.py:677`), plus two separate backend settings
(`config.py:182` `METRONOME_FAST_GATE_TABLE`, `:254` `BEDROCK_AIP_OWNERS_TABLE`, set at
`service-stack.ts:1414,1440`). Miss any and the **billing enforcement fence** stops
enforcing or the backstop kill stops firing.

(ii) `tableName` is a CloudFormation **replacement** property. Editing
`database-stack.ts:81` inside the existing stack does **not** create a parallel table to
copy from — CFN creates the new empty one and applies the removal policy to the old. In
prod (`:21` RETAIN) that orphans 21 tables of live data; in **dev** (`:20` DESTROY) it
**deletes them**.

Same shape for `database-stack.ts:440` `clusterIdentifier` — `removalPolicy: SNAPSHOT`
(`:439`) means the old Aurora is snapshotted and **deleted** and a new empty cluster takes
its place. **State this as a data-loss event.**

Also `backend.yml:72` `DYNAMODB_TABLE_PREFIX: isol8-test-` is a moto fixture with zero data
at risk and **no test pins the literal** — it is free to rename any time, decoupled from the
DDB decision. Counterpart default at `config.py:50` (the plan cites `:146,442,460-468` but
not `:50`).

### C36 — L208's "Log groups … Cloud Map … Recreate" hides a fleet-wide outage and a billing blind spot

- **Cloud Map is not "recreate", it is an outage.** `PrivateDnsNamespace.name`
  (`container-stack.ts:136`) is a replacement property, and `ecs_manager.py:1170` passes
  `serviceRegistries` **only inside `create_service`** — there is no re-register path
  anywhere. A namespace replacement strands every per-user ECS service in the deleted
  namespace; `discover_ip` (`:2267`, using `NamespaceName`+`ServiceName`) returns `None` for
  everyone. Recovery = delete and recreate every per-user service. Also
  `PAPERCLIP_INTERNAL_URL` (`service-stack.ts:1348`, `paperclip-stack.ts:383,753`) is built
  from the namespace name and is the backend's only route to the Teams BFF.
- **`/isol8/bedrock-invocation` is account-global.** **[V] live:**
  `bedrock:get-model-invocation-logging-configuration` → `{"cloudWatchConfig":
  {"logGroupName":"/isol8/bedrock-invocation","roleArn":"…isol8-prod-bedrock-meter-BedrockLoggingRole…"}}`.
  Renaming it without re-`put`ing the config **blinds the meter Lambda**, i.e. silently
  disables the backstop half of the $3/day trial cap.
- **The bucket data-at-risk list is wrong for dev.** `autoDeleteObjects: env !== "prod"`
  (`container-stack.ts:442`, `service-stack.ts:1095`, `ses-inbound-stack.ts:271,287`), so a
  dev bucket rename **deletes** the objects — rehearsing "safely on dev first" destroys the
  dev litestream replicas. Rehearse with a scratch bucket.
- The S3 list omits `isol8-{env}-agent-mail-raw` / `-attachments`
  (`ses-inbound-stack.ts:254,255`) and `isol8-{env}-paperclip-attachments`
  (`paperclip-stack.ts:309`).
- **SNS is absent from the row entirely** — `topicName` is a replacement property, so a
  rename **drops every subscription**; email subs need a fresh confirmation click.

### C37 — L207's "localStorage keys `isol8:*`" is mis-scoped three ways

(a) There are **four** prefixes and one cookie, not one glob:
`isol8:` (`PostHogProvider.tsx:27,28`, `useUserSyncOnce.ts:36`, `useActiveSession.ts:21`,
`desktopPermissions.ts:17` — a **DOM event name**), `isol8.` (`layoutStorage.ts:17,18`,
`recent-assignees.ts:5,6`, `UserSync.tsx:7`, `desktopPermissions.ts:16`), `isol8_`
(`ProvisioningStepper.tsx:139,145`, `AgentChatWindow.tsx:34`, `FirstRunFunnel.tsx:48`,
`analytics.ts:170`, `useLandingBriefReplay.ts:27`, `snooze.ts:21,25`), and the **cookie**
`signupIntent.ts:20` `isol8_signup_intent`. A `isol8:*` glob catches 5 of ~18.

(b) The mobile key is `isol8.aiConsent.v1.` (a **dot**) — `aiConsent.ts:31`, plus
`pushDevice.ts:5,9`. SecureStore, not localStorage.

(c) **`isol8:` is also a UUID5 namespace seed**, not only a storage prefix:
`catalog_service.py:349` `uuid5(NAMESPACE_URL, f"isol8:cron:v1:{agent}:{i}")` and
`run_analytics.py:51` `uuid5(NAMESPACE_URL, "isol8:agent_run_completed")`. Renaming those
silently re-mints ids for data already at rest (cron job ids on EFS; PostHog dedup UUIDs).
**Split the row: "storage keys" vs "derived-id namespace seeds (never rename)".**

**The cost claim is also backwards:** storage-key renames are **free at P3** because the
origin change from `isol8.co` to `flickmeon.ai` empties every store anyway. The cost becomes
real only if **deferred** past P3. Same logic for the mobile keys (the new bundle id wipes
them). `layoutStorage.ts:18 LEGACY_KEY_PREFIX = "isol8.workspace.layout.v1"` is the one
exception — it exists to read pre-v2 layouts; rename `KEY_PREFIX` only and leave the legacy
value at its historical string.

### C38 — L211's docs row is unachievable as written ("With P3")

`.claude/**` is **gitignored** (**[V]** `.gitignore:58`, not `:63`); `AGENTS.md`,
`routine-detail.md`, `.agents/`, `.codex/`, `.playwright-cli/` and 7 `docs/` files are
**untracked and NOT gitignored**; the memory dir lives outside the repo. None can ride a P3
PR. Only `CLAUDE.md`, tracked `docs/**`, and the two version-json `notes` can.

`docs/**` is also **not one phase** — dev hostnames go stale at **P2**
(`CLAUDE.md:94,97,98,123,141,754`, `docs/runbooks/admin-rollout.md:19,96`,
`docs/runbooks/openclaw-config-lock-protocol-rollout.md:43`), and P2 is precisely the window
someone will follow them in.

### C39 — L242 points the fallback inventory at a path P6 can orphan

*"Full file:line inventories live in the 2026-08-26 session transcript"* — those live in
`~/.claude/projects/-Users-prasiddhaparthsarthy-Desktop-isol8-nosync/` (428 memory files,
137 `.jsonl`, ~1.5 GB), a **path-keyed** directory. Renaming the working directory
`isol8.nosync` starts an empty project and orphans the plan's own record. Either state **"do
not rename the working directory"** or require `mv`-ing the projects dir atomically.
(`graphify-out` survives — `.graphify_root` is `.` and `manifest.json` has zero
`isol8.nosync` hits — but `.claude/settings.local.json` and `.claude/launch.json` do not.)

### C40 — L240's counts are exact but labelled misleadingly

258 / 290 / 792 reproduce exactly. They are **tracked-only**. The untracked mass in the
docs slice alone is 15 enumerated files + **191** `.playwright-cli` files + 428 memory
files.

### C41 — L241's test figures undercount by ~2.5×

`~359 backend + ~180 frontend/mobile + ~60 infra` counts only `isol8.co` **hostname** lines
(measured: backend 359 across **65** files — **[V]**, not 366/67; frontend+mobile 163;
shared 19; infra 52). Total isol8-bearing **test** lines: **1,821 across 281 files**
(backend 954, infra CDK 407, frontend 260, mobile 115, lambda 50, shared 19, desktop 9,
plugins 7). The gap is mail, AWS resource names, `ISOL8_*`, EFS markers, image tags, ids.

Infra tests specifically are **407 hits / 22 files** and break at **P6**, not P2/P3.

### C42 — L209's SSO row: there is no account alias, and the instance is in the wrong region

**[V] live:** `iam list-account-aliases` → `[]`. There is nothing to rename. What exists is
the **Organizations account Name `Isol8`**. And `sso-admin list-instances` in `us-east-1`
returns **empty** — the instance is `arn:aws:sso:::instance/ssoins-66841e0b4e2a6bee` in
**`us-east-2`** (identity store `d-9a6754cdcf`), matching `~/.aws/config`'s
`sso_region = us-east-2`. Anyone checking in the default region will conclude there is no
SSO to rename.

"Low; anytime" also understates it: changing the start URL invalidates every cached SSO
token simultaneously.

### C43 — L232's ACM list is wrong on two counts, and L182's delete order is impossible

**[V] live:** `assets.town.isol8.co` (`24b2c113`) is **NOT in use** (`InUse: false`, empty
`InUseBy`) and is the only cert expiring soon — **2026-09-25**, ~30 days out. Delete it
today, independent of the program.

And two of the five short ids are the **CDK-managed** `*.isol8.co` certs
(`dns-stack.ts:21`) — they are deleted by removing the construct, not by
`aws acm delete-certificate`. Hand-deleting them leaves CFN with a dangling reference.
`b9ef44ac` and `49501398` are attached to CloudFront `EG8LT2TP7FM0`/`E1VXN9XVCADEQU`, our own
ALBs `isol8--Alb16-dqix41FUVIlg`/`-9odFi9xdWAcF`, and API Gateway's shared fleet ALBs.
`acm delete-certificate` on an in-use cert returns `ResourceInUseException`.

**Correct order:** deploy new rootDomain → delete old API GW domain names → disable +
`wait distribution-deployed` + delete CloudFront (~15-20 min each) → then delete certs.

### C44 — L226-231 Appendix A is 3 records short and one record type is misstated

**[V] live:** the zone has exactly **43** records; Appendix A enumerates 40. The three
unlisted are:
`_aws_securityagent-challenge.isol8.co TXT` (a **live** AWS Security Agent domain
verification — backed by `/aws/securityagent/isol8-api-dev-pentest/…` and three IAM
policies), and two ACM validation CNAMEs (`_dc9607dba…isol8.co`, `_0933095b…goosetown.isol8.co`).
Two of the three cannot be deleted until their dependent resource is gone, so **L220's
`dig`-clean verification will fail for reasons unrelated to the rebrand** unless they are
explicitly handled.

Also: "Resend DKIM (leftover)" is a **TXT** (`resend._domainkey.isol8.co`), not a CNAME —
which changes the delete change-batch shape. And the apex A is **216.150.1.1**, while
`admin`/`dev`/`admin.dev` are **76.76.21.21** — two different Vercel IPs, so C45's
"one A-record constant" assumption is wrong.

### C45 — L156/L167 "Vercel: add … to project `frontend`" — the dev hosts are aliases, and the real prerequisite is a team-level add

**[V] live:** `GET /v9/projects/frontend/domains` returns exactly four entries —
`www.isol8.co`, `admin.isol8.co`, `isol8.co`, `frontend-pink-nine-76.vercel.app`.
`dev.isol8.co` and `admin.dev.isol8.co` are **aliases**, set by the pipeline at
`app.ts:161-169`. `vercel domains ls --scope isol8` shows **one** team domain,
`isol8.co | Third Party`.

So the P2 prerequisite is `vercel domains add flickmeon.ai` at the **team** level plus
ownership verification, *then* the alias commands work. As written, P2 fails at the alias
step. Note `www` is the only host with a `vercel domains add` step (`app.ts:249-252`); the
`admin.*` aliases have none.

### C46 — L182's GooseTown teardown is ~5% of GooseTown, and misses a full-admin OIDC door

**[V] live:** 26 CloudFormation stacks, 28 DynamoDB tables (with real rows —
`goosetown-dev-world-events` 106,331 items, `goosetown-prod-world-events` 83,953,
`goosetown-{dev,prod}-conversations` 21,583/24,030, `goosetown-dev-memories` 23,084), 14
Secrets Manager secrets, **4 WAFv2 web ACLs** (the only WAFs in the account), 4 S3 buckets
(incl. `goosetown-{dev,prod}-alb-logs` at 43,855/39,008 objects, ~272/281 MB), 31 IAM roles,
27 log groups, 14 Lambdas, 2 ECS clusters / 8 services, 2 Cloud Map namespaces, 2
EventBridge buses + **2 ENABLED `rate(15 minutes)` PixelLab-poll rules still billing today**,
2 SNS topics, 1 ECR repo, 26 CloudWatch alarms, 2 CloudFront response-headers policies, 8
Route53 records, and 4 dead Vercel aliases (`town.isol8.co`, `dev.town.isol8.co`,
`staging.town.isol8.co`, `goosetown.vercel.app` — two of which have **no Route53 record at
all**).

**`iam:role/goosetown-github-actions` carries `AdministratorAccess` and trusts
`repo:Isol8AI/goosetown:*`** — a dead product leaving a full-admin OIDC door keyed to a repo
nobody watches. Not in the plan.

Cloud Map namespaces **own** the two `.local` private zones (`Z0744511ENLXC7H1EN4V`,
`Z0014039LL5MG7RIOA5D`) — you cannot `route53 delete-hosted-zone` them; deregister
instances → `delete-service` → `delete-namespace`.

### C47 — L108's P0 mail item is missing the AWS-account-critical half

**[V] live:** `admin@isol8.co` is simultaneously
(a) the **AWS Organizations master-account email** (`organizations:describe-organization` →
`MasterAccountEmail: admin@isol8.co`; account Name `Isol8`) — i.e. root password reset, MFA
recovery, billing/tax notices, Support case identity for the account holding **every
customer's EFS, Aurora and DynamoDB**;
(b) **4 SNS email subscriptions** (`isol8-{dev,prod}-alerts-{page,warn}`);
(c) **4 AWS Budgets subscriber entries** across two $1500/mo budgets.

SNS email subs return `PendingConfirmation` until a human clicks the link **in the new
mailbox** — so none of this can be done after the Workspace is gone.
`identitystore:list-users` also shows **Aden's SSO login is `aden@isol8.co`**, and Aden is
the operator of the mail migration.

**All of this is P0, before L182's Workspace retirement.**

### C48 — L112's Termly item omits the mechanism that makes it cheap

Editing a Termly doc **in place keeps its id**, so no repo change is needed for a text edit
— only if a doc is recreated. The 7 ids are inline at `page.tsx:12` in
`acceptable-use`/`accessibility`/`cookies`/`disclaimer`/`eula`/`privacy`/`terms`.
Separately, Termly's account setting binds the published docs to a **website URL** that must
be updated (not visible in repo).

### C49 — L144's CSP derivation must keep three fixed vendor origins, and it is fixing a pre-existing hole

`next.config.ts:14-20` spells the trap: dropping `app.termly.io` from **either**
`script-src` or `connect-src` blanks all seven legal pages, and *"a blank privacy-policy URL
is an App Store 5.1.1 rejection."* Keep `app.termly.io`, `challenges.cloudflare.com`,
`js.stripe.com`/`hooks.stripe.com`/`api.stripe.com`, `va.vercel-scripts.com`,
`us.i.posthog.com`/`us-assets.i.posthog.com` as literals.

**[V] two things the plan doesn't say:**
- `:30` connect-src **omits the browser-view WS host entirely** — `bv.isol8.co` /
  `bv-dev.isol8.co` are absent. Invisible today only because the header is
  `Content-Security-Policy-**Report-Only**` (`:46`). The documented "promote to enforcing"
  follow-up (`:11-12`) would kill the browser panel with a CSP violation and no other
  symptom. Adding `bv` is a **fix**, not a port.
- `:26-31` is a **single static string shared by both Vercel environments** — it names prod
  AND dev hosts at once. During P2 (dev on flickmeon, prod on isol8) one literal cannot be
  right for both. The derivation must read the per-environment `NEXT_PUBLIC_API_URL` and
  stop enumerating the sibling environment's hosts.

**Do not promote the CSP to enforcing during the flip.**

### C50 — L145's "`admin-dev` vs `admin.dev` mismatch" is a live defect, not a rename

`middleware.ts:10` `DEFAULT_ADMIN_HOSTS = "admin.isol8.co,admin.dev.isol8.co,admin.localhost:3000"`
is what runs; `.env.example:35` says `admin-dev.isol8.co`; `docs/runbooks/admin-rollout.md:96`
has the right value. Copying `.env.example` into `.env.local` **404s every `/admin*` path
locally today** (`middleware.ts:65-67` returns 404, not a redirect — no hint). Fix it in P1.

Related: **`NEXT_PUBLIC_ADMIN_HOSTS` is set nowhere** — not in `deploy.yml`, not in
`app.ts`, not in any CDK stack, and **[V] live** not among the 17 Vercel env vars. The
default constant is authoritative, so there is **no hot-fix lever**: a wrong admin host
after the flip requires a rebuild+redeploy.

### C51 — L143's `NEXT_PUBLIC_APP_URL` proposal has an unverified premise

`marketplace/deploy-success/page.tsx:14` (`process.env.NEXT_PUBLIC_APP_URL ?? "https://isol8.co"`)
is its **only** consumer repo-wide, it is **absent from `.env.example`**, and **[V] live it
does not exist in Vercel at all** (the 17 vars are `CLERK_SECRET_KEY`,
`GITHUB_RELEASES_TOKEN`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`,
`NEXT_PUBLIC_CLERK_SIGN_IN_URL`, **`NEXT_PUBLIC_CLERK_SIGN_UP_UR`** ← live typo, missing the
trailing `L`, `NEXT_PUBLIC_POSTHOG_HOST`, `NEXT_PUBLIC_POSTHOG_KEY`,
`NEXT_PUBLIC_TEAMS_NATIVE_UI_ENABLED`). So the fallback **is** what is live today, and P1
would quietly make `isol8.co` authoritative on dev unless the var is added to all three
targets.

Also `NEXT_PUBLIC_API_URL` has **two writers** — the Vercel dashboard value and the
pipeline's build-time override from `secrets.NEXT_PUBLIC_API_URL_{DEV,PROD}`
(`app.ts:150-152,229-232`). Change both, or delete the dashboard copies.

### C52 — L156 flips `NEXT_PUBLIC_API_URL_DEV`; **`_PROD` is never mentioned**

`app.ts` builds the prod frontend with `NEXT_PUBLIC_API_URL: "${{ secrets.NEXT_PUBLIC_API_URL_PROD }}"`.
P3 (L177-179) never names it. If the P3 dispatch runs before that secret is flipped, the
prod frontend compiles against `https://api.isol8.co/api/v1` — a dead host. **Prod outage,
not a nit.** Also unlisted: `secrets.CLERK_PUBLISHABLE_KEY` (`frontend-ci.yml:76`) — a
**third** Clerk secret name distinct from the `_DEV`/`_PROD` pair, instance unknown.

### C53 — L204's "with P3" for headers is not reflected in any P3 step

The flip window (L177-181, steps 1-6) contains no header step. Either add one or move the
row's recommendation to P6.

### C54 — L212's fork row is wrong on three counts

- **"Data at risk: no"** is wrong for `isol8.notifications` — it is a DB row key
  (`bundled-plugins.ts:88` `pluginKey`, `plugin-loader.ts:130` gate). **[V] correction to
  the sweep:** it is **instance-wide**, not per-tenant —
  `packages/db/src/schema/plugins.ts:23-44` has `uniqueIndex("plugins_plugin_key_idx")`, and
  Isol8 runs one Paperclip service against one Aurora cluster. So it is **one row**; what is
  per-company are the FK children (`plugin_company_settings`, `plugin_config`,
  `plugin_entities`, `plugin_webhooks`, `plugin_jobs`, `plugin_database_namespaces`, all
  `references(() => plugins.id)`). A rename mints a **new uuid** and every per-company child
  still points at the old one. Also unverified by anyone: the old row's `packagePath`
  (`:36`) points at `plugin-isol8-notifications`, which the renamed image no longer ships —
  the practical failure is likely a dead worker + a duplicate installed row. **Check
  `ensureBundledPluginsInstalled` before writing a migration.**
- **`PAPERCLIP_ISOL8_*` is three names**, not two: `PAPERCLIP_ISOL8_NOTIFY_URL`,
  `PAPERCLIP_ISOL8_NOTIFY_TOKEN` (`plugin-loader.ts:131-134`, set at
  `paperclip-stack.ts:454,515`) and `PAPERCLIP_ISOL8_NOTIFICATIONS_PLUGIN_PATH`
  (`bundled-plugins.ts:90`).
- **"one fork PR"** understates the agent fork: its surface includes a runtime env var read
  by the container (`ISOL8_NATIVE_SETUP_SEED`, `workspace.ts` — at the **released tag**
  `v2026.8.1-isol8.1`, `:103`) and a filename on every owner's EFS (`isol8-setup-seed.json`,
  `:99`).

### C55 — L100's `-isol8.N` sits under "internals" with no cross-reference to the backend parser

It is the only P6 internal identifier with a **regex parser in production code**
(`openclaw_version.py:163`) and a **byte-equality test fixture**
(`tests/chaos/_schema/PINNED_TAG:1`). It belongs in the "with the image roll" lockstep
family, not beside localStorage keys.

### C56 — L182's ACM/SES/API-GW retire list omits the SES tenant re-association

**[V] live:** all 3 owner tenants carry an `identity/isol8.co` association *and* a
`configuration-set/isol8-{env}-agent-mail` association
(`sesv2:list-tenant-resources`). `delete-email-identity` while associations exist either
fails or strands the tenants. The plan lists the delete but not the re-association that must
precede it.

### C57 — Missing "do-not-touch" list entirely

The plan has no section for identifiers that must **never** be renamed. §5 supplies one.
Without it, the P1 grep criterion (C9) actively pushes an implementer toward disarming the
account-wipe guard.

---

### Refuted / not real

Claims made by exactly one sweep and refuted by its verifier. Recorded so they are not
re-raised.

| Claim | Refutation |
|---|---|
| `desktop-build.yml:163,351` release names reach users via the Tauri update prompt → **P3 user-visible** | `route.ts:130` is `notes: candidate.body ?? candidate.name ?? ""`, and `??` falls through only on null/undefined. `tauri-action` creates releases with an **empty-string** body — verified live: `gh api /repos/Isol8AI/isol8/releases` → `{"body_len":0,"name":"Isol8 Desktop v0.4.44"}`. `"" ?? name` → `""`. `candidate.name` is dead code. **P6 cosmetic.** |
| Renaming `-isol8.N` makes the backend emit the **older config shape onto a newer image** today | `openclaw_version.py:195-201` short-circuits on the BASE version before reaching the patch level. The only live caller is `config.py:2174` `deployed_fork_at_least("2026.7.2", 7, …)`; the current pin is `2026.8.1-…` → `(2026,8,1) > (2026,7,2)` → returns True at `:198` without calling `isol8_patch_level`. A latent trap for a future gate written at base 2026.8.1 — **downgrade H→M**. |
| A one-sided `isol8.hire_suggestion` rename makes the hire card **silently vanish** | `HireCard.tsx:59-64`: *"Returns null for anything that is not a well-formed hire suggestion, so every other tool falls through to **the ordinary tool pill** unchanged."* Real regression, **wrong failure mode**. |
| Editing `chrome-extensions/popups-to-tabs/manifest.json:3` forces a new extension id | The id derives from the **signing key**, not the manifest. `chrome-extensions/README.md:30` documents `--pack-extension-key=popups-to-tabs.pem`. The real constraint is that we chose not to keep the `.pem` in the repo (`:34`) — a possession problem. "Leave it" still holds, for the right reason. |
| A boot-chain rename makes `build-openclaw-image.yml` **silently stop rebuilding** the image | `:29`/`:45` are **directory globs** (`apps/infra/openclaw/local-sqlite/**`); a rename inside the dir still matches. Only `:94` names a file, as an argument to `smoke-test.sh`, and `build-and-push` carries `needs: boot-chain-smoke` — so a missed rename fails **loudly** and blocks the push. Risk inverted. |
| `ISOL8_AGENTS_DIR` is a five-way lockstep at risk **H** | It is **never on the task definition** (`git grep ISOL8_ -- core/containers apps/infra/lib` returns only `ISOL8_API_URL`, `ISOL8_OWNER_ID`, `ISOL8_CONFIG_LOCK_PROTOCOL`). Every setter and reader lives inside `_FILES_DIR`, so a rename bumps the fingerprint and restages both halves atomically; the `.js` fallback resolves to the same path anyway. **Risk L.** |
| There is deliberately **no update primitive** on `agent-email-addresses` | `agent_email_repo.py:148-194` `update_settings(address, *, verified_senders=…)` SETs under `ConditionExpression="attribute_exists(address)"`; `delete_by_address` (`:139-142`) and `scan_all` (`:120-136`) also exist. The M2 backfill is **one `update_settings` call**, not a fold into the delete+put. |
| `isol8_listing_slug`/`isol8_listing_id` live in Aurora `agents.metadata` | They land in `approvals.payload` — `marketplace_hire.py:61` → `:86-89` → `:298` `"payload": build_hire_approval_payload(…)`, read back at `:100`. Sink column is `approvals.payload jsonb`. The proposed `UPDATE agents SET metadata = …` returns **0 rows**. |
| Renaming the `isol8-` session-key prefix makes OpenClaw **refuse to start** | `containers/config.py:2005-2010` — the boot throw is caused by omitting **`"hook:"`**, not `"isol8-"`. `:2009-2010` say `"isol8-" is for our own session-key conventions`. Renaming orphans already-minted `isol8-email:<sha256>` keys (`agent_mail_thread.py:37`) so every live email thread cold-starts. Real, different failure, different fix. |
| The bootstrap-wipe guard is currently saved by a **stale `users` DDB row** | `account_wipe.py:602` `emails[0]`, and `resolve_account_emails:159-169` appends **Clerk** addresses first, the DDB row only afterwards and only if absent. The DDB row never gets a vote. Both guards (`account_wipe.py:175`, `paperclip_user_delete.py:77`) key off the same Clerk-first list and **disarm together**. Conclusion stands; mechanism was wrong. Also the obvious fix ("match the live secret's email") was already **evaluated and rejected in-code** at `paperclip_user_delete.py:44-48`. |
| Apple IAP is inert ("the router 503s until the bundle id is set") | See C13 — CDK sets all three gating settings; live prod task-def confirms. CLAUDE.md is stale. |
| `cdk.context.json`'s new entry "can only be generated by running `cdk synth` locally with credentials" | It is a committed JSON file whose hosted-zone value is `{Id, Name}` — hand-addable. |
| DnsStack is **the first stack** in the run | `isol8-stage.ts:27` instantiates `AuthStack` before `:32` `DnsStack`, and they are independent. Substance (network `:87` and api `:115` both `needs:` dns) is unaffected. |
| `isol8.notifications` orphans a row **in every tenant Aurora DB** | Instance-wide unique key; one row. See C54. |
| The agent-fork inventory's file:line list | Cited against `~/dev/agent-fork` **HEAD** (`isol8/2026.7.2-browser-session-persistence`), a stale 7.2 branch. The deployed base is tag `v2026.8.1-isol8.1` (`9861e0d7f`). Every agent-fork line is off by ~14-209 and the file set is wrong in **both** directions (12 files/48 items at the tag; `+src/agents/mcp-oauth.ts`, `src/config/types.mcp.ts`, `src/infra/state-migrations.mcp-oauth.ts`; `−packages/normalization-core/src/agent-id.test.ts`, `src/commands/agents.test.ts`). **Re-derive the whole fork half against the released tag.** |
| "Only 3 frontend test files are load-bearing" | ~19-20. See C10(b). |
| `api/download/desktop/__tests__/route.test.ts` is a P1 host-allowlist test | That route has no allowlist. See C10. |
| `isol8.ai` is "a SECOND domain" appearing in backend tests | `test_instagram_token_script.py:45,100,145` is `"username": "isol8.ai"` — an **Instagram handle** field. It still needs renaming, as a social account. (`docs/integrations/apollo-prospect-source.schema.json:3` `$id: https://isol8.ai/schemas/…` **is** a real second-domain reference.) |
| "`gh api /repos/Isol8AI/isol8/actions/runners`" verifies the `isol8-wsl` runner | It returns exactly one runner, `PPs-MacBook-Pro-desktop` (`[self-hosted, macOS, ARM64, macos-desktop]`). `isol8-wsl` is **org-registered**; `gh api /orgs/Isol8AI/actions/runners` returns **403** without `admin:org`. The check as written silently reports the WSL runner as missing. |
| `76.76.21.21` / `cname.vercel-dns.com` (recalled, not evidence) | **Confirmed live** by the AWS slice for `admin`/`dev`/`admin.dev` (A → `76.76.21.21`) and `www` (CNAME → `cname.vercel-dns.com`) — but the **apex** is `216.150.1.1`, a different Vercel IP. Do not assume one constant. |
| `tests/chaos/invariants/test_config_schema.py` contains isol8 literals | Zero. Its `PINNED_TAG` **dependency** at `:40-43` is real; there is nothing to find-replace in it. |

---

## 2. Missing from the plan — new required work

Format: `file:line | value | old→new | visibility | risk | what breaks if missed`.
Items already named in the plan are omitted; only net-new work is listed.

### P0 — before any mailbox or account is touched

```
aws:organizations/877352799272     | MasterAccountEmail admin@isol8.co; account Name "Isol8" | →admin@thelightbulb.company / "The Lightbulb Companies" | internal | H | root password reset + MFA recovery + billing/tax + Support identity orphaned for the account holding every customer's EFS/Aurora/DDB
aws:sns/isol8-{dev,prod}-alerts-{page,warn} | 4 email subs → admin@isol8.co | subscribe new + CLICK confirm, then unsubscribe old | internal | H | every P-tier page and W-tier warn silently stops; subs return PendingConfirmation until a human opens the mail, so this is impossible after the Workspace dies
aws:budgets/MonthlyBudget-…-MTAaieb1MdDq, …-qnO5epNhhHAW | 2×$1500/mo, 4 subscriber entries @isol8.co | re-subscribe (no confirmation needed) | internal | M | $1500 budget alerts stop
aws:identitystore/d-9a6754cdcf (us-east-2) | user "Aden" | aden@isol8.co | →@thelightbulb.company | internal | H | the operator of the mail migration loses his own SSO login
acm:24b2c113-a8ec-4d72-84af-044807ff8d87 | assets.town.isol8.co, NOT in use, expires 2026-09-25 | DELETE now | internal | L | ACM starts emailing renewal-failure notices to admin@isol8.co exactly as that mailbox is retired
vendor:Intercom | DCR redirect-URI allowlist (providers.yaml:1199-1201) | register api[-dev].flickmeon.ai callbacks | user-visible | H | NOT self-serve — needs a request to Intercom; every Intercom connect fails at registration
decision:thelightbulb.company DNS host | plan L63 open decision | RESOLVE (Route53 = CDK-owned; elsewhere = manual gate) | internal | H | ses.Identity.publicHostedZone (ses-inbound-stack.ts:110) only works for a zone CDK holds; otherwise AWS::SES::EmailIdentity goes CREATE_COMPLETE while Pending and every platform send MessageRejects silently
memory:project_rebrand_isol8_to_flickmeon_lightbulb.md | "lightbulbcompanies.com vs thelightbulb.company — confirm"; "keep co.isol8.desktop identifier + keychain service" | reconcile to plan L47/L58/L91 | internal | H | auto-loads in every session in this directory; a P1 subagent implements the wrong thing confidently
```

### P1 — domain-agnostic repo

**Backend — sign-in / identity (the `paperclip_email` cluster)**
```
routers/paperclip_proxy.py:1480,1786,1803         | 3 Better-Auth sign-in doors (HTTP proxy, cookie-JWT, WS relay) | ←company.paperclip_email | internal | H | admin whose Clerk email moves → Teams 401 loop → Better Auth per-IP limit is fleet-wide
core/services/paperclip_provisioning.py:457,981,1034 | 3 more (owner adopt, member adopt, invite-mint) | ←stored email | internal | H | every org member-add for that org fails; adopt path wedges the row; sign-UP fallthrough duplicates the Better Auth user
core/services/paperclip_provisioning.py:497,810,1019,1091 | the 4 PaperclipCompany(...) constructions | write paperclip_email at all four | internal | H | raw PutItem silently wipes a field set elsewhere (:140-155)
core/repositories/paperclip_repo.py:~194,372,467    | dataclass / put serializer / _from_item | 3 edits, not 1 | internal | H | field never persists or never reads back
core/services/paperclip_owner_email.py:51-56,66-83  | user_repo fast path is STALE FOREVER (user.updated never writes it) | decide: read stored email, or write on user.updated | internal | H | non-deterministic — provision_member works for owners whose users-row survived, fails for those it didn't
routers/webhooks.py:723-760                         | user.updated syncs Stripe only; no user_repo.put | see above | internal | H | the DDB email attribute never refreshes
core/services/user_provision.py:766,785-792         | lookup_owner_email for member + owner sign-ins | ←stored | internal | H | same 401 class
routers/webhooks.py:350                             | _extract_org_member_email → provision_member | ←stored | internal | H | same
```

**Backend — gates, guards, hosts**
```
core/auth.py:280-284 | _PLATFORM_ADMIN_EXTRA_EMAILS = {"prasiddha@gmail.com"} | ADD each admin's future @thelightbulb.company address BEFORE the Clerk flip; remove in P3 cleanup | internal | H | the gate is per-request off the JWT email claim, so a Clerk primary-email change flips admin-ness on the next token mint — minutes, not a deploy. Without this hedge, steps 2→3 of the flip window are an admin-lockout
core/services/curator.py:29,47 | find_user_by_email(ISOL8_CURATOR_EMAIL), process-global cache, NO TTL (:12-14 "restart the backend if it ever changes") | value flips P3; note the restart | user-visible | H | featured-listings gallery renders EMPTY until the ECS task restarts. clerk_admin.py:434 filters on ANY email address, so keeping the old address as a SECONDARY on the Clerk user is the zero-downtime lever
core/services/seller_profile.py:43 | RESERVED_HANDLES contains "isol8" | ADD lightbulb/lightbulbai/flickmeon; keep isol8 | user-visible | M | second reserved table the plan doesn't name; someone squats the new brand handle
core/services/company_identity.py:34-107 | RESERVED_SLUGS (plan says :34-95 — wrong range) | same | user-visible | M | as above for slugs
core/services/support_ticket.py:32 | "admin.isol8.co" / f"admin.{env}.isol8.co" | ←ADMIN_HOST setting | internal | M | admin deep link in every support ticket 404s. NOTE: test_support_ticket.py has ZERO isol8 coverage — add one assertion in P1
core/services/ses_email.py:19-26,101 | docstring claims AGENT_EMAIL_DOMAIN is "a direct proxy for is the SES identity deployed" | FALSE after the sender split | internal | H | the gate says "go" for a domain SES may not have verified; MessageRejected is swallowed at :148-151; support tickets lost silently (email_service.py:16-18 says it is the ONLY capture path)
core/services/billing_service.py:1093, routers/users.py:151 | "contact support@isol8.co" | ←settings.EMAIL_SUPPORT_ADDRESS | user-visible | M | the two code literals that survive the plan's own P1 grep
core/services/system_health.py:78 | f"isol8-{env}-admin-actions" bypasses DYNAMODB_TABLE_PREFIX | ←settings | internal | M | health probe reports {"status":"down"} after a prefix flip
core/services/admin_service.py:207-208 | primary_email = emails[0] (every sibling uses primary_email_address_id) | fix | internal | L | /admin/users shows whichever address Clerk returns first during the dual-address window
scripts/audit_paperclip_adapter_types.py:286 | Session(profile_name=args.profile) with default="isol8-admin", no empty escape | add `or None` | internal | M | one-token fix; a renamed SSO profile hard-fails with ProfileNotFound
core/services/credentials/oauth_dance.py:152-156 | DCR re-registers on SCOPE drift only, never redirect_uri | ADD a redirect_uris comparison | user-visible | H | see §3 B3 — "owner reconnects" cannot work
routers/meta_webhook.py:202-203 ← core/config.py:428 | META_WEBHOOK_VERIFY_TOKEN default "", set by NO CDK stack | add the CDK setter | user-visible | H | Meta refuses to SAVE a callback URL whose hub.challenge verify 503s — blocks the P2 Meta step entirely
```

**Infra**
```
apps/infra/lib/stacks/dns-stack.ts:21-25 | NEW construct RootCert(*.flickmeon.ai + apex); repoint isol8-stage.ts:87,115; delete WildcardCert in a LATER PR | user-visible | H | see §3 B1 — editing domainName in place is a hard CFN export deadlock
apps/infra/lib/stacks/ses-inbound-stack.ts:342,349 | BACKEND_INGEST_URL derives the API host from mailDomain | derive from rootDomain explicitly | internal | H | the moment platformMailDomain exists, a "simplification" points ingest at api.thelightbulb.company and every inbound agent email is dropped in a 5xx retry loop
apps/infra/lib/stacks/ses-inbound-stack.ts:109-110,413 | one zone-derived identity; single-domain recipients | add platformMailDomain (identity-only sibling prop) + mailHostedZone + recipients array | user-visible | H | C2; and platformMailDomain must NEVER feed ingest URLs, MX, wildcard SPF or recipients
apps/infra/lib/stacks/observability-stack.ts:151-154 | alertEmail = "admin@isol8.co" | move in P1 (independent of rootDomain) and CLICK the confirmation | internal | H | :140-150 says it: a new construct id = new subscription in PendingConfirmation, old one deleted by the same deploy → NO alarm email at all in between. PagerDuty (:175) covers prod pages; dev has nothing
apps/infra/lib/stacks/observability-stack.ts:2160,2161 | appHost/apiHost for the 4 Route53 health checks (:2187) | ←stage rootDomain | internal | M | 4 live health checks + isol8-{env}-P13/P14 alarms keep probing dead hosts; Appendix A omits them from live state
apps/infra/lib/stacks/service-stack.ts:495 | identity/isol8.co IAM grant | list BOTH identities | internal | H | every platform send AccessDenied. (:523 is the TENANT policy — agent-mail identity ONLY; listing both there is over-broad)
apps/infra/lib/app.ts:249-252 | vercel domains add exists ONLY for www | ADD equivalents for admin.* / dev.* or pre-register at team level | internal | H | see §3 B4
```

**Frontend / shared**
```
apps/frontend/src/lib/site.ts (NEW) | SITE_URL from NEXT_PUBLIC_APP_URL; WS_URL/BV_URL/ROOT_DOMAIN DERIVED (reuse deriveWebSocketUrl url.ts:33 + bvBase browserViewUrl.ts:9-15, both already domain-agnostic); scheme a compiled constant, not an env var | internal | M | 2 new env vars total (NEXT_PUBLIC_APP_URL, NEXT_PUBLIC_SUPPORT_EMAIL — the support mailbox is on a DIFFERENT domain and cannot be derived from SITE_URL)
apps/frontend/next.config.ts:30 | connect-src omits wss://bv[-env] entirely | ADD (a fix, not a port) | internal | M(H when CSP enforces) | the browser panel dies on the documented enforce-promotion with no other symptom
apps/frontend/next.config.ts:26-31 | one static CSP string names prod AND dev hosts | derive per-environment | internal | M | P2 ships a policy naming a dead domain
vercel:frontend env | NEXT_PUBLIC_APP_URL absent from all 3 targets; NEXT_PUBLIC_CLERK_SIGN_UP_UR is a live typo | add ×3; fix typo | internal | M | P1 silently makes the isol8.co fallback authoritative on dev
apps/frontend/.env.example:13-20,35 | dead NEXT_PUBLIC_COMPANY_URL (1 hit repo-wide) + 3 wrong hostnames incl. a nonexistent staging | DELETE the block; fix admin-dev→admin.dev | internal | L | rebranding it teaches the next person three wrong hosts
apps/frontend/src/app/[seoSlug]/page.tsx:46 | title.replace(" | isol8","") | lockstep with seo.tsx:99-108 | user-visible | M | JSON-LD name gets the new suffix glued on
apps/frontend/src/app/{docs,marketplace,marketplace/agents}/page.tsx:32,37,26,24 | 4 JSON-LD url: overrides bypassing pageMetadata() | ←SITE_URL | user-visible | M | structured data keeps emitting https://isol8.co/... on the new domain
```

**Mobile / desktop**
```
apps/desktop/src-tauri/capabilities/default.json:7 | ["https://dev.isol8.co/*","https://app.isol8.co/*"] — app.isol8.co has NO DNS and the PROD build loads https://isol8.co/chat, matching NEITHER | CORRECT to ["https://dev.flickmeon.ai/*","https://flickmeon.ai/*"], and make it CI-injected (new jq step in both jobs) | internal | H | a literal find-replace preserves today's mismatch. OPEN QUESTION worth 20 minutes on the installed prod app: if Tauri v2's remote.urls really gates the ACL, the prod DMG has NO capabilities and all 8 invoke sites (useDesktopAuth.ts:23, useGateway.tsx:165,207, desktopNotify.ts:15, DesktopUpdater.tsx:65, DesktopFileAccess.tsx:33, DesktopPermissionsGate.tsx:35, ImportBackupDialog.tsx:31) are denied today — which would make this a P1 bug fix, not a P3 string swap
apps/desktop/src-tauri/src/lib.rs:29-31,205,534,971-973 | 4 option_env! defaults still name isol8.co | flip the compiled-in default | internal | H | P1's "domain-agnostic" claim is untested for desktop; the plan's P1 grep covers apps/backend only
apps/mobile/src/config/env.ts:11,23 · lib/webLinks.ts:22 · lib/deepLink.ts:6,35 · components/provision/ProvisionGate.tsx:79 | one config module (origin + scheme) | internal | H | plan L148 names these; the addition is that deepLink.ts:35's regex is a SECURITY test surface (deepLink.test.ts:50 pins isol8.co.evil.com)
apps/backend/scripts/vendor_openclaw_schema.sh:18,22,31 | FORK default $HOME/Desktop/agent — the clone CLAUDE.md:18 says is "pending delete… git operations hang there" | →$HOME/dev/agent-fork | internal | M | blocks the PINNED_TAG regeneration that P6 needs
```

**Repo hygiene (do these BEFORE any find-replace)**
```
AGENTS.md (untracked, 617 lines, 41 hits) | stale fork of CLAUDE.md: :147 "VPC Link V1 --> NLB" (dead), :575 api-staging.isol8.co (env never existed), :18 ~/Desktop/agent, :115 a retired mailsac account | DELETE | internal | M | a mechanical pass mints api-staging.flickmeon.ai and a wss→NLB diagram a future agent will trust
.playwright-cli/ | 217 untracked files, 191 with isol8, NOT gitignored, contains admin@/support@isol8.co and the live dev Clerk account codex.loops.20260815+clerk_test@isol8.co | DELETE or gitignore | internal | M | largest untracked isol8-bearing dir in the repo; multiplies the `git add .` hazard by 191
routine-detail.md (untracked, 1246 lines) | :167 a LIVE prod trigger fire URL https://api.isol8.co/api/v1/hooks/t/org_3H3vi…/330e49ea… | DELETE | internal | M | same disposal class as .playwright-cli
apps/backend/deploy_dev_fast.sh:15 | REPO="Isol8AI/backend" — repo is ARCHIVED (updated_at 2026-03-16); targets a workflow that no longer exists | DELETE the script | internal | L | per CLAUDE.md's one-shot-script rule
docs/runbooks/admin-rollout.md | documents PLATFORM_ADMIN_USER_IDS / isol8/{env}/platform_admin_user_ids, which exist NOWHERE in code (:13,21,30,34,37,46,54,68,89); :42 uses the wrong host form | DELETE | internal | M | a sweep renames the secret path and someone later creates it expecting it to grant admin
docs/** ~24 orphan files | shipped/abandoned plans incl. connect.isol8.com and api-staging.isol8.co — hosts that never existed | DELETE before the find-replace | internal | L | 24 dead files show as "changed" in the P1 PR and dead names get revived into the new brand
docs/growth/2026-08-17-blitzscaling-strategy.md, docs/superpowers/plans/2026-08-26-admin-gtm-dashboards.md | UNTRACKED but cited by shipping code (ops/growth.py:3,4) | git add (or drop the citations) | internal | M | doc citations that resolve on one machine only
.claude/skills/{dev-clean-slate-reset,prod-org-reprovision}/SKILL.md | 63 isol8 hits between them; GITIGNORED; .agents/ holds a DRIFTED second copy | decide the mechanism (git add -f, or move to a tracked path) | internal | M | see §3 B7 — these are destructive runbooks, not docs
root PNGs ×4, .claude/launch.json:5,18 (goosetown-landing + isol8-landing), .claude/settings.local.json:76 (a 2nd GooseTown ref) | | DELETE / rename | internal | L | GooseTown teardown leaves them behind
```

### P2 — dev on flickmeon.ai

```
.github/workflows/e2e-dev.yml:9        | default: 'https://dev.isol8.co' | →dev.flickmeon.ai | internal | M | the manual E2E lane tests the dead host and reports a red run that looks like a product regression
.github/workflows/frontend-ci.yml:77   | NEXT_PUBLIC_API_URL: https://api-dev.isol8.co/api/v1 | →api-dev.flickmeon.ai | internal | L | once P1 derives the CSP from this var, the PR gate proves the CSP for a host that no longer exists
apps/mobile/eas.json:27,28,29          | the `preview` profile is byte-identical to `development` and the plan names only `development` | →api-dev.flickmeon.ai | internal | M | internal-distribution builds keep talking to the dead dev API
apps/mobile/package.json:8             | start:dev hardcodes api-dev.isol8.co + pk_test | → | internal | L | local `expo start:dev` silently wrong (Metro inlines it)
apps/mobile/app.json:29                | ONE applinks entry, prod-only; EAS profiles override `env` only, never app config | ADD applinks:dev.flickmeon.ai + serve an AASA there | user-visible | M | P2's own E2E line (L162 "mobile dev build sign-in + universal link") CANNOT PASS
apps/backend/tests/chaos/fis/_chat_driver.py:34 | WS_URL = "wss://ws-dev.isol8.co" hardcoded while siblings have env overrides (test_s5:26, test_s8:32) | add CHAOS_FIS_WS_URL | internal | L | the chaos suite half-follows the dev flip
apps/frontend/tests/e2e/.env.example:5,8 | BASE_URL / NEXT_PUBLIC_API_URL | → | internal | L | P2's Claude-in-Chrome E2E runs against the old host
vendor:GitHub OAuth App (DEV)          | one app per env, single callback (providers.yaml:876-879); secret isol8/dev/integrations/platform-oauth/github | →api-dev.flickmeon.ai/api/v1/oauth/github/callback | user-visible | M | plan names the GitHub OAuth App once, in P3 pre-stage; the DEV one is missing, so P2's E2E can't cover GitHub connect
vercel:team domain                     | `vercel domains add flickmeon.ai --scope isol8` + ownership verification | | user-visible | H | see §3 B4
```

### P3 — prod flag day

```
apps/mobile/app.json:3,4,5,9,18,23,29,48        | name/slug/owner/scheme/bundleIdentifier/2 usage strings/applinks | →Lightbulb / lightbulb-ai-mobile / renamed Expo acct / lightbulbai / ai.flickmeon.mobile / applinks:flickmeon.ai | user-visible | H | C14 — the flip ships flickmeon env inside a co.isol8.mobile binary
apps/infra/lib/stacks/service-stack.ts:1370,1374,1378 | APPLE_IAP_{BUNDLE_ID,PRODUCT_ID,APP_APPLE_ID} — LIVE in prod today | →ai.flickmeon.mobile{,.plan.monthly} + the NEW numeric ASC id | user-visible | H | C13; :1378 fails verification SILENTLY as an ordinary rejection
apps/mobile/eas.json:45                          | ascAppId "6788944100" | →new ASC app id | internal | H | `eas submit` uploads the new-bundle IPA against the OLD record; Apple rejects after a full local build
vendor:Apple new App ID                          | Push Notifications + Sign in with Apple capabilities | enable both | user-visible | H | EAS provisioning fails at build, or SIWA ships silently broken — the exact guideline-4.8 requirement that got 0.1.0 rejected
vendor:Clerk                                     | native redirect allowlist `isol8://oauth-native-callback` (sign-in.tsx:87-90) + Apple SSO NATIVE bundle-id registration | add lightbulbai:// + ai.flickmeon.mobile | user-visible | H | Google/Apple SSO from the iOS app dead-ends with no error path
apps/frontend/public/.well-known/apple-app-site-association:6 | "WZX4U3C22Y.co.isol8.mobile" | →<TeamID>.ai.flickmeon.mobile | user-visible | H | Universal Links fail SILENTLY (Safari just opens the web page). Apple's CDN caches it — must be right BEFORE submission (next.config.ts:82-83 supplies the application/json override; :77-81 documents the silent-failure mode)
.github/workflows/desktop-build.yml:183,186,195,196,199,201,372,375,384,385,388,390 | Isol8.app / Isol8_*.dmg / isol8_rw.dmg / isol8_dmgmnt / -volname "Isol8" | follow tauri.conf.json:3 productName | internal | H | `ditto`/`hdiutil` fail under `set -euo pipefail` AFTER a ~90-min build and a completed notarization. productName "Lightbulb" (locked, L89) is SAFE for the updater (route.ts:83 suffix match) but every $APP/$DMG/$MNT shell path must be quoted
apps/desktop/src-tauri/{src/lib.rs:338,450 · src/file_access.rs:73-82 · scripts/vendor-sidecars.sh:120} | app-support dir has THREE writers: app_data_dir() FOLLOWS `identifier`; file_access.rs store_dir() and the generated openclaw shim are HARDCODED "co.isol8.desktop" | all three in one commit | internal | H | pair_state.json moves to the new dir while file_access.json and OPENCLAW_HOME stay in the old one, in the same running app
apps/desktop/src-tauri/src/file_access.rs:6,91,95,127,142 | ~/Isol8/ seeded into the file-access allowlist on first run | decide (rename or keep) | user-visible | M | the user's existing ~/Isol8/ drops out of the allowlist and the agent can no longer read files already there
apps/infra/lambda/bedrock-gate/errors.mjs:31,33,35,37,41,43,45,52-58,62,64 | TEN "isol8: " messages surfaced verbatim to the agent/Claude Code; :56 embeds "contact support@isol8.co" | →brand + new support address | user-visible | H | :56 is a MAIL-move item, not just a brand string; the plan's P3 list excludes apps/infra/lambda/** entirely
core/services/seller_display.py:34,35 | CURATOR_HANDLE="isol8" → CURATOR_BYLINE="@isol8" | →@lightbulb | user-visible | M | every curated/featured listing shows @isol8 after the rebrand
core/services/node_proxy.py:136-140 | strip-then-append " | Isol8 Desktop" (the comment at :137 says the strip exists to prevent compounding) | strip BOTH old and new; append only new | user-visible | M | a blind replace yields "Alice | Isol8 Desktop | Lightbulb Desktop"
core/services/inbound_triggers.py:293,294 | _PLATFORM_BLOCK_START/_END markers written INTO third-party webhook descriptions | keep the old string in the matcher | user-visible | H | same matcher-vs-writer trap as C20; renaming orphans every already-armed trigger's block
core/services/openclaw_backup_import.py:838,841,861 | import-wizard warnings shown to the user ("reconnect channels in Isol8", "your Isol8 provider and model", "Isol8 runs {fleet_version}") | →Lightbulb | user-visible | L | filed as "comments only" by the sweep; they are UI copy
routers/orgs.py:80 | HTTPException detail "…already has an active personal Isol8 subscription…" rendered verbatim in the org-invite UI | →brand | user-visible | L |
routers/paperclip_proxy.py:133,140 | _BRAND_REWRITES: b"<title>Isol8 Teams</title>", og:site_name | →Lightbulb | user-visible | L | the Teams page title
core/billing/metronome_funding.py:93,185 | "isol8 top-up owner=…" / "isol8 checkout top-up…" written to the Metronome COMMIT LEDGER | →brand | internal | L | historical, no backfill. TRAP: idempotent replay needs a byte-identical body — changing it mid-flight on a retried top-up yields a 409
vendor:Metronome webhook destination | https://api.isol8.co/api/v1/webhooks/metronome (metronome_webhook.py:99) — absent from the plan entirely | →api.flickmeon.ai, AFTER step 3 | user-visible | H | low-balance alerts fire ONCE per threshold crossing (:127-132), so a missed delivery is permanently lost even after the URL is fixed → the fast-gate flag never sets → paid owners spend past a depleted balance
vendor:Stripe Customer Portal configuration | account-default ToS + Privacy URLs (billing_service.py:229 passes no configuration=) | → | user-visible | M | a paying customer opens the portal and both legal links 404
vendor:PostHog | Authorized URLs / toolbar / web-analytics domains; PostHog OAuth app redirect | 4 flickmeon hosts | internal | M | project id 380894, host us.posthog.com and the phc_ write key are UNCHANGED — say so, so nobody rotates them
aws:sesv2 tenants ×3 | each carries identity/isol8.co AND configuration-set/isol8-{env}-agent-mail | re-associate BEFORE delete-email-identity | internal | H | C56
ses-inbound-stack.ts:244,248-249 + a NEW TXT at the receiving zone | DMARC rua cross-domain | + flickmeon.ai._report._dmarc.thelightbulb.company "v=DMARC1"; and TWO _dmarc records | internal | H | C29
ses-inbound-stack.ts:181,182 | apex SPF google include + a GSV token scoped to isol8.co, inside a deleteExisting:true set | drop the include, re-issue the token, AUDIT the new zone's apex TXT into the array first | user-visible | H | C30
27 × support@isol8.co (non-test, user-facing) | settings/page.tsx:109,110,123 · support/page.tsx:51,89,91,102,104 · ProvisioningStepper.tsx:988,1156,1354,1444,1804,1847,1924 · FirstRunSteps.tsx:181 · SupportTicketDialog.tsx:113,171 · docs/pages/approvals-security-privacy.md:42 · mobile settings.tsx:337,338 · PaywallScreen.tsx:118,130,303 · mobile FirstRunFunnel.tsx:283 · mobile ProvisioningStepper.tsx:424 · users.py:151 · billing_service.py:1093 · skills/isol8/SKILL.md:466 | →support@thelightbulb.company; prefer ONE exported constant in P1 | user-visible | M | see §3 B10 — the mobile OTA and the baked skill reach users on DIFFERENT clocks than a backend deploy
apps/frontend/src/content/blog/posts/*.md (6) + docs/pages/*.md (5) + index.ts + markdown.test.ts | 13 files; the blog is absent from the plan | brand + hosts | user-visible | M | public SEO surface
apps/frontend/public/goosetown*.png (3 files, 1,114,676 B incl. a 1.09 MB preview) · globals.css:1027-1144 · landing.css:7 · SupportTicketDialog.tsx:16 ("GooseTown" support CATEGORY, user-visible) | DELETE | mixed | L | dead product still shipping on every deploy and offered as a ticket category
core/services/paperclip_provisioning.py:551,562 | description="Isol8 Teams workspace" — STORED on the Aurora company row | →brand + backfill | user-visible | L | §4
```

### P5 — legal-entity-gated

```
apps/desktop/src-tauri/tauri.conf.json:50 + desktop-build.yml:117,184,305,373 + SIGNING.md:5,17,95 | signingIdentity "Developer ID Application: Prasiddha Parthsarthy (WZX4U3C22Y)" — FIVE copies | →the company-named cert, all five in ONE commit | internal | H | Apple reissues Developer ID under the org name; the pre-sign step signs with one identity and the DMG step with another, or codesign fails outright
route53domains:flickmeon.ai | OrganizationName: null, registrant prasiddha@gmail.com | →The Lightbulb Companies | internal | L | (auto-renew already on, expires 2028-08-26; isol8.co expires 2027-01-22, also auto-renew)
apps/frontend/src/components/marketplace/drawer/wizardSteps.tsx:620,625,739 | "Listings over $20 require Isol8 approval" / "Isol8 platform fee (15%)" / "Isol8 retains the right to remove listings" — a right the SELLER agrees to at publish time | →entity, with Termly | user-visible | M | contract text, not brand copy
apps/frontend/src/lib/seo.tsx:54,57 | organizationJsonLd.name; sameAs linkedin.com/company/isol8ai/ | →entity + new slug | user-visible | M | dead link if the LinkedIn slug is renamed first
```

### P6 — see §5 for the cost table. Families the plan omits entirely:

```
core/billing/bedrock_aip.py:492 | f"isol8-{env}-owner-bedrock-{owner_id}" with :491 verbatim: "'isol8-{env}-owner-bedrock-*' prefix, so this MUST start with it" | permission-boundary lockstep | internal | H | rename without moving the CDK boundary policy in the SAME deploy → EVERY per-owner Bedrock role creation AccessDenied = hard provisioning break
core/billing/bedrock_aip.py:367 | f"isol8-{env}-{owner_id}-{tier}" — the per-owner Application Inference Profile name; 38 live (36 per-owner + 2 duplicate isol8-idempotency-probe) | | internal | H | the ATTRIBUTION + PRICING unit; AIP names are immutable, so a rename = create-new + repoint isol8-{env}-bedrock-aip-owners + reissue the gate token + full per-owner reprovision, and Cost Explorer history splits
iam:role/isol8-{dev,prod}-owner-bedrock-{owner} ×45 | runtime-created by the backend, not CDK | | internal | H | a rename is PERMANENTLY partial — existing owners keep old names until reprovisioned; two conventions coexisting inside a fence
apps/infra/aws/aws-connector.yaml:3,11,17,18,35,41,43-44,61,66-68 | the CloudFormation template CUSTOMERS launch in their own accounts; :41 RoleName: isol8-connector | see §5 "never" | user-visible | H | not a repo rename — a customer-notified migration
core/services/credentials/aws_cfn.py:14,46 | _STACK_NAME = CONNECTOR_ROLE_NAME rendered into &stackName= | | user-visible | H | an owner re-launching gets a SECOND stack rather than an update
ISOL8_CONFIG_LOCK_PROTOCOL | ecs_manager.py:183 (backend writes the task-def env) ↔ openclaw-config-lock-recovery.ts:207 (CDK-baked shell reads it) | one PR | internal | H | a one-sided rename makes the test evaluate empty → boot recovery permanently takes the "not migrated yet" branch → the lock protocol never transitions. SILENT, no alarm
ISOL8_SYSTEM_SELLER_ID = "__isol8__" | scripts/ingest_companies_registry.py:38, WRITTEN to seller_id/approved_by on every company-registry listing (:124,137,146,152) | | internal | M | a DATA VALUE; renaming orphans every system-seller listing unless backfilled
sns topicName isol8-{env}-alerts-{page,warn} | observability-stack.ts:117,123 + 5 ARN consumers | | internal | M | replacement drops every subscription; email needs a fresh confirmation click
sqs isol8-{env}-agent-mail-complaint-dlq | agent-mail-outbound-stack.ts:78 (replay runbook in the comment at :74-77) | | internal | L-M | replacement destroys any un-replayed SES complaint
CfnOutput exportName isol8-{env}-* (~14) | api-stack.ts:667-691, database-stack.ts:455, service-stack.ts, paperclip-stack.ts:742,759,766 | | internal | M | CFN REFUSES to modify an export another stack imports — must land with every Fn::ImportValue consumer or the deploy hard-fails mid-stage
IAM tag value ManagedBy=isol8-backend | service-stack.ts:452,467 — an aws:RequestTag/aws:ResourceTag CONDITION, not a name | backfill tags BEFORE changing the condition | internal | H | the backend loses the ability to manage roles it created
Isol8, Isol8/Authorizer, Isol8/BedrockGate, Isol8/ECS | FOUR custom metric namespaces (live), not one "Isol8/*" family | | internal | H | the bare `Isol8` is what a `Isol8/` prefix rename misses; renaming orphans 226 alarms + 2 dashboards + all history, and any alarm whose window spans the cutover fires INSUFFICIENT_DATA
vars.CI_RUNNER / vars.DEPLOY_RUNNER | hold the literal `isol8-wsl` as their VALUE (verified live) | set both BEFORE re-labelling the runner | internal | H | highest-blast-radius item in the CI slice and it appears nowhere in the plan; a mismatch does not fail, it QUEUES FOREVER
apps/backend/tests/chaos/fis/conftest.py:71 | assert "isol8-dev-" in cluster_arn — the ONLY thing stopping the FIS chaos suite from firing at prod | pin to a named constant in the same PR as any stack rename | internal | H |
vercel:team slug "isol8" | generates all 26 *-isol8.vercel.app preview aliases | | user-visible | M | not in the table
ssm:/isol8/prod/pagerduty-page-url | a URL, not a secret — "Recreate + rotate consumers" is the wrong note | | internal | L |
```

---

## 3. Blockers and ordering traps

Things that **stop** something, in the order they must be handled.

### B0 — the mail move gates the AWS account, not the other way round

`admin@isol8.co` is at once the AWS **root-account recovery email**, 4 SNS alarm
subscriptions, and 4 Budgets subscribers — and SNS email subs need an interactive click.
**Order (all P0):** change the Organizations master email → subscribe + confirm all 4 SNS
topics + both budgets at the new address → move Aden's SSO identity → *only then* can Aden
retire the Workspace. The plan puts the Workspace retirement inside P3 step 6 with none of
this before it.

### B1 — the wildcard cert is a cross-stack CFN Export; editing it in place deadlocks the whole stage

`dns-stack.ts:21-25` `WildcardCert`; `dns.certificate` is passed as an **object** to
`isol8-stage.ts:87` (network) and `:115` (api), so CDK emits a real export. Proven from the
local synth: `devisol8devdns…` exports `ExportsOutputRefWildcardCert4A8FDF87CF4600B1`,
imported **3×** by the api template and **1×** by the network template.
`AWS::CertificateManager::Certificate.DomainName` is **Replacement**, so an in-place edit
changes an export value while it is in use → *"Export … cannot be updated as it is in use by
isol8-{env}-network, isol8-{env}-api"*.

**Order:** ONE PR adds a new `RootCert` construct, repoints `isol8-stage.ts:87,115`, and
flips every hostname (the new `CfnDomainName` must be created against a cert that already
covers it — this is why it cannot be split across two PRs). A **later, separate** run
deletes `WildcardCert`. ACM refuses `DeleteCertificate` while the `bv-*` CloudFront
distribution is still `InProgress`.

**Prerequisite:** `dig NS flickmeon.ai @8.8.8.8` must return the Route53 nameservers before
merging — `dns-stack.ts:24` uses `CertificateValidation.fromDns(hostedZone)` and CFN blocks
on ACM validation (hours, then failure). **[V] live: delegation is already correct**, so
this is a check, not a task.

### B2 — the `paperclip_email` backfill has a one-way door

Better Auth's `user.email` in Aurora is set at provision time and never updated. Once a
Clerk primary email changes, **nothing in the system knows what address Better Auth holds**
(and `lookup_owner_email` reads a DDB row that `user.updated` never refreshes). The backfill
must complete **while Clerk still holds the old addresses**; recovery afterwards is a manual
per-owner `SELECT email FROM "user"`.

**Order:** P1 field + 4 write sites + 6 read sites → deploy dev, then prod → **verify the
backfill** (`scan isol8-prod-paperclip-companies --projection-expression paperclip_email`
non-empty for every `status=active` row) → *only then* touch any mailbox.

### B3 — DCR clients never re-register on a redirect-URI change, so "owner reconnects" cannot work

`oauth_dance.py:152-156` re-registers only when the client is missing **or the scope
changed**; `:88` pins `"redirect_uris": [redirect_uri]` at registration time, and
`oauth_app_store.put` stores no `redirect_uri` field, so **nothing detects the drift**. Nine
providers register this way: `stripe` (:18), `notion` (:711), `sentry` (:734), `linear`
(:802), `attio` (:1101), `intercom` (:1210), `posthog` (:1249), `apollo` (:2000),
`cloudflare` (:2028). Existing tokens keep working (refresh grants don't send
`redirect_uri`); **reconnects and reprovisions do not.**

**Ship the redirect_uris comparison as CODE in the flip PR** (one line, permanent), or
delete every `isol8/{env}/integrations/*/oauth-app/*` secret whose stored
`client_information.redirect_uris` contains the old domain. Second-order:
`mcp_oauth.py:113,130` materializes that same `client_information` onto each container's
EFS, so a fleet re-roll must happen **after** the fix, not before.

### B4 — Vercel: team-level domain add + verification precedes any alias, and the alias runs INSIDE a deploy job

`app.ts:163,168,245,255,260` run `vercel alias`; only `www` has a preceding
`vercel domains add` (`:249-252`). An alias to a domain not on the project **exits non-zero
→ the `DeployVercelDev`/`DeployVercelProd` job goes RED after the CDK stacks have already
deployed**, leaving DNS/API on the new domain and Vercel serving nothing.

**[V]** the actual prerequisite is `vercel domains add flickmeon.ai` at the **team** level
(one team domain exists today: `isol8.co`). Since flickmeon.ai's NS already point at
Route53, verification is a TXT that can land in the same CDK PR. Ordering inside a run is
already correct (`DeployVercelDev` `needs:` `dev-…-dns-Deploy`), so moving the Vercel
A/CNAME records into `dns-stack.ts` (plan L123) puts DNS before the alias.

### B5 — `paperclip_adapter_config._GATEWAY_URL_RE` is fail-CLOSED and raises

```
_GATEWAY_URL_RE = re.compile(r"\A(?:wss://ws(?:-[a-z]+)?\.isol8\.co|ws://localhost:[0-9]+)\Z")
```
(`:59-61`). `validate_gateway_url` (`:134-139`) **raises** `AdapterConfigError`, and
`synthesize_openclaw_adapter` calls it first (`:171`). **[V] six call sites**, not four:
`paperclip_provisioning.py:666,732`, `paperclip_agent_lifecycle.py:316,443`,
`routers/teams/agents.py:571`, and `company_deploy_service.py:303` — the last has **no
isol8 literal**, so it never appears in any `git grep isol8`.

**Any env handed a `flickmeon.ai` `WS_GATEWAY_URL` before the regex derivation ships cannot
create or update a single Paperclip agent.** P1's derivation must merge and deploy strictly
before P2's `rootDomain` flip.

**Useful negative:** the regex is only on the **write** path, so the Aurora
`adapter_config.url` backfill can lag it safely. Do not add a defensive dual-accept.

### B6 — the bootstrap-email guard disarms silently, and the P1 grep pushes you into it

`paperclip_user_delete.py:49` `BOOTSTRAP_EMAILS = frozenset({"admin@isol8.co",
"admin+paperclip@isol8.co"})` is the guard that refuses to delete the Paperclip bootstrap
admin — the identity `PaperclipAdminClient` needs on **every provision, fleet-wide**. The
address is a **live Better Auth row in Aurora** + a value inside Secrets Manager
`isol8/{env}/paperclip_admin_credentials`; **no repo edit can change either.**

Renaming the frozenset (or `bootstrap_paperclip_admin.py:107 ADMIN_EMAIL`) means the guard
stops matching and an admin wipe deletes the bootstrap admin. Nine literals are in the
find-replace blast radius: `paperclip_user_delete.py:49`, `account_wipe.py:143`,
`paperclip_admin_session.py:4`, `paperclip_api_key.py:16`,
`paperclip_provisioning.py:295`, `bootstrap_paperclip_admin.py:107`,
`auth-stack.ts:198-203`, `CLAUDE.md:147-148`,
`docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md:120`.

**Add a `# rebrand: intentionally NOT renamed` marker to each in P1, before any sweep runs.**
Note the obvious fix (match the live secret's email) was already evaluated and **rejected
in-code** at `paperclip_user_delete.py:44-48`: *"a read whose failure mode is 'delete the
bootstrap admin'. Not worth it for a two-element allowlist."* Any P0/P1 fix has to argue past
that comment.

### B7 — the two destructive ops runbooks are gitignored, so no PR forces them to move in lockstep

`.claude/skills/dev-clean-slate-reset/SKILL.md:29-32` instructs: *"**Do NOT protect
`admin@isol8.co`** when deleting Better Auth users. Only `admin+paperclip@isol8.co` holds
`instance_admin`"*, and `:199-200` name the dev/prod bootstrap identities.
`prod-org-reprovision/SKILL.md:113,119` carries the same class. `.claude/` is gitignored
(`.gitignore:58`) and `.agents/` holds a **drifted** second copy.

If these lag the `BOOTSTRAP_EMAILS` change, the next dev wipe protects a user that no longer
holds `instance_admin` and deletes one that does. **Mitigation: move both into a tracked path
in P1.**

### B8 — the Clerk domain change → deploy gap is a full prod auth outage, and its length is the deploy's length

After "Change domain", Clerk mints tokens with `iss = https://clerk.flickmeon.ai`. The
backend reads its issuer from an `ecs.Secret` resolved at **task start**
(`service-stack.ts:1540`), and the WS authorizer reads a **CDK literal**
(`api-stack.ts:353-358`). Every authed REST and WS call 401s until the ECS rollout finishes.
`core/auth.py`'s JWKS cache does not help — the mismatch is the issuer string.

**Minimise:** have the secret and both GH publishable-key secrets already updated before
dispatching, so step 3 is the only clock. And Clerk issues the five CNAME targets **only
after** the domain change, and won't provision TLS until they resolve — so either
hand-create the five records inside the window and CDK-ize them in a follow-up PR (lazy,
recommended), or budget **two** prod dispatches. The plan implies both.

### B9 — `PINNED_TAG` byte-equality hard-gates every backend PR

`apps/backend/tests/chaos/_schema/PINNED_TAG:1` = `ghcr.io/isol8ai/agent:2026.8.1-isol8.1`,
asserted byte-equal against `openclaw-version.json#upstream` by
`tests/chaos/test_schema_staleness.py:96-101`. The test carries **no marker**, `pytest.ini:7`
has no `-m` deselect, and `backend.yml:77` runs `uv run pytest tests/`. So the instant
`#upstream` moves to a new GHCR org, **every backend PR goes red** until
`scripts/vendor_openclaw_schema.sh` is re-run in the same PR — and that script does a full
`pnpm install --frozen-lockfile && pnpm build` of the fork checkout (`:36-38`), against a
default path that is currently the dead `~/Desktop/agent` clone. Budget it as a real step.

### B10 — the extended image tag is DERIVED, so the fork release is the chicken-and-egg

`build-openclaw-image.yml:123,126` computes `TAG="${UPSTREAM}-${SHORT_SHA}"` from
`openclaw-version.json#upstream.split(':')[1]`. The instant the fork publishes a
`-lightbulb.1` tag, the ECR tag carries it **with no workflow edit**, and
`openclaw_version.py:163` stops matching (fail-open-to-zero).

**Order, one PR each:** (1) fork PR renames the tag scheme + cuts the tag; (2) **one atomic
isol8 PR** bumping `openclaw-version.json:3` + `apps/infra/openclaw/Dockerfile:84` +
regenerated `PINNED_TAG` + the dual-spelling regex + the 15 tag literals in
`test_openclaw_version.py` — any two of those split across PRs is a red suite or a silent
config regression; (3) flip `dev.tag`, validate one dev container, flip `prod.tag`.
**Land the dual-spelling regex at least one release before the first renamed tag.**

### B11 — the P3 "brand swap" reaches ZERO containers without a pin flip + reprovision

`build-openclaw-image.yml:22,33` are `push` path filters on `skills/**` and `plugins/**`.
Merging the brand-swap PR publishes a **new ECR tag and deploys nothing** — the workflow
never writes the pins (`:270` tells a human to bump `dev.tag`). The flip window (L177-181)
has no pin-flip step, and step 4 says "fleet re-roll" without naming a tag.

**Real shape: brand-swap PR → pin-flip PR for `dev.tag` → dev validate → pin-flip PR for
`prod.tag` → fleet REPROVISION via the admin UI.** Three merges plus a non-merge. And per
C23, "roll" must read **reprovision** or none of the EFS-side work lands.

Corollary: one image serves **both** envs, and `skills/isol8/SKILL.md` hardcodes
`isol8.co/chat` at ~26 lines — it cannot be env-pinned. Flipping `dev.tag` to a
flickmeon-text image while prod stays on the isol8-text image is fine; the inverse is not.

### B12 — the GitHub org rename is a two-key operation and one key has no code home

**Order is forced:** widen **both** OIDC trusts to accept old AND new `sub`
(`container-stack.ts:492` by PR+deploy; `isol8-dev-github-actions` by hand in IAM) → rename
the org → land the PR fixing the in-repo literals → drop the old `sub`. Rename first and
**every deploy job fails at `configure-aws-credentials` with no repo change that can fix it,
because fixing it requires a deploy.** The same provider serves
`isol8-openclaw-image-builder`, so an un-updated `sub` kills both image builds too.

**Sequence the two forks around the rename**, not with it: land the agent-fork `IMAGE_NAME`
edit **before** (hardcoded, won't move), and the paperclip digest re-pin **after** (derived,
moves itself). Re-grant the GHCR package-level "Manage Actions access" to the renamed repo
(`build-paperclip-image.yml:80`) or `docker build` 403s on `FROM`.
`ghcr.io/isol8ai/*` redirect behaviour after an org rename is **unverified** — pin by
digest across the window so the fleet doesn't depend on the answer.

### B13 — the runner label is a physical prerequisite with no fallback

`build-openclaw-image.yml:105-109` **deliberately** hardcodes `[self-hosted, isol8-wsl]`
outside the `CI_RUNNER` switch (*"flipping CI_RUNNER back to hosted to escape a box outage
would silently reintroduce the OOM here"*). Same literal at `runner-maintenance.yml:34` and
`scripts/switch-ci-runner.sh:10,16,22,32`. **[V]** `vars.CI_RUNNER` and `vars.DEPLOY_RUNNER`
hold `isol8-wsl` as their **value**.

**Order:** set both repo variables → re-label the runner in GitHub settings → edit the three
workflows + the script in one PR. A mismatch does **not** fail — it queues indefinitely with
no signal.

### B14 — container boot scripts are absolute paths with a FATAL guard on one side and CDK on the other

`container-stack.ts:818` `[ -x /usr/local/bin/isol8-local-sqlite-boot.sh ] || { echo
'[boot] FATAL: …'; exit 1; }` and `:883` `exec setpriv … /usr/local/bin/isol8-local-sqlite-boot.sh`;
`:785` is the soft warp equivalent. The CDK task-def command deploys fleet-wide on merge;
the image reaches a container only on its own roll. **Neither side can move first.** Only
safe path: image ships **both** names (symlink) → flip CDK → reprovision the fleet → a later
image drops the old name. In practice: **don't rename these.**

### B15 — the session-key prefix is a widen-then-narrow, two-image rollout

`containers/config.py:2011` `allowedSessionKeyPrefixes: ["isol8-", "hook:"]` (written into
every owner's `openclaw.json`) ↔ `agent_mail_thread.py:37` `_PREFIX = "isol8-email:"` ↔
already-minted keys in every container's SQLite. **Order:** allowlist accepts BOTH prefixes
(image roll) → flip the backend prefix → narrow the allowlist. Renaming the prefix without
widening first makes the gateway **reject** the key; renaming it at all cold-starts every
live email thread.

### B16 — the Paperclip session cookie is a hard break, not a re-login

`paperclip_session_cookie.py:55` `_COOKIE_DOMAIN = ".isol8.co"`, set on responses at `:195`
and `:205`. Once the API host is `api.flickmeon.ai`, a `Set-Cookie` with `Domain=.isol8.co`
is **rejected outright** by the browser (RFC 6265 §5.3). The cookie is never stored, so
`read_session_cookie` (`:145-152`) finds nothing on **every** request and every request
cold-mints. `_COLD_MINT_BUFFER_SECONDS = 10.0` (`:69`) bounds it to ~1 Better Auth sign-in
per user per 10s — but Better Auth's sign-in limit is **per-IP** and every backend task
shares one NAT IP. **Fleet-wide 429 storm on the Paperclip BFF.**

**This file must ship in the same deploy as the frontend host flip. There is no partial
state that works.** Do **not** also rename the cookie NAMES (`isol8_paperclip`,
`isol8_pc_bind`) in that deploy — that doubles the sign-in storm. P6, separately.

### B17 — `PAPERCLIP_PUBLIC_URL` must match across two stacks that deploy at different times

`service-stack.ts:1339-1342` (the `Origin` header the BFF sends) and
`paperclip-stack.ts:374-375` (Better Auth's `trustedOrigins`) must match **exactly**, and
the comments in both say so. They are separate stacks in the same run, so there is a real
window where one has flipped and the other hasn't → Better Auth 403 on `/api/auth/*` →
Teams 401s → the global `PaperclipApiError` handler thrashes re-minting. **Expect a few
minutes of Teams noise; don't diagnose it as a bug.**

### B18 — the Aurora `adapter_config.url` backfill window is unavoidable

The UPDATE must run after the new WS domain resolves and before any agent wakes — but the
agents ARE what wake to do work. Between the prod deploy and the Data API UPDATE, every
heartbeat/issue run fails to connect. **Budget the window, pause routines, make it short and
expected.**

### B19 — negative test assertions go vacuous on a blind find-replace

These stop testing anything the moment the literal moves but the assertion doesn't. Each
must be **re-anchored to the new domain**, and the flip PR must show these lines changed:

`mobile-ota.yml:110` (`grep -aq 'api-dev.isol8.co' && FAIL=1` — the poison-OTA detector that
exists *because* a no-env export once shipped an empty Clerk key; its target literal is
`apps/mobile/src/config/env.ts:11`), `ProvisionGate.test.tsx:113,138,195,277,296,449`,
`settings.test.tsx:98`, `webLinks.test.ts:23`, `desktopPermissionsStorageEvent.test.ts:18`,
`deepLink.test.ts:50,52,53,58`, `test_marketplace_purchases.py:994-996`,
`test_paperclip_adapter_config.py:38-44`, `lambda/ses-inbound/test_index.py:671-678`.

`mobile-ota.yml:106` (positive) and `:110` (negative) must change in the **same commit** as
`env.ts:11` — and the guard fails *after* `eas export`, so a half-change burns a build.

### B20 — mail replace before host replace, never a single sed

Three families map to **two** domains:

1. `s/@isol8\.co\b/@thelightbulb.company/` restricted to **bare-apex** mailboxes (106
   test lines + 27 non-test) — including `observability-stack.test.ts:290` (the SNS **paging
   endpoint**) and `ses-inbound-stack.test.ts:167` (the DMARC **rua**).
2. `s/@([a-z0-9-]+(\.dev)?)\.isol8\.co/@\1.thelightbulb.company/` for **agent mail** (139 test lines).
   (was `.flickmeon.ai` — corrected to the mail domain per the 2026-08-27 decision.)
3. `s/isol8\.co/flickmeon.ai/` for everything left (599 hostname lines).

Reversing (1) and (3) silently points alarm paging and DMARC reporting at a nonexistent
`@thelightbulb.company` mailbox. One file (`test_ses_email.py`) contains **both** targets.

**Verify:** `git grep -n '@flickmeon\.ai' -- '*/tests/*' '*__tests__*' | grep -vE '@[a-z0-9-]+\.(dev\.)?flickmeon\.ai'`
returns zero — no bare-apex mailbox on the product domain.

### B21 — the SES receipt rule set is an account+region singleton, in the prod stage

`ses-inbound-stack.ts:428-453` owns `SetActiveReceiptRuleSet`; there can be exactly one per
account+region (**[V] live:** `AgentMailRuleSetF43CB31C-G8kkZtVR92az`, active, one catch-all
rule with `recipients: .isol8.co,isol8.co`). So old and new inbound cannot run in parallel
**except** by widening `recipients` on that one rule — which needs a code change (C2) **and
a prod dispatch during P2**.

### B22 — Google Pub/Sub push audience dies at flag day and only the owner can fix it

`routers/paperclip_trigger_proxy.py:422-423` computes `audience = trigger_fire_url(owner_id,
public_id)` **fresh** (the new domain) and rejects the token if `aud` doesn't verify. The
owner's push subscription was created in **their own** Google Cloud project with the audience
set to the OLD proxy URL (`inbound_triggers.py:1704-1705`), and `_GMAIL_MAINTENANCE_CLAUSE:250-256`
has **no `{address}`**, so the twice-weekly renewal re-arms the Gmail *watch* and never
touches the subscription. **Every `owner_setup` gmail trigger dies silently.** Owner notice
required; nothing self-heals.

### B23 — three more auto-registered third-party webhooks the plan misses

`inbound_triggers.py:559-569` `_create_vercel_webhook` (**in neither the plan nor any
sweep**), `:504-513` `_create_github_hook` (plan names it), `:1433-1480` per-owner Meta asset
subscriptions (plan covers only the *platform* Meta webhook). All P3, all dead-host, none
self-heal. Plus 5 `guided_paste` providers (Stripe, Sentry, Linear, Attio, PostHog) whose
`{proxy_url}` the **owner** must re-paste in their own dashboard.

### B24 — CFN stack deletes must respect exports (GooseTown)

Per env, leaf→root: `worldgm, sprite-worker, gameserver, compute, api, observability,
eventing, storage, database, auth, cluster, dns, network`. The two `alb-logs` buckets
(43,855 / 39,008 objects) have **no** `CustomS3AutoDeleteObjects` lambda — empty them first
or `DELETE_FAILED`. Cloud Map namespaces own the two `.local` zones: deregister instances →
`delete-service` → `delete-namespace`.

### B25 — `pnpm --filter` on a non-existent scope exits 0 (silent-green CI)

Verified in this repo: `pnpm --filter @lightbulb/frontend run lint` →
`No projects matched the filters` , **EXIT=0** (pnpm 9.15.0). Renaming `@isol8/*` in
`package.json` without the same-PR edits to `frontend-ci.yml:62,65,72,78`,
`mobile-ci.yml:65,68,71,74,77,80,85`, `infra-ci.yml:79` produces **fully green CI that ran
nothing** — 12 steps across three gate workflows, including the infra synth and the entire
mobile suite. After the rename, deliberately break one file and confirm the job goes red.

The scope rename also touches `next.config.ts:55` `transpilePackages: ["@isol8/shared"]` —
`packages/shared` ships TS **source with no build step** and there is **no tsconfig path
alias** (`apps/frontend/tsconfig.json:21-23` maps only `@/*`), so a miss fails `next build`
on untranspiled TS and looks like a bundler bug. And it regenerates `pnpm-lock.yaml`, so it
cannot land in a frontend-only PR.

### B26 — Expo rename ordering, and the fingerprint wall

Rename `owner` + slug **on expo.dev first**, then flip `app.json:4,5`. Reversed, `eas build`
/ `eas update` / `mobile-ci.yml`'s `expo export` all fail on a slug/owner mismatch against
`projectId: 2def259d-…` — and `mobile-ota.yml` runs on **every merge to main**, so a
merged-but-unrenamed `app.json` breaks the OTA job on main.

`runtimeVersion.policy: "fingerprint"` (`app.json:11-13`) means the bundle-id change rotates
the fingerprint, so **OTAs published after the flip reach zero installed binaries**. Correct
behaviour, harmless today (iOS never shipped) — but a green `mobile-ota` run is **not**
"the app is updated", and there is no OTA bridge.

### B27 — the deep-link scheme is a same-day, cross-artifact flip

`desktop-callback/page.tsx:41` and `checkout_redirect/[status]/page.tsx:70` **emit**
`isol8://`; the Rust host **registers** it at build time (`tauri.conf.json:57`,
`lib.rs:1018,1080,1087,1096`). If the web pages flip first, every desktop sign-in dead-ends
at *"Opening Isol8 desktop app…"* with nothing happening and no error.

**Safe shape: emit both schemes for one release** (`lightbulbai://` primary, `isol8://`
fallback anchor) — a two-line cost. The plan sets `lightbulbai://` as a default (L57) and
lists the emitters (L147) but records no ordering constraint.

**Do NOT "helpfully" add the new Clerk host to `lib.rs:976-998 is_oauth_url`** — it matches
`accounts.google.com`, `appleid.apple.com`, `clerk.accounts.dev`, `clerk.com`, never
`clerk.isol8.co`, and a find-replace won't touch it. The custom-domain popup is handled by
`href.includes('clerk')` at **`lib.rs:1053`**, which keeps working. Adding the host would
start bouncing `/v1/client/handshake` to Safari — the white-screen failure the comment at
`:986-991` warns about.

### B28 — the Atlas bundle fingerprint is the ONLY thing that heals `build.lobster`'s baked URL

`atlas_seed/__init__.py:174-177` says it verbatim: *"Deliberately NOT covered: the per-owner
bridge URL."* The P3 brand-swap PR happens to edit `atlas_seed/files/AGENTS.md:82`, which
bumps `_bundle_fingerprint()` → `auto_refreshed` → `force` → `build.lobster` is re-staged
with the fresh URL. **That is accidental coupling.** If the domain flip and the brand swap
ever ship in separate PRs, Atlas silently keeps curling a dead host at its register step.
Either guarantee the brand-swap PR touches `files/`, or run
`scripts/restage_atlas.py --all-from-containers --apply`.

### B29 — `{{ISOL8_BRIDGE_URL}}` has a refuse-to-proceed rule on the seed side

`atlas_seed/__init__.py:103` defines the placeholder; `:366` substitutes it; the **seeded
files** carry it at `BOOTSTRAP.md:20,21,114`, `build.lobster:139`, `TOOLS.md:31` — and
`BOOTSTRAP.md:21` instructs Atlas to **refuse to proceed** if it still reads the literal.
Renaming the placeholder in `__init__.py` without renaming all five file-side occurrences
(or vice versa) **bricks every new Atlas build**. Risk H.

### B30 — the marketplace S3 slice makes the plugin-id rename un-backfillable

`marketplace_service.py:107-111` writes immutable, versioned artifacts; the slice's
`plugins` block (`catalog_slice.py:475`) includes `"isol8-marketplace"`
(`containers/config.py:2074`). Every already-published `openclaw-slice.json` deep-merges a
dead plugin key into the buyer's `openclaw.json`. **No backfill is possible by design** —
either keep the id or carry a compat alias in the image.

### B31 — `isol8.hire_suggestion` cannot be renamed in a frontend PR

Emitter is the baked plugin (`plugins/isol8-marketplace/src/index.ts:92,107` +
`dist/index.js:81,91`); consumer is Vercel (`HireCard.tsx:39,69,74`). Frontend deploys on
merge; the image reaches an owner on a manual roll, and idle containers self-roll only on
next wake — so old and new emitters coexist for an **unbounded** window, during which
`parseHireSuggestion` returns null and the card renders as a plain tool pill. **Record it as
an explicit KEEP in P6**, or ship dual-accept → roll the whole fleet → drop the old.

Same class: `plugins/isol8-marketplace/dist/` is a **committed build artifact** (`.gitignore:6-8`:
*"CI has no step that rebuilds it for you, so a src/ change without a matching dist/ change
ships the OLD tool"*). Every `src/` edit needs a matching `dist/` commit.

### B32 — `isol8-setup-seed.json` is a cross-repo contract with a fork reader

Backend writes it (`friday_identity.py:79,175`); the fork reads-converts-deletes it
(`ensureAgentWorkspace`, at the released tag `v2026.8.1-isol8.1:src/agents/workspace.ts:99`
filename / `:103` env `ISOL8_NATIVE_SETUP_SEED`). Both halves in one image roll. Sentinels
already on EFS are only cleaned by the **old** filename — leave the old constant as a second
read path for one roll, or accept a stale dotfile (harmless; the fork's collision guard fails
safe). Backend-first = the sentinel is inert for every fresh provision until the roll lands,
and every agent re-runs OpenClaw's native onboarding.

### B33 — do the storage-key renames inside the P3 domain PR, or never

The origin change already empties every store (C37). Deferring converts a free change into
"everyone loses their workspace layout, active session, onboarding draft and attribution".

### B34 — the prod dispatch queue rule still applies

`deploy.yml:11` `concurrency: group: isol8-deploy, cancel-in-progress: false`. A pending
dispatch is killed as `completed/cancelled` with **0 prod jobs** by any merge to main.
**Dispatch into an EMPTY queue and verify by counting the 14 prod jobs**, never by reading
the run conclusion. Note that renaming the concurrency group (P6) means an in-flight run
under the old group no longer serializes against the new one — sequence that into an empty
queue too.

Related: **`deploy.yml` is generated.** Every `apps/infra/lib` change needs
`cd apps/infra && npx cdk synth` (OrbStack running) with the regenerated file committed in
the same PR. Local synth **rewrites silently**; `git status` after synth is the check.

### B35 — `www.isol8.co` gets re-added by any prod dispatch from a pre-flip ref

`app.ts:249-250` → `deploy.yml:2371-2372` runs
`vercel domains add www.isol8.co frontend --force` on **every** prod promotion. P3 step 5
retires that domain. The flip PR must land before any subsequent dispatch.

### B36 — the support-mailbox retirement is not deploy-atomic with the strings

The 27 `support@isol8.co` sites live in the frontend bundle (Vercel deploy), the **mobile
OTA**, and `skills/isol8/SKILL.md:466` — which reaches the fleet only on an **image roll**.
**Sequence: string change → deploy → OTA → image roll → *then* retire the mailbox.** Or set
up forwarding and keep the Workspace domain alive past P3 — which conflicts with the hard-
cutover constraint, so flag it as the one deliberate exception.

### B37 — process-global caches need a restart, not a config change

`curator._curator_user_id` (`curator.py:29,47`) and `agent_mail_tenant._ensured` (`:27`) are
process-lifetime memos with **no TTL** — the docstring at `curator.py:12-14` says *"restart
the backend if it ever changes"*. A prod deploy supplies the restart, so no action; but do
not expect a hot config change to take. (`_ensured`'s expiry is what makes SES tenant
re-association self-heal on first send after the deploy.)

### B38 — `is_internal` is stamped into PostHog at emit time and PostHog events are immutable

Between the Clerk email flip and the deployed constant flip, internal runs are stamped
`is_internal=false` (`run_analytics.py:160`) and land in the EXTERNAL segment of
`/admin/growth` (`ops/growth.py:109`) **permanently**, plus a 1h cache tail
(`_INTERNAL_TTL_S = 3600.0`). Adding the admins to `_PLATFORM_ADMIN_EXTRA_EMAILS` (B-order
step 1) makes the window **zero**.

---

## 4. Data-at-rest backfill spec

Consolidated from the data-at-rest sweep and its adversarial verification. **Corrections
from the verification are marked [V] and supersede the sweep.** One script,
`apps/backend/scripts/backfill_domain_flip.py`, dry-run by default (`--apply`),
`--env {dev,prod}`, `--old-domain isol8.co --new-domain flickmeon.ai`, `--owner-id`
repeatable / `--all-from-containers` — the CLI shape `restage_atlas.py` and
`rematerialize_instagram_skill.py` already use. **Delete it once run** (CLAUDE.md's
one-shot-script rule).

### 4.1 Timing split

**FLAG-DAY (must complete inside the P3 window):** D1, D2, D3, D4, D5, D6, D9, D10, D14, A1,
A2, A3, plus the new Friday heal migration (F1).
**AFTER / self-healing (say so in the plan so nobody writes a script):** D7, D8, D11, D12,
D13, E4, E5, S1, and everything in §5's KEEP rows.
**Owner-action-only (no backfill exists):** O1, O2, O3.

### 4.2 DynamoDB

**D1 — `agent-email-addresses.address` is the PARTITION KEY (no UPDATE path for the key)**
- Writer: `core/repositories/agent_email_repo.py:53` `"address": normalize(address)`
  (PK per `database-stack.ts:171`), called from `core/services/agent_email.py:120`;
  address built at `agent_email.py:59` `f"{agent}@{company}.{domain.strip().lower()}"`
  (**[V]** `:59`, not `:63`).
- Consumers: `routers/ses_inbound.py:199,390` route inbound by `get_by_address(candidate)`
  on the To: header; `core/services/agent_mail_send.py:158` resolves outbound.
- **Self-heals? NO, and it is dead both ways.** `agent_email.resolve_from_address`
  (**[V]** `:212-222`) returns the stale `status=="active"` row **before**
  `ensure_agent_address` (**[V]** `:225`) can re-mint, and that only runs on the agent's
  first owner-directed send anyway. Inbound to the new domain finds no row and is dropped;
  outbound uses the old address as SES `From`, which SES refuses once the identity is gone.
- **Mechanism: scan + delete-and-put.** `agent_email_repo.put` **cannot** be reused — it
  stamps a fresh `created_at` (documented `:45-50`), and `owner_facts._lookup_email_address`
  sorts on the earliest `created_at`. Write the item directly, preserving `owner_id`,
  `agent_id`, `verified_senders`, `created_at`, `status`.
- **[V] dev shape ≠ prod shape:** `isol8-stage.ts:72` makes dev's domain `dev.isol8.co`, so
  addresses are `@{slug}.dev.isol8.co`. Match on the **substring** `isol8.co`, not on the
  apex form, or the rehearsal proves nothing.
- Dry-run: `aws dynamodb scan --table-name isol8-{env}-agent-email-addresses --projection-expression address | grep isol8.co`
- Proof: 0 hits; then send a real message to `friday@{slug}.thelightbulb.company` and see it land.

**D2 — `agent-email-addresses.verified_senders` holds the OWNER's email**
- Writer: `agent_email.py:124` `verified_senders=[owner_email] if owner_email else []`.
- Consumer: `agent_email.sender_allowed` (**[V]** `:140-158`) — the exact-match allowlist
  deciding whether inbound mail may **wake** the agent.
- Affects only owners whose Clerk email is `@isol8.co` (our own team).
- **Self-heals? NO.** Fails silently (returns False, no error).
- **[V] Mechanism: `agent_email_repo.update_settings(address, verified_senders=…)` EXISTS**
  at `:148-194` (SETs under `ConditionExpression="attribute_exists(address)"`). This is
  **one call**, not a fold into D1's delete+put. Do NOT hand-roll a scan/put/delete — three
  helpers already exist (`update_settings`, `delete_by_address:139-142`, `scan_all:120-136`).
- Dry-run: `scan … --filter-expression 'contains(verified_senders, :d)'` with `:d = "isol8.co"`
- Proof: 0.

**D3 — `users.email` goes stale forever**
- Writer: `core/repositories/user_repo.py:25-41` `put()` — the **only** writer of the email
  attribute, called from `routers/webhooks.py:701` (`user.created` **only**) and
  `routers/devices.py:30` (guarded by "row absent"). `routers/users.py:39` calls it with no
  email.
- **`user.updated` (`webhooks.py:723-760`) syncs Stripe only — no `user_repo.put`.**
- Consumer: `paperclip_owner_email.lookup_owner_email:51-56` reads it **FIRST**, falling
  back to live Clerk at `:66-83`; `ops/ecs_ops.py:88-95` shows it in the admin fleet list.
- **Self-heals? NO.** And it makes `provision_member` **non-deterministic** — accidentally
  correct for owners whose row survived, broken for those whose row is missing.
- **Decision required (P1), not just a backfill.** Either (a) make `lookup_owner_email` read
  `PaperclipCompany.paperclip_email` (preferred — folds into C6), or (b) add
  `user_repo.put(user_id, email=new_email)` to the `user.updated` branch. **(b) alone makes
  things worse** — it breaks the previously-accidentally-correct path too.
- Dry-run: `aws dynamodb scan --table-name isol8-{env}-users --projection-expression "user_id,email" | grep '@isol8.co'`

**D4 — `paperclip-companies` gains `paperclip_email` (net-new field, not a rewrite)**
- Writers to add: `paperclip_provisioning.py:497,810,1019,1091` (all four
  `PaperclipCompany(…)` constructions) + repo edits at `paperclip_repo.py:~194,372,467`.
- **[V] Backfill source is AURORA, not Clerk:**
  `SELECT email FROM "user" WHERE id = :paperclip_user_id` via the Data API. Seeding from
  Clerk after a mailbox move writes an address Aurora does not have and cements the 401.
- **Must complete before any Clerk primary email changes** — see §3 B2.
- Proof: `scan isol8-{env}-paperclip-companies --projection-expression paperclip_email` →
  non-empty for every `status=active` row.

**D5 — `company-slugs` / `marketplace-sellers` pre-flight collision scan (P1, read-only)**
- `company_slug_repo.py:35` `"slug": normalize(slug)` is a conditional put keyed on the
  slug; `company_identity.RESERVED_SLUGS:34-107` and `seller_profile.RESERVED_HANDLES:43`
  are checked **at allocation time only**. Adding `lightbulb`/`lightbulbai`/`flickmeon`
  does **not** evict a row a tenant already holds.
- **Risk:** a tenant sitting on `lightbulb` owns `@lightbulb.flickmeon.ai` as their agent-
  mail subdomain — our own brand as someone's mail domain.
- **REPORT ONLY.** Evicting a tenant slug is an owner decision.
- Dry-run: `scan isol8-prod-company-slugs --filter-expression 'slug IN (:a,:b,:c)'`; same for
  `marketplace-sellers` handles.

**D11 — `agent-mail-inbox` reply keys: the plan overstates the risk**
- **[V] Composite key**, not a bare PK: `agent_mail_inbox_repo.py:81` `address` (PK) +
  `:82`/`:116` `received_key` (SK). `:37` `reply_key = f"{address}/{received_key}"`;
  **[V]** 30-day TTL at `:25`.
- After the flip, `agent_mail_send.py:158` `get_by_address` resolves the **new** address →
  empty inbox → the agent loses ≤30 days of stored history. Replies against an
  already-issued `reply_key` **still resolve** (looked up by the key's embedded old address).
- **Self-heals in 30 days via TTL. Do not write a backfill — say so in the plan.**

**D13 — `marketplace-takedowns.filed_by_email`, `marketplace-listings.manifest_json`**
- `marketplace_repo.py:1067` (our team's `@isol8.co` on takedown rows);
  `marketplace_service.py:181` (`"manifest_json": manifest`, an immutable copy of seller
  brand text). Cosmetic; no backfill.

### 4.3 Paperclip Aurora (Data API; **`paperclip_sql.py:74-75` is SELECT-only** — the UPDATEs need their own `rds_data_client().execute_statement`, as `paperclip_user_delete.py:87-90` already does)

**A1 — `agents.adapter_config->>'url'` (the WS gateway URL)**
- Writers: `paperclip_adapter_config.py:196`
  `"url": f"{gateway_url}?token={service_token}&clientId={PAPERCLIP_OPERATOR_CLIENT_ID}"`,
  fed by `_ws_gateway_url` (`paperclip_provisioning.py:209-222`, hardcoded
  `wss://ws.isol8.co` / `f"wss://ws-{env}.isol8.co"`).
- **Six call sites** (**[V]**, not four): `paperclip_provisioning.py:666,732`,
  `paperclip_agent_lifecycle.py:316,443`, `routers/teams/agents.py:571`,
  `company_deploy_service.py:303`.
- **Self-heals? NO.**
- **Mechanism: string `replace()` on the host prefix ONLY, never a rebuild** — a `jsonb_set`
  that reconstructs from the host destroys a live service-token JWT and a live device
  pairing.
  ```sql
  UPDATE agents SET adapter_config = jsonb_set(adapter_config, '{url}',
    to_jsonb(replace(adapter_config->>'url', 'wss://ws.isol8.co', 'wss://ws.flickmeon.ai')))
  WHERE adapter_config->>'url' LIKE '%isol8.co%';
  -- run once per host form: 'wss://ws.' and 'wss://ws-{env}.'
  ```
- **Do not clobber the sibling key** `adapter_config->>'paperclipApiUrl'`
  (`paperclip_adapter_config.py:219` = `settings.PAPERCLIP_INTERNAL_URL`, the Cloud Map
  host `http://paperclip.isol8-{env}.local:3100`) — P6 only.
- Dry-run: `SELECT id, adapter_config->>'url' FROM agents WHERE adapter_config->>'url' LIKE '%isol8.co%'`
- **Token-intact invariant (before AND after):**
  `SELECT count(*) FROM agents WHERE adapter_config->>'url' NOT LIKE '%token=%'` → must stay 0.

**A2 — `routines.description` carries the dead trigger fire URL literally**
- Writer: `inbound_triggers.py:1492-1496` `.format(…address=proxy_url…)` →
  `:1502-1506` `patch_routine(body={"description": …})`. Column
  `routines.description text` (`~/dev/paperclip-fork/packages/db/src/schema/routines.ts:38`).
- **Self-heals? NO** — descriptions are never re-rendered. And a model that follows the
  prompt over the state file re-arms onto the dead host.
- ```sql
  UPDATE routines SET description = replace(description, 'https://api.isol8.co', 'https://api.flickmeon.ai')
  WHERE description LIKE '%isol8.co%';   -- + the api-{env} form
  ```
- Dry-run: `SELECT id, company_id FROM routines WHERE description LIKE '%isol8.co%'`

**A3 — `companies.description = 'Isol8 Teams workspace'`**
- Writers: `paperclip_provisioning.py:551` and `:562` (both call sites).
- `UPDATE companies SET description = 'Lightbulb Teams workspace' WHERE description = 'Isol8 Teams workspace'`
- Proof: `SELECT count(*) FROM companies WHERE description LIKE '%Isol8%'` → 0.
- **Also live and user-visible:** the prod Paperclip company is literally **named `Isol8`**
  (`admin-gtm-dashboards.md:114`). `companies.name` needs the same treatment — the company
  header in every Teams view. Confirm the column before writing it.

**A4 (P6 only) — the marketplace listing keys are in `approvals.payload`, NOT `agents.metadata`**
- **[V]** `marketplace_hire.py:61` `build_hire_approval_payload` → `:86-89`
  `"metadata": {LISTING_SLUG_KEY: …, LISTING_ID_KEY: …}` → `:298`
  `"payload": build_hire_approval_payload(…)`; read back at `:100` as
  `payload["metadata"][…]`. Sink column is `approvals.payload jsonb`
  (`schema/approvals.ts:14`), and `marketplace_hire.py:48` says so explicitly.
- The sweep's `UPDATE agents SET metadata = metadata - 'isol8_listing_id' …` targets the
  wrong table and returns **0 rows**.

**Confirmed negative:** `routine_triggers` has **no** URL column
(`schema/routines.ts:125-141`). `issue_work_products.url` (`schema/issue_work_products.ts:33`)
may hold agent-authored isol8.co links — cosmetic; not queried against prod.

### 4.4 EFS (per owner root `/mnt/efs/users/{user_id}/`)

**E1 — `google-watch/{public_id}.json` bakes the dead fire URL and the routine RE-ARMS ONTO IT FOREVER**
- Writer: `inbound_triggers.py:1541` `"address": proxy_url` via `_write_watch_state`
  (`:266-269`, `atomic_write`); `proxy_url = trigger_fire_url(owner_id, public_id)`
  (`:1298`) = `f"{settings.PUBLIC_API_BASE}/api/v1/hooks/t/{owner_id}/{public_id}"`
  (`trigger_fire_url.py:29`).
- **Self-heals? NO — it actively RE-BREAKS itself.** The maintenance clause
  (`:233` `_CALENDAR_MAINTENANCE_CLAUSE` `'"address":"{address}"'`, `:242`
  `_DRIVE_MAINTENANCE_CLAUSE` `--webhook-url={address}`) tells the routine's own agent,
  twice a week (`_GOOGLE_RENEWAL_CRON`, **[V]** `:211`), to read the file and re-arm the
  Google push channel at that address — indefinitely.
- **Mechanism:** rewrite `address` in every `users/*/google-watch/*.json` **and** A2's
  routine descriptions, then let the next renewal tick re-arm (or force it). Fixing one
  without the other leaves the other re-breaking it.
- Dry-run: `grep -rl 'isol8.co' /mnt/efs/users/*/google-watch/` → empty after.

**E2 — `.aws/isol8-creds.mjs` bakes `PUBLIC_API_BASE` and NOTHING re-materializes it**
- Writer: `credentials/materializers/aws_cli.py:132`
  `helper = _HELPER_TEMPLATE.replace("__API_BASE__", settings.PUBLIC_API_BASE)`, written at
  `:135`; the template's `const API_BASE = "__API_BASE__";` is `:51`, and the baked `aws`
  CLI calls `${API_BASE}/api/v1/connections/aws/credentials` at `:84`.
- **Self-heals? NO — see C21.** Only a fresh interactive connect runs `apply_connection`.
- **[V] Timing is looser than the sweep claimed:** `REFRESH_MARGIN_MS` is `:54` (not `:55`),
  and `:107-111` emits a **stale-but-valid** cache while `msLeft(cached) > 0` (`msLeft` at
  `:63-64` reads the STS `Expiration`). The break lands at **real credential expiry**
  (typically ≤1h), not flip+10min. Same verdict, slightly more slack.
- Failure message the owner sees: `"isol8: credential service unreachable"` (`:104`).
- **Mechanism:** an EFS sweep rewriting `API_BASE` in place, **or** re-run `apply_connection`
  per owner from the retained Secrets Manager credential — the pattern
  `rematerialize_instagram_skill.py:72-116` already implements.
- Dry-run: `grep -rl 'api.isol8.co' /mnt/efs/users/*/.aws/isol8-creds.mjs` → empty after;
  then `aws sts get-caller-identity` from inside a container.

**E3 — Atlas `workspace-{atlas}/build.lobster` bridge URL**
- Writer: `atlas_seed/__init__.py:380-383` substitutes `{{ISOL8_BRIDGE_URL}}` with
  `_bridge_url(owner_id)` (`:124`) built from `_api_base_url` (`:119-120`, hardcoded
  `https://api.isol8.co` / `f"https://api-{env}.isol8.co"`).
- **Self-heals? CONDITIONALLY, and only by accident** — see §3 B28.
- **Mechanism:** guarantee the brand-swap PR touches `atlas_seed/files/`, or run
  `scripts/restage_atlas.py --all-from-containers --apply`.
- Dry-run: `grep -rl 'isol8.co' /mnt/efs/users/*/workspace-*/build.lobster` → empty after.

**E4 — `.xurl` (`apps["isol8-x"].redirect_uri`)**
- `credentials/materializers/xurl_cli.py:29` `_XURL_APP_NAME = "isol8-x"`; `:42` the
  redirect URI from `PUBLIC_API_BASE`; written at `:136-158`.
- **Cosmetic until re-auth** — X's refresh grant does not send `redirect_uri`. Re-connect
  rewrites it.
- **Two traps:** (a) it is a **shared** file the agent also writes — go through
  `xurl_cli._locked_update`, never `atomic_write`; (b) do **not** rename the `isol8-x` key
  at flag day — `remove()` (**[V]** `:165-183`) pops by that exact key, and `:158`/`:179`
  also set `store["default_app"]`, so a rename orphans the old entry, leaves a stale token in
  a shared file, and makes `default_app` non-deterministic.

**E5 — `mcp-oauth/{name}-{key16}.json` + the mcporter vault**
- `credentials/materializers/mcp_oauth.py:113` returns `client_information` (with the stale
  `redirect_uris`), written `:130`, re-seeded into mcporter `:145` (**[V]** `mcp_oauth:144`
  for the cited line).
- **Cosmetic until re-auth**; covered by B3's fix. **No separate sweep** — but the fleet
  re-roll must come **after** the B3 code fix, not before.

**E6 — `devices/paired.json` display names**
- `desktop_pair.py:351,358` (`"| Isol8 Mac"`, `"| Isol8 Desktop (CLI)"`) → written at
  `ecs_manager.py:4224`. **Self-heals on the next `POST /desktop/pair`.** No backfill —
  note it so nobody writes one. (`node_proxy.py:136-140` is the strip-then-append trap, §2 P3.)

**E7 — `isol8-facts.json` CONTENTS self-heal; the FILENAME does not**
- `owner_facts.py:150` writes with `Workspace.write_file` (unconditional overwrite) and
  `write_user_configs` calls `write_owner_facts` every pass (`ecs_manager.py:4087`) — so
  after D1 lands, `owner_facts.py:107 "email_address"` corrects itself.
- **[V] a 4th writer the docstring omits:** `agent_mail_owner_send.py:179`.
- Only the **filename** (`owner_facts.py:30 FACTS_FILENAME`) is image-coupled — P6, and it
  has a **boot-script consumer nobody listed**: `isol8-local-sqlite-boot.sh:56-59` reads
  `$STATE_ROOT/isol8-facts.json` as the owner-id fallback and **FATAL-exits** if unresolved.

**F1 — Friday's SOUL.md / IDENTITY.md / USER.md / AGENTS.md need a NEW heal migration**
- Not a backfill script — a code change. See C20 and C22. Model it on
  `_migrate_legacy_user_md:816-869` (the only migration that rewrites lines), keep
  `_SELF_GROUNDING_HEADING`'s old value in a legacy-matcher tuple, and leave
  `:802`/`:806` byte-identical.
- Dry-run: `grep -rl 'made of Isol8' /mnt/efs/users/*/workspace-*/SOUL.md`
- Proof: 0 after the migration runs (which requires a **reprovision**, not a roll — C23).

**Confirmed negative — [V], and it deletes a whole imagined workstream:**
`grep 'PUBLIC_API_BASE|FRONTEND_URL|https://|wss://' core/containers/config.py` returns
exactly **one** line — `:740` `base_url = gate_url() or f"https://bedrock-runtime.{region}.amazonaws.com"`,
an SSM-sourced Lambda URL. **`openclaw.json` on EFS bakes NO `isol8.co` host at all.** The
only isol8-bound things in it are the P6 identifier keys (`:2011`
`allowedSessionKeyPrefixes`, `:2074` the plugin id). The plan's "EFS `openclaw.json` fields"
framing implies a host backfill that is **not needed**.

### 4.5 Secrets Manager

**S1 — DCR OAuth clients hold the OLD `redirect_uris` and a reconnect does not fix it**
- Stored at `credentials/oauth_app_store.py:38`
  `isol8/{env}/integrations/{owner}/oauth-app/{provider}`; written by
  `oauth_dance.py:87-88`. **[V] nine providers**, enumerated in §3 B3.
- **Preferred fix is code, not data** (one line, permanent). Fallback: delete every such
  secret whose stored `client_information.redirect_uris` contains the old domain — they
  self-re-register on the next connect.
- Dry-run: for each secret, `jq -r '.client_information.redirect_uris[]' | grep isol8.co`
- Proof: 0; then one live Attio (or Intercom) connect on dev.

**S2 — `isol8/{env}/paperclip_admin_credentials` holds the bootstrap admin's EMAIL**
- `paperclip_admin_session.py:58`. **Do NOT touch the email inside it** (§3 B6). The secret
  *name* is a P6 family item; the *value* is not.
- Proof: `aws secretsmanager get-secret-value --secret-id isol8/prod/paperclip_admin_credentials --query SecretString`
  → the `email` field still equals a literal in `BOOTSTRAP_EMAILS`.

**S3 — the per-owner namespaces scale with the fleet, and `owner_secret_sweep` deletes BY PREFIX**
- `key_service.py:43` `isol8/{env}/user-keys/{user}/{provider}` (**BYOK ARNs are pinned into
  per-user task definitions** at `ecs_manager.py:2488-2496` `valueFrom`),
  `integration_secret_store.py:33`, `oauth_app_store.py:38`,
  `platform_oauth_app_store.py:37`, `paperclip_api_key.py:24`,
  `paperclip_admin_session.py:58` — swept by `owner_secret_sweep.py:41-42`.
- **Trap:** the sweep's prefix and every store's path builder must move in **ONE** commit, or
  account-wipe silently leaves credentials behind. And an un-re-rolled BYOK owner gets a task
  that **cannot start** (secret ARN no longer resolves).
- P6 only; recommendation **keep** (the cost scales with customer count and nobody sees them).

### 4.6 SES resource associations

**D14 / A-live — 3 owner tenants pinned to `identity/isol8.co` + `configuration-set/isol8-{env}-agent-mail`**
- Writer: `agent_mail_tenant.py:87` `_ignore_exists(ses.create_tenant_resource_association,
  TenantName=name, ResourceArn=arn)` with `arn` from `_identity_domain()` (`:52-58`).
- **Self-heals? PARTIALLY.** `_ensured` (`:27`) is a per-process memo, so the P3 backend
  restart makes the first post-deploy send create the **new** association. The **stale** one
  is never removed, and `delete-email-identity` with associations present either fails or
  strands the tenants.
- **Mechanism (manual, in the flip window):** `create-tenant-resource-association` for the
  new identity on all 3 → widen the receipt rule's `Recipients` → cut MX →
  `delete-tenant-resource-association` for the old → `delete-email-identity`.
- Dry-run/proof: `aws sesv2 list-tenant-resources --tenant-name owner-…` shows only the new
  identity ARN before the delete. **[V] live tenant names carry no isol8 string** — they need
  re-association, not renaming.

### 4.7 Immutable / no-backfill-possible

- **S3 marketplace artifacts** — `marketplace_service.py:107-111` writes
  `workspace.tar.gz`, `manifest.json`, `openclaw-slice.json` to
  `s3://…/listings/{id}/v{n}/`, versioned and immutable by design. See §3 B30.
- **`catalog_service.py:349`** `uuid5(NAMESPACE_URL, f"isol8:cron:v1:{new_agent_id}:{index}")`
  — changing the literal changes every derived cron id, so a re-deploy **mints duplicate
  cron jobs** instead of colliding with the existing one. **Never rename.**
- **`run_analytics.py:51`** `uuid5(NAMESPACE_URL, "isol8:agent_run_completed")` — the PostHog
  dedup namespace. Renaming re-ingests history. **Never rename.** (And
  `test_run_analytics.py:345` references the **symbol**, so a seed change passes silently —
  add a test pinning the literal.)
- **`paperclip_agent_idempotency.py:17`** `_KEY_PREFIX = "isol8-agent-create:v1"` — backend
  N and N+1 with different prefixes both admit the same create ⇒ **duplicate Paperclip
  agents**. If it ever must change, bump to `v2` with dual-read, never rename.
- **Metronome commit ledger** — `metronome_funding.py:93,185`. Historical, brand-visible, no
  backfill.

### 4.8 Owner-action-only (no script can fix these)

- **O1** Gmail Pub/Sub push subscriptions — §3 B22.
- **O2** `guided_paste` trigger URLs (Stripe, Sentry, Linear, Attio, PostHog): 5
  `instructions:` blocks embed `{proxy_url}` (`providers.yaml:45,758,827,1125,1496`); the
  owner re-pastes in their own dashboard.
- **O3** BYO Google — `providers.yaml:600-605` is `byo_required: true`, so each owner edits
  **their own** Cloud Console client's Authorized redirect URI to
  `https://api.flickmeon.ai/api/v1/oauth/google/callback`. (Distinct from the **Clerk**
  social-connection Google client `1011290189072-…`, which is ours.)

### 4.9 Re-run, don't rewrite

Existing tools that cover part of the surface — prefer them to new code:
`scripts/restage_atlas.py --all-from-containers --apply` (E3),
`scripts/reapply_openclaw_config.py --owner-id … --apply` (openclaw.json + owner facts),
`scripts/rematerialize_instagram_skill.py` (the `apply_connection` re-run pattern for E2),
admin-UI **reprovision** (task-def `ISOL8_API_URL`, and the only path that re-runs
`write_user_configs`).

---

## 5. Internal-identifier rename cost table

`Occurrences` are tracked-file line counts unless marked (live). **Recommendation** is the
lazy default; each row that says *never* should be recorded as an explicit decision so it is
not re-litigated as an oversight.

| Family | Occurrences | What a rename entails | Data at risk | Incremental? | Recommendation |
|---|---|---|---|---|---|
| **CDK stack names `isol8-{env}-*`** (13 stacks, `isol8-stage.ts:27…302`) | 109 files / 963 hits for `isol8-{env}-*` overall | CFN has no stack rename. `cdk-pipelines-github` deploys **by stack name**, so a change creates a brand-new stack and leaves the old standing. Every resource inside is created fresh — EFS, Aurora, all 21+3 DDB tables, the ECS cluster (whose physical name feeds `ECS_CLUSTER_PREFIX` at `bedrock-meter-stack.ts:256`), Cloud Map, every IAM role. Also leaks into 24 generated job names asserted by `deploy-prod-promotion.test.ts:91,92` | **ALL of it** — EFS agent workspaces, every container's SQLite, Aurora tenant data, every DDB table. Prod: old resources orphan (RETAIN). **Dev: DESTROYED** (`container-stack.ts:72`, `database-stack.ts:20`) | No | **Never in place.** Only as a new-AWS-account migration where the copy is deliberate. **The pipeline name (`app.ts:53`) and concurrency group (`:119`) and the stage class name are separable and cost one `cdk synth`** — do those alone if the goal is repo hygiene (note the concurrency-group rename means an in-flight run no longer serializes; sequence into an empty queue) |
| **DynamoDB `isol8-{env}-*`** (24 tables live: 25 dev + 28 prod incl. 3 outside `database-stack`) | `database-stack.ts:81,393`; `api-stack.ts:276,685`; `bedrock-gate-stack.ts:124,162,163,166`; `bedrock-meter-stack.ts:211,237,243,244,249`; `service-stack.ts:702-734,1401,1414,1440`; `config.py:50,182,254`; backend tests 163 hits/35 files | `tableName` is **replacement-only** — a name edit does NOT create a parallel table to copy from (C35). Real migration = stand up new tables (`fromTableName` import or a second stack), dual-write or PITR-restore-to-new-name, flip `DYNAMODB_TABLE_PREFIX` **plus 7 non-prefix names in 3 stacks plus 2 backend settings**, then delete | **Yes, all of it.** `ws-connections` orphans every live socket | Per-table only, via PITR restore + a code flip per table — 24+ rounds | **Keep.** Invisible to users and every vendor. If the answer is a new account, do it there |
| **S3 buckets** (13 live) | `container-stack.ts:112,438`; `service-stack.ts:274,281,299,1030,1089,1142,1254,1483`; `ses-inbound-stack.ts:254,255`; `paperclip-stack.ts:309` | `bucketName` is replacement. Prod = RETAIN (old orphans, new empty). **Dev = `autoDeleteObjects: true` → objects DELETED** | `isol8-{env}-openclaw-replicas` holds the **litestream replicas every container restores its SQLite from at boot** — an empty bucket means every container boots with no sessions, transcripts or memory. `marketplace-artifacts` holds every existing buyer's deploy source | Yes, per bucket: `aws s3 sync` → flip name + IAM ARNs + env → verify → delete. Litestream must be stopped (or the fleet re-rolled) between sync and flip | **Keep.** If forced: `cfn-templates` and `marketplace-artifacts` first (small, cold), `openclaw-replicas` last and only fleet-stopped. **Rehearse on a scratch bucket, never on dev** |
| **`isol8-{env}-cfn-templates` (split out of the above)** | `service-stack.ts:1142,1483`; `config.py:108,113`; `connections.py:540`; `credentials/aws_cfn.py:44-45` | The bucket is **public-read** (live: `PublicReadConnectorTemplate`, `Principal:"*"`) and its object URL is the AWS quick-create link **customers click** | none | Yes | **P3 user-visible, not P6 internal.** Old object must keep serving (or 301) — customers hold console links |
| **Secrets `isol8/{env}/*` + the two DASH-form Aurora secrets** | 50 files / 225 hits; live: 254 isol8-named secrets (75 structural + 179 per-agent) | No rename — create new, copy value, repoint, delete old (7-day minimum recovery). `SecretName` is a **replacement** property, so every `GenerateSecretString` secret gets a **NEW RANDOM VALUE** unless pre-created | `gog-keyring-password` (`auth-stack.ts:219-231`: *"Never rotate — rotation orphans every existing keyring"*), `encryption_key` (orphans every ChatGPT OAuth token + Paperclip credential), `paperclip_better_auth_secret` (invalidates every session). Plus: BYOK ARNs are pinned into **per-user task defs**, so every BYOK owner needs a task-def re-register + re-roll | Yes, one sub-namespace at a time with a dual-read window | **Platform secrets (`clerk_*`, `stripe_*`): medium, batch with P3 if wanted — but NOT with the P3 secret rotations, or you rotate into a path nothing reads.** Per-owner namespaces (`user-keys`, `integrations`, `paperclip-agent-keys`): **keep**. `isol8-{dev,prod}-paperclip-db-credentials` are dash-form and missed by a `isol8/` sweep |
| **KMS alias `isol8-{env}-general`** | `auth-stack.ts:48` (1) | Aliases are mutable pointers; key material untouched. **Nothing in this repo reads the alias** — every consumer takes `kmsKey.keyArn` (`isol8-stage.ts:101,126,177,239`; `service-stack.ts:1287` passes an ARN). `config.py:394`'s comment naming `alias/isol8-{env}-container-secrets` is **stale** | none | Yes | **The cheapest item in P6. Do it anytime** |
| **SSM `/isol8/*`** (3 live) | `bedrock-gate-stack.ts:220`; `service-stack.ts:786,796`; `observability-stack.ts:175`; `bedrock_gate_url.py:37`; `scripts/bedrock_gate_break_glass.py:11` | Replacement; the backend reads the path at provision time | gate-url mismatch = **all Bedrock dead**. The literal value `direct` is the fleet break-glass switch — a rename must land in `docs/runbooks/bedrock-gate-break-glass.md` in the same PR | Yes | Medium. `/isol8/prod/pagerduty-page-url` is a **URL**, not a secret — "rotate consumers" is the wrong note |
| **Cloud Map `isol8-{env}.local`** | `container-stack.ts:136`; consumed via `namespaceName` at `service-stack.ts:1348`, `paperclip-stack.ts:383,753` | `PrivateDnsNamespace.name` is **replacement**, and `ecs_manager.py:1170` passes `serviceRegistries` **only inside `create_service`** — there is no re-register path anywhere in the codebase | Fleet-wide: every per-user ECS service is stranded in the deleted namespace, `discover_ip` returns None for **everyone**, every container reads as unreachable. Recovery = delete and recreate every per-user service. Also kills `PAPERCLIP_INTERNAL_URL`, the backend's only route to Teams | No | **Never** short of the account migration. Treat as equivalent to a full fleet reprovision — **the single most dangerous "just a name" in the list** |
| **Log groups `/ecs/isol8-*`, `/isol8/*`, `/aws/api-gateway/isol8-*`** (67 live) | `service-stack.ts:872,1223`; `container-stack.ts:525,1149`; `paperclip-stack.ts:171,671`; `api-stack.ts:192,537`; `bedrock-meter-stack.ts:82`; `isol8-stage.ts:254`; `cloudwatch_logs.py:41`; `cloudwatch_url.py:19` | Replacement; the old group keeps its data and stops receiving | **`/isol8/bedrock-invocation` is the ACCOUNT-GLOBAL Bedrock invocation-logging destination** (live-verified). Renaming it without `put-model-invocation-logging-configuration` in the same change **blinds the meter Lambda** — the auditor + backstop half of the $3/day trial cap. Everything else: only Logs-Insights continuity | Yes | **The cheapest thing in P6 apart from the KMS alias — except `/isol8/bedrock-invocation`, which is a coordinated change** |
| **IAM role/policy names `isol8-{env}-*`** (110 live: 65 CDK + **45 runtime-created**) | `service-stack.ts:349,372,389,413,420,452,467,489,979`; `container-stack.ts:293,331,489`; `bedrock_aip.py:47,492`; meter `index.py:649,651`; `bedrock-meter-stack.ts:375,450` | Replacement for the CDK half. The **45 per-owner `isol8-{env}-owner-bedrock-{owner}` roles are minted by the BACKEND at provision time**, so a rename is permanently partial — existing owners keep old names until reprovisioned. `bedrock_aip.py:491` says the prefix is load-bearing for the **permission boundary**. And `service-stack.ts:452,467` gate `iam:*` on the **tag VALUE** `ManagedBy=isol8-backend` (`aws:RequestTag`/`aws:ResourceTag`) | Owner attribution fails **open** (the meter's `f"isol8-{env}-owner-bedrock-{owner}"` match); the boundary mismatch makes every per-owner role creation AccessDenied; the tag-condition mismatch makes the backend unable to manage roles it created | No (two conventions inside one fence) | **Keep.** If ever attempted: backfill the `ManagedBy` tags **before** changing the condition, and accept a mixed fleet |
| **`isol8-connector`** (customer-account IAM role + CFN stack name) | `bedrock_aip.py:47`; `aws-connector.yaml:38,41`; `aws_cfn.py:14,46`; `materializers/aws_cli.py:35`; `service-stack.ts:372,979`; `connections.py:599-607`; plugin `aws-exec-profile/{index.ts:39,dist/resolve.js:11,22,24,34,36,openclaw.plugin.json:4,package.json:5}`; `Dockerfile:684`; `containers/config.py:1012`; `container-stack.ts:945`; tests at `test_connections.py:1711,1795,1809,2040,2385`, `test_materializer_aws_cli.py:34`, `test_aws_cfn.py:30`, `index.test.mjs:29` | A **five-way lockstep across a trust boundary we do not control.** The role physically exists in each customer's AWS account. `connections.py:601-607` hard-rejects any other role name (`detail=f"role must be named {CONNECTOR_ROLE_NAME}"`); `service-stack.ts:979` scopes `sts:AssumeRole` to `arn:aws:iam::*:role/isol8-connector`. **[V]** `aws_cfn.py:14` also makes it the customer's **stack name**, so an owner re-launching the quick-create link gets a **second stack**, not an update. The EFS `[profile isol8-connector]` and the baked plugin's line-anchored section-header match must flip in the same image roll — and the plugin "never throws, returns `{}`" (`dist/resolve.js:34-36`), so a miss makes agents silently fall back to the task role and AccessDeny | Every already-connected owner's AWS integration, until each of them redeploys a CloudFormation stack **by hand** | No | **NEVER. Record it as a decision.** Third-party-account identifier with zero brand exposure. If the display strings must change, change those only and keep the role name |
| **ECR `isol8/*`** (3 live, 1 empty) | `container-stack.ts:458,474,516`; `paperclip-stack.ts:154`; `admin_service.py:696`; `routers/admin.py:100`; `ContainerActionsPanel.tsx:399`; both `*-version.json` `extendedImage` | Repos are `fromRepositoryName`-**imported**, so CDK will never create them: `aws ecr create-repository` by hand (matching immutability + lifecycle), re-push every tag, then edit 6 sites, deploy, and only then delete the old repo — **not before every task definition has been rolled off it** (rollback afterwards is impossible) | none (images rebuildable) | Yes — dev tag first, validate, prod | **With the next image roll.** Also delete `isol8/hermes-extended` (zero images, in no doc) |
| **GHCR `ghcr.io/isol8ai/*`** | `openclaw-version.json:3`; `paperclip-version.json:2`; `Dockerfile:84`; `PINNED_TAG:1`; fork workflows | Two **opposite** mechanisms — see C33 and §3 B12. Plus re-granting the package-level "Manage Actions access" | The digest pin + a byte-equality test fixture | Sequenced, not incremental | **With the fork release + org rename, in the order in B12.** Pin by digest across the window |
| **Image tags `-isol8.N`** | `openclaw_version.py:159-201`; `openclaw-version.json:9,12,14`; `PINNED_TAG:1`; `test_openclaw_version.py` (20 lines, incl. `test_isol8_patch_level` at `:169` and parametrize tables at `:160-163,185-189`); `test_deployed_image_tag.py` (11) | The suffix is **derived** by `build-openclaw-image.yml:123` from `#upstream`, and **parsed at runtime** by `_ISOL8_PATCH_RE` which fails **open-to-zero** | **YES** — a fail-quiet config regression on EFS (the plan's "Data at risk: no" is wrong). **[V] the paperclip rail carries no `-isol8.N` at all** | Only with a dual-spelling regex landed **one release earlier** | **With the image roll that carries the brand swap**, atomically (B10) |
| **GitHub org `Isol8AI`** | 36 files / 76 hits; `container-stack.ts:492`; `app.ts:49,101` + 64 generated ARNs in `deploy.yml`; `githubReleases.ts:23,111`; `desktop-build.yml:211,219,400,408`; `mobile-release.yml:69`; `SIGNING.md:48-67`; `build-paperclip-image.yml:80`; `paperclip-skill-primitives.test.ts:142` **and `:158` URL-ENCODED** (`%2FIsol8AI%2F` — a naive find-replace misses it); the 58-ref desktop-release frontend test surface | See §3 B12. Plus: Vercel git-link re-authorization, `GITHUB_RELEASES_TOKEN` re-mint (fine-grained PATs are repo-scoped), fork remotes in `~/dev/agent-fork` and `~/dev/paperclip-fork`, and **9 org repos** (`isol8, agent, paperclip, goosetown, hermes-agent, desktop, terraform*, backend*, frontend*` — 3 archived) | none | Sequenced | **After P3**, in B12's order. `githubReleases.ts:23` is why it cannot be silent: it serves the desktop download route **and** the Tauri auto-updater — a stale path 404s both, for real installs |
| **Headers `x-isol8-*`** (4 names) | 20 files / 74 hits — see C31's table | Three PRs (accept-both → producers flip → drop old). Cannot be one deploy: `deploy.yml:1454-1473` forces producers first | none | Yes, per header | **With P3, as three PRs.** `x-isol8-gate-code` has **zero** consumers — rename unilaterally, any time |
| **EFS `.isol8-guard` / `.isol8-protocol` + `ISOL8_CONFIG_LOCK_PROTOCOL`** | `efs_file_lock.py:35,36`; `openclaw-config-lock-recovery.ts:2,3,21,22,207`; `ecs_manager.py:183,1802,1853`; guard test `openclaw-config-lock-recovery.test.ts:66-67` (reads the backend file and asserts the literals match); backend tests 22 hits/10 files; `container-stack.test.ts:334` | A boot-script migration on **every** EFS volume, run exactly once, renaming the sidecars **while holding the guard** (CLAUDE.md:632-640: *"Never unlink the sidecar outside boot recovery while holding that guard"*). Synchronized 3-file change **plus** a live-EFS migration. The env-var half fails **silently** (§2 P6) | Lock-protocol correctness — concurrent `openclaw.json` writers | Only via a dual-name read window in both the backend and the boot script | **Keep.** Invisible dotfiles on a private filesystem; the cost is a fleet-wide migration with a fatal failure mode and the benefit is zero |
| **`isol8-facts.json` — SPLIT OUT of the row above** | `owner_facts.py:3,4,14,30,98`; `friday_identity.py:326,351,812`; `skills/isol8/SKILL.md:231,440,508`; `isol8-local-sqlite-boot.sh:56,57,59`; tests 17 hits/3 files | **Not lock-protocol-risky.** The backend is the sole writer with an unconditional overwrite, so contents self-heal on any reprovision. Only the **filename** is coupled — to the bundled skill's prose, to Friday's USER.md pointer, and to the boot script's **FATAL-on-miss** owner-id fallback | none (the stale old file lingers harmlessly) | Yes | **Group it with the `ISOL8_*` image-roll row, not the lock row.** Note it is named inside artifacts P3 already re-renders — either fold it into P3's re-render or accept a second fleet SOUL.md rewrite |
| **`isol8-setup-seed.json` + `ISOL8_NATIVE_SETUP_SEED`** | `friday_identity.py:30,79,110`; fork `workspace.ts` (at tag `v2026.8.1-isol8.1`: `:99` filename, `:103` env) + 19 items in `workspace-native-seed.test.ts` | Cross-repo, fork-first (§3 B32) | The sentinel that suppresses OpenClaw's native setup wizard — lose it and **every agent re-onboards** | Dual read-path for one roll | With the fork release |
| **`ISOL8_*` env names (16 distinct)** | 52 files / 173 hits. By count: `ISOL8_AGENTS_DIR`(30), `ISOL8_API_URL`(21), `ISOL8_OWNER_ID`(20), `ISOL8_CONFIG_LOCK_PROTOCOL`(20), `ISOL8_BRIDGE_URL`(14), `ISOL8_SYSTEM_SELLER_ID`(9), `ISOL8_PROXY_PORT`(8), `ISOL8_STATE_ROOT`(7), `ISOL8_API_BASE`(6), `ISOL8_WS_URL`(4), `ISOL8_CURATOR_EMAIL`(4), `ISOL8_CALLBACK_URL`(4), `ISOL8_REPO`(3), `ISOL8_THEME`(2), `ISOL8_LITESTREAM_CONFIG`(2), `ISOL8_APP_CMD`(2) | **Not one family.** `ISOL8_API_URL`/`ISOL8_OWNER_ID` are the genuine lockstep — set on the task def (`ecs_manager.py:2775,2783`), read by the baked plugin (`src/env.ts:30-34`, which **throws** on missing), by `isol8-mail.sh:16,25,26,48`, by the boot script (`:55`), and by agent-facing prose the model **executes** (`SKILL.md:134,204`; `onboarding_brief.py:266,272`; `AGENTS.md:82`). They bind to a **reprovision**, not a roll. `ISOL8_STATE_ROOT`/`LITESTREAM_CONFIG`/`APP_CMD` are **image-internal** (boot chain only). `ISOL8_AGENTS_DIR` is **bundle-internal and self-heals** via the fingerprint (risk L — refuted claim). `ISOL8_CONFIG_LOCK_PROTOCOL` belongs to the lock row. `ISOL8_SYSTEM_SELLER_ID`'s **value** `__isol8__` is stored in DDB | Only `ISOL8_SYSTEM_SELLER_ID` (a data value on every company-registry listing row) | Per-name | **Do the container-side set in one image roll bundled with the brand swap; accept both names in the plugin for one release.** Leave `PAPERCLIP_ISOL8_*` (3 names) to the next fork release. **Leave `__isol8__` alone** |
| **Plugin / skill ids** (`isol8-marketplace`, skill dir `isol8`, `isol8-mail.sh`, `isol8.hire_suggestion`) | 20 files / 38 hits + the frontend consumer | The plugin **id** must match in three places or it does not load: `openclaw.plugin.json:2`, the on-disk dir the Dockerfile copies to (`Dockerfile:562-563` → `/app/dist/extensions/isol8-marketplace`), and the key the backend writes into `openclaw.json` (`containers/config.py:2074`) — and the committed `dist/` must be rebuilt. The skill **directory name is the skill id** and is written into already-materialized SOUL.md files. `isol8-mail.sh` is invoked by **absolute path from the backend** at two sites | `isol8.hire_suggestion` is a cross-artifact wire contract with unbounded skew (B31); published S3 slices bake the plugin id immutably (B30) | Dual-id for one release | **Image roll that carries the brand swap. Keep `isol8.hire_suggestion` forever** (or dual-accept → full fleet roll → drop). **Resolve the skill-dir-vs-frontmatter question first (C19)** |
| **`@isol8/*` packages + root `package.json:2 "name": "isol8"` + cargo crate** | 96 files / 188 refs (**[V]** 50 occurrences across 28 files in the frontend slice alone); `Cargo.toml:2` + `Cargo.lock:2095` + `main.rs:4` + 4 integration tests | Mechanical find/replace + one `pnpm install` (regenerates the root lockfile, so it cannot land in an app-scoped PR) + `next.config.ts:55` `transpilePackages` + the 12 CI `--filter` invocations | none | Yes | **Bundle into the P3 flip PR** — but see §3 B25: the CI filters are a silent-green trap |
| **Storage keys / cookies / DOM event / `isol8-file://`** | 131 files carry an isol8-prefixed literal; 37 distinct browser/mobile keys, 2 cookies, 1 DOM event, 2 URI schemes | Mechanical. `desktopPermissions.ts:17` is a **CustomEvent name** — listener and dispatcher must move together. `layoutStorage.ts:18 LEGACY_KEY_PREFIX` must **keep** its historical value | Only `analytics.ts:170 isol8_attribution_v1` (marketing attribution zeroes) and `signupIntent.ts:20` (a live funnel step across the sign-up redirect) | Yes | **Free at P3** (the origin change wipes every store anyway); **costs a UI-state reset if deferred** |
| **CloudWatch namespaces** — `Isol8`, `Isol8/Authorizer`, `Isol8/BedrockGate`, `Isol8/ECS` (live: 4, not 1) | `metrics.py:29,99`; `ops/gate_blocked.py:60,64`; `observability-stack.ts` ~23 sites incl. the `cloudwatch:namespace` IAM condition at `:1614`; `bedrock-gate/metrics.mjs:3,11`; `bedrock-usage-meter/index.py:257,268,276`; `websocket-authorizer/index.py:102`; `drift_monitor.py:58` (`Isol8/Billing`); `providers.yaml:1523` (inside an agent starter prompt) | CloudWatch has **no metric rename**. Alarms must flip in the same deploy as `:1614` or the task-stopped Lambda AccessDenies silently | All 226 alarms, both `isol8-{env}-orr` dashboards, and **all history** — any alarm whose evaluation window spans the cutover fires INSUFFICIENT_DATA | No | **Keep.** High friction, low value. `providers.yaml:1523`'s braces are **doubled** (`{{code, …}}`) to survive `.format` — a naive rewrite to single braces raises `KeyError` on the next trigger seed |
| **SNS topics / SQS DLQ / SES config sets** | `observability-stack.ts:117,123` + 5 ARN consumers; `agent-mail-outbound-stack.ts:36,78,91` | All replacement properties. **SNS: every subscription dropped, email needs re-confirmation.** SQS: any un-replayed SES complaint destroyed. **SES config-set name is also filtered on in the EventBridge rule pattern (`:91`)** — the two must move together or complaint handling silently stops, and SES **rejects** a send naming a nonexistent config set | Alerting goes dark until someone clicks; queued complaints lost | No | **Keep** unless the account migration. Absent from the plan's row entirely |
| **AWS SSO / CLI profile / account "alias"** | `CLAUDE.md:107,282-283` + 15 files / 34 hits of `--profile isol8-admin`; live SSO instance `ssoins-66841e0b4e2a6bee` in **us-east-2** | Support-gated for the start URL. **There is no account alias** (live: `[]`) — the plan's row is moot; what exists is the Organizations account **Name** `Isol8`. The real breakage surface is the **argparse defaults** in ~10 operator scripts (`audit_paperclip_adapter_types.py:278` has no empty-string escape) | none | Yes | **Low, but not "anytime"** — the start-URL change invalidates every cached SSO token simultaneously. Fix `audit_paperclip_adapter_types.py:286` (`or None`) in P1 regardless |
| **Cloudflare Zero Trust team** | `auth-stack.ts:233-253` (secret `cloudflare-warp-enrollment`, JSON `{organization, auth_client_id, auth_client_secret}`); the team name itself is **not in the repo** | `:243-247`: *"ONE FLEET-WIDE TOKEN … a Cloudflare account caps service tokens at 50 … Rotation = delete + recreate the token in the ZT dashboard, then overwrite this secret; containers pick it up on their next task start."* No hot rotation | none | Yes | **Low; anytime.** Quote the mechanism into the runbook so nobody expects a hot swap |
| **Docs / memory / graphify / untracked scratch** | `CLAUDE.md` 79 · `docs/**` 36 files/310 · `AGENTS.md` 41 (untracked) · `.claude/**` ~78 (gitignored) · `.agents/**` 63 · `routine-detail.md` 21 · `.playwright-cli/` 191 files · memory 428 files/1136 hits (72 with a hostname; 15 dev-only) | Text — **but split by trackability, not by phase** (C38), and dev-host docs go stale at **P2**, not P3. The two `.claude/skills` runbooks are operational scripts that delete data, not docs | The memory dir is **path-keyed** to `isol8.nosync` (C39) | Yes | **Delete the ~24 orphans and the 4 untracked scratch trees first (P1), then rewrite the 18 living docs. Run `/graphify . --update` ONCE, after P3** — doc content is the paid extraction path; per-phase runs pay three times for the same corpus |

**Never-rename set (nine entries; give each an explicit row so a future sweep does not
"fix" it on autopilot):** `bedrock_aip.CONNECTOR_ROLE_NAME:47` + `aws_cli.AWS_CONNECTOR_PROFILE:35`
· `catalog_service.py:349` cron uuid5 seed · `run_analytics.py:51` PostHog dedup uuid5 seed ·
`paperclip_agent_idempotency.py:17` `_KEY_PREFIX` · `friday_identity.py:802,806` legacy
matchers · `paperclip_user_delete.py:49` `BOOTSTRAP_EMAILS` (add new, never remove old) ·
`ingest_companies_registry.py:38` `__isol8__` · `layoutStorage.ts:18` `LEGACY_KEY_PREFIX` ·
`isol8.hire_suggestion`.

A **frozen-identifier test** (one backend file) asserting each of these equals its literal,
with a one-line comment naming what breaks, is the cheapest guard in the program.

---

## 6. Full item inventory

Format: `file:line | value | old→new | phase`. Repeated identical values in one file are
collapsed onto one row with the full line list — no information lost, the transformation is
identical. `{X}` = template. Tests are summarized per file with counts at the end.

### 6.1 `.github/workflows/**` + CI identity

```
backend.yml:72                   | DYNAMODB_TABLE_PREFIX: isol8-test-              | →lightbulb-test-      | P6 (free, decoupled from the DDB decision)
build-openclaw-image.yml:28      | comment naming the 3 boot-chain files            | follows the renames   | P6
build-openclaw-image.yml:94      | path arg to smoke-test.sh (isol8-local-sqlite-boot.sh) | → | P6 (gates build-and-push)
build-openclaw-image.yml:109     | runs-on: [self-hosted, isol8-wsl]                | →lightbulb-wsl        | P6 (queues forever)
build-openclaw-image.yml:123,126 | TAG derived from upstream.split(':')[1]          | see B10               | P6
build-openclaw-image.yml:137     | role/isol8-openclaw-image-builder                | →                     | P6
build-openclaw-image.yml:142     | comment "Isol8 OpenClaw fork"                    | →Lightbulb         | P6
build-openclaw-image.yml:22,33   | push path filters skills/** plugins/**           | (mechanism, B11)      | —
build-paperclip-image.yml:73     | role/isol8-openclaw-image-builder                | →                     | P6
build-paperclip-image.yml:78     | comment "Isol8 Paperclip fork"                   | →                     | P6
build-paperclip-image.yml:80     | GHCR Manage-Actions-access grant "add Isol8AI/isol8" | re-grant           | P6 (private FROM 401s)
build-paperclip-image.yml:58-61  | TAG from upstreamCommit — NO -isol8.N here       | (no work)             | —
desktop-build.yml:4              | comment "registered to the Isol8AI/isol8 repo ONLY" | →                  | P6
desktop-build.yml:71,72          | dev chat URL + updater URL (+ VERCEL_AUTOMATION_BYPASS_SECRET) | →dev.flickmeon.ai | P2
desktop-build.yml:89,225,277,414 | $RUNNER_TEMP/isol8-build.keychain-db (rename PAIRWISE :89↔:225, :277↔:414) | → | P6 (if:always cleanup stops deleting the Apple signing key from a persistent runner)
desktop-build.yml:117,184,305,373| SIGN_ID "Developer ID Application: Prasiddha Parthsarthy (WZX4U3C22Y)" ×4 | →company cert | P5
desktop-build.yml:163,351        | releaseName "Isol8 Desktop [DEV]/…"              | →                     | P6 cosmetic (REFUTED as user-visible)
desktop-build.yml:174,178,179    | ISOL8_CALLBACK_URL/API_BASE/WS_URL (dev)         | hosts P2, env NAMES P6| P2/P6
desktop-build.yml:183,186,195,196,199,201 | APP=…/macos/Isol8.app; DMG=Isol8_${VERSION}_aarch64.dmg; isol8_rw.dmg; isol8_dmgmnt; -volname "Isol8"; ditto → $MNT/Isol8.app | follow productName; QUOTE every path | P3
desktop-build.yml:211,219,400,408| gh release upload/edit --repo Isol8AI/isol8      | →                     | P6
desktop-build.yml:259,260        | prod chat URL + updater URL                      | →flickmeon.ai         | P3
desktop-build.yml:362,367,368    | ISOL8_CALLBACK_URL/API_BASE/WS_URL (prod)        | →                     | P3/P6
desktop-build.yml:372,375,384,385,388,390 | prod bundle path/DMG/rw/mnt/volname/ditto| as :183-201           | P3
e2e-dev.yml:9                    | default: 'https://dev.isol8.co'                  | →dev.flickmeon.ai     | P2
frontend-ci.yml:62,65,72,78      | pnpm --filter @isol8/frontend ×4                 | →@lightbulb/          | P6 (silent-green, B25)
frontend-ci.yml:76               | secrets.CLERK_PUBLISHABLE_KEY — a THIRD Clerk secret name | confirm which instance | P3
frontend-ci.yml:77               | NEXT_PUBLIC_API_URL: api-dev.isol8.co            | →                     | P2
infra-ci.yml:79                  | pnpm --filter @isol8/infra run test              | →                     | P6 (silent-green)
infra-ci.yml:100                 | comment naming isol8-wsl                         | →                     | P6
infra-ci.yml:102-131             | the deploy.yml drift check (mechanism)           | —                     | —
mobile-ci.yml:3,65,68,71,74,77,80,85 | 7 pnpm --filter @isol8/{shared,mobile} + 1 comment | →                | P6 (silent-green)
mobile-ota.yml:63                | EXPO_PUBLIC_API_URL https://api.isol8.co/api/v1  | →                     | P3
mobile-ota.yml:64                | pk_live_Y2xlcmsuaXNvbDguY28k (base64 "clerk.isol8.co$") — INVISIBLE to any isol8 grep | →new pk_live_ | P3
mobile-ota.yml:68                | EXPO_PUBLIC_APPLE_IAP_PRODUCT_ID                 | →ai.flickmeon.…       | P3
mobile-ota.yml:106,110           | the two OTA poison-pill guards (:110 NEGATIVE)   | re-anchor, same commit as env.ts:11 | P2/P3 (B19)
mobile-release.yml:69            | "--repo Isol8AI/isol8" inside an ::error:: string | →                     | P6
mobile-release.yml:99,109,115    | $RUNNER_TEMP/isol8.ipa ×3 (pair :99↔:115)        | →                     | P6
runner-maintenance.yml:34        | runs-on: [self-hosted, isol8-wsl]                | →                     | P6
deploy.yml:11                    | concurrency group: isol8-deploy (gen app.ts:119) | →                     | P6
deploy.yml ×64                   | arn:…:role/isol8-dev-github-actions (gen app.ts:49,101) | →              | P6
deploy.yml:1597,1598,1604,1606   | Alias to dev./admin.dev.isol8.co (gen app.ts:162-168) | →                | P2
deploy.yml:2364,2365,2371,2372,2377,2379,2384,2386 | apex/www domains-add/www/admin aliases (gen app.ts:244-260) | → | P3
deploy.yml ×286                  | job names + CFN logical ids derived from stack names | KEEP              | P6
scripts/switch-ci-runner.sh:10,16,22,32 | isol8-wsl ×4                              | →                     | P6
scripts/switch-ci-runner.sh:27   | REPO="${ISOL8_REPO:-Isol8AI/isol8}"              | env name + org        | P6
apps/frontend/src/lib/githubReleases.ts:7,16,23,111 | ISOL8_REPO = "Isol8AI/isol8" + 2 doc refs + the consumer | → | P6 (desktop download + updater 404)
apps/desktop/SIGNING.md:8        | "Isol8 is damaged" (derives from productName)    | →                     | P3
apps/desktop/SIGNING.md:39       | app-specific-password label isol8-notarize       | →                     | P6
apps/desktop/SIGNING.md:48,51,54,57,61,67 | gh secret set/list --repo Isol8AI/isol8 ×6 | →                   | P6
apps/mobile/README.md:1,3,13,14,31,98,100,135 | @isol8/mobile, hosts, Expo org `isol8`, project @isol8/isol8-mobile, pnpm filter | → | P3 (Expo)/P6 (docs)
apps/backend/deploy_dev_fast.sh:15 | REPO="Isol8AI/backend" — repo ARCHIVED         | DELETE the script     | P1
GH secret NEXT_PUBLIC_API_URL_PROD | never mentioned in P3                          | →api.flickmeon.ai     | P3 step 1 (prod outage)
gh vars CI_RUNNER / DEPLOY_RUNNER  | value = "isol8-wsl" (verified live)            | set BEFORE re-labelling| P6
```
Clean: `claude.yml`, `claude-code-review.yml` (zero brand-bound strings).
No GitHub **secret name** contains `isol8` (30 distinct verified) — only values change.
`desktop-v*`/`desktop-dev-v*` release tags and `tagPrefixFromHost` need **no** change.

### 6.2 `apps/infra/lib/**` + `cdk.context.json`

```
app.ts:14,53,127,205         | import/new Isol8Stage; GitHubWorkflow "isol8-pipeline" | →LightbulbStage / lightbulb-pipeline | P6
app.ts:49,101                | role/isol8-dev-github-actions (NOT CDK-managed)       | →                    | P6
app.ts:107                   | comment naming isol8-wsl                              | →                    | P6
app.ts:119                   | concurrency group "isol8-deploy"                      | →                    | P6
app.ts:138,162,163,167,168   | dev Vercel alias hosts + comment                      | →dev./admin.dev.flickmeon.ai | P2
app.ts:188,191               | comments quoting PUT /repos/Isol8AI/isol8/environments/prod | →              | P6
app.ts:203,218,313           | PROD_PROMOTE_IF gate (mechanism)                      | —                    | —
app.ts:220,244,245,249,250,254,255,259,260,307 | prod alias/domains-add hosts + 2 comments | →         | P3
isol8-stage.ts (FILENAME)    | lib/isol8-stage.ts                                    | →lib/stage.ts        | P6
isol8-stage.ts:17,21,22      | Isol8StageProps / Isol8Stage                          | →                    | P6
isol8-stage.ts:27-33,53-54,75-76,84-85,90-91,97-98,111-112,133-134,226-227,249,289,301 | 13 stack ids + stackNames isol8-{env}-* | KEEP | P6
isol8-stage.ts:52            | if (env === "prod") SES gate                          | (C2)                 | P2
isol8-stage.ts:60,61         | mailDomain "isol8.co" / devMailDomain "dev.isol8.co"  | →thelightbulb.company | P2/P3
isol8-stage.ts:65,79         | isol8/{dev,${env}}/ses_ingest_secret                  | →                    | P6
isol8-stage.ts:72            | agentEmailDomain (the REAL site; plan cites service-stack:1519) | →         | P2/P3
isol8-stage.ts:78            | apiDomain api[-env].isol8.co (SES event destination)  | →                    | P2/P3
isol8-stage.ts:87,115        | certificate: dns.certificate → repoint to RootCert    | (B1)                 | P2/P3
isol8-stage.ts:162-168       | 7 secretNames isol8/{env}/*                           | →                    | P6
isol8-stage.ts:224           | comment paperclip.isol8-${env}.local:3100             | →                    | P6
isol8-stage.ts:254           | backendLogGroupName /ecs/isol8-${env}                 | →                    | P6
isol8-stage.ts:271           | authorizerFunctionName isol8-${env}-ws-authorizer     | →                    | P6
isol8-stage.ts:308           | Tags Project "isol8" (cost-allocation grouping)       | →                    | P6
dns-stack.ts:17-19           | HostedZone.fromLookup domainName "isol8.co"           | →flickmeon.ai        | P2/P3
dns-stack.ts:21-25           | WildcardCert *.isol8.co + SAN isol8.co                | NEW construct (B1)   | P2/P3
dns-stack.ts (new)           | file has ZERO records today — apex A, www/dev/admin/admin.dev CNAME, 5 Clerk CNAMEs | ADD | P2/P3
cdk.context.json:18-21       | hosted-zone cache for isol8.co                        | ADD flickmeon.ai (C1)| P2/P3
api-stack.ts:100,228,249     | api[-env].isol8.co domain + mapping + record          | →                    | P2/P3
api-stack.ts:108,109,125     | frontendUrl + allowOrigins incl. www.isol8.co         | →                    | P2/P3
api-stack.ts:113,145,192,276,314,452,463,474,521,537,667-691 | api/vpc-link/logGroup/ws-connections table/6 fn names/exportNames isol8-{env}-* | KEEP | P6
api-stack.ts:153-164,261,263,486,716,718 | comments (X-Isol8-Public-Host, hosts, x-isol8-ws-internal, CF distribution comment) | → | P3/P6
api-stack.ts:178             | overwrite:header.X-Isol8-Public-Host                  | →                    | P6
api-stack.ts:353-358         | CLERK_ISSUER / CLERK_JWKS_URL literals (C3)           | →clerk.flickmeon.ai  | P3 step 1
api-stack.ts:364,365         | CLERK_AUTHORIZED_PARTIES prod/dev (mode=audit at :366)| →                    | P2/P3
api-stack.ts:514,621,641     | ws[-env].isol8.co domain + mapping + record           | →                    | P2/P3
api-stack.ts:714,744,749,751 | bv[-env].isol8.co CloudFront alias + record + exports | →                    | P2/P3
service-stack.ts:207,246,1190| SG/role descriptions "Isol8 {env} …" (ASCII-guarded)  | →                    | P6
service-stack.ts:266,274,281,299,1254 | isol8-{env}-openclaw-replicas (litestream)   | KEEP                 | P6
service-stack.ts:349,413,420 | role/isol8-{env}-owner-bedrock-*                      | KEEP                 | P6
service-stack.ts:372,979     | role/isol8-connector                                  | NEVER                | —
service-stack.ts:389,489     | managedPolicyName isol8-{env}-backend-*               | →                    | P6
service-stack.ts:452,467     | IAM tag CONDITION value ManagedBy=isol8-backend       | KEEP                 | P6
service-stack.ts:481,483     | comments (identity, {agent}@{slug}.isol8.co)          | →                    | P3
service-stack.ts:495         | ses identity/isol8.co (AgentMailSesSend) — list BOTH  | →                    | P1
service-stack.ts:522,523     | configuration-set ARN + tenant-policy identity ARN (agent-mail ONLY) | →      | P1/P6
service-stack.ts:538-734     | ~20 secret ARNs isol8/{env}/* + 8 DDB table ARNs      | KEEP                 | P6
service-stack.ts:786,796     | SSM /isol8/{env}/bedrock-gate-url                     | KEEP                 | P6
service-stack.ts:856,857,872,1223 | /ecs/isol8-{env} log group + IAM                | KEEP                 | P6
service-stack.ts:1017,1030,1031 | isol8-{env}-openclaw-configs — IAM grant with NO creating construct and NO backend consumer | DELETE instead | P6
service-stack.ts:1089,1104,1105 | marketplace-artifacts bucket + S3 CORS prod/dev    | bucket KEEP; CORS →  | P2/P3/P6
service-stack.ts:1140,1142,1483 | CFN_TEMPLATE_KEY / cfn-templates bucket / quick-create URL | → (customer-visible) | P3
service-stack.ts:1259,1272,1329,1345,1352,1515,1521,1642,1653 | comments             | →                    | P3/P6
service-stack.ts:1264         | WS_ENFORCE_INTERNAL_SECRET "true" (mechanism, C31)   | —                    | —
service-stack.ts:1266,1278-1281,1282 | CORS_ORIGINS / CLERK_AUTHORIZED_PARTIES / AZP_MODE audit | →           | P2/P3
service-stack.ts:1320,1326    | FRONTEND_URL / PUBLIC_API_BASE                       | →                    | P2/P3
service-stack.ts:1339-1342    | PAPERCLIP_PUBLIC_URL (lockstep paperclip-stack:374-375) | →                  | P2/P3
service-stack.ts:1360-1362    | Clerk pk from GH secret / dev fallback literal        | →                    | P3
service-stack.ts:1370,1374,1378,1383,1387 | APPLE_IAP_{BUNDLE_ID,PRODUCT_ID,APP_APPLE_ID,ENVIRONMENT,ROOT_CERTS_DIR} — ALL LIVE | → the first three | P3
service-stack.ts:1401,1414,1440 | DYNAMODB_TABLE_PREFIX + 2 non-prefix table literals| KEEP                 | P6
service-stack.ts:1417         | comment "isol8-bedrock-prod" (Metronome rate-card name) | vendor rename     | P5
service-stack.ts:1487-1494    | STRIPE_CONNECT_{REFRESH,RETURN}_URL                  | →                    | P2/P3
service-stack.ts:1500-1509    | POSTHOG_HOST/PROJECT_ID 380894/phc_ write key        | UNCHANGED            | —
service-stack.ts:1540         | ecs.Secret.fromSecretsManager (resolved at TASK START)| (B8 mechanism)      | —
service-stack.ts:1554-1580    | stripe_{marketplace,connect}_webhook_secret names     | KEEP (do NOT batch with P3 rotations) | P6
service-stack.ts:1675,2174    | taskdef families isol8-{env}-{provisioner,backend}    | KEEP                 | P6
service-stack.ts:2066,2100    | stateMachineName isol8-{env}-provision + exec ARN     | KEEP                 | P6
service-stack.ts:2115,2120,2125,2137,2158 | SNS ARN + 3 alarmNames                   | KEEP                 | P6
service-stack.ts:2257,2258    | Tags Name/Project isol8                              | →                    | P6
ses-inbound-stack.ts:17-70    | prop docs + PROD-ONLY rationale                      | + mailHostedZone, platformMailDomain, recipients[] | P1/P2
ses-inbound-stack.ts:105-107  | "No mailFromDomain override" (explains the orphaned send.isol8.co records) | —   | —
ses-inbound-stack.ts:109-111  | Identity.publicHostedZone(hostedZone) — ONE identity  | →TWO                 | P1/P3
ses-inbound-stack.ts:114-117,134-174,187-198,215-243,386-395 | ~90 comment lines incl. the deleteExisting + DMARC + SPF-budget warnings | rewrite the MEANING | P2/P3
ses-inbound-stack.ts:121,129  | *.{mailDomain} MX + SPF                              | →                    | P2/P3
ses-inbound-stack.ts:176-184  | apex TXT: SPF w/ google include + GSV token, deleteExisting:true | (C30)     | P2/P3
ses-inbound-stack.ts:203,209  | *.{devMailDomain} MX + SPF                           | →                    | P2 (blocked, C2)
ses-inbound-stack.ts:244,248,249 | reportMailbox + _dmarc TXT                        | +prop, +2 records, +auth record | P3
ses-inbound-stack.ts:254,255  | isol8-{envName}-agent-mail-{raw,attachments} buckets  | KEEP                 | P6
ses-inbound-stack.ts:342,348,349 | BACKEND_INGEST_URL / DEV_MAIL_SUFFIX / DEV_BACKEND_INGEST_URL | ←rootDomain  | P1/P2
ses-inbound-stack.ts:413      | recipients: [mailDomain, `.${mailDomain}`]           | array prop (B21)     | P2/P3
ses-inbound-stack.ts:428-453  | AwsCustomResource ActivateAgentMailRuleSet (singleton)| —                   | —
ses-inbound-stack.ts:459      | Tags Project "isol8"                                 | →                    | P6
paperclip-stack.ts:43-490     | ~22 comment lines (hosts, isol8-{env}.local, isol8.notifications) | →        | P3/P6
paperclip-stack.ts:90,92,454,515 | PAPERCLIP_ISOL8_NOTIFY_{URL,TOKEN} (+ docs)       | fork-gated           | P6
paperclip-stack.ts:154        | ecr "isol8/paperclip-extended"                       | →                    | P6
paperclip-stack.ts:171,671    | /isol8/{env}/paperclip{,-migrate} log groups         | KEEP                 | P6
paperclip-stack.ts:248,251,647| secret ARNs incl. isol8-{env}-paperclip-db-credentials | KEEP               | P6
paperclip-stack.ts:309        | isol8-{env}-paperclip-attachments bucket             | KEEP                 | P6
paperclip-stack.ts:322,580,681| taskdef families + serviceName                       | KEEP                 | P6
paperclip-stack.ts:374,375    | PAPERCLIP_PUBLIC_URL / trustedOrigins (B17)          | →                    | P2/P3
paperclip-stack.ts:383,753    | PAPERCLIP_INTERNAL_URL from the Cloud Map namespace   | KEEP                 | P6
paperclip-stack.ts:390,391    | PAPERCLIP_ISOL8_NOTIFY_URL api[-env].isol8.co (SSRF filter needs a PUBLIC host) | → | P2/P3
paperclip-stack.ts:742,759,766,770 | exportNames + Tags                             | KEEP                 | P6
observability-stack.ts:84-2708| namespace "Isol8"/"Isol8/ECS" ×~23 incl. the cloudwatch:namespace IAM condition at :1614 | KEEP | P6
observability-stack.ts:117,118,123,124 | SNS topicNames + displayNames "Isol8 Page/Warn"| names KEEP; displayNames → | P3/P6
observability-stack.ts:151,153,154 | alertEmail admin@isol8.co + 2 subscriptions     | →+CONFIRM            | P1
observability-stack.ts:175    | SSM /isol8/prod/pagerduty-page-url (hand-created)     | KEEP                 | P6
observability-stack.ts:223,228| exportNames isol8-{env}-{page,warn}-topic-arn        | KEEP                 | P6
observability-stack.ts:254,325,573,637,2248 | metric dimension service:"isol8-backend"| KEEP                 | P6
observability-stack.ts:270…2217 | ~25 alarmNames isol8-{envName}-*                   | KEEP                 | P6
observability-stack.ts:860    | BucketName isol8-{envName}-openclaw-replicas          | KEEP                 | P6
observability-stack.ts:1420…2808 | ~7 ruleName/functionName                          | KEEP                 | P6
observability-stack.ts:2024-2026,2030,2045 | Budgets subscribers admin@isol8.co (NO confirmation needed) | → | P1/P3
observability-stack.ts:2158-2161,2187,2202 | comment + appHost/apiHost + 4 CfnHealthChecks + tags | ←rootDomain | P1/P3
observability-stack.ts:2252   | dashboardName isol8-{envName}-orr                    | KEEP                 | P6
container-stack.ts:62,797,862,945,972,1008,1165 | comments (warp boot, boot-chain, [profile isol8-connector], ISOL8_OWNER_ID, image tag, goosetown noise) | → | P3/P6
container-stack.ts:112        | isol8-{env}-openclaw-replicas                        | KEEP                 | P6
container-stack.ts:136        | Cloud Map namespace isol8-{env}.local                 | NEVER                | P6
container-stack.ts:138,148,155,178,193,235 | descriptions "Isol8 {env} …" (ASCII-guarded) | →                | P6
container-stack.ts:212        | secret ARN isol8/{env}/user-keys/*                    | KEEP                 | P6
container-stack.ts:293,331    | managedPolicyName isol8-{env}-owner-bedrock-{base,boundary} | KEEP           | P6
container-stack.ts:442        | autoDeleteObjects: env !== "prod" (the dev-delete trap)| —                   | —
container-stack.ts:458-471,474,516 | ECR imported-existing rationale + 2 repo names   | →                    | P6
container-stack.ts:479,489,492| deploy-role comment; roleName isol8-openclaw-image-builder; OIDC sub repo:Isol8AI/isol8:* | → BEFORE the rename | P6
container-stack.ts:525,1149   | /isol8/{env}/{openclaw,ecs-task-events} log groups    | KEEP                 | P6
container-stack.ts:541        | taskdef family isol8-{env}-openclaw (base the backend clones from) | KEEP     | P6
container-stack.ts:785,818,883| absolute boot-script paths (:818 FATAL exit 1)        | see B14              | P6
auth-stack.ts:47,48           | KMS description + alias isol8-{env}-general           | →                    | P6 (cheapest)
auth-stack.ts:55,65,66        | secretName isol8/{env}/${name} + descriptions         | KEEP                 | P6
auth-stack.ts:108,118,121,127,172,240 | comments (isol8.notifications, PAPERCLIP_ISOL8_NOTIFY_TOKEN, X-Isol8-SES-Secret, warp boot) | → | P6
auth-stack.ts:196-203         | bootstrap-admin email prose                          | DO NOT TOUCH         | —
auth-stack.ts:219-231         | gogKeyringPassword "Never rotate"                    | —                    | —
auth-stack.ts:233-253         | cloudflare-warp-enrollment secret + rotation note     | KEEP                 | P6
database-stack.ts:20,21       | dev DESTROY / prod RETAIN removal policies            | —                    | —
database-stack.ts:62,81,393   | tableName isol8-{env}-${suffix} (21 suffixes) + the exported prefix | KEEP    | P6
database-stack.ts:280,406     | comments                                             | →                    | P6
database-stack.ts:434,440,455 | paperclip-db credentials secretName / clusterIdentifier / exportName | KEEP (data loss) | P6
network-stack.ts:61,139       | SG description + a host comment                       | →                    | P3/P6
bedrock-gate-stack.ts:124,162,163,166,309-311 | 4 hardcoded table names            | KEEP                 | P6
bedrock-gate-stack.ts:141,144 | functionName isol8-{env}-bedrock-gate (matched by the meter's is-gateway test) | KEEP | P6
bedrock-gate-stack.ts:156,395,406 | namespace "Isol8/BedrockGate"                    | KEEP                 | P6
bedrock-gate-stack.ts:220,344,351,359,376,403,427 | SSM param + 2 SNS ARNs + 3 alarms + Tags | KEEP          | P6
bedrock-meter-stack.ts:36,65,427-431 | comments (incl. the only other GooseTown ref in lib/) | →             | P6
bedrock-meter-stack.ts:82     | logGroupName /isol8/bedrock-invocation (ACCOUNT-level)| KEEP                 | P6
bedrock-meter-stack.ts:186,211,237-256,331-360,394,420,421 | queue/table/ECS_CLUSTER_PREFIX/role ARNs | KEEP  | P6
bedrock-meter-stack.ts:258,280,375,450,470-565,599 | namespace/secret/role wildcard/CW filter/SNS/alarms/Tags | KEEP | P6
openclaw-config-lock-recovery.ts:2,3,21,22 | .isol8-guard / .isol8-protocol ×2 (TS + embedded python) | KEEP    | P6
openclaw-config-lock-recovery.ts:207 | ISOL8_CONFIG_LOCK_PROTOCOL test              | KEEP                 | P6
apps/infra/aws/aws-connector.yaml:3,11,17,18,35,41,43,44,61,66-68 | the CUSTOMER-launched template; :41 RoleName | NEVER (display strings only) | P6
apps/infra/aws/README.md:24   | "Because the role uses a fixed name (isol8-connector)…" | →                 | P3
apps/infra/package.json:2, vitest.config.ts:7, paperclip/RUNBOOK.md, package-lock.json | @isol8/infra + prose | → | P6
```

### 6.3 `apps/infra/lambda/**`

```
bedrock-gate/errors.mjs:10,24              | x-isol8-gate-code (ZERO repo consumers)   | →                | P6 (free)
bedrock-gate/errors.mjs:31,33,35,37,41,43,45,52-58,62,64 | TEN "isol8: " messages surfaced to the agent | → | P3
bedrock-gate/errors.mjs:56                 | "…contact support@isol8.co"                | →company domain  | P3 (MAIL move)
bedrock-gate/metrics.mjs:3,11              | Namespace "Isol8/BedrockGate"              | KEEP             | P6
bedrock-gate/auth.mjs:22                   | comment naming the tokens table            | →                | P6
bedrock-gate/spike/SPIKE-RESULTS.md:34     | AWS_PROFILE=isol8-admin                    | DELETE the doc   | P6
bedrock-usage-meter/index.py:55,59-69,668  | env docstrings + a cluster comment         | →                | P6
bedrock-usage-meter/index.py:257,268,276   | Namespace "Isol8", service isol8-bedrock-meter | KEEP         | P6
bedrock-usage-meter/index.py:649,651       | f"isol8-{env}-owner-bedrock-{owner}" — the OWNER-ATTRIBUTION key | KEEP | P6
bedrock-usage-meter/index.py:677           | ECS_CLUSTER_PREFIX consumer                | KEEP             | P6
bedrock-usage-meter/index.py:865-872,888,907 | f"isol8-{env}-bedrock-gate" is-gateway test + dedup marker | KEEP (mismatch double-bills or blinds reconciliation) | P6
bedrock-usage-meter/FIELDS.md:21           | /isol8/bedrock-invocation                  | →                | P6
bedrock-usage-meter/fixtures/inline_record.json:7 | assumed-role/isol8-dev-owner-bedrock-org_abc | fixture   | P6
ses-inbound/index.py:2,28,120              | "Isol8 backend" prose                      | →                | P3
ses-inbound/index.py:332-339               | docstring on isol8.co / dev.isol8.co suffix matching | →       | P3
ses-inbound/index.py:384                   | sends X-Isol8-SES-Secret                   | →                | P6
ses-inbound/README.md:7,25,26              | backend name + ingest URL + header         | →                | P3/P6
websocket-authorizer/index.py:9            | comment naming a secret path               | →                | P6
websocket-authorizer/index.py:37,38,376    | reads CLERK_JWKS_URL / CLERK_ISSUER (C3 consumer) | —          | P3
websocket-authorizer/index.py:102          | Namespace "Isol8/Authorizer"               | KEEP             | P6
ws-connect/index.py:19,89 · ws-disconnect:19,57 · ws-message:17,49 | x-isol8-ws-internal ×3 | →            | P6
```

### 6.4 `apps/backend/{core,routers,schemas,scripts}` + `main.py`

**Settings / gates**
```
core/config.py:50,141          | DYNAMODB_TABLE_PREFIX "isol8-dev-"; WS_CONNECTIONS_TABLE   | KEEP           | P6
core/config.py:146,442         | WS_GATEWAY_URL / FRONTEND_URL dev-shaped defaults          | →              | P2
core/config.py:460,464,468     | EMAIL_FROM_WELCOME / EMAIL_SUPPORT_ADDRESS / EMAIL_FROM_TICKETS — CDK sets NONE of them today (verified: only a comment at ses-inbound-stack.ts:151-152), so PROD sends from these defaults | → + CDK must SET them | P1/P3
core/config.py:108,113         | isol8-{env}-cfn-templates comment + CFN_TEMPLATE_URL       | →              | P3/P6
core/config.py:428             | META_WEBHOOK_VERIFY_TOKEN "" — set by NO stack             | ADD the setter | P1
core/config.py:182,254,394,416 | METRONOME_FAST_GATE_TABLE / BEDROCK_AIP_OWNERS_TABLE / a STALE KMS-alias comment / PAPERCLIP_ISOL8_NOTIFY_TOKEN | KEEP / fix the comment | P6
core/config.py:349-351,402,406,436-438,457,266,389,413-415 | doc-comment hosts + resource names | text  | P3/P6
core/config.py (NEW)           | PLATFORM_ADMIN_EMAIL_DOMAIN, ADMIN_HOST, CURATOR_EMAIL    | 3 new settings | P1
core/auth.py:268,271,273,275,288,289,298,303 | _PLATFORM_ADMIN_EMAIL_DOMAIN + 7 doc lines | →setting     | P1/P3
core/auth.py:73-75             | the JWT template `{{user.primary_email_address}}` — a CLERK DASHBOARD artifact, no repo lever | — | P3
core/auth.py:280-284           | _PLATFORM_ADMIN_EXTRA_EMAILS — the break-glass             | ADD then REMOVE| P1/P3
core/constants.py:7,11         | ISOL8_CURATOR_EMAIL = "admin@isol8.co" + a doc line        | →setting       | P1/P3
main.py:171                    | b"x-isol8-public-host" ASGI scope key                      | →              | P6
main.py:543,974                | title="Isol8 API"; {"message":"Welcome to Isol8 API"}      | →              | P3
main.py:554,556,560            | OpenAPI servers api[-dev].isol8.co                         | ←PUBLIC_API_BASE| P1
main.py:86-94,108,121,145-149,178,213,663-667,788,893,920 | comments                        | text           | P3/P6
```

**Paperclip / Teams**
```
routers/paperclip_proxy.py:130-142 | _BRAND_REWRITES: <title>Isol8 Teams</title>; og:site_name | →         | P3
routers/paperclip_proxy.py:652,655-658 | _rewrite_set_cookie_domain default ".isol8.co" + docs | ←FRONTEND_URL | P1
routers/paperclip_proxy.py:723,693,1250,1453,1599,1697,1772 | isol8_paperclip / isol8_pc_bind cookie NAMES | KEEP at P3 | P6
routers/paperclip_proxy.py:1230     | fallback "https://dev.isol8.co"                     | ←FRONTEND_URL, no literal | P1
routers/paperclip_proxy.py:1480,1786,1803,1500,1852 | 3 sign-in doors + 2 sign_in_user calls | ←stored email | P1
routers/paperclip_proxy.py:1537     | reads x-isol8-public-host                           | →              | P6
routers/paperclip_proxy.py:13,17,20,22,36,47,49,206,656-658,691-714,800,803,812,942,974,1107,1227-1228,1243-1278,1452,1476,1525-1531,1599,1696-1719,1773-1777,1810 | comments (incl. 4 GooseTown refs) | text | P3
core/services/paperclip_session_cookie.py:55,195,205 | _COOKIE_DOMAIN ".isol8.co" (B16)   | ←registrable domain | P1
core/services/paperclip_session_cookie.py:62,4,14-26,52,53,83,107 | _BIND_COOKIE + docs   | KEEP at P3     | P6
core/services/paperclip_user_session.py:40,59 | clerk_email_resolver                       | ←stored        | P1
core/services/paperclip_adapter_config.py:59-61 | _GATEWAY_URL_RE (fail-CLOSED, B5)        | ←WS_GATEWAY_URL| P1
core/services/paperclip_adapter_config.py:85,156,177,196,219 | brand strings; the url composer; paperclipApiUrl | → / KEEP | P3/P6
core/services/paperclip_provisioning.py:209-222 | _ws_gateway_url                          | ←WS_GATEWAY_URL| P1
core/services/paperclip_provisioning.py:457,981,1034-1038 | 3 sign_in_user               | ←stored        | P1
core/services/paperclip_provisioning.py:461-465,985-989 | 2 sign_up_user — ALSO write paperclip_email | + | P1
core/services/paperclip_provisioning.py:497,810,1019,1091 | the 4 row constructions        | + field        | P1
core/services/paperclip_provisioning.py:551,562 | description="Isol8 Teams workspace" (STORED) | →+backfill  | P3
core/services/paperclip_provisioning.py:140-155 | the put-wipes-fields docstring          | —              | —
core/services/paperclip_provisioning.py:295     | bootstrap-admin prose                    | DO NOT RENAME  | —
core/services/paperclip_provisioning.py:12,182,214,631,873,1045,1546-1558 | comments        | text           | P3
core/repositories/paperclip_repo.py:~194,372,467,337,4-23,177-211,726 | 3 edits + docstrings | + field      | P1/P6
core/services/paperclip_owner_email.py:51-56,66-83 | the stale-first read (D3)             | decide         | P1
core/services/user_provision.py:766,785-792,805-810 | lookup_owner_email × provision_member | ←stored       | P1
routers/webhooks.py:350,697-701,723-760 | member email; user.created put; user.updated (no put) | ←stored / + | P1
core/repositories/user_repo.py:25-41    | put() — the only email writer                    | —              | P1
core/services/paperclip_admin_session.py:4,56,58,102,136 | prose + secret template + the 6th sign_in_user | KEEP email; name P6 | P6
core/services/paperclip_api_key.py:16,24,59-74,205 | prose; secret path; name="isol8-mcp-server" (STORED in Paperclip) | KEEP / P6 | P6
core/services/paperclip_sql.py:46,52,74-75 | cluster + credentials ARNs; SELECT-only guard | KEEP           | P6
core/services/paperclip_agent_idempotency.py:17 | _KEY_PREFIX "isol8-agent-create:v1"     | NEVER          | —
core/services/paperclip_openclaw_pairing.py:312,346,393,752 · paperclip_admin_client.py:15-86,214,484 · paperclip_events.py:1,3,14 · paperclip_trigger_proxy.py:9 | comments + the plugin env pair | text / fork | P3/P6
routers/teams/{agents.py:146,151,156,265-272,305-321,525-533,572 · approvals.py:46 · attention.py:3,229 · schemas.py:205 · work.py:66} | comments + _gateway_url_for_env (a wrapper) + the email resolver wiring | ←stored / text | P1/P3
core/services/company_deploy_service.py:71,88-90,303 | _gateway_url() wrapper — NO isol8 literal, invisible to grep | ←derived | P1
core/services/paperclip_agent_lifecycle.py:316,443 | 2 more gateway-URL call sites          | ←derived       | P1
```

**Mail / SES**
```
core/services/agent_email.py:5,47,59,88,120,124,140-158,212-222,225 | address builder, put, verified_senders, sender_allowed, resolve_from_address, ensure_agent_address | mostly derived; §4 D1/D2 | P3
core/repositories/agent_email_repo.py:45-50,53,56,120-142,148-194 | PK write, docstring, and the 3 EXISTING helpers | §4 D1/D2 | P3
core/repositories/agent_mail_inbox_repo.py:13,25,37,81,82,116 | composite key, TTL, reply_key, a skill-path docstring | AFTER (TTL) | P3/P6
core/services/agent_mail_tenant.py:9,10,27,52-58,84,87 | apex collapse + per-process memo + the association | ←apex-of-mail-domain | P1
core/services/ses_email.py:6-27,100-108,145,148-151 | docstring hosts; the AGENT_EMAIL_DOMAIN gate; the swallow | (C-F1) | P1/P3
core/services/email_service.py:9,16-18,20,44,45,59 | "the email IS the only capture path"; never-raises | text | P3
core/services/agent_mail_autoreply.py:37 · agent_mail_reply.py:35 | "https://isol8.co/settings" ×2 | ←FRONTEND_URL | P1
core/services/agent_mail_inbound.py:249 | "/app/skills/isol8/scripts/isol8-mail.sh send --reply" | image lockstep | P3-or-P6
core/services/agent_mail_thread.py:37   | _PREFIX "isol8-email:" (B15)                     | KEEP           | P6
core/services/agent_mail_owner_send.py:175,179,215 · agent_mail_send.py:49,158 · agent_mail_delivery.py:98 | comments + the 4th owner_facts writer + get_by_address | text | P3
core/services/welcome_email.py:18,28,62,75,103 | subject + 4 body/sign-off strings (uses settings for URLs — no domain work) | → | P3
core/services/support_ticket.py:32,137,140 | admin host literal; to=; from_addr=            | ←ADMIN_HOST    | P1/P3
```

**Identity / persona / marketplace**
```
core/services/friday_identity.py:204,216-221,230,238,260,326,345,351,385 | SOUL/IDENTITY/USER/AGENTS template brand text (~20 strings) | → | P3
core/services/friday_identity.py:210,905,922,924 | _SELF_GROUNDING_HEADING + its migration (C20) | legacy tuple | P3
core/services/friday_identity.py:802,806,812      | legacy matchers + _FACTS_POINTER_LINE_1  | DO NOT RENAME  | —
core/services/friday_identity.py:30,79,110,112,123,170,172,822,1191,1254,1315,1327,1401,1413,1070,1080-1100 | seed filename, fork-version prose, write_if_missing sites, heal-migration list | P6 / — | P6
core/services/owner_facts.py:3,4,14,30,38,98,107,150 | facts filename ×4, the provider label, email_address, the unconditional write | filename P6; label P3 | P3/P6
core/services/seller_display.py:8,34,35,99-125,133-140 | CURATOR_HANDLE/BYLINE + the fallback chain | →@lightbulb | P3
core/services/seller_profile.py:43              | RESERVED_HANDLES                            | ADD 3          | P1
core/services/company_identity.py:34-107,47,63,64,102 | RESERVED_SLUGS + comments + "admin-dev" | ADD 3        | P1
core/services/curator.py:4,12-15,22,29,34,47,51 | the memoized curator lookup                 | ←setting       | P1/P3
core/services/marketplace_search.py:3,136 · marketplace_hire.py:17-66 · catalog_package.py:19 · agent_export.py:3,158,783 · agent_provenance.py:3,59 · agent_redaction.py:18-20 · agent_roster.py:7 | comments + fork-tag prose | text | P3/P6
core/services/marketplace_hire.py:48,52,54,61,86-89,100,298 | isol8_listing_{slug,id} → approvals.payload | P6 + Aurora | P6
core/services/marketplace_service.py:107-111,181 | the 3 immutable S3 objects + manifest_json | no backfill    | P3/P6
core/services/catalog_service.py:220,349         | cron uuid5 seed                            | NEVER          | —
core/services/catalog_slice.py:475               | _scrub_plugins_block(openclaw_json.get("plugins") or {}) | —   | —
core/services/takedown_service.py:39             | ADMIN_FILED_BY_EMAIL "admin@isol8.internal"| →+backfill     | P6
core/services/run_analytics.py:51,72,160,254,279 | uuid5 seed (NEVER); TTL; the is_internal stamp; the gate consumer | — / P3 | P3
core/services/openclaw_backup_import.py:838,841,861 | 3 USER-VISIBLE import warnings           | →              | P3
core/services/node_proxy.py:136-140              | strip-then-append (compounding trap)        | strip both     | P3
core/services/desktop_pair.py:245,303,351,358    | 2 display-name suffixes + comments          | self-heals     | P3/P4
core/services/inbound_triggers.py:210,211,229-249,266-269,293,294,504-513,525,559-569,573,701,712,713,1114,1298,1329,1433-1480,1492-1506,1541,1704-1705 | renewal cron, maintenance clauses, EFS watch state, the block markers, GitHub/Vercel/Meta auto-hooks, error copy, the starter_prompt renderer, the Gmail audience instruction | §4 E1/A2, §3 B22/B23 | P3
core/services/trigger_fire_url.py:29,32,33       | derived at read time — NOTHING persisted     | no backfill    | —
core/services/system_health.py:78,82             | a table name bypassing the prefix + the down-status | ←settings   | P1
core/services/connection_service.py:110,113      | default "isol8-websocket-connections"        | KEEP           | P6
core/services/cloudwatch_logs.py:11,12,41 · cloudwatch_url.py:19 | /ecs/isol8-{env}          | KEEP           | P6
core/services/ops/alarms.py:14,15 | _SEVERITY_RE ^isol8-[^-]+-(P|W)\d+- (lockstep w/ alarmNames) | KEEP  | P6
core/services/ops/deploys.py:62   | CFN stack prefix f"isol8-{env}-"                          | KEEP           | P6
core/services/ops/gate_blocked.py:60,64 | Namespace "Isol8"; service "isol8-backend"           | KEEP           | P6
core/services/ops/ecs_ops.py:79-85,88-95 | primary-email resolver + the stale-email read       | (D3)           | P3
core/services/ops/growth.py:3,4,109 | 2 citations to UNTRACKED docs; the is_internal filter  | git add        | P1/P3
core/services/admin_service.py:207,208,300,696,736,770 | emails[0]; _PAPERCLIP_PROVISION_FIELDS (correct as-is); _OPENCLAW_EXTENDED_REPO + 2 consumers | fix / KEEP | P1/P6
core/services/account_wipe.py:115,125,140-147,153-160,165-176,602,862 | the bootstrap guard chain (B6) | DO NOT RENAME | —
core/services/paperclip_user_delete.py:36,44-48,49,77,79-83,87-90 | BOOTSTRAP_EMAILS + the pre-rejected fix + the raw DELETE | DO NOT RENAME | —
core/services/atlas_seed/__init__.py:19,103,106-120,124,167,174-177,280-296,308-316,366,380-383 | fingerprint marker, bridge placeholder, _api_base_url, the deliberate non-coverage, force/restage | hosts P1; ids P6 | P1/P6
core/services/openclaw_backup_history.py:133     | references isol8-local-sqlite-boot.sh       | →              | P6
core/services/efs_file_lock.py:35,36             | .isol8-guard / .isol8-protocol              | KEEP           | P6
core/services/key_service.py:4,40,43 · integration_secret_store.py:7,30,33 · oauth_app_store.py:3,38 · platform_oauth_app_store.py:3,7,10,37 · owner_secret_sweep.py:11-13,41,42 · webhook_dedup.py:3 | isol8/{env}/* paths + the prefix sweep | KEEP (lockstep) | P6
core/services/credentials/oauth_dance.py:10,87,88,114,152-156,266,272 | client_name "Isol8"; redirect_uris pinned; the re-register condition (B3) | → + code fix | P1/P3
core/services/credentials/registry.py:20,146,147,195,216,327,346,395 | RefreshOwner "isol8_rematerialize" + comments | P6 / text | P3/P6
core/services/credentials/materializers/aws_cli.py:35,36,44,51,78,84,98,101,104,112,132,135 | connector profile, helper filename, 6 "isol8: " console.error strings, API_BASE bake | NEVER / P3 / §4 E2 | P3/P6
core/services/credentials/materializers/xurl_cli.py:29,42,136-158,165-183 | isol8-x app key, redirect_uri, default_app, remove() | KEEP at P3 | P6
core/services/credentials/materializers/mcp_oauth.py:113,130,144 | client_information → EFS + mcporter | after B3 | P3
core/services/credentials/providers.yaml:18,34,45,600-605,711,734,758,802,827,876-879,1101,1125,1199-1201,1210,1235,1249,1482,1494-1499,1496,1523,1571,1894,1902,1929,1967,1968,2000,2028,2045,2050,2051,2119,2155 | 9 DCR registration_urls, 5 guided_paste {proxy_url} blocks, the Intercom allowlist note, the metric-namespace starter_prompt (DOUBLED braces), a fork tag, Meta app ids, the owner_allowlist (UNCHANGED) | mixed | P0/P3/P6
core/billing/bedrock_aip.py:47,367,443,491,492,654,657,879 | connector role; AIP name; ManagedBy tags; the boundary-prefix comment; owner role name; description | NEVER / P6 | P6
core/billing/bedrock_gate_tokens.py:25,124 · bedrock_gate_url.py:6,17,37 | table + SSM templates | KEEP | P6
core/billing/metronome_funding.py:93,185 · metronome_provisioning.py:279 · metronome_units.py:10 | ledger descriptions ×2; per-owner alert name; a comment | P3 text / P6 | P3/P6
core/billing/billing_service.py:38,39,215,603,1093,1475 | module-level getenv (import-time); comments; the support literal | ←settings | P1/P3
core/observability/metrics.py:29,99             | NAMESPACE "Isol8"; service "isol8-backend" | KEEP           | P6
core/openclaw_version.py:159,160,163,166,170,174-201 | _ISOL8_PATCH_RE + isol8_patch_level + deployed_fork_at_least | dual-spell | P6
core/containers/config.py:174,1012,1146-1161,1454,1555,1714,2005-2011,2022-2030,2065-2074,2160,2174,2403,740 | comments; the session-key allowlist; the plugin id + in-image path; the fork gate consumer; the ONLY http(s) line (an SSM Lambda URL) | KEEP | P6
core/containers/ecs_manager.py:183,316,379,382,686,1170,1801,1802,1852,1853,2045-2070,2267,2299,2300,2488-2496,2775,2783,4079,4087,4224 | lock-protocol env; base taskdef family; tags; the per-user clone; serviceRegistries (create only); boot-script protocol literals; the reprovision-vs-roll comments; discover_ip; BYOK secret ARNs; ISOL8_API_URL/OWNER_ID; write_owner_facts; paired.json | mixed | P1/P3/P6
core/containers/workspace.py:535,1068 · core/crypto/operator_device.py:163,272 · core/gateway/connection_pool.py:244,250-256,989-1001,2141,2816 · core/tenancy_codes.py:13 | comments + a paired.json display name (BUDGET_BLOCK_MESSAGES verified brand-free) | text | P3/P6
routers/marketplace_purchases.py:3,29,39,43,110-146 | the host allowlist + its error text + 2 webhook registrations | ←FRONTEND_URL | P1/P3
routers/marketplace_payouts.py:47,48        | Connect refresh/return from settings (NOT dashboard) | —           | P3
routers/marketplace_listings.py:5,513,1541 · marketplace_admin.py:521,573 | curator prose + scope comments | → | P3
routers/users.py:39,151                     | put(no email); the support literal                   | ←settings    | P1
routers/websocket_chat.py:361,362,390,738,794,1354,2402 | the ws-internal dep + header alias ×2 + comments + "managed by Isol8" | → | P3/P6
routers/ses_inbound.py:13,170,199,363,390   | the header ×2 + 2 get_by_address routes + a comment   | →            | P3/P6
routers/connections.py:454,495,540,585,599,603-607,618,631,651,870,873-876,998 | STS session name; 3 apply_connection callers; CFN_TEMPLATE_URL; the connector-name reject + its text; the callback URL builder | NEVER / ←settings | P1/P6
routers/orgs.py:41,80                       | comment + the HTTPException detail                    | →            | P3
routers/billing.py:377,656,1131,1139        | "Isol8 credits."; a comment; the webhook handler + single-secret verify | → | P3
routers/apple_iap.py:97,153,179,188,210,225-228,235-244,248,486,631 | comments; the return path; the webhook route; the config gate; the numeric-id contract | → | P3
routers/admin.py:4,100,275,920-951          | comments; the ECR placeholder; admin_container_reprovision; the type-to-confirm wipe | → | P3/P6
routers/{automations.py:21,browser_view.py:16,channels.py:82,desktop_pair.py:93,hooks_proxy.py:10,integrations.py:5,platform_info.py:4,6,12,updates.py:221,devices.py:30,desktop_auth.py:22,meta_webhook.py:202-203,metronome_webhook.py:99,107,127-132,webhooks.py:87-106,271-283} | comments, doc refs, route surfaces whose PUBLIC URL moves with PUBLIC_API_BASE | text / vendor | P3
schemas/marketplace.py:193                  | comment "needs the Isol8 desktop app"                 | →            | P3
scripts/bootstrap_paperclip_admin.py:26,91-105,107,108,114-118,127,209 | prose; ADMIN_EMAIL (NEVER); the secret template; _public_host; name="Isol8 Backend Admin"; a profile | mixed | P1/P3/P6
scripts/audit_paperclip_adapter_types.py:17,33,47,54,87,165,176,278,279,284,286 | docs; 2 ARNs; the profile default with NO escape | + `or None` | P1/P6
scripts/set_platform_oauth_app.py:41,49,52  | profile default (HAS the escape); secret template      | →            | P6
scripts/recover_orphan_paperclip_member.py:14,34,77,110,121,138,202 | the bootstrap import + 4 hardcoded names | KEEP / P6 | P6
scripts/ingest_companies_registry.py:38,124,137,146,152 | ISOL8_SYSTEM_SELLER_ID "__isol8__" (STORED ×4) | NEVER  | —
scripts/backfill_metronome_ledgers.py:212,217 | sets the prefix env + asserts the table name        | →            | P6
scripts/drift_monitor.py:58                 | Namespace="Isol8/Billing"                             | KEEP         | P6
scripts/{provision_runner.py:8,bedrock_gate_break_glass.py:11,bootstrap_agent_routines.py:15,fix_workspace_state_timestamps.py:39,reconcile_bedrock_vs_ledger.py:3,41,75,358,644,reconcile_paperclip_profile_meta.py:23,46,288,rematerialize_instagram_skill.py:8,22-26,72-116,scrub_manifest_snippets.py:29,30,sweep_wipe_orphans.py:36-83,backfill_seed_team_roster.py:291,restage_atlas.py:1-17,reapply_openclaw_config.py} | docs, prefix examples, profile defaults, and the 3 re-runnable ops tools §4.9 names | text / P6 | P6
```

### 6.5 `apps/infra/openclaw/**` + repo-root pins + the two forks

```
Dockerfile:84                        | FROM ghcr.io/isol8ai/agent:2026.8.1-isol8.1 (lockstep w/ openclaw-version.json:3) | → | P6
Dockerfile:388,408,409               | warp boot comment + COPY + chmod                        | →              | P6
Dockerfile:540,543,561,562,563       | marketplace-plugin layer comment ×3 + COPY + chown      | →              | P3/P6
Dockerfile:684                       | AWS_PROFILE=isol8-connector comment                     | NEVER          | —
Dockerfile:718,724,738,739,746       | skill auto-discovery note (the C19 unknown); write-path note; skill layer; facts filename | → | P3/P6
Dockerfile:750                       | COPY skills/isol8 /app/skills/isol8 (the skill ID)      | →              | P6
Dockerfile:759,760,761               | /opt/isol8/chrome-extensions/*.crx + the EXTENSION_ID json + chmod | →   | P6
Dockerfile:836,837,838,839           | 3 boot-chain COPYs + chmod (B14)                        | →              | P6
.dockerignore:3 · README.md:5        | prose (plugin dir; ECR repo)                            | →              | P6
chrome-extensions/popups-to-tabs.external.json:2 | external_crx /opt/isol8/…                   | →              | P6
chrome-extensions/popups-to-tabs/manifest.json:3 · background.js:1 · README.md:30,34 · EXTENSION_ID | "Isol8 popups-to-tabs"; a comment; the re-sign instruction; the id | LEAVE (no .pem) | P6
local-sqlite/isol8-local-sqlite-boot.sh (FILENAME) :2,39,41,42,55,56,57,59,61,63,104,105 | ISOL8_STATE_ROOT/LITESTREAM_CONFIG/APP_CMD/OWNER_ID; the isol8-facts.json owner-id fallback (FATAL); /tmp/isol8-s3-err | → | P6
local-sqlite/isol8-local-sqlite-app.sh (FILENAME) :2 · isol8-litestream.yml (FILENAME) :4,5,42,49 · smoke-test.sh:2,61-63 | | → | P6
warp/isol8-warp-boot.sh              | FILENAME only — body has ZERO isol8 strings             | →              | P6
sage/                                | verified ZERO isol8 strings (path-filtered at build-openclaw-image.yml:36) | — | —
plugins/aws-exec-profile/{index.ts:39,dist/index.js:30,dist/resolve.js:11,18,26-28,34-36,openclaw.plugin.json:4,package.json:5} | the connector profile + its line-anchored HEADER_RE | NEVER | —
plugins/paperclip-exec-creds/dist/resolve.js:31 | "The file shape is Isol8-defined and locked" | →              | P3
plugins/isol8-marketplace/ (DIR)     |                                                         | →lightbulb-marketplace | P6
  openclaw.plugin.json:2,3,4         | id; name "Isol8 Marketplace"; description               | id P6; labels P3| P3/P6
  package.json:2 · package-lock.json:2,8 · .gitignore:2,6-8 | @isol8/marketplace-plugin + the REBUILD-dist warning | → | P6
  src/env.ts:8,18,20,22,24,29,30,31,33,34 (+ dist/env.js:11,13,15,21,22,25,27 + dist/env.d.ts:7,17,19,21,26) | Isol8Env interface; {{ISOL8_BRIDGE_URL}}; env reads; the THROWS | → | P6
  src/index.ts:2,40,46,47,48,52,54 (+ dist/index.js:2,35,40,41,42,46,47) | header; "Isol8 API {status}"; plugin id; name; 3 tool descriptions the MODEL reads | id P6; text P3 | P3/P6
  src/index.ts:92,107 (+ dist/index.js:81,91) | kind "isol8.hire_suggestion"                | KEEP           | —
skills/isol8/ (DIR) SKILL.md         | 63 isol8 lines. :2 frontmatter name; :4-19 the retrieval-trigger description (do NOT shorten); :23,45,47,48,50,53,55,82,84,94-110,134,152,191,204,214,222,227-231,238,254-256,306,314,331,346,375,397,406,416,440,446,454,462-487,508 — ~26 isol8.co links agents PASTE to owners, 2 env vars agents EXECUTE, 5 runnable isol8-mail.sh command blocks, the facts-file pointer, the plan/pricing prose that must stay numerically consistent with owner_facts.py:60-81, and :466 support@isol8.co | hosts+brand P3; ids P6 | P3/P6
skills/isol8/scripts/isol8-mail.sh (FILENAME) :2,9-13,16,25,26,48,85 | usage lines; 2 env vars; the base URL | → | P6
skills/paperclip/SKILL.md:14,16,32,53,479 · references/{api-reference.md:710,718,982,1145 · company-skills.md:217,220 · attention-decisions-search.md:50} | vendored-with-local-edits header + 11 brand lines + a github.com/Isol8AI URL | → | P3/P6
openclaw-version.json:3,7,9,12,14    | upstream; extendedImage; dev.tag; prod.tag; notes (~30 historical refs — LEAVE as history) | → | P6
openclaw-version.json:4-6            | the LEGACY public upstream fallback — no isol8 string   | —              | —
openclaw-version.schema.json:12,13   | prose naming the fork; the pattern (already permissive)  | text only      | P6
paperclip-version.json:2,5,12        | upstream digest; extendedImage; notes (~40 refs, LEAVE)  | →              | P6
paperclip-extended/Dockerfile:1,4,15 · README.md:44,56,60,62,66,76,77,80 | fork prose; ECR repo; profile; builder role; the cross-account repo policy | → | P6
apps/backend/tests/chaos/_schema/PINNED_TAG:1 | ghcr.io/isol8ai/agent:2026.8.1-isol8.1        | regenerate     | P6
~/dev/agent-fork @ v2026.8.1-isol8.1 | 12 files / 48 items — docker-release.yml:3,15,25 (IMAGE_NAME HARDCODED); workspace.ts (seed filename :99, env :103, + prose/log lines); 6 ISOL8 FORK PATCH markers; 3 files that exist ONLY on the tag (mcp-oauth.ts, types.mcp.ts w/ an ISOL8 FORK KEY, state-migrations.mcp-oauth.ts); 10 local / 17 remote isol8/* branches; 17 v*-isol8.N tags | → (KEEP tags) | P6
~/dev/paperclip-fork                 | 17 files / 67 items — docker.yml:81,176,209,210,265,360,396,397 (DERIVED); Dockerfile:45,59,60; pnpm-lock.yaml:595; plugin-isol8-notifications/{package.json:2,4, src/manifest.ts:4,5,7,12,19,21,22, src/worker.ts:5,11,13,15,25,41,42,44,59,64,71,75}; bundled-plugins.ts:84,85,87,88,89,90,115; plugin-loader.ts:123,124,130,131-134,182; execute.ts:118,192,195,1272,1281,1301; built-in-agents.ts:803; productivity-review.ts:882; recovery/service.ts:370,375; 17 local / 18 remote branches; 27 tags | → (KEEP tags) | P6
```

### 6.6 `apps/mobile/**` + `apps/desktop/**`

"baked" = compiled/inlined into a shipped binary (Metro inlines `EXPO_PUBLIC_*`; Rust
`option_env!` at compile time; `tauri.conf.json` at bundle time).

```
mobile/app.json:3,4,5,9,18,23,29,48  | name/slug/owner/scheme/bundleIdentifier/2 usage strings/applinks — ALL baked | → | P3 (dev values :12,:27 at P2)
mobile/app.json:11-13,15,20,26,61    | runtimeVersion fingerprint; updates.url; appleTeamId; aps-environment; projectId | UNCHANGED (needs a new push key) | —
mobile/eas.json:12,27,36             | EXPO_PUBLIC_API_URL ×3 (dev/preview/production)      | →              | P2/P3
mobile/eas.json:13,28                | pk_test (dev Clerk not domain-bound)                 | UNCHANGED      | —
mobile/eas.json:14,29,38             | EXPO_PUBLIC_APPLE_IAP_PRODUCT_ID ×3                  | →              | P3
mobile/eas.json:37                   | pk_live_Y2xlcmsuaXNvbDguY28k (base64) — must mirror mobile-ota.yml:64 | → | P3
mobile/eas.json:45                   | ascAppId "6788944100"                                | →new ASC id    | P3
mobile/eas.json:46-48                | ascApiKeyPath/IssuerId/KeyId SZ68D5WBR2 (team-scoped) | UNCHANGED      | —
mobile/package.json:2,8,9,22         | @isol8/mobile; start:dev + start:prod hardcode BOTH hosts and BOTH keys; @isol8/shared | → | P2/P3/P6
mobile/src/config/env.ts:11,23       | api-dev fallback (the mobile-ota:110 guard target); WEB_BILLING_URL | ←config | P1/P3
mobile/src/lib/{webLinks.ts:5,22 · deepLink.ts:2,6,9,23,26,35 · providerAuthStatus.ts:2,43 · purchaseCta.ts:6,8 · teamsData.ts:13} | WEB_ORIGIN; 2 scheme/host REGEXES (deepLink.ts:35 is a security surface); prose | ←config | P1/P3
mobile/src/lib/{aiConsent.ts:26-31 · pushDevice.ts:5,9} | KEY_PREFIX "isol8.aiConsent.v1." (its own docstring: "Renaming the key is the whole mechanism"); 2 push keys | rename WITH the disclosure text | P6
mobile/src/app/(auth)/sign-in.tsx:87,89,102,123,147,237 | the Clerk native-callback comment; 2 "finish on isol8.co"; the wordmark; the sign-up line | → | P3
mobile/src/app/(app)/settings.tsx:23,24,29,337,338,480,510,527,584,591 | comments; 2 support mailtos; "Isol8 Free plan"/"Isol8"; 2 "on isol8.co"; privacy + terms URLs (App Review reads these) | → | P3
mobile/src/app/(app)/control/{integrations.tsx:28,230,232,251,253 · llm.tsx:37,371,373,402,430,488 · usage.tsx:23,288,294,296 · agent-detail.tsx:527 · channels.tsx:205 · overview.tsx:114 · listing/[id].tsx:261} | 3 MANAGE_*_URL constants + 17 "on isol8.co" strings | ←config / → | P1/P3
mobile/src/app/(app)/teams/{index.tsx:417,420,525,528,793,796 · issue/[id].tsx:307,310 · routines.tsx:81,153,156,186 · run/[runId].tsx:163} · components/teams/BoardView.tsx:171,174 | 15 "Manage … on isol8.co" | → | P3
mobile/src/components/billing/PaywallScreen.tsx:4,118,130,196,303,316,374,375,384 | 3 support mailtos; the title; 3 guideline-3.1.3(b) copy strings | → | P3
mobile/src/components/consent/AiConsentGate.tsx:43,107,128 | PRIVACY_URL + 2 disclosure strings | → | P3
mobile/src/components/{onboarding/FirstRunFunnel.tsx:283 · provision/{ByoKeyScreen.tsx:46,ProvisionGate.tsx:21,29,79,82,184,189,191,216,478,ProvisioningStepper.tsx:424,Wordmark.tsx:6,8,21,23} · chat/ChatErrorCard.tsx:9,16} | 2 mailtos; WEB_ORIGIN; 5 "on isol8.co"; the wordmark + a11y label; comments | → | P3/P4
mobile/.gitignore:42 + apps/mobile/ios/** (UNTRACKED, EXISTS on disk) | Isol8.xcodeproj/.xcworkspace; Info.plist:10,30,31,37; 55 hits in project.pbxproj | rm -rf / prebuild --clean | P3
mobile/{build-*.ipa ×3, dist/}       | untracked stale build outputs carrying co.isol8.mobile| DELETE         | P3
desktop/src-tauri/tauri.conf.json:3,5,9,10,17,24,41,50,57,62 | productName; identifier; devUrl; frontendDist; title; window url; externalBin; signingIdentity; schemes; updater endpoint | 4 CI-patched; 6 are NOT | P1/P3/P5
desktop/src-tauri/tauri.conf.json:64 | updater minisign pubkey                              | NEVER CHANGE   | —
desktop/src-tauri/capabilities/default.json:4,7 | description + remote.urls (C-P3/§2)       | CORRECT + inject| P1/P3
desktop/src-tauri/src/lib.rs:25,29-31,82,205,334,338,346,350,385-403,450,534,967-973,976-998,1006,1018,1053,1059,1080,1087,1096,1173,1175,1183,1187,1193,1205-1217,1219 | KEYCHAIN_SERVICE; 4 option_env! defaults; the log path; app_data_dir ×2 (auto-follows); the sidecar resolver; the callback URL; is_oauth_url (DO NOT ADD the new host); the scheme checks ×4; setup logs | → | P1/P3/P6
desktop/src-tauri/src/file_access.rs:5,6,71,73-82,91,95,127,142,274-280 | the ~/Isol8 doc line; store_dir() HARDCODED "co.isol8.desktop" (does NOT follow identifier); the ~/Isol8 seed + allowlist; 5 inline test literals | → | P3
desktop/src-tauri/src/{main.rs:4 · keychain.rs:4 · bootstrap.rs:49 · node_invoke.rs:85,298 · tray.rs:18,39 · screen.rs:95 · proxy.rs:56,157,177 · subprocess.rs:36,107,116,125 · node_client.rs:970,978,984 · pair_state.rs:13,142 · Info.plist:23,24} | crate ref; comments; 2 user/server-visible display names; tray label + tooltip; snapshot filename; ISOL8_PROXY_PORT; 4 inline test fixtures | → | P3/P6
desktop/src-tauri/scripts/vendor-sidecars.sh:2,6,61,97,109,111,120,124 | 2 comments; isol8-openclaw-host-<triple>; isol8-browser-service-<triple>; ISOL8_PROXY_PORT ×3; **:120 OPENCLAW_HOME ".../co.isol8.desktop/openclaw" baked into a GENERATED shim** | :120 P3, rest P6 | P3/P6
desktop/src-tauri/{Cargo.toml:2, Cargo.lock:2095} · package.json:2 | crate name ×2; @isol8/desktop | → | P6
desktop/src-tauri/bin/** (untracked, regenerated) | isol8-browser-service-<triple> shim      | regen          | P6
```

### 6.7 `apps/frontend/**` + `packages/shared/**`

```
next.config.ts:11,12,14-20,26,30,31,46 | the enforce-later note; the Termly/Turnstile warning; script-src/connect-src/frame-src (clerk + api + ws hosts; bv MISSING); Report-Only | derive per-env; ADD bv; KEEP 3 vendor origins | P1/P3
next.config.ts:53-55                   | transpilePackages ["@isol8/shared"] + the no-build-step note | →       | P6
next.config.ts:77-83,81,94,103,113,114,118,119 | the AASA content-type override + its silent-failure note; 3 comments; 2 company-host rewrite pairs | derive | P1/P3
src/middleware.ts:10,22,59,65-67,88-91 | DEFAULT_ADMIN_HOSTS; the never-set env; the injectable 3rd param; the 404 (not redirect); PAPERCLIP_HOSTS ×2 | derive | P1/P2/P3
src/app/api/desktop/update/[target]/[current_version]/route.ts:14,25,81-84,116-118,124,125,130 | comments; the SUFFIX-only bundle match; the host allowlist ×3 + the fallback pair; notes (always "") | ←env | P1
src/app/api/download/desktop/route.ts:29,44,45 | tagPrefixFromHost + suffix-only asset match | NO CHANGE      | —
src/lib/githubReleases.ts:7,16,23,52,63-73,111 | doc refs; ISOL8_REPO; githubHeaders (GITHUB_RELEASES_TOKEN); tagPrefixFromHost (domain-agnostic); the consumer | → | P6
src/app/layout.tsx:53,58,64,65,67       | SITE_TITLE; metadataBase; og url; siteName; og image alt | ←SITE_URL / → | P1/P3
src/lib/seo.tsx:3,4,54,57,66,72,99-108  | SITE_URL; SITE_NAME; org name; the LinkedIn URL; 2 descriptions; the 10 seoLandingPages rows (each ends "| isol8") | ←SITE_URL / → | P1/P3/P5
src/app/[seoSlug]/page.tsx:46,82        | the title.replace suffix strip; "Start with isol8"   | lockstep       | P3
src/app/{sitemap.ts:7 · robots.ts:3 · page.tsx:11 · blog/page.tsx:9,22,42 · blog/[slug]/page.tsx:11,26,30,41,71 · docs/page.tsx:8,10,30,32,37,48 · docs/[slug]/page.tsx:21 · marketplace/page.tsx:10,11,26,38 · marketplace/agents/page.tsx:8,24,35 · marketplace/layout.tsx:9,10,72,81 · settings/layout.tsx:4 · chat/layout.tsx:4 · checkout_redirect/[status]/page.tsx:26} | SITE_URL constants; titles; og alts; JSON-LD names + the 4 url: overrides | ←SITE_URL / → | P1/P3
src/app/{terms,privacy,cookies,eula,disclaimer,acceptable-use,accessibility}/page.tsx:6,7,8,12 | 7 × (title, canonical, description) + 7 Termly dataIds (uuids — unchanged if docs are edited in place) | ←SITE_URL / → | P1/P3
src/components/legal/LegalDocument.tsx:19,35 | the Logo call site + the © on all 7 pages       | →              | P3/P5
src/components/legal/TermlyEmbed.tsx:37 | loads app.termly.io/embed-policy.min.js (CSP dependency) | —          | —
src/components/Logo.tsx:20-29,36,45,46  | LogoMark (the bare Didone "8"); ariaLabel; the internal ref; ISOL8 — 18 call sites | → + retype | P3/P4
src/components/landing/{Footer.tsx:47,52 · PricingThreeCard.tsx:76 · WorkspaceDemo.tsx:453,610,633,814 · demoData.ts:182} | ©; the decorative wordmark; "Isol8 for Teams"; 2 friday@…isol8.co in PUBLIC MARKETING COPY; a11y label; a Logo site; "Isol8 v0.1" | → | P3/P4
src/app/globals.css:1512-1548 · landing.css:1767-1784 | the brand block (letter-spacing :1517, the Didone comment :1525-1528) + .ld-footer-word clamp() | retype for 12 chars | P4
src/app/globals.css:1027-1144 · landing.css:7 · public/goosetown/*.png ×2 · public/goosetown-preview.png (1.09 MB) | dead GooseTown CSS + assets, zero TSX refs | DELETE | P3
src/components/support/SupportTicketDialog.tsx:16,113,171 | the "GooseTown" support CATEGORY + 2 support addresses | DELETE / → | P3
src/app/support/page.tsx:20,51,89,91,99,102,104,107,119 · support/layout.tsx:5,6,7 | the Logo site; 5 mailtos incl. the account-DELETION path; 2 brand strings; the ©; title/canonical/desc | → | P1/P3
src/app/settings/page.tsx:109,110,123,462,503,757 · settings/connections:104 | 3 support literals; 3 plan-name strings ("Isol8 plan" / "— Free tier" / "— $50 / month" — note the backend's public name is "Starter"; do not silently "fix" it here); 2 Logo sites | → | P3
src/components/settings/AwsConnectCard.tsx:55,206,273 | 3 connector-flow brand strings          | →              | P3
src/components/chat/{ChatErrorCard.tsx:19,25,79,97,110 · ProvisioningStepper.tsx:139,145,949,988,1094,1134,1156,1308,1354,1444,1680,1804,1847,1924,2155 · AgentChatWindow.tsx:34 · HireCard.tsx:39,69,74 · shell/useUserSyncOnce.ts:36} | 5 brand strings; 2 storage keys + 5 Logo sites + 7 mailtos + a comment; a storage key; the wire discriminator; an event name | → / KEEP | P3/P6
src/components/{onboarding/{FirstRunSteps.tsx:181,FirstRunFunnel.tsx:48,InviteTeammatesStep.tsx:87} · getstarted/{getStartedContent.tsx:44,GetStartedChecklist.tsx:16} · desktop/{DesktopUpdater.tsx:7,15,101,160,165,NodeStatusPill.tsx:12,DesktopFileAccess.tsx:117} · channels/BotSetupWizard.tsx:620 · control/panels/{SkillsTab.tsx:585,PluginsTab.tsx:12,139,llm-auth-status.ts:1,39} · teams/panels/{RoutinesPanel.tsx:1118,RoutineDetailPanel.tsx:1629} · marketplace/{drawer/wizardSteps.tsx:620,625,739,storefront/{UserSync.tsx:7,11,Listing/BuyButton.tsx:12,136}} · workspace/{WorkspaceShell.tsx:56,386,layoutStorage.ts:17,18,panels/{browserViewUrl.ts:1,2,browserNav.ts:3}} · PostHogProvider.tsx:25,27,28} | a mailto; a draft key; 2 invite strings; 2 onboarding strings; the "isol8 v{version}" render + comments; a log-path comment; a brand string; the Slack app-token name the USER TYPES; 4 plugin/skill strings; 2 webhook copy strings; 3 marketplace legal strings; a storage key + comment + a buy-button string; ISOL8_THEME + a dockview name + 2 layout keys (KEEP the v1); comments; POSTHOG_API_HOST "/ingest" (same-origin, no change) + 2 analytics keys | → | P3/P5/P6
src/app/{auth/desktop-callback/page.tsx:6,40,41 · checkout_redirect/[status]/page.tsx:35,39,44,50,70 · oauth/done/page.tsx:15 · onboarding/page.tsx:163 · sign-in/[[...sign-in]]/page.tsx:272,306,310 · sign-up:232,290} | the API fallback; "Opening Isol8 desktop app…"; isol8://auth; 4 "the isol8 app" + href="isol8://"; comments; an org string; 4 Logo sites + a sign-in heading | ←config / → | P1/P3
src/app/admin/{layout.tsx:57 · deploys/page.tsx:32 · _actions/marketplace.ts:12 · marketplace/takedowns/page.tsx:29 · users/[id]/container/ContainerActionsPanel.tsx:399 · _lib/url.ts:26} | "isol8 admin"; "No isol8 stacks found."; 2 scope comments; the ECR placeholder; already env-driven | → / P6 | P3/P6
src/lib/{api.ts:13,29,30 · analytics.ts:170 · signupIntent.ts:20 · snooze.ts:21,25 · desktopPermissions.ts:16,17 · filePathDetection.ts:5,25,28,29,38,43,46,50,53 · slashCommands.ts:20 · slackManifest.ts:4,18,19,22,29 · agentDesign.ts:73 · toolDisplay/index.ts:14 · marketplace/{api.ts:30,types.ts:2}} | the shared wrapper; the attribution key; the cookie; 2 snooze prefixes; a key + a DOM EVENT; the isol8-file:// scheme ×9 (frontend-only: produced :38, consumed MessageList.tsx:3,227) + isol8_agents/; comments; the Slack manifest ×6 (guard-tested); a fallback description; a fork-tag comment; 2 comments | → | P3/P6
src/hooks/{useActiveSession.ts:21 · useLandingBriefReplay.ts:27 · useDesktopAuth.ts:11 · useAgentChat.ts:8,488,1377 · useBilling.ts:140 · useConnections.ts:82 · useTeamsApi.ts:9 · internal/buildChatItems.ts:5} | 2 storage keys; a scheme comment; 5 comments; a cookie-scope comment | → | P3/P6
src/components/teams/** (16 files)      | provenance comments: InboxPage.tsx:189 · useAttentionFeed.ts:48,50,89 · shared/types.ts:781,1378 · shared/components/{ProductivityReviewBadge.tsx:8,Identity.tsx:4,ConnectedAppTrigger.tsx:336,836} · shared/lib/{assignees.ts:6,searchQueryParser.ts:8,issueDetailBreadcrumb.ts:34,timeAgo.ts:6,keyboardShortcuts.ts:9} · search/{HighlightedText.tsx:7,SearchResultRow.tsx:6} · issues/{OutputFeedbackButtons.tsx:6,7,lib/documentReviewTypes.ts:3,lib/documentAnnotationSelection.ts:3,pickers/{StageParticipantPicker.tsx:5,recent-assignees.ts:5,6},IssueThreadInteractionCard.tsx:68} · agents/AgentOrgControls.tsx:10 | + the "topics/isol8-gmail" placeholder (customer-side, KEEP), 2 storage keys, __isol8_other__ | → | P3/P6
src/content/docs/{index.ts:28,30,33,38,45 · pages/{get-started.md:7,11,57,62 · browser-and-apps.md:31 · approvals-security-privacy.md:42 · skills-and-automations.md:30 · use-cases.md:3,7,31}} | 5 FAQ strings; a link whose TEXT names the domain; an agent-mail example; a support mailto; 5 brand strings | → | P3
src/content/blog/posts/*.md (6)         | authors "The isol8 team"/"isol8", body copy, and webhook-event-reference.md:17,18,19 (3 links into a PRIVATE repo) | → | P3/P6
packages/shared/{package.json:2 · src/index.ts:2 · api/{client.ts:35,208,245 · url.ts:5,6,7,33} · gateway/{useGatewayConnection.ts:29 · types.ts:2} · chat/history.ts:23} | the scope; 3 cookie-scope comments; 3 doc comments (the :33 REGEX is domain-agnostic); 2 comments; a fork ref | → | P3/P6
@isol8/* import sites                   | 50 occurrences / 28 files (frontend) + 89 lines / 55 files (mobile, 37 import-only) | mechanical | P6
apps/frontend/.env.example:10,13-20,35 · tests/e2e/.env.example:5,8 · vercel.json (ZERO refs) · playwright.config.ts:22 (BASE_URL-driven) | the dev host comment; the DEAD block; the admin-host typo; 2 e2e vars | → / DELETE / NO CHANGE | P1/P2
public/{og-image.png (the Didone "8" source), src/app/icon.png, favicon.ico} · public/logos/{gcal,gmail,salesforce}.png (UNREFERENCED) | brand binaries; dead assets | P4 / DELETE | P3/P4
```

### 6.8 Docs, context, memory, scratch

```
CLAUDE.md (79 hit lines)  | :7,9,11,12,16,18,63,90,612 overview+fork+skill+facts · :67,76,77,402,450,455 @isol8/* · :94,97,98,123,141,754 DEV hosts (**P2**) · :105,107,112,132,148,282,283 skills paths + profile + secret + the bootstrap-admin note (KEEP :147-148) · :118 mailsac · :154,155,194,199,265,750,780 org+repo+bot+forks · :168 concurrency · :223-233 graphify · :245-247,251,252,261,510,512,513,514,537,770 hosts + headers + tables · :314,360,361,370,423,424,461,466,588,598,614,634,635,752,755,761,767,775,786,789,790,791,800,802,806 prefixes, lock files, secrets, admin hosts, PAPERCLIP_HOSTS, bundle ids, schemes, cert, stage file, API URLs · :38-49 the disposable-docs rule · :197,210 issue back-refs (LEAVE) | mixed | P2/P3/P6
CLAUDE.md — CORRECTIONS TO MAKE | the Apple-IAP "503s until set" claim is STALE (C13); "current pin v2026.813-isol8.3" is stale (paperclip-version.json:12 says isol8.6); the infra-tests-in-no-CI-job belief is corrected by infra-ci.yml | fix as part of this program | P1
AGENTS.md (UNTRACKED, 41 hits) | a stale mirror + 3 dead facts                                    | DELETE         | P1
docs/ — LIVING (18 files, code- or CLAUDE.md-referenced) | audit-2026-05-02-paperclip-routes.md (19) · runbooks/{admin-rollout.md (21 — DELETE, §2), bedrock-gate-break-glass.md (11), page-alarms.md (11), openclaw-config-lock-protocol-rollout.md (3)} · superpowers/plans/{2026-07-05-mobile-app-expo-ios.md (13), 2026-07-10-mobile-push-notifications.md (33 — also carries a STALE path `lib/stages/isol8-stage.ts`), 2026-07-17-platform-info-live-read.md (33 — :150-202 is a VERBATIM copy of the shipped SKILL.md; edit both or delete), 2026-07-24-onboarding-implementation-prompts.md (6), 2026-08-26-admin-gtm-dashboards.md (17, UNTRACKED)} · growth/2026-08-17-blitzscaling-strategy.md (9, UNTRACKED) · superpowers/research/2026-07-24-onboarding-current-state.md (4) · superpowers/specs/{2026-05-04-teams-realtime-design.md (2), 2026-05-30-multi-instance-backend-architecture-design.md (1), 2026-06-24-oauth-reprovision-stale-token-design.md (2), 2026-07-10-mobile-push-notifications-design.md (4), 2026-07-12-onboarding-aha-first-run-design.md (5), 2026-07-14-admin-account-wipe-design.md (7 — KEEP the bootstrap literals at :120), 2026-07-15-bedrock-gate-design.md (11 — :85 the 429 strings, :108 the role-name log filter), 2026-07-24-onboarding-architecture-design.md (4)} | rewrite | P2/P3/P6
docs/ — ORPHANS (~24 files) | architecture/2026-05-07-paperclip-openclaw-pairing.md · architecture/credentials/{ONBOARDING.md, problem-discovery.md (45), research-notes.md (incl. `connect.isol8.com`, a host that NEVER existed), 2026-05-29-printing-press-spike.md} · audit-2026-05-04/SUMMARY.md · growth/2026-07-10-content-seo-gtm-plan.md · integrations/{apollo-cloudflare-mcp.md, apollo-prospect-source.schema.json:3 (`$id: https://isol8.ai/…` — a REAL second domain)} · issue-reconciliation-2026-08-15.md · landing-rehaul/{README.md,landing.html,blog.html,faq.html,how-it-works.html,pricing.html,_page.css} · superpowers/appstore-3.1.1-reply-draft.md · superpowers/plans/{2026-08-17-chat-surface-craft-pass.md, 2026-08-20-browser-session-persistence.md, 2026-08-20-cdp-screencast-liveview.md, 2026-08-26-admin-growth-dashboard-plan.md (17, incl. the "Isol8" company-name test fixtures at :94,102,586,660), renders/*.html} · superpowers/specs/2026-07-26-personal-to-org-conversion-design.md | DELETE first | P1
.claude/** (GITIGNORED)   | skills/dev-clean-slate-reset/SKILL.md (40, incl. :26,29,30,32,199,200 bootstrap emails) · skills/prod-org-reprovision/SKILL.md (23, incl. :113,119) · settings.local.json (13, incl. :6 the test prefix, :76 a 2nd GooseTown ref, 10 abs paths) · launch.json:5,18 (goosetown-landing, isol8-landing) · RESUME.md (0) | decide the mechanism | P1
.agents/** (UNTRACKED, NOT ignored) | a DRIFTED copy of the 2 ops skills (63 hits) + tauri/SKILL.md | DELETE or make canonical | P1
.playwright-cli/ (UNTRACKED, NOT ignored) | 217 files, 191 with isol8, incl. live dev credentials | DELETE/ignore | P1
routine-detail.md (UNTRACKED, 1246 lines) | 21 hits incl. a LIVE prod trigger URL at :167          | DELETE         | P1
root PNGs ×4 · graphify-out/{graph.json 44M, GRAPH_REPORT.md 337K, cache/ 52M, 2026-08-26/ a SECOND full snapshot} | pre-rebrand renders; the graph (root = "." so a dir rename is safe) | DELETE / rebuild ONCE after P3 | P3/P4/P6
memory/ (428 files, 275 with isol8, 1136 line hits) | 72 carry a *.isol8.co HOSTNAME (the real P2/P3 work; 15 are dev-only) · 175 carry only P6 internal ids (NO edit while P6 defaults to KEEP) · ~15 CURRENT-fact files must be rewritten (the rebrand memory FIRST, in P0) · the remaining ~255 are dated incident/shipped-work records — **LEAVE; rewriting them falsifies the record** | ~72 files | P0/P2/P3
.worktrees/ | openclaw-{dev,pin,prod}-isol8-8, paperclip-prod-813-isol8-5 (mirror fork branch names) | die with the worktrees | P6
.codex/config.toml · no root README exists | ZERO isol8 hits / absent                          | —              | —
```

### 6.9 Live AWS + Vercel (locator = `<service>:<identity>`)

Beyond the resource families already in §5. Full teardown command list with delete ordering
is in the live-state sweep's appendix — reuse it rather than re-deriving.

```
r53:Z09248243AOUC775CDUI4     | 43 records (35 isol8 + 8 goosetown). Apex A → 216.150.1.1; admin/dev/admin.dev A → 76.76.21.21; www CNAME → cname.vercel-dns.com; MX → Google; apex TXT (SPF + 2 GSV); _dmarc; Aikido CNAME; 3 SES DKIM; resend._domainkey TXT (leftover); 5 Clerk records; *.isol8.co + *.dev.isol8.co MX+TXT; 6 API-GW/CloudFront A-aliases; send.isol8.co MX+TXT (ORPHANED — the identity has no MailFromDomain); 3 UNLISTED (securityagent TXT + 2 ACM validation CNAMEs) | P2/P3
r53-hc:00079663…,582d4498…,d9f0dfcb…,48571347… | 4 health checks on api-dev/dev/api/apex, each feeding isol8-{env}-P13/P14 | P1/P3
acm | b9ef44ac + 49501398 (*.isol8.co, in use, exp Mar 2027); 24b2c113 (assets.town, NOT in use, exp 2026-09-25); 4579e4bc + ecbbc78b (goosetown) | P0/P3
apigw | 4 domain names (api, api-dev, ws, ws-dev) + 4 API ids + the 2 X-Isol8-Public-Host integrations | P2/P3/P6
cloudfront | EG8LT2TP7FM0 (bv) + E1VXN9XVCADEQU (bv-dev), each with a brand comment; 2 VPC origins | P2/P3
elbv2 | isol8--Alb16-dqix41FUVIlg, -9odFi9xdWAcF (internal, hold the 2 certs) | P6
ses | identity isol8.co (3 DKIM, no MailFromDomain); 2 config sets; 3 tenants × 2 associations each; 1 ACTIVE receipt rule set + 1 catch-all rule (recipients ".isol8.co,isol8.co"); ProductionAccess **already enabled account-wide — no new request needed** | P2/P3
secretsmanager | 254 isol8-named (75 structural + 179 per-agent) + 14 goosetown + 2 EventBridge-owned; **2 are DASH-form** and missed by a `isol8/` sweep | P6
ssm | /isol8/{dev,prod}/bedrock-gate-url + /isol8/prod/pagerduty-page-url | P6
ddb | 25 dev + 28 prod + **isol8-terraform-locks** (pre-CDK leftover, no consumer) + scratch-verify-gsi-include-keyattr (debris) + 28 goosetown | P3 (the 2 orphans) / P6
s3 | 13 isol8 buckets incl. **isol8-prod-cfn-templates (PUBLIC-READ, customer-facing URL)**, isol8-prod-agent-catalog (early marketplace, no policy — audit), **isol8-terraform-state-877352799272** (pre-CDK, DELETE) + 4 goosetown | P3/P6
ecr | isol8/openclaw-extended, isol8/paperclip-extended, **isol8/hermes-extended (ZERO images — DELETE)**, goosetown-dev-backend | P3/P6
ecs/efs/rds/sfn | 2 clusters / 33 services / 12 taskdef families; **3 EFS filesystems — fs-0d6e8fc1351dce5c8 is an ORPHAN (0 mount targets, 24 KB)**; 2 Aurora clusters; 2 state machines | P3 (the orphan) / P6
iam | 110 isol8 roles (65 CDK + 45 runtime), 15 customer-managed policies, 1 shared OIDC provider, **NO account alias**; iam:role/isol8-connector exists in OUR account too (trust: account root + sts:ExternalId, PowerUserAccess); **goosetown-github-actions has AdministratorAccess** | P3/P6
logs | 67 isol8 groups + 27 goosetown + an orphan double-prefixed lambda group + /aws/securityagent/isol8-api-dev-pentest (8 MB, never expires) | P3/P6
cloudwatch | 226 isol8 alarms, 2 dashboards, 4 custom namespaces (+ GooseTown) | P6
events | 14 isol8 rules (incl. isol8-{env}-access-analyzer-findings, ENABLED but `accessanalyzer list-analyzers` is EMPTY — fires for nothing), 2 api-destinations (api/api-dev ses-events), 2 connections (X-Isol8-SES-Secret) | P2/P3/P6
sns/budgets | 4 topics, 5 subs (4 email → admin@isol8.co, 1 PagerDuty HTTPS); 2 budgets × 2 subscribers | P0/P3/P6
bedrock | 38 application inference profiles; account-global invocation logging → /isol8/bedrock-invocation | P6
cloudformation | 24 isol8 stacks + **isol8-connector** (ours) + 26 goosetown | P3/P6
org/sso | account 877352799272 name "Isol8", MasterAccountEmail admin@isol8.co; SSO instance in **us-east-2**; identity store user Aden = aden@isol8.co | P0/P6
vercel | team slug "isol8" (drives 26 *-isol8.vercel.app aliases); 1 team domain; 3 projects (frontend, goosetown, iso-app — the last has NO isol8 hostname, pure dead weight); frontend has 4 project domains + 2 pipeline-set aliases; 17 env vars (1 typo, NEXT_PUBLIC_APP_URL absent) | P1/P2/P3/P6
```

### 6.10 Tests — summarized per file

**Backend** (164 files / 954 isol8 lines; 65 files / 359 lines carry `isol8.co`). Behavior-
changing (need new cases, not find-replace): `test_main.py` 22 (host dispatch + the header) ·
`test_paperclip_adapter_config.py` 28 (accept/reject incl. lookalikes `ws.isol8.com.evil`,
`ws-dev.isol8.co.evil`) · `test_marketplace_purchases.py` 12 (allowlist + lookalikes) ·
`test_paperclip_proxy.py` 48 (`Domain=.isol8.co`) · `test_agent_email.py` 28 ·
`test_ses_email.py` 22 (**both** mail targets in one file) · `test_agent_mail_reply.py` 18 ·
`test_friday_identity.py` 27 (`:1233` pins `"skill isol8"`; `:1256` proves the migration is
idempotent by finding the OLD string) · `test_atlas_seed.py` 21 ·
`test_ses_inbound.py` 21 · `test_owner_facts.py` 13 · `test_agent_mail_send.py` 13 ·
`test_paperclip_session_cookie.py` 8 · `test_auth_platform_admin.py` 6 (**[V]** 4) ·
`test_openclaw_version.py` 20 (incl. `test_isol8_patch_level`) · `test_config_patcher.py` +
the ecs_manager/config suites ~40 (guard/protocol/lock env) · `ops/test_alarms.py` 17 ·
`ops/test_deploys.py` 7 · `test_platform_info.py` 5 · `test_paperclip_provisioning_moto.py` 5
· `test_seller_display.py` 3 · `test_run_analytics.py` 3 (references the SYMBOL, so a seed
change passes silently — add a literal assertion) · `test_websocket_chat_internal_auth.py` 6
· `chaos/test_schema_staleness.py` 2 + `_schema/PINNED_TAG` 1 · `chaos/fis/conftest.py:71`
(the prod fence). Mechanical fixtures: ~90 further files at 1-22 hits.
Pure-KEEP (zero flip-time work under the P6 defaults): 33 files of ARNs, table/secret/SSM
names, image tags, FIS names.

**Infra CDK** (22 files / 407 lines). Domain-bound: `ses-inbound-stack.test.ts` 43 (~28
domain-bound, incl. `:21-23` the context stub, `:71` the exact recipients array, `:167` the
DMARC rua, `:188` the identity) · `api-stack.test.ts` 10 (6 domain-bound) ·
`observability-stack.test.ts` 27 (3: `:290` the SNS endpoint, `:301`/`:586` the health-check
FQDNs) · `service-stack.test.ts` 43 (3: `:698,699,723`) · `paperclip-stack.test.ts` 25 (2) ·
`agent-mail-outbound-stack.test.ts` 11 (3) · `marketplace-resources.test.ts` 30 (1) ·
`ascii-descriptions.test.ts` 6 (the 2nd context stub + the stage construction).
Name-bound / KEEP: `container-stack.test.ts` 45 (**incl. 10 assertions on the absolute boot
paths at `:75,77,109,315,555,557,559,560,611,613`**) · `bedrock-meter-stack.test.ts` 31 ·
`integrations-resources.test.ts` 30 · `credential-export-decouple.test.ts` 25 ·
`apikeys-export-decouple.test.ts` 23 · `database-stack.test.ts` 20 ·
`bedrock-gate-stack.test.ts` 18 · `openclaw-config-lock-recovery.test.ts` 5 (the cross-file
guard reading `efs_file_lock.py`) · `credits-export-decouple.test.ts` 4 · `helpers.ts` 4 ·
`deploy-prod-promotion.test.ts` 3 (job names) · `paperclip-skill-primitives.test.ts` 2
(**one URL-ENCODED**) · `auth-stack.test.ts` 1 · `scratch-tmpdir.ts` 1.
Clean: `network-stack.test.ts`, `deploy-workflow-cdkout-transport.test.ts`.

**Lambda** (3 files / 50 lines, absent from the infra sweep's table):
`ses-inbound/test_index.py` 27 (incl. `:197` the header — a header-rename co-edit — and the
`_route_for` dev/prod suffix suite at `:634-678`) · `bedrock-usage-meter/test_index.py` 20
(the dev-vs-prod gate discrimination at `:336-347,377-378` — the guard on double-billing) ·
`ws-connect/test_index.py` 3 (`:138,149,159` — the other header co-edit).

**Frontend** (54 files / 260 lines). Load-bearing: `desktop/update/…/route.test.ts` 30 (P1,
the host allowlist) · `middleware.test.ts` 13 (admin-host allowlist) ·
`HireCard.test.tsx` 11 (the wire discriminator) · `BillingPanel.billing.test.tsx` 9 ·
`slackManifest.test.ts` 2 (`:70` `toContain("name: Isol8 Agent")` — fails the moment L174
lands) · `layoutStorage.test.ts` 3 (the v1→v2 migration) · plus the ~9 copy-asserting files
in C10(b). Fixtures only: `download/desktop/route.test.ts` 26 ·
`AutomationGoogleAccount.test.tsx` 24 · `RoutinesPanel.test.tsx` 23 ·
`GoogleAccountsSection.test.tsx` 9 · `AgentEmailSection.test.tsx` 7 ·
`RoutineDetailPanel.edit.test.tsx` 7 · `ProfilePanel.test.tsx` 7 ·
`ImportBackupDialog.test.tsx` 6 · `useOnboardingGate.test.tsx` 5 (the cookie) ·
`BrowserDockPanel.test.ts` 5 · `AwsConnectCard.test.tsx` 2 (an IAM role fixture — P6 KEEP
family, **not** the P3 brand sweep) · `content/__tests__/markdown.test.ts` 2 · ~24 more at
1-3. Needs nothing ever: `url.test.ts` 15, `api.test.ts` 12, `middleware.test.ts`'s own
ADMIN_HOSTS set.

**Mobile** (29 files / 115 lines): `deepLink.test.ts` 16 (the security test — negative
`isol8.co.evil.com` at `:50`) · `PaywallScreen.test.tsx` 16 (product id ×6 + copy) ·
`webLinks.test.ts` 11 · `ProvisionGate.test.tsx` 10 (5 NEGATIVE regexes) ·
`settings.test.tsx` 8 · `AiConsentGate.test.tsx` 2 · 23 more at 1-4 (18 of them
`jest.mock("@isol8/shared")` only). `fingerprint.config.test.ts`: zero.

**Desktop Rust** (4 files / 9 lines, absent from the plan): `keychain_test.rs:1,25` ·
`pair_client_test.rs:1,12,29` · `proxy_test.rs:18` · `subprocess_test.rs:8,9,91`.

**Shared** (2 / 19) · **Plugins** (3 / 7: `isol8-marketplace/src/index.test.ts` 5 — the
env-name contract; `aws-exec-profile/index.test.mjs:29` and
`paperclip-exec-creds/index.test.mjs:18` — both KEEP) · **agent-fork** (4 / 23, of which
`workspace-native-seed.test.ts` 19 must move in lockstep with the seed constants) ·
**paperclip-fork** (5 / 17).

**Tests to ADD** (each guards a hole found above):
1. `apps/backend/tests/unit/test_no_hardcoded_domains.py` — promote the P1 verify grep from a
   manual check to a CI gate, with the five-entry do-not-rename allowlist.
2. `apps/infra/test/domain-agnostic.test.ts` — synth the dev stage with
   `rootDomain: "example.test"` and assert **zero** `isol8.co` in `Template.toJSON()` for
   every stack. Fails today; passes at the end of P1. **The single highest-value new test —
   it turns "make the repo domain-agnostic" from a review judgement into a build gate.**
3. `test_company_identity.py` — assert `lightbulb`/`lightbulbai`/`flickmeon` are reserved
   **and** that `isol8` still is (nothing enforces the latter today).
4. `test_run_analytics.py` — pin the literal uuid5 seed with a comment saying a change
   re-ingests history.
5. A frozen-identifier test for the nine never-rename entries (§5).
6. `ses-inbound-stack.test.ts` — a during-cutover case (4-element recipients) and a
   post-cutover case (the legacy pair is gone).
7. Desktop route tests — one case with a space-bearing bundle name
   (`"Lightbulb_0.4.1_aarch64.app.tar.gz"`) proving the suffix match and URL construction
   still hold.
8. `deepLink.test.ts` — re-anchored lookalikes (`https://flickmeon.ai.evil.com/…` → null).
9. `test_support_ticket.py` — one assertion that `_admin_user_url("user_x")` contains the
   configured admin host (currently **zero** coverage).

**Fixture-constant strategy (do NOT convert everything).** Infra is the only suite where a
shared constant is *mandatory* — export `ROOT_DOMAIN`, `HOSTED_ZONE_CONTEXT`, `MAIL_DOMAIN`,
`PLATFORM_MAIL_DOMAIN` from the existing `apps/infra/test/helpers.ts` (which already
centralizes the secret-name pattern at `:39-41`) and import them in the 8 domain-bound
files. Backend: add `apps/backend/tests/_domains.py` and convert only the ~25 behavior-
changing files. Frontend/mobile: one `testDomains.ts` per app for the ~12 behavior-changing
files. **Leave everything else as literals** — they are the find-replace safety net the plan
wants, and they work as one once §3 B20's three-family order is respected.

---

## 7. Counts

### Repo-wide (tracked files only — the plan's Appendix B figures, all verified exact)

| | files |
|---|---:|
| contain `isol8.co` | **258** |
| contain `Isol8` | **290** |
| contain `isol8` (any case) | **792** |

**Untracked mass the above excludes:** `.playwright-cli/` **217 files (191 with isol8)** ·
memory dir **428 files (275 with isol8, 1,136 line-hits, ~1.5 GB, + 137 `.jsonl`)** ·
`AGENTS.md` (41) · `routine-detail.md` (21) · `.claude/**` (~78, gitignored) ·
`.agents/**` (63) · 7 `docs/` files · `apps/mobile/ios/**` · 3 stale `.ipa` files ·
4 root PNGs · `graphify-out/` (2 full snapshots, ~140 MB).

### By area

| Area | Files | isol8-bearing lines | Notes |
|---|---:|---:|---|
| `.github/workflows/**` (hand-authored) | 12 of 14 | **68 items** | 15 hostname literals; **2 base64-hidden** (`mobile-ota.yml:64`, `eas.json:37`); clean: `claude.yml`, `claude-code-review.yml` |
| `.github/workflows/deploy.yml` (GENERATED) | 1 | **363 occurrences** | 64 role ARNs + 12 Vercel host lines + 1 concurrency group + **286** stack-derived job names / CFN logical ids. 10 hand-editable generator sites in `app.ts` |
| CI generator / identity files | 10 | 28 items | `app.ts`, `container-stack.ts`, `cdk.context.json`, `switch-ci-runner.sh`, `githubReleases.ts`, `SIGNING.md`, mobile `README.md`, 2 infra tests, `deploy_dev_fast.sh` |
| `apps/infra/lib/**` | 16 of 16 | **513** | ~200 change items |
| `apps/infra/cdk.context.json` | 1 | 2 | + 2 duplicate copies in tests |
| `apps/infra/lambda/**` | 16 of 16 (7 dirs) | **110** | of which 3 test files / 50 lines |
| `apps/infra/test/**` | 22 of 24 | **407** | ~47 domain-bound assertions; the rest P6 name-bound |
| `apps/infra/openclaw/**` | 56 files w/ hits | **316** (305 non-test) | + 2 dir renames, 5 file renames; `sage/` verified clean |
| repo-root pins + schema | 3 | ~13 | `openclaw-version.json`, `openclaw-version.schema.json`, `paperclip-version.json` |
| `apps/backend/{core,routers,schemas,scripts}` + `main.py` | **154** | **662** | 79 `isol8.co` lines, of which **17 are code literals**; ~171 items; **48 files need an edit** |
| `apps/backend/tests/**` | **164** | **954** | 65 files / 359 lines carry `isol8.co` |
| `apps/frontend/**` + `packages/shared/**` | **210** | **728 occurrences** (647 matching lines) | 153 non-test + 57 test; `isol8.co` host literals **84 lines / 40 files**; `@isol8/*` **50 occurrences / 28 files**; ~196 substantive items after excluding scope refs and provenance comments |
| `apps/mobile/**` | 82 | **276** | 89 `@isol8/*` import lines (37 import-only files) + 187 brand/domain/id items (119 non-test) |
| `apps/desktop/**` | 23 | **104** | 94 non-test (~9 inline `#[cfg(test)]` fixtures) + 9 in `tests/` |
| `~/dev/agent-fork` @ `v2026.8.1-isol8.1` | 12 | **48** | 4 test files / 23 items; + 10 local/17 remote branches, 17 tags |
| `~/dev/paperclip-fork` | 17 | **67** | 5 test files / 17 items; + 17 local/18 remote branches, 27 tags |
| docs / context / scratch | 50 of 60 examined | **694** | 37 tracked, 15 untracked; 18 living docs, ~24 orphans, 4 runbooks |
| memory (outside the repo) | 275 of 428 | **1,136** | **only ~72 need editing** (the hostname-bearing ones); 175 are pure P6 ids = no edit under the KEEP defaults |

### Tests, whole-program

| | count |
|---|---:|
| Files carrying an isol8 literal | **281** |
| isol8-bearing lines | **1,821** (backend 954 · infra CDK 407 · frontend 260 · mobile 115 · lambda 50 · shared 19 · desktop 9 · plugins 7) |
| — of which `isol8.co` **hostnames** | 599 (the only number the plan budgeted) |
| — of which **mail addresses**, split across TWO target domains | 245 (106 platform/human + 139 agent) |
| — of which ids / env vars / EFS markers / image tags / AWS resource names | ~977 |
| Test cases living in affected files | ~3,447 (2,257 backend `def test_*` · 1,081 JS/TS · 97 lambda · 12 desktop Rust) |
| Files needing **new cases**, not find-replace | **~33** |
| Files that are purely mechanical | ~190 |
| Files that need **nothing, ever** (domain-agnostic fixtures) | 3 |
| Files that are pure-KEEP resource names (zero flip-time work) | ~43 |
| New tests to add | **9** |

### Live AWS (read-only enumeration, us-east-1 unless noted)

isol8-named: **43** Route53 records · 3 zones · 4 health checks · 3 ACM certs · 4 API-GW
domains + 4 APIs · 2 CloudFront + 2 VPC origins · 2 ALBs · 1 SES identity + 2 config sets +
3 tenants (6 associations) + 1 active rule set · **254** secrets · 3 SSM params · **54** DDB
tables · 3 ECR repos (1 empty) · 13 S3 buckets · **67** log groups · **226** alarms + 2
dashboards + **4** custom namespaces · **110** IAM roles (65 CDK + **45 runtime**) + 15
policies · 2 KMS aliases · 2 Cloud Map namespaces + 4 services · 2 ECS clusters + 33
services + 12 taskdef families · **3** EFS filesystems (1 orphan) · 2 Aurora clusters · 2
state machines · **40** Lambdas · 14 EventBridge rules + 2 api-destinations + 2 connections ·
4 SNS topics + 5 subs · 2 Budgets · **38** Bedrock AIPs · **24** CFN stacks + `isol8-connector`.

GooseTown (all deletable): 26 CFN stacks · 28 DDB tables (with ~280k rows) · 14 secrets ·
**4 WAFv2 web ACLs** (the only WAFs in the account) · 4 S3 buckets (~83k objects, ~553 MB) ·
31 IAM roles (one with `AdministratorAccess`) · 27 log groups · 14 Lambdas · 2 ECS clusters
+ 8 services · 2 Cloud Map namespaces · 2 event buses + 2 **ENABLED** 15-minute poll rules ·
2 SNS topics · 1 ECR repo · 26 alarms · 2 CloudFront distributions + 2 response-headers
policies · 8 Route53 records · 4 Vercel aliases.

Vercel team `isol8`: 1 team · 3 projects · 1 team-level domain · 8 project-domain entries ·
**31 aliases (26 carry `isol8`)** · 17 env vars on `frontend` (1 typo; `NEXT_PUBLIC_APP_URL`
absent).

### Findings

| | count |
|---|---:|
| Plan corrections (§1) | **57** |
| — of which refuted single-sweep claims | 20 |
| New required work items (§2), excluding the §6 long tail | ~120 |
| Blockers / ordering traps (§3) | **39** (B0-B38) |
| Data-at-rest stores requiring a backfill or an explicit "no backfill" note (§4) | 24 |
| — flag-day | 13 · **after/self-healing** 8 · **owner-action-only** 3 |
| Internal-identifier families cost-scoped (§5) | 24 |
| — recommended **never** | 4 (CDK stack names, Cloud Map, `isol8-connector`, the 9-entry frozen set) |
| — recommended **free / anytime** | 3 (KMS alias, log groups except the Bedrock sink, `backend.yml` test prefix) |

### The three things most likely to be missed

1. **`admin@isol8.co` is the AWS root-account recovery email** — and the plan's only mention
   of it is a P3 teardown line (C47/B0).
2. **The P1 verify grep actively pushes an implementer into disarming the account-wipe
   guard** (C9/B6).
3. **The P3 brand swap reaches zero containers** without a pin flip and a *reprovision* —
   and "re-roll" in the plan means the wrong operation (B11/C23).
