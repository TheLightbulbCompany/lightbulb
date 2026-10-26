# Integrations and accounts: master plan (updated 2026-10-25)

The coordinator is session `lightbulb-nosync-af`. It owns every third-party app review, the legal and tax chain, and the integration code fixes.

The Stripe provisioning rail, Connect, agent-money and Router/budgets belong to session `lightbulb-nosync-c6`. Do not touch the Stripe dashboard or Stripe code on main while c6 is building.

The board tasks mirror this file: one Paperclip issue per open step, assigned to Friday or the owner. Update both when a step moves.

Evidence: `docs/research/2026-10-23-integrations-mac-identity-audit.md`. Google detail: `2026-10-23-google-verification.md`. Meta detail: `2026-10-24-meta-app-review-kit.md`.
Delete this file when every track is done.

Who does each step:
- **O** = owner (consoles, signatures, money, phone calls, decisions).
- **A** = Aden.
- **C** = the coordinator or its subagents (code, drafts, read-only checks).

## Open now

| # | Step | Who | Waiting on | Next date |
|---|---|---|---|---|
| 1 | Call Apple Developer Support; ask for escalation to Finance (see Track 1) | O | — | Mon 10-26, 11am ET (Friday reminds) |
| 2 | Reconnect Google while recording the consent video | O | — | when free |
| 3 | Submit Google data access (Limited Use text, justifications, video) | C then O | step 2 | after step 2 |
| 4 | Pick the Meta screencast option: (a) a permission rule so C can post, (b) owner records, (c) partial recording | O | — | when free |
| 5 | Submit Meta App Review | O | step 4 | after step 4 |
| 6 | D&B officer verification for D-U-N-S 145072220 | D&B | D&B (~8 business days from 10-24) | check ~11-05 |
| 7 | D&B legal-name change with the Delaware certificate | O + C | step 6 | after step 6 |
| 8 | Apple Organization Name Change to The Lightbulb Company | O | step 7 and step 1 | after step 7 |
| 9 | Stripe App 0.0.5 review | Stripe | Stripe | — |
| 10 | Revert review org `org_3Jshao85twVsEky1nB16fVWqFeW` billing to `trial_local` | C | step 9 | after step 9 |
| 11 | Microsoft Partner Center enrollment (stopped at the card step) | O | owner: "we will do this last" | last |
| 12 | MPN ID → Entra Branding → Verify publisher | C | step 11 | after step 11 |
| 13 | Decide: Slack (delete or rebuild), X (keep BYO or own app), Intuit (sign in) | O | — | — |
| 14 | Rename consoles: GitHub OAuth apps "Isol8", Google clients, Meta portfolio | O | — | — |

## Track 1 — Legal, tax and Apple's entity

**Root cause of the two-month block:** the Apple membership is isol8, Inc., but App Store Connect → Business lists the individual. So the W-9 (C-Corp) is rejected and the bank stays "Processing".

The name chain is Delaware → D&B → Apple:
- **Delaware:** amendment isol8, Inc. → The Lightbulb Company, filed 2026-08-28, file 10548467. The owner has photos (`~/Downloads/cert-amendment-p1-*.png`, `-p2-*.png`).
- **D&B:** D-U-N-S 145072220 is still "ISOL8 INC.". Officer verification submitted 10-24 with the Articles and CP 575.
- **Apple:** uses D&B. Ask for the entity switch to isol8, Inc. now (it matches D&B today); the name change comes after D&B updates.
- **Stripe and IRS:** Stripe's legal name is "isol8, Inc", which matches the IRS. Mail the IRS name-change letter (`~/Desktop/lb-handoffs.nosync/irs-name-change-letter-2026-10-14.md`) only before a Stripe legal-name change. Tell c6 when that clears; it gates Issuing KYC.

Apple cases:
- Finance **22443039**: no reply since 09-23. The 10-10 follow-up asked for the new name, which conflicts with D&B.
- Developer Support **102998772882**: replied, redirected to Finance.
- Developer Support **103007592630**: sent 10-24 (entity switch request).
- Finance contact form: submitted 10-25 (banking setup, vendor ID 92858213).
- Finance has no phone. Developer Support calls back on weekdays: developer.apple.com/contact → Membership and Account → Agreements and Contracts → Call.

## Track 2 — Apple: submission, then selling

1. **O:** once Track 1 clears, confirm the bank and W-9 in ASC → Business.
2. **O:** submit the free app (demo account `externalreview@thelightbulb.company`, screenshots in `~/Desktop/lb-handoffs.nosync/appstore-shots-2026-10-16/`, reply in `mobile-appstore-reply-2026-10-13.md`).
3. **C:** ASC selling setup: Team product metadata, Server Notifications V2 URL, Small Business Program (O approves).
4. **C:** sandbox acceptance (`docs/runbooks/ios-iap-local-storekit-testing.md`).
5. **O:** decide on `APPLE_IAP_SELLING` in prod; **C** makes the CDK change.

## Track 3 — Google

- **Done:** #4323 switched Google to the platform app with phase 1 scopes; it is in prod. `retire_byo_google` ran: the BYO rows are `reconnect_required`.
- **Open:** steps 2 and 3 above, then CASA Tier 2 (Track D in the Google plan).

## Track 4 — Meta

- **Done:** #4321 dropped `instagram_basic` and `catalog_management`, fixed the reviewer allowlist to an org id, and fixed the Instagram skill paths. The Instagram skill was re-projected into 7 profiles.
- **Open:** steps 4 and 5 above. Remove flickmeon.ai from the app domains.

## Track 5 — Microsoft

- **Done:** `thelightbulb.company` verified in the tenant (TXT in CDK, #4324) and set as the publisher domain on both apps. `prasiddha@thelightbulb.company` is a Global Admin.
- **Open:** steps 11 and 12 above.

## Track 6 — Stripe App listing

- **Done:** 0.0.5 (#4319) resubmitted 10-24 with support email `admin@thelightbulb.company` (owner ruling); reply sent to the reviewer from admin@.
- **Open:** steps 9 and 10 above.

## Track 7 — Integration code fixes

- **Done:** #4317 (CLI credentials into every new profile), #4318 (`reconnect_required` visible), #4352 (Codex follow-ups, reconnect announcements). All in prod (task def 314).
- **Open:** delete Slack, Telegram and Discord after the step 13 decision; stop the shared `GOG_KEYRING_PASSWORD` reaching tenant shells.
