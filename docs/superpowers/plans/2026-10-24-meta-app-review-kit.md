# Meta App Review kit (2026-10-24)

Apps: **Lightbulb** `2080424579211211` (Facebook Login for Business, Marketing API, ads MCP, Pages) and **Lightbulb - IG** `1049347507456011` (Instagram API with Instagram Login). Nothing is submitted. Business verification: done (Tech Provider, 2026-07-26). Both apps' state is in the Meta console notes; code facts below are from the repo on 2026-10-24.

## 0. Verdict per permission

| Permission | App | Code use | Verdict |
|---|---|---|---|
| instagram_business_basic | IG | requested, `providers.yaml:2194`; `/me` check `skills/instagram/SKILL.md:26` | KEEP |
| instagram_business_content_publish | IG | `providers.yaml:2195`; `SKILL.md:37-60` (`/media`, `/media_publish`) | KEEP |
| instagram_business_manage_comments | IG | `providers.yaml:2196`; `SKILL.md:64-70`; webhook `comments`/`mentions` `providers.yaml:2234-2239` | KEEP |
| instagram_business_manage_messages | IG | `providers.yaml:2197`; `SKILL.md:76-80`; webhook `messages` | KEEP |
| instagram_business_manage_insights | IG | NOT in code (not in scopes, no skill text) | DROP (do not add) unless we add an insights section to the skill and a scope; recommended drop |
| instagram_basic | Lightbulb | `providers.yaml:2100` (meta-ads scope list). Nothing calls it: the Instagram path uses the IG app | DROP from code and from submission |
| ads_mcp_management | Lightbulb | `providers.yaml:2078-2087`, gates `https://mcp.facebook.com/ads` (`providers.yaml:2102-2105`) | KEEP |
| ads_read | Lightbulb | `providers.yaml:2085`; used through the Meta ads MCP (14.4k calls) | KEEP |
| ads_management | Lightbulb | `providers.yaml:2086`; ad create/edit through the MCP | KEEP |
| business_management | Lightbulb | `providers.yaml:2087`; lists the owner's Business Manager and ad accounts for the MCP | KEEP |
| pages_show_list | Lightbulb | `providers.yaml:2089`; `inbound_triggers._meta_pages` reads `/me/accounts` (`inbound_triggers.py:624-650`) | KEEP |
| pages_manage_metadata | Lightbulb | `providers.yaml:2099`; `POST {page-id}/subscribed_apps` (`inbound_triggers.py:732-750`) | KEEP (Page webhook subscribe) |
| pages_read_engagement | Lightbulb | NOT in scopes. Console lists it pending; no code reads Page posts/engagement | DROP from the submission. Note: Meta often requires it with pages_manage_metadata; if Meta forces it, justify as "needed to subscribe a Page to feed/leadgen webhooks" |
| catalog_management | Lightbulb | `providers.yaml:2088`; no Lightbulb code or skill text uses catalogs, and no demo path | DROP (remove from scopes and from the 14 pending) |
| public_profile | Lightbulb | default, no review | n/a |

Result: submit 5 for Instagram app is wrong; submit **4** there (basic, content_publish, manage_comments, manage_messages) and **8** on Lightbulb (ads_mcp_management, ads_read, ads_management, business_management, pages_show_list, pages_manage_metadata, plus pages_read_engagement only if forced). Remove `instagram_basic` and `catalog_management` from `providers.yaml` scopes in the same PR as the allowlist fix (a scope change needs users to reconnect, and none exist in prod).

Code anchors for the Meta webhook: `apps/backend/routers/meta_webhook.py:194` (`GET /api/v1/hooks/meta` verify), `:213` (`POST` receive, app-secret signature, routes by `entry[].id`). Meta ads events: `leadgen`, `feed`, `messages` (`providers.yaml:2125-2133`); Instagram: `comments`, `mentions`, `messages`.

## 1. Use-case justifications (paste into the form)

**General description (both apps).** Lightbulb is a workspace where a company's AI employees work alongside its people. A workspace admin connects their own Instagram professional account or Meta ad accounts from Settings > Connections. Nothing runs until the admin connects and asks an agent (in chat) to do something; agents act only on that admin's connected assets, and only when asked or when the admin turned on an automation they chose. We store the access token encrypted in AWS (Secrets Manager / the workspace's private storage), never share it between workspaces, and use data only to perform the requested task for that workspace. We do not sell data, build audiences or train models on it. Deleting the connection or the account deletes the token; data deletion instructions are at https://thelightbulb.company/support#data-deletion.

**instagram_business_basic.** After the user connects, Lightbulb reads the account id and username to show which account is connected and to address API calls to it. Without it no other Instagram call can be made.

**instagram_business_content_publish.** The user tells an agent in chat "post this photo/Reel with this caption". The agent drafts the post, shows it to the user, and publishes only after the user confirms the exact media and caption (this rule is in the agent's instructions, `skills/instagram/SKILL.md:30-34`). Used to create a container and call `media_publish` on the user's own account.

**instagram_business_manage_comments.** The user asks an agent to review and answer comments on their posts, or turns on a "New comment / Mentioned" automation. The agent lists comments on the user's media and replies as the user's account. Comment text is processed to draft a reply and shown in the workspace; it is not used for anything else.

**instagram_business_manage_messages.** The agent answers DMs sent to the user's account within Meta's 24-hour window, on request or via the "New DM" automation the user enabled. Message content is passed to the agent to draft a reply and kept in the workspace's activity history.

**ads_mcp_management.** Required to authorize Lightbulb as a client of Meta's ads MCP server (`mcp.facebook.com/ads`). The user connects their Meta ads account; their agents then use Meta's MCP tools to answer questions about, and manage, that user's ads.

**ads_read.** The user asks, e.g., "how did last week's campaigns perform?" The agent reads campaigns, ad sets, ads and insights for the ad accounts the user selected and summarizes them in chat.

**ads_management.** The user asks the agent to pause an underperforming ad set, change a budget, or create a campaign. The agent applies the change to the user's ad account and reports back. The agent must be told to do this by the user; no autonomous spend.

**business_management.** Needed to list the Business Manager and ad accounts the user can access so they can pick which accounts their agents may use, and to resolve assets owned by that business.

**pages_show_list.** Lists the Facebook Pages the user administers so they can pick which Page's activity (leads, posts, messages) should trigger an agent.

**pages_manage_metadata.** Subscribes the user's chosen Page to the Lightbulb app's webhook (`subscribed_apps`: leadgen, feed, messages) so the "New lead / Page post activity / New message" automations fire. Meta requires this permission for that endpoint. We make no other metadata edits to the Page.

**pages_read_engagement** (only if Meta forces it). Required by Meta to read the Page access token and subscribe the Page; the agent then reads the lead or message that triggered the automation.

## 2. Screencast scripts (prod, thelightbulb.company)

Record one video per app, 1080p, English captions on each step (Meta asks for narration or captions). Use the reviewer-style test org (section 4), not the founders' org. Keep the Meta-side windows (instagram.com, Meta Ads Manager / Business Suite) visible beside the Lightbulb window or switch to them for the result.

### Video A: Lightbulb - IG (4 permissions)
1. Open https://thelightbulb.company, click Sign in, sign in with the reviewer test account. Show the workspace.
2. Settings > Connections > Instagram > Connect. The Instagram login dialog shows; sign in with the test Instagram professional account; the consent screen lists the four permissions (pause on it); Allow. Back in Lightbulb, Instagram shows Connected with the @username (instagram_business_basic).
3. In chat, ask Friday: "Post this image to Instagram with the caption: Hello from Lightbulb" (use a public image URL). The agent shows the draft; reply "yes". Switch to instagram.com / the app: the post is visible (instagram_business_content_publish).
4. From a second Instagram account, comment on that post. In Lightbulb ask: "Reply to the new comment saying thanks." Show the reply appear on instagram.com (instagram_business_manage_comments). Optionally show the "New comment" automation in Teams > Automations firing.
5. From the second account send a DM to the test account. In Lightbulb ask: "Reply to the latest DM: thanks, we'll be in touch." Show the DM reply arrive in the second account's inbox (instagram_business_manage_messages).
6. Settings > Connections > Instagram > Disconnect; show it removed. Show https://thelightbulb.company/support#data-deletion.

### Video B: Lightbulb (ads + pages)
1. Sign in as above.
2. Settings > Connections > Meta Ads > Connect. Facebook login for Business dialog; select the Business and ad account; the permissions screen lists ads_mcp_management, ads_read, ads_management, business_management, pages_show_list, pages_manage_metadata; Allow. Lightbulb shows Connected.
3. Ads read (ads_read, business_management, ads_mcp_management): ask the agent "List my ad accounts and summarize last week's campaign performance." Show the answer, then open Ads Manager and show the same numbers.
4. Ads management (ads_management): "Pause the campaign <name>." Show the status flip to Paused in Ads Manager.
5. Pages (pages_show_list, pages_manage_metadata): Teams > Automations > Add trigger > Meta > pick the Page from the list (shows pages_show_list) > enable "New lead" or "Page post activity". Then create a Page post or submit a test lead (Lead Ads Testing tool) and show the automation fire in Lightbulb.
6. Disconnect and show the data deletion page.

Before recording check: the sandbox test Instagram account is a **professional** account added as an Instagram Tester (IG app roles); the Meta user is Admin/Developer/Tester of the Lightbulb app and has access to a test ad account with at least one campaign and a Page.

## 3. Reviewer test instructions (paste into "Notes for reviewer")

> Go to https://thelightbulb.company/sign-in and sign in with the credentials below. The account owns a pre-provisioned workspace ("Meta Review"). Open Settings > Connections. You will see Instagram and Meta Ads with Connect buttons. Connect your own Instagram professional account (Instagram) and your own Meta ad account (Meta Ads); we cannot provide Instagram/Facebook credentials. If you need a test Instagram account, use the tester invitation we sent. Then in chat (left rail > Chat > #general) ask Friday the prompts listed in our screencast: [list]. The result is visible in your Instagram account / Ads Manager. Login email: <reviewer email>, password: <set at submission>. No payment is required; the workspace is on a free trial. Data deletion: https://thelightbulb.company/support#data-deletion.

Test accounts to provide: (1) a Clerk **workspace** account (email + password, created for Meta review, in its own org); (2) one Instagram professional account added as Instagram Tester on the IG app; (3) a Facebook user with a role on the Lightbulb app, a test ad account (campaign present) and a Page. OTP/2FA must be off for the reviewer account. Per owner rule: sign-in codes and passwords are entered by the owner only.

## 4. Reviewer allowlist: why it breaks and what to change

- The gate: `apps/backend/core/services/credentials/registry.py:595-615` (`available_for`): a provider is shown when `environments` includes the current env **or** the owner id is in `owner_allowlist` (field defined `registry.py:489`). It is applied for the connections list via `routers/platform_info.py` (~line 47).
- The entry: `apps/backend/core/services/credentials/providers.yaml:2187-2189`, under `instagram:` (`environments: [local, dev]` at `:2167`). Second entry `"user_3HmnXQdFUk2pP2Zjw4HEGkefcNM"` is a personal `user_` id. Since `resolve_owner_id` returns 409 `workspace_required` for any caller without an org (`core/auth.py:94` and callers), that user can no longer even call the product APIs, so the entry is dead.
- Fix: create a reviewer Clerk account that is a member/admin of a dedicated org (e.g. "Meta Review"), let its workspace provision, then replace the `user_3Hmn...` line with that org's `org_...` id (owner id = org id in org context). Do the same for the `meta_ads` provider: it has no `owner_allowlist` today (check `environments` on that entry, `providers.yaml` ~2060-2077; the comment at `:1995` says it is env-gated). Add `owner_allowlist: ["org_<reviewer>"]` there too so the reviewer sees Meta Ads on prod. Both edits are code + a prod deploy (dispatch), plus the reviewer org must exist in **prod** (dev org ids differ).
- Remove all allowlists once approval lands (`providers.yaml:2174` comment).

## 5. Meta general requirements checklist

- Privacy policy URL: https://thelightbulb.company/privacy (set). Terms: https://thelightbulb.company/terms (set).
- Data deletion: **instructions URL** set to `https://thelightbulb.company/support#data-deletion` (`apps/frontend/src/app/support/page.tsx:91`, email-based). Meta accepts an instructions URL, but a callback is safer for Instagram. No data-deletion or deauthorize **callback endpoint** exists in the backend (grep found none). If the IG app requires a callback, build one (`POST` signed_request, return `{url, confirmation_code}`) or stay with instructions; recommend the instructions URL first.
- Business verification: done (Tech Provider, 2026-07-26). Access verification also verified.
- App mode: Lightbulb is Live. Confirm "Lightbulb - IG" is Live too, and App Domains: drop retired `flickmeon.ai` (it 308s).
- App icon, category, contact email set. DPO fields empty (needed only if EU users are targeted; fill with support@thelightbulb.company).
- Webhooks: callback URL `https://api.thelightbulb.company/api/v1/hooks/meta` (route `meta_webhook.py:194,213`), verify token and app secret must match the Secrets Manager values; check this in each app's Webhooks product before submit (not verified in the console notes). The IG app and the Lightbulb app have separate app ids, so `_app_secret_names` (`meta_webhook.py:113`) must include both secrets (read it: it appears to).
- Redirect URIs to register: `https://api.thelightbulb.company/api/v1/oauth/{meta-ads|instagram}/callback` (`routers/connections.py:1053`; check the provider path segment). Valid OAuth redirect URIs must be set in both apps.

## 6. Blockers and risks

1. Reviewer allowlist is stale (section 4); prod deploy needed before recording.
2. `skills/instagram/SKILL.md` still points at `/home/node/.openclaw/skills/instagram/...` (OpenClaw era). Check that the `instagram_cli` materializer and bridge still resolve this path on Hermes before recording; if not, the agent cannot publish and the video fails. Fix the paths first (credentials live in the profile's `skills/instagram/`).
3. No Lightbulb-owned skill for Meta ads: ads_read/ads_management work only through Meta's MCP tools. The video must show the MCP being used (that is what the 14.4k calls were).
4. Unused/undemoable: `instagram_basic`, `catalog_management`, `pages_read_engagement`, `instagram_business_manage_insights` — drop (section 0).
5. Meta app Basic settings still list retired `flickmeon.ai`.
6. Data deletion is email-only (no callback endpoint).
7. Advanced access on `ads_mcp_management` is required for OTHER businesses; until approved only role holders can connect (`providers.yaml:1995`).
