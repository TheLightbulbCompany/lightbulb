# Google: one verified Lightbulb app, CASA later (2026-10-23)

Decision (owner, 2026-10-23):
- Replace bring-your-own-app (BYO) Google with ONE Lightbulb-owned Google OAuth app.
- First, get it verified for non-restricted scopes only. That needs no CASA.
- Start CASA Tier 2 in parallel, then add the restricted scopes to the same app.

Background is in `docs/research/2026-10-23-integrations-mac-identity-audit.md`, Part 3.
Delete this file when phase 3 ships.

## Scopes

| Phase | Scope | Google class | Why |
|---|---|---|---|
| 1 | `openid`, `email`, `profile` | non-sensitive | names the connected account (gog `-a <email>`) |
| 1 | `gmail.send` | sensitive | the agent sends mail as the user, when asked |
| 1 | `calendar.events` | sensitive | the agent reads and creates events; `events.watch` triggers (watch accepts this scope). Narrower than today's `calendar`; gog commands that list calendars (not events) lose access, so check in B2 |
| 1 | `drive.file` | non-sensitive | the agent creates Docs and Sheets, and opens files the user picks (Picker, phase 2) |
| 3 | `gmail.readonly` | restricted (CASA) | the agent searches the inbox; the Gmail triggers come back |
| 3 | `drive` | restricted (CASA) | the agent browses and searches the whole Drive |

Phase 1 loses three things: reading the inbox, the two Gmail triggers, and whole-Drive search.
Inbound mail works by forwarding it to the agent's own address (agent mail already exists).

## Track A — Owner (console work, human only)

**State found on 2026-10-24.** The project already exists: "Isol8" (`isol8-485204`).
- **Done:** steps A1 and A3, and most of A2. It is In production and External, its branding is verified, and it has 2 of 100 users.
- **Still to do:**
  - Change the support email (currently prasiddha@gmail.com) to `support@thelightbulb.company`.
  - Of the 5 existing "Isol8" clients, pick one, rename it, and give it the two callback URIs. That is step A4.
  - A5, A6 and A7. Data access has never been submitted.
- **Secrets:** the `lightbulb/{env}/oauth-clients/google` secret is likely one of those clients. Move it to the `platform-oauth/google` path rather than creating a new client.

A1. **Create the Google Cloud project** `lightbulb-prod`. Use one app for both environments.
  - Enable these APIs: Gmail, Calendar, Drive, Picker.

A2. **Fill in the OAuth consent screen (Branding).**
  - App name: Lightbulb.
  - Logo: 120×120 px.
  - Support email: `support@thelightbulb.company`.
  - Homepage: `https://thelightbulb.company`.
  - Privacy policy: `https://thelightbulb.company/privacy`.
  - Terms: `https://thelightbulb.company/terms`.
  - Authorized domain: `thelightbulb.company`.
  - Developer contact: `admin@thelightbulb.company`.
  - Audience: External. Publishing status: **In production** (Testing mode expires refresh tokens after 7 days).

A3. **Verify the domain** in Google Search Console for `thelightbulb.company`.
  - The TXT record goes through CDK (Track B, task B1), never into Route53 by hand.

A4. **Create the OAuth client** (type: Web) with these redirect URIs:
  - `https://api.thelightbulb.company/api/v1/oauth/google/callback`
  - `https://api-dev.thelightbulb.company/api/v1/oauth/google/callback`

A5. **Paste the client into Secrets Manager**, in both envs, as `{"client_id": "...", "client_secret": "..."}`:
  - `lightbulb/dev/integrations/platform-oauth/google`
  - `lightbulb/prod/integrations/platform-oauth/google`

  Use the opened-file pattern: the owner pastes, the agent loads it blind.

A6. **Update the privacy policy (Termly).** It is a Termly embed, not in this repo. Add:
  - The Limited Use statement: "Lightbulb's use and transfer of information received from Google APIs adheres to the Google API Services User Data Policy, including the Limited Use requirements."
  - Which Google data we access (mail send, calendar, files the user picks) and why. That use is limited to features the user asks for.
  - That Google data is sent to AI model providers only to perform the user's request, and is **never used to train generalized AI/ML models**.
  - Retention, and how to delete: disconnect in Settings → Connections, or email support.

A7. **Submit for verification** (Verification Center). Include the scope justifications below and the demo video (Track C).
  - Expect 3–6 weeks with no CASA.

## Track B — Code (one PR per task, on `origin/main`)

B1. **CDK: the Search Console TXT record** on the `thelightbulb.company` zone (`dns-stack.ts`). The value comes from task A3.

B2. **Switch Google to the platform app** (after A5 in dev).
  - `providers.yaml` `google`:
    - Drop `byo_required` and `byo_setup_url`.
    - Add `platform_app: google`.
    - Set the scopes to the phase 1 list.
  - Remove the `gmail.poll` and `gmail.message` inbound events. The Gmail triggers need read scopes; they come back in phase 3.
  - Keep `calendar.changed`. Keep `drive.changed`, which with `drive.file` covers only files the app can see.
  - Frontend: delete the Google BYO setup step. `ConnectButton` BYO tests: Google is no longer BYO.
  - Skill: `skills-src/lightbulb/references/platform-primitives.md`.
    - Drop the Gmail-inbox and Gmail-trigger text.
    - Say that inbox mail reaches the agent by forwarding to its own address.
    - Update the line in `MAINTENANCE.md`.
  - Existing BYO Google connections: their refresh tokens belong to the owner's own app. Count the connected `google` rows in both envs before merging.
    - If the count is above zero, mark them `reconnect_required`.
    - This depends on audit fix #2, which makes `reconnect_required` visible. Otherwise the users see only "Not connected".

B3. **Google Picker (phase 2).** A "Choose files" button on the Google connection. It grants the picked files to the `drive.file` grant.
  - Needs a browser API key restricted to the Picker API and our origins, plus the project number.
  - Ship it before the demo video if possible, so the video shows `drive.file` in use.

B4. **Phase 3 (after CASA passes).**
  - Add `gmail.readonly` and `drive` to the scopes.
  - Restore the two Gmail events. Gmail push publishes to OUR Pub/Sub topic now, not the owner's.
  - Users reconnect to grant the new scopes.

## Track C — Demo video (agent drives dev, owner records or approves)

Record one unlisted YouTube video, in English, on `dev.thelightbulb.company`. Show:
1. The Lightbulb sign-in, then Settings → Connections → Google → Connect.
2. The Google consent screen, with the **client ID visible in the address bar** and every requested scope listed.
3. `gmail.send`: ask an agent to email someone, then show the sent mail.
4. `calendar.events`: ask an agent to schedule a meeting, then show it in Google Calendar.
5. `drive.file`: an agent creates a Doc, and the user picks an existing file with the Picker that the agent then reads.
6. Disconnect in Settings → Connections, which revokes the grant.

## Scope justifications (paste into the submission)

- **gmail.send**: Lightbulb's AI employees send email on the user's behalf only when the user asks, for example "email the subcontractor the updated schedule". We never read the user's mailbox with this scope.
- **calendar.events**: Lightbulb's AI employees read the user's events to find free time, create and update events the user requests, and get a push notification when an event changes, so they can follow up.
- **drive.file**: Lightbulb's AI employees create Docs and Sheets for the user and open files the user explicitly selects with the Google Picker.

## Track D — CASA Tier 2 (owner + agent, starts with A7)

D1. **Pick an authorized CASA lab** (App Defense Alliance list).
  - Tier 2 is a self-scan the lab validates. About $540–$1k a year; that is a vendor quote, so confirm it.

D2. **Run the scan against prod** (API and web), fix findings, and submit the lab's letter of assessment.

D3. **Add the restricted scopes** to the app's data access, then do task B4.
  - Re-assessment is yearly; add a calendar reminder.

## Not doing (until a customer asks)

- **Workspace admin trust for the restricted scopes before CASA.** It needs a per-owner scope set and a second consent path. Build it only when a named customer is blocked on inbox reading.
- **A BYO app for power users.** `platform_app` and `byo_required` cannot both be set on one provider (`registry.py:540`), so this would be a second provider key.
