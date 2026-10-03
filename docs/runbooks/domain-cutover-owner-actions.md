# Domain cutover — per-owner actions

`isol8.co` → `thelightbulb.company`. Built from a read-only sweep of live prod
on **2026-08-27**: `isol8-prod-{billing-accounts,agent-email-addresses,integrations,oauth-tokens,channel-links,company-slugs,containers,users}`,
the prod Paperclip Aurora (`routine_triggers` ⋈ `companies`), the prod backend
task definition, and SES (`list-email-identities`, `describe-active-receipt-rule-set`,
`list-tenants`). Owner ids and slugs are real. Re-measure before acting — the
fleet is 22 owners and moves.

## What the live data says (and where it contradicts the assumptions)

| Assumption going in | What prod actually holds |
|---|---|
| Owners have connected integrations to re-auth | 16 integration rows, **12 of them one owner** (`org_3H3vi…`, the house org). The other 4 owners have a single `github` row each. |
| `channel-links` needs cross-referencing | `isol8-prod-channel-links` is **empty**. Nothing to migrate. |
| Inbound trigger URLs need re-arming across the fleet | **6 webhook triggers exist in the whole fleet and all 6 belong to the house org.** The only customer with any trigger at all is Lull Brands, and it is a `schedule` trigger (no fire URL, unaffected). |
| `verified_senders` will strand owners after the human email move | **One row.** `friday@isol8-2.isol8.co` holds `admin@isol8.co`. Every other owner's verified sender is external (`ben@enjoylull.com`, `…@privaterelay.appleid.com`, gmail, outlook) and is untouched by our domain move. |
| Agent mail is broadly in use | 18 addresses exist, but SES has **3 tenants total** — a tenant is created on an owner's *first send*, so only 3 owners have ever sent agent mail, and 2 of those 3 (`user_3Hr0nEW4…`, `user_3Hwnrt08…`) are not in the current billing set at all. **Exactly one live owner has ever sent: Lull Brands.** |
| The 4 paid owners have Bedrock spend exposure | All four are `provider_choice = chatgpt_oauth`. No paid Bedrock owner exists, so no credit ledger is in the blast radius. |

Net: the cutover is far less owner-facing than it looks. The concentrated risk
is **our own org**, and the fleet-wide risk is a single universal item — the
sign-in host — plus desktop.

## The two universal breakages

**1. Every session drops.** Clerk session cookies are host-scoped. Every owner
re-signs-in at the new host on their first visit. Self-healing; no action beyond
knowing the new URL.

**2. Installed desktop apps die silently.** `desktop-build.yml` bakes five
absolute prod URLs into the binary at build time (lines 259, 260, 362, 367, 368):
`https://isol8.co/chat`, the updater endpoint `https://isol8.co/api/desktop/update/…`,
`https://isol8.co/auth/desktop-callback`, `https://api.isol8.co`, `wss://ws.isol8.co`.
Because the **updater endpoint is itself on the dying domain**, an installed app
cannot pull the fix — it presents as a blank window, never an update prompt.
**Reinstall is the only path.** Prod desktop installs are not tracked; the only
signal in the data is `containers.paired_devices`, and exactly one owner has any
(`org_3H3vi…`, 4 devices). Assume the true count is higher and put the reinstall
line in the notice to **everyone**.

## S1 — paid, active container, revenue at risk (4 owners)

| Owner id | Slug | What breaks | They must | We do | Sev |
|---|---|---|---|---|---|
| `org_3H3viGAfq2SXc9dIdcatmGXg8Td` | `isol8-2` | Everything below, at once: **6 webhook triggers** (2 GitHub `github_hmac`, 2 Stripe `hmac_sha256` incl. `we_1U6OwYI54BysGS3rTwq0MzRS` → `…/hooks/t/org_3H3vi…/ab0ddd6d91aa50c27b2285ae`, PostHog `bearer`, Google Calendar `bearer`); **12 integrations** (`google`, `aws`, `github`, `apollo`, `instagram`, `meta_ads`, `posthog`, `x`, + 3 `google#account#…` rows whose address is in the **sort key** — `aden@`, `admin@`, `support@`, all `@isol8.co`); 4 paired desktop devices; `friday@isol8-2.isol8.co` whose verified sender `admin@isol8.co` is the one stale entry in the fleet | Re-arm all 6 triggers at the provider (Stripe, GitHub ×2, PostHog, Google) — **not edit the URL**; reinstall 4 desktop apps; re-add `verified_senders` | Re-key the mail row; re-key the 3 `google#account#…` rows (sort-key change = delete+put, same shape as the mail script); run the flip | **S1** |
| `user_3IEUfhBJrhDbABVjjcKo4ECy4wk` | `lull-brands` | **The only customer whose agents actually send mail** (sole live SES tenant). `friday@lull-brands.isol8.co` → `.thelightbulb.company`, so correspondents see a new From. Container running; 1 schedule trigger (unaffected) | Nothing required — verified sender `ben@enjoylull.com` is external and survives. Re-sign-in; reinstall desktop if installed | Re-key the mail row; keep the `isol8.co` receipt rule so replies to the old address still land | **S1** |
| `user_3IQ0sBQskW3v7XNiFSd1PIzZSQv` | `admin-2` | Sign-in host; `friday@admin-2.isol8.co` From changes; 1 `github` integration | Re-sign-in; reinstall desktop if installed | Re-key the mail row | **S1** |
| `user_3I77n75HrSqC8v3MBOkVBiJIhLc` | `mytrainer` | Sign-in host; `friday@mytrainer.isol8.co` From changes; 1 `github` integration | Re-sign-in; reinstall desktop if installed | Re-key the mail row | **S1** |

Stored OAuth tokens are **not** in this list on purpose: `oauth-tokens` rows bind
to the provider account (`account_id` / `bound_owner_id`), not to our hostname,
so all four paid owners' ChatGPT sessions survive the flip untouched. What has to
move is the **redirect URI on our side of the provider app registrations** —
our work, not theirs, and it must be added *before* the flip so a re-auth
attempted during the window does not dead-end.

## S2 — trial, has a company slug and an agent address (14 owners)

Containers are all `stopped` (trial scale-to-zero). Identical breakage for each:
sign-in host, `friday@{slug}.isol8.co` → `.thelightbulb.company`, desktop
reinstall if installed. No integrations, no triggers, no stale verified sender —
every one of these owners' verified sender is an external mailbox.

**They must:** nothing. Sign in at the new host when they next return.
**We do:** re-key the mail row; keep the old address routing as an alias.

| Owner id | Slug | | Owner id | Slug |
|---|---|---|---|---|
| `user_3H8Lm4IT7oxg0Oa4Hg3InN60gWz` | `gupta-labs` | | `user_3I240x9RmfH8NWgWUiB9XrglHNK` | `meridian-labs` |
| `user_3HCL0JFNi0B2zD79yv75TFqKR4F` | `kinn` | | `user_3I77i7TS65lhis55lki2w9DjPYr` | `velvet-ante` |
| `user_3HT2lhfkJHa31WnYX7se4tODBrD` | `amargosa-gold` | | `user_3I7FwBIyYNTo3B4hPyXOYHiyHsb` | `bench-club` |
| `user_3HhHeMNI7EVWEK7sY2XMXSISiMl` | `pragma-consulting` | | `user_3I8uBYyT1Kb1Z3H5b8iaDxN6KQV` | `tapdrop` |
| `user_3HmnXQdFUk2pP2Zjw4HEGkefcNM` | `goosetown` | | `user_3I8ucWr6yd4KRaz4GZ7V3AMYDy8` | `fairway-finds` |
| `user_3HnGOFhR3rsgTrHAZeDFierE0gb` | `clearcall` | | `user_3I8xLTB42gvTEHOyMkRCyqdjPkg` | `small-batch-social` |
| `user_3HouICXKf8unAxJWOvDBJD8Zgkt` | `flint` | | `user_3ICfRJPsJs1tDJUG6wZjuKzp3aG` | `jkw-llc` |

Severity **S2** across the board.

## S3 — trial, no slug, no agent address (4 owners)

Nothing to re-key. Sign-in host only.

| Owner id | Note | Sev |
|---|---|---|
| `user_3ISoDVN96JaMYqFRlemCAXQPdWK` | has a `github` integration but never got a slug or agent mail | S3 |
| `user_3ITEV5cLcpziEqY3cinqawhklJt` | ChatGPT device-auth row stuck `pending` — will need to restart the flow at the new host either way | S3 |
| `user_3HF4UAKWNQiWabmP3CTkaf7QflF` | — | S3 |
| `user_3HUZ3ZmIh7GLJHdDM9flmDVdDHT` | — | S3 |

**Bands: S1 = 4, S2 = 14, S3 = 4.**

## Why triggers are re-armed, never edited

`routers/paperclip_trigger_proxy.py:422` uses `trigger_fire_url(owner_id, public_id)`
as the **OIDC audience** for Google-signed deliveries, and that URL is
`{PUBLIC_API_BASE}/api/v1/hooks/t/{owner_id}/{public_id}` — today
`https://api.isol8.co/…`. Pasting the new URL into the provider is not enough:
Google mints its token against the URL registered at *watch* time, so a
Calendar/Gmail push aimed at the new host carries an audience the proxy rejects
with `invalid_oidc_token`. The `bearer` and `hmac_sha256`/`github_hmac` modes
do not sign the URL, but the provider-side registration still has to be
re-pointed, and rotating while you are there is the only way to know the
provider actually took it. Treat all 6 as re-arms.

Of the 6, three have never fired (`last_fired_at` NULL): both Stripe hooks
(`ab0ddd6d…`, `b9b40b9a…`) and the PostHog one (`666a2f96…`). Re-arm them
anyway — a hook that has never fired is not a hook that never will.

## Platform-side prerequisites (ours, before any owner notice)

1. **`thelightbulb.company` is not a verified SES identity.** `list-email-identities`
   returns exactly one: `isol8.co`. Verify the domain, publish wildcard MX + SPF
   + DKIM for `*.thelightbulb.company`, and add it to the receipt rule set
   *before* re-keying anything, or the new addresses are unroutable in both
   directions.
2. **Keep the `isol8.co` receipt rule.** The active rule set has one rule with
   `Recipients: ['.isol8.co', 'isol8.co']`. `routers/ses_inbound.py:199` resolves
   recipients with `get_by_address` and applies **no status filter**, which is
   what makes the alias rows keep working — but only while SES still accepts
   mail for the domain. Deleting that rule silently defeats the whole alias
   design. Add a second rule; do not repoint this one.
3. **`apps/infra/lib/domains.ts` is the lever.** `ROOT_DOMAIN` and
   `PLATFORM_MAIL_DOMAIN` are already separate parameters threaded through
   `Isol8StageProps`, so mail can move independently of the product domain.
   Prod backend today: `AGENT_EMAIL_DOMAIN=isol8.co`, `PUBLIC_API_BASE=https://api.isol8.co`,
   `FRONTEND_URL=https://isol8.co`, `CORS_ORIGINS=https://isol8.co,https://www.isol8.co`,
   `PAPERCLIP_PUBLIC_URL=https://company.isol8.co`.
4. **SES tenants re-associate themselves, and never let go.**
   `agent_mail_tenant._ensured` is a per-process memo, so after the backend
   restarts on the new `AGENT_EMAIL_DOMAIN` each owner's tenant picks up
   `identity/thelightbulb.company` on that owner's next send. But
   `create_tenant_resource_association` only ADDS: the `identity/isol8.co`
   association stays on all 3 tenants forever. **`isol8.co` can never be
   deleted as an SES identity until each tenant is dissociated**
   (`DeleteTenantResourceAssociation`, one call per tenant). Fine for now — the
   identity has to stay verified anyway while alias rows route.
5. ~~**Run `scripts/rekey_agent_email_domain.py --env prod --dry-run` first.**~~
   **DONE 2026-08-27.** The script ran against both envs (18 prod + 6 dev moved,
   0 collisions), superseded alias rows were deleted, and all three SES tenants
   were re-associated onto `identity/thelightbulb.company` — see
   `apps/infra/lib/domains.ts` (`LEGACY_MAIL_DOMAIN = ""`). Zero addresses remain
   on the old domain, so this step is spent and the script can be deleted.
6. **Add the new redirect URIs to the provider app registrations** (GitHub,
   Google, OpenAI/ChatGPT) alongside the old ones, before the flip.
7. **Cut a new desktop release from the new domain** and have the download live
   at the new host before the notice goes out — the notice tells people to
   reinstall, so there has to be something to install.

## Two things that surprise people mid-cutover

- **A reply to an old address goes out from the new one.**
  `agent_mail_send.py:113-121` deliberately resolves the agent's *current active*
  address for replies, with a comment naming alias rotation. So a correspondent
  who writes to `friday@acme.isol8.co` gets a reply from
  `friday@acme.thelightbulb.company`. That is correct and intended; it just
  looks wrong if nobody said so first.
- **`verified_senders` gates the wake, and the owner owns the fix.** The one
  affected row is the house org's. Self-serve:
  `PATCH /api/v1/agent-mail/address` with
  `{"verified_senders": ["@thelightbulb.company"]}` — `sender_allowed` accepts a
  bare `@domain` entry and matches the sender's domain exactly (no subdomain
  matching), and `patch_address` resolves the **active** address, so after the
  re-key the PATCH lands on the new row rather than the alias.

## What to send

**Timing and sender.** The notice goes out **before** the flip, from an address
that outlives it — `hello@` / `support@` on the *new* domain, or a personal
mailbox. A notice sent from `@isol8.co` after the cutover is the one message
guaranteed not to be trusted, and a reply to it may not reach anyone.

**Paid owners (4).** *We are moving Isol8 to a new home: thelightbulb.company.
On [date] the app moves to `https://thelightbulb.company` — bookmarks and saved
links to isol8.co will stop working, and you will be asked to sign in once at
the new address. Your subscription, your agents, their memory and their work all
move with you; nothing is being recreated. Two things need you: if you use the
desktop app, please **download and install it again** from the new site — the
installed copy points at the old address and will open to a blank window rather
than offering an update. And your agents' email addresses change from
`friday@{yourcompany}.isol8.co` to `friday@{yourcompany}.thelightbulb.company`;
the old address keeps receiving mail indefinitely, so nothing in flight is lost,
but replies will come from the new one — worth a heads-up to anyone who emails
your agents regularly. If you have pointed a webhook (Stripe, GitHub, Google,
PostHog) at an Isol8 URL, we will contact you separately to re-arm it: that one
cannot be fixed by editing the URL.*

**Trial owners (18).** *Isol8 is becoming thelightbulb.company. From [date] the
app lives at `https://thelightbulb.company` — same account, same agents, you
will just sign in once at the new address. If you installed the desktop app,
download it again from the new site; the old install points at an address that
no longer exists and will open blank instead of updating itself. If your agent
has an email address, it moves to `@{yourcompany}.thelightbulb.company` and the
old one keeps receiving mail, so nothing gets lost.*

Do not send the trial paragraph to the 4 owners in S3 with no slug and no agent
address without trimming the email line — they have no agent address, and
telling them one moved invites a support thread about a thing that does not exist.

---

## DCR OAuth clients — self-healing, and the one exception is an orphan

Added 2026-08-27. This store is **Secrets Manager**, not the `integrations` table, so no row
above covers it.

Dynamic Client Registration **pins `redirect_uris` at registration time**, so a domain move
would normally invalidate every DCR-registered client: the provider rejects the new callback and
nothing in the OAuth response distinguishes that from any other failure.

**The code already handles it.** `oauth_dance.start()` compares the stored `redirect_uri` against
the one it is about to use and **re-registers the client** when they differ
(`oauth_dance.py:181-186`). DCR integrations therefore heal themselves on the next connect.

**No owner action is required for DCR. Verified, not assumed.** The entire prod population is
**one** record:

| owner | provider | stored `redirect_uri` | live account? |
|---|---|---|---|
| `user_3ForZYBrDpmRb7usKnVVTsFGfsd` | `notion` | missing (pre-dates the field) | **No** — zero rows in `containers`, `billing-accounts`, `users`, `company-slugs` |

That record would *not* self-heal, because records written before the `redirect_uri` field read
as UNKNOWN and the code deliberately declines to re-register on a guess
(`_registered_redirect_uris` returns `[]` → treated as no-drift). But it belongs to a **deleted
account**, so it is an orphaned secret rather than an integration anyone will try to use.

Two consequences:

- **Do not send a "reconnect your DCR integrations" message.** It would be wrong for every live
  owner (they self-heal) and pointless for the only non-healing record (nobody owns it).
- The orphan is a cleanup candidate for the account-wipe sweep — an OAuth client record that
  outlived its owner.

**Re-check before the flip**, since new DCR clients can register at any time:

```bash
aws secretsmanager list-secrets --region us-east-1 --max-results 100 \
  --query "SecretList[?contains(Name,'isol8/prod/integrations/') && contains(Name,'oauth-app')].Name" \
  --output text
```

For any record whose JSON lacks a `redirect_uri` key, confirm the owner still exists before
treating it as an owner action.
