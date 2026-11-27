# Product Marketing Context

**Document version:** v4
**Last updated:** 2026-11-25
**Status:** the source of truth for marketing copy. Spark is the only product (founder, 25 Nov 2026). The phone receptionist, the Google Meet agent and the AI-agent messaging workspace are retired: claim none of them.

"VERIFIED" means built on `main` and reachable by a customer. `main` is ahead of prod by default: confirm a claim is deployed to prod before a launch that depends on it.

---

## Claims ledger

### Spark (the Mac app, page `/os`; formerly "Lightbulb OS")

| Capability | Status | Proof / what is missing |
|---|---|---|
| The Mac app is called Spark; `/os` says "Meet Spark" and offers the download (no waitlist) | VERIFIED | Founder, 21 Nov 2026: "The thing on the page and home page card should say spark ... let people download the app". `app/os/page.tsx`. The URL stays `/os` because invite links `/os?ref=CODE` exist. |
| "One-click voice interface for your Mac." | VERIFIED (founder wording) | Founder, 21 Nov 2026, verbatim: "The line should be one click voice interface for your mac". No other capability of the app may be claimed: it is built outside this repo. |
| Download: `/download/spark` (DMG) and update feed `/spark/appcast.xml` | VERIFIED as redirects | `apps/frontend/next.config.ts` `redirects()` to the `spark` GitHub release. The download works only once the app team uploads `Spark.dmg` and `appcast.xml` to that release and prod is deployed. |
| Requires macOS 14 or later | VERIFIED | The app's sign-in library needs macOS 14 (app team, 21 Nov 2026). |
| Apple silicon only (M1 or later); no Intel build | VERIFIED | The app team's build is arm64 only (21 Nov 2026). Say "For Mac with Apple silicon (M1 or later)". |
| On an invite link (`/os?ref=CODE`), signed out: "You've been invited to Spark. Create your account, then download." with the sign-up form and "Skip, just download"; signed in: the download and "Open Spark and sign in with the same Apple, Google or email." Before the installer is published: "Download coming soon." No referrer name. | VERIFIED | Founder, 21 Nov 2026: record the referral at web sign-up; the referred person gets nothing extra. `app/os/OsInvite.tsx`. |
| On a phone or tablet (decided by user agent in the browser; the server HTML always carries the download for crawlers and Macs), every download button on `/os` is replaced by "Spark is a Mac app. Open this page on your Mac to download it." with "Copy link" (the page address, invite code kept) and "Email me the link" (a `mailto:`). An invited visitor can still create the account; "Skip, just download" is hidden | VERIFIED | Owner, 22 Nov 2026 (a friend tapped a Mac download on an iPhone). `app/os/SparkDownload.tsx` `SparkGet`. |
| Price and trial on `/os`: one small line under the download, "Free for 3 days, then $20/month. Renews monthly until you cancel.", built from the backend's public `GET /os/config` (no line when it cannot be read), followed by "Terms" (`/terms`) and "Privacy" (`/privacy`) links, always shown. Nowhere else on the page | VERIFIED | Founder, 21 Nov 2026. `app/os/page.tsx`. Spark's Stripe Checkout asks for Terms of Service consent when the Stripe account has a terms URL. |
| Voice audio is never stored; a text transcript of each conversation is kept for 90 days | VERIFIED | `apps/backend/core/voice/os_voice.py`: the sideband saves only the text of `session.input_transcript.delta` / `session.output_transcript.delta`, one item per turn with `ttl` = now + 90 days (`TRANSCRIPT_TTL_S`); the `spark-transcripts` table expires items on that `ttl` (`apps/infra/lib/stacks/database-stack.ts`). Audio goes from the Mac straight to OpenAI and never reaches our servers. Deleting a Spark account deletes the transcripts. Prod writes only once `SPARK_TRANSCRIPTS_ENABLED` is "1" there and deployed. |

## Copy rules

- Never hardcode a price or trial length. The `/os` price line is built from `GET /os/config`; with no answer, show no line.
- Words customer copy must not use: `apps/frontend/src/content/__tests__/bannedCopy.ts`.
- Never overclaim. Spark is built outside this repo (`TheLightbulbCompany/lightbulb-os`): claim only what the ledger marks VERIFIED.
- The legal entity is "Isol8 Inc., doing business as Lightbulb AI". Write no other legal name.

## Kept routes (do not delete)

- **The CLI / agent network:** `/network`, `/j`, `/docs`, `/install.sh`. Kept, not featured; a footer or nav link is fine.
- **Legal and account pages** (`/terms`, `/privacy` and the other legal pages, `/sign-in`): edit copy only.

## Changelog
*Newest first.*
- v4 (2026-11-25) — Spark is the only product. The phone receptionist, Google Meet agent, voice-plan pricing, personas, positioning and SEO sections are removed (git history keeps them). The Spark ledger is unchanged.
- v3 (2026-11-23) — At the owner's word the Spark page's new first-conversation wording, its own share picture and the `/spark` forward were removed again.
- v2 (2026-11-22) — Spark page: phone visitors get "open this on your Mac" instead of a download.
- v1 (2026-11-18) — Initial context for the 7 Nov 2026 voice pivot.
