# Lightbulb voice agents — design (7 Nov 2026, draft for the founder)

Research behind this: `~/Lightbulb/pivot-research-2026-11-07/` (01–16; `08` verified every claim, `14` is the agreed architecture). This document is the plain-words version a builder can plan from. Sections marked **(pending)** wait on the UX reference research (15, 16).

## 1. What we are building, and why

Lightbulb becomes **a voice agent for a business**: it answers the business's phone, books and moves appointments, takes messages, and — invited into the business's Google Meet or Zoom — listens, answers when addressed, and does the work that comes out of the meeting. The website and app are how the owner sets it up, watches it, and steers it.

Why now: after a year building a communications platform, traction is ~5 active workspaces and no paid invoices; the bill is ~$2,000/month of mostly idle infrastructure. The super-agent architecture we built this month (Router + per-task Workers on Temporal, approvals, Knowledge, Needs-you, Claude-Projects-style UX) is the right engine; the product on top of it changes from "chat with your team" to "a receptionist and a meeting participant that get work done".

What makes it different from the thirty phone-receptionist vendors: **the call becomes finished work in the business's own systems** (calendar, booking platform — through an API when there is one, through the browser when there isn't), and the same Lightbulb that answers the phone sits in the owner's meetings with the business's whole context. Nobody else does both; none of the receptionists do browser write-back (Bland and Quo send links).

## 2. Goals, non-goals, anti-goals

**Goals**
- A salon/barber/spa (first) or dental practice (second) can forward its phone to Lightbulb and have every call answered, booked or messaged within an hour of signing up.
- The owner sees every call, what happened, and what Lightbulb did, and can correct it in one place ("Next time, …").
- The owner's meetings get the same Lightbulb: it speaks only when addressed, and action items become Follow-ups without the owner managing anything. **The MVP is both seats** (founder, 7 Nov): phone and Meet ship together with the Meetings list; nothing is "1.5".
- **Set up to run medical from day one** (founder, 7 Nov): the BAAs (Twilio Security Edition, Google Cloud for Vertex, LiveKit Scale/Enterprise, AWS already in place, a practice BAA template), zero-retention settings everywhere, PHI-safe model choices (Gemini on Vertex, Haiku on Bedrock, no Claude API for PHI), audit logging — started in Phase 2, so the first dental customer is not waiting on paperwork.
- Nothing runs per business while no call or meeting is happening. Fixed cost ≈ $200–275/month; per-business cost ≈ $25–30/month at $250/month price.
- Single-player first (one owner); multiplayer (staff, roles) without a redesign.

**Non-goals (v1)**
- Marketing SMS and cold outbound. Payments on the call. Multi-location. A Team page. Zoom/Teams beyond what Recall gives for free.

**Anti-goals (would make it a failure even if it works)**
- An agent that answers the phone but nothing happens afterwards ("just an SMS link").
- A dashboard the owner has to learn: if a salon owner cannot set it up from their phone in 15 minutes, we failed.
- Idle infrastructure per business creeping back (per-business VMs, per-business secrets, always-on anything).
- Surprising the owner: the agent doing something they would not have done (money, outside email, other customers' information) — the contact boundary is server-side, not prompt text.
- Markdown or chat-platform leftovers in a product whose users never type to it.

## 3. Constraints

- Keep the super-agent architecture and Hermes (founder ruling): Workers are full Hermes specialists; what changes is where they run.
- One workspace = one business (Clerk org = owner_id). Shared agent address `agents@thelightbulb.company`, routed by sender.
- Prod runs the legacy (Paperclip) path today; the voice product is built on the super-agent path, so promoting that switch and deleting the legacy code is Phase 1, before any voice work.
- Compliance from the first call: AI + recording disclosure in the first utterance; vendors on zero-retention (OpenAI realtime is ZDR-eligible; Anthropic's BAA needs 30-day retention, so Claude never touches PHI); no outbound in v1; medical only after Twilio/OpenAI-or-Google/LiveKit BAAs.
- Prod only after the founder has seen dev. Dev merges are reviewed before merge.
- No new always-on process per business. No Secrets Manager secret per business.

## 4. How it works, in plain words

### 4.1 The pieces

| Piece | What it is | Where it runs | What it costs |
|---|---|---|---|
| **The phone line** | A Twilio number the business forwards its own number to (`*72`), ported later | Twilio → LiveKit SIP | $1.15/number + $0.0034/min |
| **The voice session (the Router)** | Gemini 3.8 Live on Vertex AI, driven by our existing LiveKit worker (`apps/huddle-agent`). It listens and speaks; it reads, does bounded writes, and hands anything open-ended to a Worker with `start_work` (§4.1b); callers can reach only the bounded set | LiveKit Cloud agent hosting (per session-minute; no idle) | ≈ $0.045/call-minute all-in; ≈ $1/meeting-hour |
| **Workers (specialists)** | Hermes agents with the business's Knowledge, skills, browser, memory. A Customer-questions specialist for callers; a Scheduling specialist for bookings; Growth/Ops etc. for meetings | **A fresh AgentCore session per task or per meeting on one pooled runtime (v2)** — not a VM per business | ≈ $0.001–0.008 compute + tokens per task |
| **Tasks (Follow-ups)** | What a Worker is doing or did after a call/meeting; statuses Working · Done · Needs you | Temporal Cloud + the `tasks` table | $50 per million actions |
| **Knowledge** | What Lightbulb knows about the business (hours, services, staff, prices, policies, FAQ, instructions) + what it learned from calls | AgentCore Memory (our cloud memory engine today), per-business namespaces; per-caller memory as an actor per phone number; a retention rule | retrieval + records, ≈ $1–4/business/month |
| **Needs you** | The one list of things only the owner can decide: hand-offs, "Lightbulb didn't know", approvals, line problems | questions + approval modes (Ask / Auto / Never ask), enforced server-side | — |
| **Credentials** | The business's Google Calendar / Square / Calendly / Zenoti tokens | AgentCore Identity (OAuth vault, auto-refresh); website logins and API keys in DynamoDB encrypted with KMS | ≈ $0 |
| **Meet bot** | Recall.ai joins as a signed-in Google account on the invite; its page streams the meeting audio to the voice session | Recall | $0.60/meeting-hour |
| **Website + app** | The owner's four surfaces (§5) | Vercel + FastAPI on one small Fargate task + DynamoDB | ≈ $150–200/month fixed |

### 4.1a The specialists a business gets

Workers stay Hermes specialists (founder ruling); the owner never manages them. **Only two are seeded** (founder, 7 Nov); the rest are available from the catalogue but not created until someone needs them.

| Specialist | Does | Shows up as |
|---|---|---|
| **Nova** (the owner's chosen name) | the voice itself: the Router persona on the phone, in the app, in meetings. Not a Hermes agent — the Gemini session with the business's Knowledge and the six tools | the one everyone talks to |
| **Front desk** (seeded; today's Customer Operations) | everything customer-facing after the call: the booking write-back into Fresha/GlossGenius/Booksy through the browser, callbacks, texts, messages, "didn't know" → Knowledge, reminders | "Front desk · wrote the booking to Fresha, texted Maya" |
| **Office** (seeded) | everything that comes out of meetings and owner-mode asks: drafts, emails, orders, posts, research — under the approval mode | "Office · drafted the holiday-hours post" |
| Catalogue (not seeded): Growth, Bookkeeping, … | added when a business asks for it | — |

Profiles live in S3 per business as today and are restored into each per-task session; nothing about them needs a per-business VM.

### 4.1b What the Router does itself, what goes to Workers, and where Hermes is

What the DeepSeek article actually says (our notes, `super-agent-design/evidence/research_notes/…/deepseek.md`): "one continuous identity fronts a **lightweight, persistent Router** and **isolated task Workers**"; "deterministic admission precedes semantic assessment"; "async tools permit interruption; output fits each channel"; in the prototype "the main session handles routing; ordinary progress stays internal… a Router can still have Bash, **with guards limiting waits**"; Workers "lack direct presentation" — the one identity speaks, Workers do not post to the human. The Router is allowed to act directly for small things as long as it never blocks; the Workers do the heavy, isolated work.

That is the shape here, and **the model decides within it** — we do not hand the Router a fixed list of six things:

1. **Reads** — answer from Knowledge, memory, the calls and meetings data, calendar availability. "How many no-shows last week?", "is Saturday 2:30 free?", "what did we decide about holiday hours?" The Router does these itself, in well under a second, through backend tools on the Gemini session.
2. **Bounded writes** — book, move, cancel, waitlist, take a message, transfer to a person, remember ("next time, parking is behind the building"). Small, reversible or approval-gated, done by the Router itself through the same backend tools; "guards limiting waits" = each returns in under a second or the Router moves on.
3. **`start_work`** — anything open-ended: "draft the post", "order the colours", "write this booking into Fresha", "find out whether…". A specialist (Hermes, the general runtime: browser, CLIs, MCP, OpenCLI adapters, `gog`, memory) does it in its own session, chooses its own tools, and the result lands back in the conversation or after it. The Router never waits on it.

**The "six tools" are what a caller can reach** — the contact boundary (the Agent-page checkboxes, enforced in `effect_check`) — not the Router's ceiling. In a meeting or in owner mode the same Router has the full set. The adapters behind the bounded writes are decided at onboarding step 3 (Google Calendar / Square / Calendly / Zenoti by API during the call; Fresha, GlossGenius, Booksy, Vagaro by a hold in Nova's calendar during the call and a Front-desk write-back after).

**Where Hermes is.** Today's chat Router *is* a Hermes turn (the CEO profile) that emits decisions; the voice Router is the Gemini session (5 Nov ruling) because a caller needs an answer in under a second and a Hermes turn takes seconds. Workers are Hermes task sessions, unchanged. So we remain a general agent runtime: everything open-ended runs on it; only the live conversation itself does not. This is the same division the old Meet build lacked — there, the voice was a mouth and every line went through a full Hermes run, which is where the 10–20 s answers and the floor problems came from.

**The meeting seat's reads must be substantial (founder, 7 Nov): the wedge against Memoir is live information in the meeting.** So the Router's read surface is not "Knowledge and the calendar"; it is everything we can answer by an API call in under a couple of seconds, growing with each connection the business makes:

| Read | Source | Day one? |
|---|---|---|
| Business facts, policies, FAQ, instructions | Knowledge | yes |
| What was said and decided in earlier meetings; what a caller asked last month | memory (meetings, calls, decisions kept — unlike Memoir) | yes |
| Calls, bookings, no-shows, minutes — counts and lists over any range | the call/meeting records | yes |
| Calendar: who is free when, what is booked | Google Calendar / Square / the business's system (API) | yes |
| Email and documents: "what did the supplier say?", "what's in the holiday-hours doc?" | Gmail / Drive / Docs search through the existing Google connection (`gog`) | yes |
| **Every integration the product already has** — the business's numbers, customers, tickets, code, ads, social, books: Stripe, Google (Gmail, Calendar, Drive, Docs), Microsoft 365, Notion, Linear, GitHub, Sentry, PostHog, Attio, Intercom, Apollo, Shopify, QuickBooks, Meta Ads, Instagram, X, Cloudflare, Vercel, AWS, Parallel (`providers.yaml` on origin/main), Slack (the Slack app), plus whatever MCP servers and OpenCLI adapters the workspace has set up | the same credentials the workspace already holds (moving to the AgentCore Identity vault), read through each provider's API as a Router read tool; one read tool per provider, generated from the provider catalogue, not hand-written per question | **day one for every provider the workspace has connected** (founder, 7 Nov) |
| The web: "what does Fresha charge for…", "is that supplier open on Sundays?" | web search | yes |
| Anything that needs a computer: a draft, a comparison that takes browsing, a calculation over files | the **Office specialist pre-warmed for the meeting** (a Hermes session alive for its duration) — result back into the meeting, as speech if short, as a page in Meet chat if not | yes |

The rule: **every connection the product has today is a read source on day one**, through a generic per-provider read tool (search / get / list over that provider's API with the workspace's credential) rather than a hand-written tool per question; any read that can be an API call is the Router's; anything else is a Worker, warm during the meeting. Adding a provider to the product adds it to the meeting seat with no extra work. The acceptance test for the meeting seat is a list of live asks that must be answered in the meeting, not after it: "Nova, what did we decide about holiday hours last week?", "how many bookings this month versus last?", "what did the supplier's email say?", "what does our website say about group bookings?", "pull October's revenue", "draft the post and put the link in chat".

**Two things are strict, nothing else is:** the Router never blocks the conversation on a Worker, and callers never reach past the bounded set.

**What "browser write-back" means.** When the business's booking system has no API (Fresha, GlossGenius, Booksy, Vagaro), the Front-desk specialist opens a browser session (AgentCore Browser), signs into the business's own dashboard with the login the owner gave at onboarding — injected by the bridge, never shown to the model as text — and creates the appointment by clicking through the screens, then confirms it matches the hold Nova made on the call. It happens after the call because it takes 20–60 seconds; the caller never waits on it. Each browser task is its own microVM, scoped to that one site, destroyed when done, and leaves its step-by-step and screenshots in the call record; what it may do on the site runs under the approval modes (book/move = Auto; pay, change settings, export, email outside = Ask).

### 4.2 One architecture, two seats

- **In a meeting Lightbulb sits *with* you.** The people talking are members. It is addressed by name, answers from Knowledge or by handing a question to a Worker that is warm for the meeting's duration, and action items become Follow-ups.
- **On the phone Lightbulb sits *as* you, facing strangers.** The caller is a **contact**, not a user: no account, no rights. A contact may book, move, cancel, join the waitlist, ask hours and prices, leave a message. A contact may not pay, send outside email, change prices, or hear about other customers. That rule lives in `effect_check` on the server, not in a prompt. Anything beyond the six tools goes by `start_work` to the Customer-questions specialist.

Same Router, same Workers, same Knowledge, same Needs you. The only new primitive is the contact: identity by phone number, its own memory, and the boundary.

### 4.3 A call, second by second

1. 7:12 pm, salon closed. The call hits Twilio → LiveKit → the voice session opens: "Hi, I'm Lightbulb, the assistant at Maya's Nail Studio. This call is recorded. How can I help?"
2. Caller wants a gel manicure Saturday afternoon. The session calls `check_availability` (the synced calendar: Ana is free 2:30 and 4:00), offers both, caller picks 2:30, `book` writes a hold, the session confirms, asks for a name and whether to text the details. 2 min 40 s.
3. After the call, a task "Book Maya — gel manicure, Sat 2:30, Ana" runs in a fresh Hermes session: if the salon is on Google Calendar the booking is already there; if it is on Fresha, the Worker opens the browser with the vaulted login and creates the appointment, then checks the hold matches. A second task texts the caller. The call appears on the owner's Home: *7:12 pm · (415) 555-0133 · Booked · Sat 2:30 gel with Ana*. If the slot was gone meanwhile, the Worker takes the caller's second choice or texts two alternatives; *Needs you* only if neither works.
4. 7:40 pm: "Can you do a bridal party of six?" — Knowledge has nothing. Lightbulb takes a message and creates a *Needs you — Lightbulb didn't know* card. The owner answers it in the morning; the answer is written into Knowledge; the next bridal call is handled.

### 4.4 A meeting

The owner invites `agents@thelightbulb.company` to the Monday Meet. Lightbulb joins as a signed-in participant (so Google does not park it in the waiting room), listens, and speaks only when addressed: "Lightbulb, how many no-shows last week?" — answered from the calls data. "Lightbulb, draft the holiday-hours post" — handed to a warm Worker, back in the meeting in seconds. Afterwards the action items become Follow-ups and the organizer gets the notes.

### 4.4a Approvals are verbal too

A Needs-you card is resolved by tapping, or by saying it: "Nova, yes, order them" from a **member** — in the meeting, in owner mode on the phone, or in the app — approves the same way the button does (same `effect_check`, same 10-second undo for anything sent or spent). From a guest or a caller it is just words: the card stays, and Nova says so ("I'll need Maya's OK for that"). Who may approve is the member list and the approval mode, never who is loudest in the room.

### 4.4b What the earlier voice tests taught us (carried into this design)

We tested multi-party voice in Live and Meet through October and November. One voice removes the worst of it; these are the things to build and test against, not rediscover:

1. **Several voices answered talk meant for others and took over the call** (12 Oct — why every voice became a "mouth"). Now: one voice, addressed by name, with Gemini's native decide-not-to-respond mode *and* the existing wake rules kept as a second gate; replay the 17 scenarios in `tests/replay/` with the new model before any live meeting.
2. **The agent left meetings because the room looked empty for 3 seconds** while Recall reconnected and re-seated participants (`EMPTY_ROOM_GRACE=3`, `huddle.py:69`; `meet_bridge.py:113`). Now: leave only on Recall's own "bot left / call ended" webhook, never on an empty-room timer.
3. **A voice run had no time budget**; nothing stopped one that went on (`voice_calls.py:270-274`). Now: every `start_work` from a call or meeting is a task with the standard budget and a visible Working row; the voice session never waits on it.
4. **Held answers never expired**, so it spoke stale answers minutes later (`NEWS_TTL` covered Live only; meetings kept unsaid answers on the META). Now: an answer not spoken within 20 s of its question is dropped and posted to chat instead.
5. **Asking the same thing twice created a new job each time** (`_same_ask` word-overlap heuristic, `voice_calls.py:1055`). Now: tasks are de-duplicated by the Router's task id (the existing idempotency keys) and the tile says "already on it".
6. **Nothing was recorded** — transcripts only in CloudWatch (`record=False`, `agent.py:536`). Now: transcript, recording and the step-by-step are stored per call and meeting; guests' opt-outs honoured per audio stream.
7. **Voice bypassed tasks, approvals and memory.** Now: voice is a Router entry point like any other — tools → tasks → approvals → memory.
8. **Google parks bots in a denied-by-default queue** (since 24 Mar 2026). Now: the bot is a signed-in Google account on the invite (Recall Google Login Groups).

Rules from the current engine worth keeping as they are: wait for a real 3 s pause before speaking (`MEET_QUIET_S`); a question nobody was named in is picked up only if unclaimed for 3.5 s; leave after 60 min of silence; names in a wake position open the agent. The replay rig is the regression suite for all of this and runs on every voice change.

### 4.5 The owner talking to Lightbulb

There is no composer. The owner calls their own Lightbulb number: caller ID plus a PIN (caller ID can be spoofed) puts them in **owner mode** — "Move Priya's 11 to 11:30 and text her" — the Claude-Projects conversation, by voice, with full authority. Or they talk to it in a Meet. The website is for reading and settings **(pending: whether a press-to-talk voice session in the browser/app is wanted too — 15/16)**.

### 4.6 What is shared and what is per business ("pool tenancy")

Shared: the runtime per tier, the LiveKit project, the backend, the databases. Per business: its Router session, its Hermes sessions (profile restored into each one), its Knowledge and memory namespaces, its rules, approvals, billing, number and calendar. Each session is its own microVM; nobody shares a running process. What is gone is the idle VM per business and the per-business provisioning (role, secret, runtime, roll).

### 4.7 Money

Fixed ≈ $200–275/month (AWS floor + LiveKit Ship + Clerk + Vercel). Per business ≈ $5 AWS + ≈ $16.6 voice/Meet pass-through at 300 call-minutes and 2 meeting-hours + ≈ $1–5 tokens and memory. At $250/month per business that is ~90% gross margin on the platform. Today: $1,983/month with $0 revenue.

## 5. The owner's experience (approved on mockups, 7 Nov — `pivot-research-2026-11-07/.superpowers/brainstorm/…/content/`)

**Shape.** The Claude-Projects shape, voice-only: the project is the business's assistant (named by the owner — "Nova" in the mockups; "Lightbulb" is only the product); conversations are spoken and come through three doors (the app, the phone line, the Meet); Requests are Follow-ups; Needs you is Needs you; memory is Knowledge. Four rail items on web, four tabs on the phone: **Home · Needs you · Agent · Settings**. No composer: the composer is your voice.

**Home (web and phone) — "Lightbulb-first".** Top: hold-to-talk to Nova ("what happened today?", "move Priya's 11 to 11:30 and text her", "draft the holiday-hours post"). This is the existing in-app Live session re-pointed at the Router model, authenticated by Clerk — owner mode with full authority. Then the one Needs-you line. Then **one timeline of every conversation Nova had**, grouped by day: your own talks (🗣), customer calls (☎), meetings (🎥). Filter chips: All · Calls · Meetings · Yours · Needs you. The foot: Answered · Booked · Working · Done · minutes used of the allowance. On the phone the orb is the app.

**Every call, and what it did — in the row.** A row = time · who (name or number, "returning" / "new caller") · outcome tag (Booked, Moved, Cancelled, Message, Answered, Needs you, Spam) · **one plain sentence of what Nova did** ("Gel manicure, Sat 14 Nov 2:30 with Ana · put in Google Calendar · texted confirmation"; "Cancelled Fri 3:00 · slot freed · waitlist texted (2)"; "Didn't know the group policy → took a message") · duration; Call back / Text back on rows that need them. The pane: outcome + booking card; **What Nova did, step by step with timestamps** (answered → checked availability → booked → told her about parking, flagged *guessed — not in Knowledge* → texted); *What Nova remembers about this caller* with Forget; transcript; recording; Call back / Text back / Mark as spam; the **"Next time…" box** that writes into Knowledge. A meeting row reads the same way ("Asked twice · 3 follow-ups · notes emailed") and its pane lists what Nova was asked and did, the follow-ups with status, Notes / Transcript / Decisions.

**Needs you.** One list, four kinds of card, the action on the card, same cards as pushes on the phone: **Nova didn't know** (type or hold-to-talk the answer → it goes into Knowledge and Nova calls the customer back; or I'll call / Text back); **Needs your OK** (spend, outside email, publish — Approve / Change / No, 10 s undo); **Caller wanted a person** (hand-off missed → message → Call back / Text back / Done); **Line problem** (calendar sign-in expired, forwarding broken, minutes at 80%). Approvals are verbal too (§4.4a). The foot says what did *not* need you. No coloured side-tabs in the build; the kind label carries it.

**Agent page.** One long page, no tabs; changes are live as you make them; Go live once, **Pause** is the brake; **Ring me (test call)** in the header. Sections: *Name, greeting & voice* (greeting leads with the business, then "this is Nova, the salon's AI assistant. This call is recorded." — the two phrases the disclosure and recording laws want; no real person's name, no cloned voice; English + Spanish); *What Nova may do for callers* — Fresha-style checkboxes (book / move & cancel / waitlist / greet returning clients by name / answer prices-hours-services / payments off in v1) = the contact boundary, "anything unchecked or unknown → message + Needs you"; *Knowledge* as Q&A with provenance ("added from a call", "you answered this in Needs you") and **questions to add** (asked but unknown), plus free-text Instructions; *Hours* (answers 24/7, books inside hours, holidays); *Services & staff* (duration, price, who, buffer); *Calendar & booking system* (Google Calendar / Square connected; Fresha etc. "signed in — Nova writes each booking there after the call using your login"); *Number & forwarding* (Nova's number, your line forwards here, `*73` to stop, port later); *Hand-off to a person* (number, only during opening hours); *Meetings* (invite `agents@thelightbulb.company`; joins, listens, speaks only when someone says "Nova"; guests told, can opt out of transcription; action items → follow-ups; notes by email); *Reminders & follow-ups* (day-before text, text back missed callers, waitlist fills).

**The meeting agent.** Nova joins as a signed-in Google participant on the invite (no waiting room), announces itself and the rules in chat on joining, and **its tile is its status** (Listening · Thinking · Speaking · Drafting · Page ready · Not transcribing Sam). Speaks only when named ("Nova, …"); "Nova, stop" cuts it off. Spoken answers for short things; **drafts and anything longer than a sentence go to Meet chat as a link**. Anything that spends, sends or publishes becomes a Needs-you card for a member (verbal "Nova, yes, order them" from a member approves it; from a guest it is just words). Guests are guests: told it is there, can opt out of transcription per person (that STT stream stops), cannot act. After: the Home row with what Nova was asked and did, follow-ups with status, notes emailed to the organizer (`Notes: <title>`), attendees get nothing unless shared; transcript, decisions and memory are **kept** (unlike Memoir) so next week it can answer "what did we decide about holiday hours?".

**Onboarding.** Five steps, live within 15 minutes, works from a phone: (1) business name + city, your mobile — Nova finds and reads the website and Google listing; (2) check what Nova learned (hours, services with guessed durations flagged, staff, answers to fill in) — these are the Agent page's cards; (3) where bookings go (Google Calendar / Square / sign Nova into Fresha-GlossGenius-Booksy-Vagaro / Nova's own calendar for now; buffer); (4) your number: Nova's local number, forward your line with `*72`, we ring your line to confirm, skip for later; (5) name the assistant (suggestions; not a real staff name), greeting prefilled and editable, voice preview + Spanish, **Ring me — try a booking**, Go live. After Go live the first three calls each push the row and the step-by-step; week-one unknowns show as "questions to add".

**Settings.** Billing & minutes (minutes used / included, overage per minute, run-out choice: keep answering at overage with 80%/100% emails and a one-tap "send calls to my phone" — never a silent cut-off), Members (multiplayer later: roles for hand-offs, approvals, pause), Connections, Approvals (Ask / Auto / Never ask + the voice-shaped standing rules), Notifications.

**What we deliberately do not build.** A flow canvas or "jobs" editor; credits as a unit; revenue tiles; a Publish button; a Contacts page in v1 (click a number to filter the list; caller memory lives on the call); a Team page in v1; a companion desktop app for meetings (the tile and Meet chat are the live surface).

## 6. Decisions as decided (summary; detail in research `14`)

| Decision | As decided |
|---|---|
| Voice model | Gemini 3.8 Live on Vertex (test first); `gpt-realtime-2.1-mini` one-line fallback; `gpt-live-1` phone A/B after 50 calls |
| Voice host | LiveKit Cloud Ship + Twilio SIP; OpenAI SIP direct only at ~100 businesses if blind transfer is acceptable |
| Workers | Hermes stays; per-task and per-meeting sessions on a pooled AgentCore runtime (v2); no per-owner VMs |
| Worker model | Haiku 5.5 on Bedrock where PHI can flow; today's broker/DeepSeek otherwise |
| Orchestration | Temporal Cloud PAYG, promoted to prod |
| Memory | AgentCore Memory + contact actor + retention rule; CLI engine stays in the Hermes sidecar |
| Credentials | AgentCore Identity for OAuth; DynamoDB+KMS for logins/API keys; ~10 platform secrets |
| Identity / tenancy | Clerk stays; pool tenancy |
| Backend | Stays on Fargate, shrunk to one ARM task (2 for HA), no NAT, CloudFront free plan |
| Meet | Recall + signed-in bot account; v1 session on the LiveKit worker; `RunTask` per meeting later |
| Contact boundary | ~30 lines in `effect_check`; calls route beyond the six tools to a Customer-questions specialist |
| Price | $250/month per business to start |
| Vertical | Salons/barbers/spas on Google Calendar + Square first; dental via NexHealth second, after BAAs |
| Delete | Chat, Paperclip (+ both Aurora clusters, heartbeats), sessions/desktop, marketplace, Apps backend, Stripe Projects, OpenRouter gate, OpenSearch, per-owner runtimes/secrets, 3 NATs, WAF |
| GooseTown | Paused (services to zero); the Lightbulb CLI becomes the GooseTown CLI; coordinated with the GooseTown agent; rebuilt cheaper or decided last |

## 7. Phases (each shippable; dev first; prod after the founder sees dev)

0. **Stop the bleed** (this week): dormant prod runtimes off, nightly heartbeats off in prod, dev Paperclip off, GooseTown services to zero, Activate Founders application.
1. **Ship the super-agent to prod and delete the legacy** — the step we already owe: promote `superAgentEnabled`, prod Temporal namespace, then remove chat UI/routes, Paperclip + Aurora, sessions/desktop, marketplace, Ideas/Feed/Live UI, Apps + Stripe Projects, OpenRouter gate, OpenSearch; infra stacks with them. ≈ −160k lines, ≈ −$1,100/month.
2. **Golden-path infra**: pooled runtime per tier + per-task sessions (v2), AgentCore Identity, memory retention rule, secrets to ~10, no NAT, ARM task, CloudFront.
3. **Phone agent**: Twilio + LiveKit SIP, the listening Gemini session with the six tools, the contact primitive + boundary, call record (a room per call), booking Workers (Google Calendar, Square; browser for the rest), confirmations by text, owner mode by phone.
4. **Website + mobile**: the four surfaces, onboarding, Agent page + Knowledge, Home/Calls + call detail, Needs you, Settings; mobile four tabs; impeccable pass.
5. **Meet v2 (part of the MVP, built alongside 3–4)**: one voice on the Recall page, signed-in bot account, guests under the contact boundary, action items → tasks, Meetings list and meeting pane.
6. **Billing + go-to-market**: $250/month, minutes display, Metronome per-minute; salons first.
7. **Dental**: NexHealth integration and the first practice — the BAAs, zero-retention and the HIPAA-tier runtime are started in Phase 2 ("medical from day one"), not here.

Each phase gets its own plan (writing-plans) and is built by subagents with review before merge.

## 8. Left to the builder

Exact tool schemas for the six voice tools; the Gemini plugin's session-resumption handling; the call-record META shape (mirror `META.meeting`); the contact policy's exact allow-list; the memory retention rule's thresholds; the shrink of the backend's 11 background loops; the order of stack deletions; test plans per phase (replay scenarios for proactive audio; 50-call A/B).

## 9. Builder check (7 Nov) — what it asked, and the answers

A fresh reviewer read this document as a builder. Its questions, sorted:

**Answered from the conversation / the code (now in the document):**
- *What keeps working through Phase 1?* Rooms, tasks, memory, network and the A2A door are **data and routes that stay** — the super-agent's projects are rooms, `voice_start` needs a room, a call is a room. What goes in Phase 1 is the legacy path (Paperclip + its DB, the pre-super-agent branches) and the comms *features* (channels, threads, reactions, mentions, pins, composer formatting, Ideas/Feed, Live-room UI, marketplace, sessions/desktop). The routes the Lightbulb CLI and A2A use (`/messaging/rooms*`, `/local-sessions/*`, `/memory/*`, `/sessions`, `/network/*`, `routers/a2a.py`) are kept until the CLI's future is decided with the GooseTown agent. The founder's own workspace (Friday, rooms, tasks through the `lightbulb` MCP tools) keeps working.

**Settled by the lead (marked as the lead's call; overturnable):**
- *Which channel sends texts?* **SMS from Nova's own Twilio number** (the number the caller called), registered once under a Lightbulb ISV 10DLC campaign with a low-volume brand per business created during onboarding; the Photon iMessage line stays an optional add-on, not the default, so the $25–30/business cost model holds. To verify in the Phase 3 plan: Twilio's current ISV/sole-proprietor brand path and lead time.
- *Availability on platforms with no API and no calendar sync?* **Nova's own calendar is the truth for `check_availability`; the platform is written after the call.** For Fresha/GlossGenius/Vagaro, Google Calendar sync being on is a v1 onboarding condition (checked at step 3); Booksy (no sync) is labelled "beta — Nova keeps its own calendar" and a conflict found at write-back becomes a Needs-you card with the caller's second choice. No periodic scraping (anti-goal).
- *Beyond the six tools on a live call?* **A Front-desk specialist session is pre-warmed when a call connects** (a per-call session on the pooled runtime, ≈ $0.01/call at 8 GB × 4 min; ends with the call), so a question beyond Knowledge can be answered in seconds; if it is not ready in time, Nova says "I'll text you that" and the task runs after. Meetings already get a warm session for their duration.
- *Minutes at $250/month?* **1,500 minutes included, $0.35/minute overage** (research 15 §E.3); cheap to change.

**The founder's (asked in chat, 7 Nov):**
1. Keep the existing chat UI in prod as the manager surface until Phase 4 replaces it, deleting only the legacy path and the comms features in Phase 1 — or delete the chat UI in Phase 1 and accept weeks with no usable prod surface?
2. Outbound in v1: callbacks the caller asked for, booking confirmations and opt-in day-before reminders in; "text back missed callers who didn't leave a message" and waitlist texts out until a consent review — agree?
3. Google: a calendar-only OAuth client needs Google verification (4–6 weeks, your console account). Start it in Phase 2?
