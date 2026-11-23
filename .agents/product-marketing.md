# Product Marketing Context

**Document version:** v1
**Last updated:** 2026-11-18
**Status:** the only source of truth for every marketing agent. If this file and any other doc disagree, this file wins. Repo `CLAUDE.md` is stale (it describes the retired AI-agent messaging workspace): never take product facts from it.

**How this was built:** claims checked against the code on `main` at `6f1f8d6b0` (18 Nov 2026), then the plans `docs/superpowers/plans/2026-11-07-voice-agents-master-plan.md` and `2026-11-08-founder-goal.md`, then the research in `~/Lightbulb/pivot-research-2026-11-07/` (04, 06, 07, 08, 15; dated 7 Nov 2026). "VERIFIED" means built on `main` and reachable by a signed-in customer through the app. `main` is ahead of prod by default: confirm that a claim is deployed to prod before a launch that depends on it.

---

## Product Overview

**One-liner:** Lightbulb gives your salon, spa or barbershop an AI receptionist with the name you choose. It answers your phone day and night, books appointments, takes messages and puts anything it can't decide in front of you.

**What it does:** You forward your business line (or get a new number). Your assistant answers in a natural voice, answers questions from what you told it (hours, services, prices, staff), books, moves and cancels appointments in its own calendar or your Google Calendar, takes messages, and transfers callers to your mobile when they want a person. You see every call, with a transcript and a list of what the assistant did. The same assistant can join your Google Meet and answer questions when you say its name, and you can talk to it in the app.

**Product category (the shelf):** AI receptionist / AI phone answering service for salons, spas and barbershops. Secondary shelves: virtual receptionist, after-hours answering service, AI meeting assistant (Google Meet).

**Product type:** self-serve SaaS, one subscription per business (one workspace = one business).

**Business model:** one monthly voice plan per business with included minutes, then a per-minute rate. Paid by card through Stripe Checkout. See "Pricing facts" for how the numbers reach the site; never hardcode them.

**Pivot date:** 7 Nov 2026. Before that, Lightbulb was an AI-agent messaging workspace for teams ("six AI employees", channels, DMs). That positioning is retired for marketing. See "What from the old product is kept".

---

## Target Audience

**Target companies:** independent, owner-operated salons (hair, nails, lashes, brows), day spas and massage studios, and barbershops in the US. One location, roughly 1 to 15 chairs or rooms, no IT person. US only: numbers are US local numbers and the forwarding instructions cover US carriers.

Lightbulb Meetings is for any business that meets on Google Meet (founder, 20 Nov).

**Decision-maker:** the owner, who is usually also a working stylist, tech, therapist or barber. They decide alone and pay with a card.

**Primary use case:** calls that come in while the owner's hands are busy with a client, after closing time, or when the front desk is busy go unanswered, and some of those callers book somewhere else.

**Jobs to be done:**
- Answer the phone when I physically can't (mid-service, hands in color, wet, gloved).
- Answer the phone after hours and on days off, and still get the booking.
- Handle the routine questions (are you open, how much is X, do you do Y) without me.
- Get the booking, move or cancellation into my calendar without me touching the phone.
- Only bother me when a person or a decision is really needed.

**Use cases (each maps to built features):**
- 7 pm Tuesday, salon closed: a caller books a Saturday slot; it appears on the owner's Home in the morning.
- Mid-color service: a regular calls to move her appointment; the assistant moves it (if her number is carrier-verified).
- "Do you do bridal parties?": the assistant doesn't know, takes a message, and a "Needs you" card appears; the owner answers once and the assistant knows next time.
- "Can I talk to someone?": the assistant transfers the call to the owner's mobile (optionally only during opening hours), or takes a message.
- Monday team meeting on Google Meet: the owner says "Nova, how many calls did we miss last week?" and the action items become follow-ups.

## Personas

Single buyer. One persona; staff are users later.

| Persona | Cares about | Challenge | Value we promise |
|---|---|---|---|
| Owner-operator (stylist, tech, therapist or barber who owns the shop) | A full book, regulars feeling known, not looking unprofessional, not paying for a full-time front desk | Can't answer the phone with a client in the chair; voicemail loses bookings; a receptionist costs thousands a month | Your phone gets answered and booked while you work, and you see exactly what was said and done |
| Front desk / manager (larger shops, later) | Not drowning in calls at peak times | Two lines ringing while checking someone out | Overflow and after-hours calls handled; hand-offs come to them |

**Anti-persona (do not target):** medical, dental or med-spa practices that handle health information (no HIPAA, no BAA); multi-location chains needing per-location routing; businesses whose booking must land in Square, Vagaro, Fresha, Boulevard, GlossGenius, Booksy, Mindbody or Acuity today; businesses outside the US; anyone wanting outbound calling, marketing calls or SMS campaigns; teams on Zoom or Microsoft Teams who want the meeting assistant.

---

## CLAIMS LEDGER

Strict rule: copy may state a VERIFIED claim. A PARTIAL claim may only be stated with its limit in the same sentence, or not at all. A DO NOT CLAIM item must not appear in any customer-facing copy, ad, email, FAQ, schema or comparison page, even as "coming soon", unless the founder approves.

Paths are relative to the repo root `/Users/prasiddhaparthsarthy/Desktop/lb-marketing.nosync`.

### Phone receptionist

| Capability | Status | Proof / what is missing |
|---|---|---|
| Answers calls 24/7, including outside opening hours | VERIFIED | `apps/backend/routers/voice_phone.py` `start_call` answers whenever the line is live; Agent page hint "answers 24/7; it only books inside these hours" (`apps/frontend/src/components/voice/AgentPage.tsx`). Limits to keep in mind: a call ends at 20 minutes (`apps/huddle-agent/src/huddle_agent/phone.py` `CALL_MAX_S`); more than 5 calls at once on one line are answered but only take a message (`LIVE_CALLS_PER_LINE`); when minutes run out and the owner chose "Send calls to my phone", or payment is blocked, calls go to the hand-off number and the assistant does not answer (`voice_phone.py` `start_call`). Do not say "unlimited calls" or "never misses a call". |
| Several calls at the same time | VERIFIED (up to 5 live on one line) | `voice_phone.py` `LIVE_CALLS_PER_LINE = 5`. Say "handles several calls at once", not "unlimited". |
| Takes messages for the owner (name, callback number) | VERIFIED | `phone.py` TOOLS "Messages"; every message becomes a "Needs you" card (`voice_phone.py` `end_call`, `call_ask` kind `message`). |
| Books appointments during the call | VERIFIED | Front desk actions `availability`, `book` (`apps/backend/routers/runtime_internal.py` `FrontDeskBody`); slots respect opening hours, closures, service length, buffer and staff (`voice_phone.py` `open_starts`, `free_slots`, `_lanes`). Owner can switch booking off (`allow.book`). |
| Moves and cancels appointments | VERIFIED, with a limit | `move`, `cancel`, `my_bookings` actions. Limit: earlier bookings can only be found, moved or cancelled when the carrier verifies the caller's number (STIR/SHAKEN level A, `voice_phone.py` `_verified`); otherwise the assistant takes a message. A booking made on the same call can always be moved. Say "moves and cancels bookings for your clients", never "anyone can cancel by phone". |
| Books into its own calendar (bookings on the owner's Home) | VERIFIED | `bookingsGo: "own"` default (`apps/frontend/src/components/voice/data.ts`); Onboarding step 3 "Just its own calendar for now" (`Onboarding.tsx`). |
| Books into Google Calendar | VERIFIED, with limits | `apps/backend/core/services/voice_booking.py` (freeBusy + events insert/patch/delete); Onboarding step 3 "Connect" (`Onboarding.tsx` `Bookings`). Limits: Lightbulb's Google app is unverified on purpose, so the owner clicks through Google's "unverified app" screen once and there is a lifetime cap of 100 Google users (`apps/backend/core/services/credentials/providers.yaml`, `google` comment); the grant also asks for Gmail and Drive scopes; with Google, one booking per time slot (`_capacity`). Do not say "Google-verified" or "certified Google integration". |
| Books into Square Appointments | DO NOT CLAIM | No adapter (the unwired `voice_booking_square.py` was deleted). AgentPage offers only Google or own calendar. |
| Books into Acuity Scheduling | DO NOT CLAIM | No adapter (the unwired `voice_booking_acuity.py` was deleted). |
| Books into Vagaro, Fresha, Boulevard, GlossGenius, Booksy, Mindbody, Calendly | DO NOT CLAIM | No code. |
| Waitlist | PARTIAL | A caller can join a waitlist and the owner sees and clears it (`apps/backend/routers/voice.py` `waitlist`, `AgentPage.tsx` `WaitlistList`). Missing: texting a caller when a slot frees ("texting a freed slot needs SMS registration and is not built", `voice_phone.py`). Never promise a waitlist spot or a callback. |
| Answers questions from business info (hours, services, prices, staff, closures, directions, your own Q&A) | VERIFIED | `voice_phone.py` `_facts` and `owner_instructions`; Agent page Knowledge, Hours, Closures, Services & staff, Instructions (`AgentPage.tsx`). It states only what the owner entered; otherwise it says it will check or takes a message (`phone.py` PROMPT). |
| Learns your hours, services and prices from your website at setup | VERIFIED | Onboarding step 1 "Read my site" → `POST /voice/onboarding/draft` (`Onboarding.tsx`, `apps/backend/core/services/voice_onboarding.py`). The owner checks the draft in step 2; durations it guesses are marked. |
| Imports from Google Business Profile | DO NOT CLAIM | Not built; only the website is read. |
| Learns from what you teach it ("didn't know" answered once, "Next time…" box) | VERIFIED | `voice_phone.py` `remember_answer` writes the owner's answer into Knowledge; "Next time…" box on each call (`apps/frontend/src/components/voice/VoiceHome.tsx`). |
| Remembers returning clients and greets them by name | VERIFIED, with a limit | Caller memory (`VOICECALLER#`, up to 10 facts) and `allow.greetByName`; only for carrier-verified numbers; the owner can make it forget a caller (`voice.py` `forget_caller`). |
| Hands off to the owner: transfers the call to a person | VERIFIED | Agent page "Hand-off to a person" with "Only during opening hours" (`AgentPage.tsx`); SIP transfer in `phone.py`. A failed transfer becomes a "Needs you" card (`handoffMissed`). Needs a hand-off number. |
| "Needs you": anything it can't decide goes in front of the owner | VERIFIED | `/voice/needs-you` (`apps/frontend/src/components/voice/NeedsYou.tsx`); cards for messages, "didn't know" and missed hand-offs (`voice_phone.py` `CALL_OPTIONS`). |
| Owner sees every call: who, when, outcome, one-line summary, what the assistant did, transcript | VERIFIED | Home timeline and call pane: "What {name} did", "Transcript", caller memory (`VoiceHome.tsx`); summary built only from the caller's own request and the tool steps (`voice_phone.py` `call_summary`). |
| Call recordings the owner can play | DO NOT CLAIM | The Home renders `recordingUrl` if present (`VoiceHome.tsx`), but no backend code writes it (`grep recordingUrl apps/backend` finds only a docstring). Say "transcript", never "recording". |
| Owner alerts on their phone | PARTIAL | Push for the first 3 answered calls after Go live and for "Needs you" cards (`voice_phone.py` `_first_calls_push`), sent through Expo push (`apps/backend/core/services/push_service.py`), so it needs the Lightbulb iPhone app, which is on TestFlight, not a public App Store listing. Do not claim SMS or email alerts to the owner. |
| SMS: confirmation texts, reminders, text-back to callers | DO NOT CLAIM | Not built for the voice product. Numbers are bought SMS-capable (`voice_onboarding.py`), but nothing sends a text to a caller. 10DLC registration is an open item in the master plan. |
| Reduces no-shows | DO NOT CLAIM | No reminders, no confirmations, no outbound. We can say callers can move or cancel by phone instead of not turning up, nothing stronger. |
| Outbound calls (reminders, follow-ups, reactivation) | DO NOT CLAIM | Only an internal forwarding check call exists (`apps/backend/core/services/voice_outbound.py`). Compliance line in the master plan forbids outbound beyond consented callbacks. |
| Owner chooses the assistant's name | VERIFIED | Onboarding step 5 and Agent page "Name & greeting"; suggestions Lightbulb, Nova, Sol, Remy (`Onboarding.tsx` `NAMES`). Default if left blank: "Lightbulb". The UI asks owners not to use a real staff member's name. |
| Owner chooses the voice | VERIFIED (3 voices) | Marin (default), Quartz, Willow (`packages/shared/src/voicePreferences.ts`), with stock samples in `apps/frontend/public/voice-samples/`. Say "pick from three voices"; do not say "dozens of voices" or "clone your voice". |
| Owner writes the greeting | VERIFIED | "Greeting style" field (`AgentPage.tsx`); default when blank: "Hi, I'm {name}, {business}'s AI assistant. How can I help?" (`voice_phone.py` `greeting`). |
| Says it is an AI at the start of each call | PARTIAL | Only the default greeting says "AI assistant". An owner-written greeting replaces it (the voice follows it as "a guide, not a script", `phone.py` OPENING). The assistant tells the truth if a caller sincerely asks whether it is a person (`phone.py` PROMPT). Safe copy: "It tells callers it's an AI assistant when asked, and the default greeting says so." |
| Says "this call is recorded" at the start of each call | DO NOT CLAIM | Not in the phone greeting or prompt. The app tells owners: "Some states require telling callers they are talking to an AI or that calls are recorded: that is your business's responsibility" (`Onboarding.tsx`, `AgentPage.tsx`). The master plan's "This call is recorded" first line is intent, not built. Never claim automatic consent or two-party-consent compliance. |
| Never takes card numbers | VERIFIED | Card numbers, expiry and CVV are removed from every stored record (`voice_phone.py` `redact_cards`), and "remember" refuses card-like digits. Safe copy: "It doesn't take payments or keep card numbers." Do not claim PCI compliance. |
| Won't share other clients' information | VERIFIED | Prompt rule plus server-side limits: callers reach only their own bookings, and caller tasks may not pay, email outside, or change prices (`voice_phone.py` `CONTACT_NOTE`, `_callers`). |
| Owner controls what it may do (book, move/cancel, greet by name, answer questions) | VERIFIED | Agent page checkboxes, enforced server-side (`voice_phone.py` `_allowed`). Anything unchecked becomes a message. |
| Pause and resume the line | VERIFIED | Agent page Pause/Resume; a paused line only takes messages (`voice_phone.py` `_line_state`). |
| Spanish | VERIFIED (English + Spanish only) | "Also speaks Spanish" switch; opens in English, follows the caller (`PhoneVoiceSettings.tsx`, `phone.py` `prompt`). |
| Other languages | DO NOT CLAIM | Without the switch it speaks only English. |
| Keep your existing number via call forwarding | VERIFIED | Onboarding step 4 "Keep my number … Nothing to port", carrier codes for Verizon, AT&T, T-Mobile, landline, VoIP apps, Google Voice; "Missed and busy calls only" or "All calls"; a forwarding check call (`Onboarding.tsx`, `apps/frontend/src/components/voice/forwarding.ts`, `voice_outbound.py`). Limit: forwarding from consumer Google Voice to our number is untested (master plan open item): do not promise Google Voice. |
| Get a new local number (choose area code) | VERIFIED | Onboarding step 4 "Get a new number" (`voice_onboarding.py`, US local numbers). The number is bought only at Go live, after payment. |
| Number porting | DO NOT CLAIM | Not built. |
| Setup time | PARTIAL | Five steps, designed to finish in one sitting from a phone (`Onboarding.tsx` header: "live within 15 minutes"); the website read takes about a minute. Never measured end to end with customers. Safe copy: "Set up in five steps." Do not print "15 minutes" or "in minutes" until timed with real owners. |
| Test call before going live | PARTIAL | The owner's own number always reaches the live assistant, even before Go live (`voice_phone.py` `_line_state`), but the assistant's number is only bought after the voice plan is paid (`voice_onboarding.py` `_provision_number`). So a test call happens after payment. Do not say "try it free before you pay". |
| Owner calls in and talks to the assistant as the owner (phone PIN) | VERIFIED | Phone PIN card (`PhonePinCard.tsx`); keypad PIN checked by the backend (`voice_phone.py` `owner_pin`). Approvals still happen in the app, never by voice. |
| Works with multiple staff (books the right person) | VERIFIED, with a limit | Staff list and which services each person does (`voice_phone.py` `_staff`, `_lanes`). Per-person schedules and days off are not modelled. |
| Multi-location | DO NOT CLAIM | One business, one line per workspace. |
| Spam / robocall filter | DO NOT CLAIM | Not built. |

### Google Meet agent and in-app voice

| Capability | Status | Proof / what is missing |
|---|---|---|
| Joins your Google Meet when invited | VERIFIED | Invite the address shown on the Agent page (`agents@{your-slug}.thelightbulb.company`) to a Google Calendar event with a Meet link (`apps/backend/core/services/meeting_invites.py` `_MEET_URL`; `AgentPage.tsx` "Meetings"). Only invites from a member of the business work. |
| Speaks only when named; answers questions live | VERIFIED | Announcement "Say its name to wake it" (`apps/backend/routers/voice_meet.py` `ANNOUNCE`); `/meet-bot` page publishes its voice (`apps/frontend/src/app/meet-bot/page.tsx`). |
| Tells guests it is there, that the meeting is recorded, and how to opt out | VERIFIED | `voice_meet.py` `ANNOUNCE`: "This meeting is recorded and notes are saved…", "Say '{name}, stop transcribing me'". |
| Notes, decisions, what it was asked, follow-ups | VERIFIED | Meeting pane: Notes, Decisions, "What {name} was asked and did", Follow-ups (`VoiceHome.tsx`); "Turn action items into follow-ups" switch (`AgentPage.tsx`). |
| Spending, sending or publishing needs the owner's approval | VERIFIED | `voice_meet.py` header ("a spend, an outside send or a publish always gets a Needs-you card"). |
| Zoom, Microsoft Teams, Webex | DO NOT CLAIM | Google Meet links only. |
| In-app voice orb: tap and talk to your assistant | VERIFIED | `/voice/lightbulb` (`apps/frontend/src/components/voice/LightbulbPage.tsx`, `TalkOrb.tsx`). Keep it secondary in marketing; the phone is the product. |
| Meeting minutes count toward the plan | VERIFIED | `apps/backend/routers/meetings.py` `_report_minutes`. Say it if you explain minutes. |
| Meetings is offered on the site by waitlist only; `/meetings` sells nothing and shows no price | VERIFIED | `app/meetings/page.tsx`; `waitlist` in `apps/backend/schemas/setup_request.py` |

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

### Billing, trial, compliance

| Capability | Status | Proof / what is missing |
|---|---|---|
| Month-to-month, no contract, no setup fee | VERIFIED | Monthly Stripe subscription (`apps/backend/core/billing/voice_plan_checkout.py`); no setup fee anywhere in code. |
| Cancel anytime, yourself, in Settings | VERIFIED | "Cancel renewal" in `/voice/settings` (`apps/frontend/src/components/voice/VoicePlanCard.tsx`, `DELETE /billing/voice-plan`); access continues to the end of the paid period. Limit: restarting after a cancellation needs support ("Contact support to start a new plan"). Do not promise refunds or proration. |
| Live minutes meter and a choice when minutes run out | VERIFIED | "Billing & minutes" meter; "Keep answering at {rate}/min" or "Send calls to my phone" (`apps/frontend/src/components/voice/VoiceSettings.tsx` `BillingMinutes`; `voice_phone.py` run-out forward). |
| Promotion codes at checkout | VERIFIED | `allow_promotion_codes=True` (`voice_plan_checkout.py`). Only mention when a real code exists. |
| Free trial of the phone receptionist | DO NOT CLAIM | Go live needs a paid voice plan (`voice_onboarding.py` raises `voice_payment_required`). `trial_days` in `GET /billing/prices` belongs to the retired team plan, not to the voice plan. A small daily free allowance for in-app voice exists in code (`voice_billing.py` `FREE_SECONDS_PER_DAY`) but is not an offer: do not market it. |
| Money-back guarantee | DO NOT CLAIM | None exists. |
| HIPAA compliant / signs a BAA | DO NOT CLAIM | No BAA; the master plan defers medical to a later phase, and the Worker model's provider requires 30-day retention (research 08). Do not target medical, dental or med spas. |
| SOC 2, PCI, GDPR certified | DO NOT CLAIM | None. |
| "Zero data retention" with every vendor | DO NOT CLAIM | Intent in the master plan, not verified per vendor. |
| Results numbers ("books X% more", "saves $Y") | DO NOT CLAIM | No customer data yet. Industry figures in "Proof points" must be cited as third-party research. |

---

## Pricing facts

- **The rule:** copy never hardcodes a price, minute allowance, overage rate or trial length. Every number is read from the backend at render time, or the copy stays neutral ("one monthly plan with minutes included", "see the price when you sign up").
- **How price reaches the site today:** `apps/frontend/src/lib/pricing.ts` `getPricing()` fetches the public, unauthenticated `GET /billing/prices` (`apps/backend/routers/billing.py` `get_public_prices`, cached 5 minutes, returns `null` on failure so pages render neutral copy). Its fields are `workspace_price_usd`, `included_members`, `seat_price_usd`, `front_desk_price_usd`, `trial_days`, `managed_markup`.
- **None of those fields is the voice plan.** `workspace_price_usd`, `included_members`, `seat_price_usd`, `trial_days` and `managed_markup` belong to the retired team plan; `front_desk_price_usd` is the old texting-line add-on. Do not render any of them as the receptionist's price.
- **Where the voice price lives:** `GET /billing/voice-plan` (signed-in only) returns `monthlyUsd`, `includedMinutes`, `overageUsdPerMinute`, `planVersion` (`billing.py` `get_voice_plan`); the values are literals that match `apps/backend/core/billing/voice_billing.py` `INCLUDED_MINUTES` / `OVERAGE_USD_PER_MINUTE` and the Stripe price `STRIPE_VOICE_PLAN_PRICE_ID`. Current values, for orientation only and never for copy: $250 a month, 1,500 minutes included, $0.35 a minute after.
- **Gap to fix before a public pricing page:** `GET /billing/prices` needs public voice fields (for example `voice_plan_usd`, `voice_included_minutes`, `voice_overage_usd_per_minute`) read from the same constants, and `pricing.ts` must read them. Until that ships, pricing copy stays neutral. A marketing agent must not add hardcoded numbers as a workaround.
- **Existing helpers that are wrong for voice:** `pricingFaq`, `teamIncludes`, `PRICING_TRIAL`, `PRICING_FOUNDING`, `pricingInference` in `pricing.ts` describe the old team plan (seats, six AI employees, ChatGPT sign-in, day-31 charge). Do not reuse their text.

---

## Problems & Pain Points

**Core problem:** the phone rings while the owner is with a client, after closing, or when the desk is busy, and a missed call is often a lost booking. (Third-party: 44% of business calls never reach a person, Invoca, 70M calls, Jul 2026.)

**Why current options fall short:**
- Voicemail: most callers don't leave one; they call the next salon.
- A human receptionist: about $3,800–4,000 a month fully loaded (BLS May 2025 median, research 04), and still off at night.
- Human answering services (Ruby, Smith.ai): priced per minute or per call ($3.45–$5.00/min Ruby; $7–$11.50/call Smith.ai, research 04), and many only take messages.
- Booking-platform AI (Fresha, GlossGenius, Boulevard, Booksy): only for shops on that platform; Boulevard's sends a booking link rather than booking new appointments.
- Cheap AI receptionists: reviews complain about billing, not voice: charged after cancelling, trials burned, surprise upsells (research 04).

**What it costs them:** lost bookings, regulars who drift away, interrupting a paying client to answer the phone, or payroll for a desk.

**Emotional tension:** guilt at ignoring the phone mid-service; fear that a robot will sound cheap or get a regular's booking wrong; distrust of software that bills by surprise.

## Competitive Landscape

Facts as checked on 7 Nov 2026 (research 04 and 08). Re-check a competitor's live page before any comparison page names them.

**Direct (standalone AI receptionists):** Goodcall, Dialzara, Rosie, My AI Front Desk, Smith.ai AI Receptionist, Beside, Phonely, ElevenLabs Reception (from $29), Yelp Receptionist ($99 inside Yelp). They fall short on billing trust (review complaints) and most book only into Google Calendar, Calendly or Cal.com. Several are much cheaper than us: we do not win on price.

**Secondary (booking platforms' own AI):** Fresha AI Concierge, GlossGenius Reception, Boulevard AI Receptionist (beta, link-only for new bookings), Booksy AI Receptionist (beta), Mangomint Mira, Zenoti. They book into their own calendars and are often cheaper. If a shop runs on one of them, its native AI may fit better; say so rather than pretend otherwise.

**Indirect:** voicemail; the owner answering mid-service; a part-time or full-time receptionist; human answering services (Ruby, Smith.ai).

## Differentiation

Only VERIFIED points:
- **It does the booking, not just the message:** books, moves and cancels during the call, inside your hours, for the right staff member, with buffers.
- **You see exactly what it did:** every call has a one-line summary, the steps it took, and the transcript; nothing is claimed that a tool didn't do.
- **It asks you instead of guessing:** anything it doesn't know or isn't allowed becomes a "Needs you" card; answer once and it knows next time.
- **It's yours:** your name for it, your greeting, one of three voices, your rules for what it may do, English or English + Spanish.
- **One assistant for the phone and your Google Meet**, and you can talk to it in the app or by calling in with your PIN.
- **Honest billing:** month to month, a live minutes meter, your choice of what happens when minutes run out, cancel yourself in Settings. This answers the category's top complaint.

**Why choose us over a cheaper AI receptionist:** it books for real, it shows its work, and the bill has no surprises. **Over a human service:** it answers at 2 am too, and the price doesn't climb per call.

## Positioning

**Value proposition (one line):** Your phone, answered and booked while your hands are busy.

**Supporting pillars:**
1. **Answers your calls day and night.** 24/7, several calls at once, in English or Spanish, in the name and voice you choose.
2. **Books like your front desk.** Books, moves and cancels during the call in your Google Calendar or its own calendar, inside your hours and for the right person.
3. **You stay in charge.** Every call shows what was said and done; anything it can't decide comes to you; it transfers callers to you when they want a person.

**Primary CTA:** "Set up your assistant" → `/sign-up` (Clerk; after sign-up it lands on `/voice`, which opens the five-step setup at `/voice/setup` for a new business). Signed-in visitors → `/voice`.

**Secondary CTA:** "Hear the voices" → play the stock samples in `/voice-samples/` (`gpt-live-1-{marin|quartz|willow}-{en|es}.wav`). Label them as stock samples, not a personalized greeting. Optional tertiary: `/request-setup` (guided setup request form) only after its copy is rewritten; today it still offers "founding teams of 3 to 15".

There is no public demo phone number. Do not invent one.

## Objections

| Objection | Honest answer (VERIFIED claims only) |
|---|---|
| "My clients will hate talking to a robot." | It answers in a natural voice, with the name you pick and a greeting you write, and if a caller asks for a person it transfers them to your mobile or takes a message. If someone sincerely asks whether it's a person, it tells the truth. |
| "What if it gets something wrong or makes things up?" | It only states what's on your Agent page. When it doesn't know, it says it will check and takes a message, and the question lands in Needs you. Before booking it repeats the name back. Every call shows the transcript and the exact steps it took. |
| "I don't want to change my number." | Keep it. Forward missed and busy calls, or all calls, with your carrier's code; the app gives you the code and checks the forwarding works. Or get a new local number. |
| "My bookings are in Square / Vagaro / Fresha." | Today it books into Google Calendar or its own calendar, which you see on your Home. It doesn't write into Square, Vagaro or Fresha yet. If your shop lives in one of those, tell the founder; don't promise a date. |
| "Will I get surprise charges?" | One monthly plan with minutes included. A meter in Settings shows minutes used, and you choose whether it keeps answering at the per-minute rate or sends calls to your phone when they run out. Month to month; cancel the renewal yourself in Settings. |
| "Is it safe? What about payments and privacy?" | It doesn't take payments and removes card numbers from every record. Callers only reach their own bookings, and it won't share anything about other clients. You decide what it may do. Recording and AI-disclosure rules differ by state: your greeting is yours, and the app reminds you of that. |

**Anti-persona answers:** medical practices, chains, non-US businesses, teams on Zoom/Teams, and shops that need Square/Vagaro/Fresha writes today are not a fit yet. Say so plainly.

## Switching Dynamics

**Push:** lost bookings to voicemail; picking up the phone with gloves on; after-hours calls going nowhere; desk cost.
**Pull:** bookings made while they work; a named assistant that feels like theirs; seeing every call and what was done.
**Habit:** "I just call them back"; the voicemail greeting; the booking platform's own tools.
**Anxiety:** sounding cheap to regulars; wrong bookings; forwarding setup; being locked into a bill. Answer with the transcript view, Needs you, the forwarding check, and self-serve cancel.

## Customer Language

Confidence: **low.** No first-party salon interviews exist yet, and Reddit was blocked during the research. The pain words below come from the founder's brief and the research synthesis; the verbatims are competitor review quotes collected in research 04 (Nov 2026). Replace with first-party quotes as soon as owners are interviewed (customer-research skill, Mode 3; Sales Safari on r/hairstylist, r/Nailtech, r/Barber, r/smallbusiness and salon-owner Facebook groups).

**How they describe the problem (owner terms to use):** missed calls; can't answer with a client in the chair / hands busy / hands in color; after-hours calls; calls when we're closed; no-shows; the front desk is expensive; "I'll call them back" and never do; voicemail full.

**Verbatims about the category (competitor reviews, research 04):**
- "kept charging after I cancelled" (Goodcall review, Jun 2026)
- "started at $59 then no notice moved to $99" (Goodcall review, Jun 2026)
- "burned through my trial before a live call" (Thoughtly review, Oct 2025)
- "a form with a voice" (Jobber community, about its AI receptionist)
- "couldn't even create an account… 0 human support" (My AI Front Desk review, Oct 2026)

**Words to use:** your assistant; answers; books, moves, cancels; takes a message; hands the call to you; Needs you; what it did; transcript; your hours, your services, your prices; keep your number; forward; month to month; cancel anytime; salon, spa, barbershop, chair, client, regular, appointment, booking.

**Words to avoid:** agents, AI employees, team of agents, workspace (in marketing copy), Router, Worker, Hermes, super-agent; "Lightbulb answers your calls" (the assistant has the owner's chosen name); unlimited; never misses a call; recordings; texts / SMS / reminders / confirmations; reduces no-shows; free trial; integrates with Square / Vagaro / Fresha / Booksy / Mindbody / Acuity; HIPAA, BAA, medical, dental, patients; human-like / indistinguishable from a human; replaces your staff; "in minutes" / "15 minutes"; any price or minute number typed into copy.

**Naming rule:** "Lightbulb" is the product and the company. The assistant is called whatever the owner chooses ("Nova", "Sol", "Remy", or anything); "Lightbulb" is only its default name when the owner leaves it blank. In copy, write "your assistant" or use an example name and say it's an example: "Name it Nova, or anything you like." Never present the assistant as a real staff member.

**Glossary:**
| Term | Meaning |
|---|---|
| Assistant | The owner's named AI receptionist (phone, Meet, in-app) |
| Home | The owner's timeline of calls, meetings and bookings (`/voice`) |
| Needs you | Cards for what only the owner can decide (`/voice/needs-you`) |
| Agent page | Where the owner sets name, greeting, voice, permissions, knowledge, hours, services, staff, number, hand-off (`/voice/agent`) |
| Knowledge | What the assistant knows about the business |
| Go live | The step that buys the number and turns the line on (after payment) |
| Hand-off | Transferring a caller to the owner's mobile |
| Owner mode | The owner calling in and proving it with a phone PIN |

## Brand Voice

**Tone:** calm, warm, plain. Like a good front desk: friendly, not peppy.
**Style:** short sentences, concrete scenes from a salon day ("7 pm, you're closed, the phone rings"), second person, no jargon, no AI hype. Show the call, the booking, the card.
**Personality:** dependable, honest, unflashy, respectful of the owner's time, a little warm.
**Hard rules:** never overclaim; every capability in copy must be VERIFIED in the ledger; when honest limits help trust (Square not yet, US only), state them.

## Proof Points

**Metrics:** none of our own yet. Do not invent customer counts, call counts, booking lifts or savings.
**Customers / testimonials:** none publishable. Do not write placeholder testimonials, logos or star ratings.
**Third-party figures usable with citation (research 04):** 44% of calls never reach a person (Invoca 2026 benchmarks, 70M calls); receptionist median wage $18.27/hr, $38,010/yr (BLS, May 2025); Ruby $3.45–$5.00/min; Smith.ai $7–$11.50 per call. Do not use the "62% of SMB calls go unanswered" stat (2016, 85 businesses).

**Value themes:**
| Theme | Proof |
|---|---|
| Never stuck on voicemail | 24/7 answering, several calls at once (ledger) |
| Books for real | availability/book/move/cancel actions, hours/staff/buffer rules (ledger) |
| Shows its work | call summary, steps, transcript (ledger) |
| Asks instead of guessing | Needs you, teach once (ledger) |
| No billing surprises | meter, run-out choice, self-serve cancel (ledger) |

## SEO

**Homepage title (58 chars):** AI Receptionist for Salons, Spas & Barbershops | Lightbulb
**Homepage meta description (149 chars):** Your salon's AI receptionist answers your calls day and night, books into Google Calendar, takes messages and hands callers to you. Keep your number.

**12 highest-intent searches a salon owner types:**
1. ai receptionist for salons
2. ai receptionist for hair salon
3. salon answering service
4. virtual receptionist for salon
5. after hours answering service for salon
6. ai receptionist for barbershop
7. spa answering service
8. ai receptionist for nail salon
9. ai receptionist that books appointments
10. answering service that books appointments in google calendar
11. how to stop missing calls at my salon
12. salon phone answering service 24/7

**Current site state:** one site for three products. `/` is the company home. `/phone` sells Lightbulb Phone (the receptionist), and `/pricing` is its one plan. `/meetings` (Lightbulb Meetings) is a waitlist page that sells nothing and shows no price. `/os` (Spark, the Mac app) says "Meet Spark" and offers the Mac download, with one price line from the backend and Terms and Privacy links; on a phone it says to open the page on a Mac instead. The shared nav lists Phone, Meetings, Spark and Pricing; its sign-up button and the footer's say "Set up Lightbulb Phone". The footer, sitemap, `llms.txt` and the facts in `lib/seo.tsx` name all three products. `/voice` layout metadata is "Your assistant's calls and meetings".

## What from the old product is kept (do not delete these routes)

- **The CLI / agent network** is kept but is not the focus: `/network`, `/j`, `/join/*` and the CLI install paths. The old network Mac app and its download are deleted (founder, 19 Nov: business products are web only); Spark is a separate Mac app (see its section). Do not delete, do not feature on the voice homepage; a footer or nav link is fine.
- **Texting is secondary, not deleted:** the Front desk texting add-on (`apps/backend/core/services/text_lines.py`, `apps/backend/routers/agent_text.py`, the `front_desk_price_usd` field) stays. Do not market it as part of the receptionist and do not remove it.
- **Legal and account pages** (`/terms`, `/privacy`, `/security`, `/acceptable-use`, `/eula`, `/cookies`, `/disclaimer`, `/accessibility`, `/support`, `/sign-in`, `/sign-up`, `/onboarding`, `/welcome/*`, `/settings`, `/oauth`, `/auth`, `/checkout_redirect`) are live flows; edit copy only. The legal entity is "Isol8 Inc., doing business as Lightbulb AI"; write no other legal name.
- Marketing agents change copy and page content. They delete no API or backend file. Retired old-product pages (the AI-workspace comparisons, posts and docs) were removed with 308 redirects in #5264 with the founder's approval; delete no other route.

## Goals

**Business goal:** the first five paying salons live on the voice plan (master plan Phase 6), then steady self-serve sign-ups.
**Conversion action:** sign up and finish the five-step setup through Go live (paid voice plan).
**Current metrics:** none published.

## Changelog
*Newest first. One line per revision: what changed and why.*
- v3 (2026-11-23) — At the owner's word the Spark page's new first-conversation wording, its own share picture and the `/spark` forward were removed again (the two lines are back to his earlier wording, which describes the tidy-your-Desktop lesson the app no longer gives). The phone state, the menu button per page, pricing for all three and error reporting stay.
- v2 (2026-11-22) — Spark page: phone visitors get "open this on your Mac" instead of a download, the first-conversation lines replace the old tidy-your-Desktop lesson (wording pending the owner), Spark's own share picture, `/spark` forwards to `/os`. The shared menu, `/pricing` and error reporting are handed to the website session.
- v1 (2026-11-18) — Initial context for the 7 Nov 2026 voice pivot: salon/spa/barbershop AI receptionist, a code-checked claims ledger, pricing-source rules, positioning, objections, SEO and the keep-list.
