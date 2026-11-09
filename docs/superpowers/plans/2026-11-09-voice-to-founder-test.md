# Voice Product to Founder Test: Program Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task. This is the program plan. A task here is one row of `docs/voice-status.md` moving one stage. The lead of each track writes the step-level brief for a task when it starts, from the task's entry here plus the spec.

**Goal:** Move every capability on the status board to "Machine: yes" on prod, so the founder's own walk (Chrome, the Mac app, a phone) finds a product that already passed the same walk run by an agent.

**Architecture:** Unchanged, and fixed by Part 1. A GPT-Live voice session talks and listens; it hands every business request to the one Router, which starts Hermes Workers on the shared pool; approvals are card taps. This plan adds no architecture. It adds a board, a stage discipline and an order of work.

**Tech stack:** FastAPI on ARM Fargate, Temporal Cloud, DynamoDB and S3, Bedrock AgentCore (shared Hermes pool), OpenAI GPT-Live through LiveKit Cloud, Twilio, Recall.ai, Next.js on Vercel, Tauri Mac app, Expo, Clerk, Stripe and Metronome.

**Spec:** `docs/superpowers/specs/2026-11-07-voice-agents-design.md`, as amended by the founder decisions in Part 1. Where they differ, Part 1 wins.

**Board:** `docs/voice-status.md`. Row ids (S1, M4, P5, L1) in this plan are board rows.

## Global constraints

- One business is one workspace.
- No per-business compute, secret or process while nothing is happening.
- First words of every call: the assistant's name, "AI assistant", "this call is recorded".
- No outbound contact beyond consented callbacks, confirmations and opt-in reminders. No card capture.
- Approvals are authenticated card taps. A spoken "yes" is a message.
- A caller gets the business profile and the caller's own bookings and messages. Never owner credentials or team memory.
- No migration code, no backfills, no compatibility layers: fix forward. There are no real customers.
- No hand-written speech rules: no canned lines, no word lists, no code that decides when the model talks.
- Shortest diff that works (ponytail). Reuse what exists. Delete over add.
- Never store customer data on this Mac. Never print a secret.
- Test spend per day: Twilio at most $5, Recall at most 3 hours. Anything else that costs money is named and waits for a go.
- UI is driven through Chrome and computer use, never DevTools or CDP automation.
- Prod changes go through `deploy.yml` dispatch, `huddle-agent.yml` dispatch and `roll-hermes-runtime.yml`. Never by hand.

## Review focus

What the tests will not catch, most likely first:

1. **The model answers a business question from its own head.** GPT-Live is not guaranteed to hand off. A caller hears a confident wrong answer. (Decision A1 and task W2-1.)
2. **A row is marked tested on an old stack.** Booking passed before GPT-Live replaced the voice layer. A pass is valid only for the build it ran on; each receipt names the backend task definition, the voice worker version and the Hermes image tag.
3. **A merge to main is not on prod.** Three things deploy separately: the backend and web (`deploy.yml`), the voice worker (`huddle-agent.yml`), the Hermes image (pin, then roll). A row is "Prod: yes" only when all the parts it needs are live.
4. **A paid checkout succeeds but the business has no number.** Payment, enrolment, number purchase and SIP routing are four steps with four failure points. (Task W2-4.)
5. **A control on the Agent page saves and does nothing.** The owner believes texts are being sent. (Task W1-6.)

---

## Part 1. Decisions already made, and why

Everything below is settled. Tasks build on it and do not reopen it. Source: the founder decision ledger recovered from the build sessions (4 to 9 Nov), with the reason recorded at the time.

| # | Decision | Why it was made | What it rules out |
|---|---|---|---|
| F1 | Pivot to a voice assistant per business: phone receptionist, Meet agent, in-app orb. | Low traction for the messaging product (7 Nov). | Building chat-product features. |
| F2 | Keep the super-agent engine: one Router that only decides, toolful Hermes Workers per task on Temporal, DynamoDB as the record. | It was already built and it is the part that does real work. Paperclip could then be retired. | A bounded voice bot with its own small tool list. |
| F3 | The Router decides only. Facts it cannot see go to a Worker thread. | Accuracy first. A coordinator that also executes was tried as a proposal and rejected (8 Nov, "Choose A"). | Giving the Router the full tool set. |
| F4 | GPT-Live is the voice layer on all three surfaces and delegates to the same Router that typed messages use. | The founder heard "Let me check" twice in the Mac app. Hand-built speaking logic kept producing bugs like that, so it was replaced with the vendor's documented shape. | Canned lines, classifiers, mute gates, question-type routing in code. |
| F5 | The model owns conversation: when to speak, when to hand off, what to ask. Code enforces permissions, settings, effect safety, identity and duplicate prevention. | Same reason as F4. "Leave it up to the model and give it the tools" (9 Nov). | Fixing a behaviour problem with a keyword rule. |
| F6 | Phone uses the same Router and Workers, under a caller principal. | Accuracy first; measure latency instead of assuming the shape is too slow (8 Nov). | A separate fast path for callers with its own knowledge copy. |
| F7 | Approvals are card taps only. | A voice can be anyone's. Supersedes the spec's verbal approval. | "Nova, yes" approving a spend. |
| F8 | In a meeting the agent acts with the authority of the member who invited it. Spend, send and publish need that member's tap. | Guests have no account. | A guest authorising work. |
| F9 | One workspace per business. It is the agent, phone, billing and permission boundary. | Stated 7 Nov and again 9 Nov. | A workspace per worker or per call. |
| F10 | Keep the six-agent starter roster. One existing agent is named the receptionist. All are workers. | Decided 9 Nov; replaces the spec's two-specialist seed. Existing names and data are kept. | Reseeding or renaming rosters. |
| F11 | One shared Hermes runtime pool per tier; a session per owner or per task. | Cost must follow use. A runtime per business cost money while idle. | Per-business runtimes, roles, boot secrets. |
| F12 | Paperclip, Aurora, marketplace, Apps and the Stripe Projects rail are deleted. | They belonged to the old product and held the fixed cost. | Rebuilding on them. |
| F13 | OpenSearch goes. | "We aren't a messaging app any more" (9 Nov). Task and Knowledge recall stay with their own stores. | A replacement search engine. |
| F14 | Customer inference choices are OpenRouter-managed and Sign in with ChatGPT. OpenRouter stays as the inference and billing gate. | "It's how all inference works and billing for models goes through that" (9 Nov). | New BYO-key paths. |
| F15 | Meeting notes are kept in the app only. No organiser email. | Notes are secondary; live answers and finished work matter more (7 Nov, 9 Nov). | An automatic notes mail. |
| F16 | Meeting follow-ups start only work that was clearly given to the agent. Human commitments are recorded, not executed. | A summary must not invent the owner's authorisation (9 Nov). | Turning every action item into a task. |
| F17 | A Meet session may sleep after five idle minutes and wake on the name. | Bounds the cost of long meetings without losing context. | An always-open voice session. |
| F18 | The Mac app stays. The orb must do the general agent work the old product did. | The orb is the third use case, not a phone-settings page (8 Nov). | Removing desktop. |
| F19 | Every existing integration is kept as a live-information source. | Stated 7 Nov. Nobody reconnects something already connected. | Dropping a connector to simplify. |
| F20 | Booking conflicts are left to the model and tool flow. | The founder did not want deterministic double-booking logic (8 Nov). Authorisation and isolation checks still apply. | A scheduling rules engine. |
| F21 | Price: $250 a month, 1,500 minutes, $0.35 a minute after. | Starting price, changeable (7 Nov). | n/a |
| F22 | Speed over gates: merge without waiting on CI, deploy, test, fix forward on prod. CI was stripped on purpose. | "Implementation is cheap." No customers yet. | Waiting on a pipeline. See decision A4 for the cost of this. |
| F23 | A phase that is built, deployed and machine-verified is "done, acceptance pending". The founder walk is its own task. | Stated 9 Nov. | Blocking engineering on the walk. |

---

## Part 2. Decisions that need the founder

Each has a recommendation. Work that does not depend on a decision goes ahead without it.

### Architecture

**A1. May the phone voice session be given the business's public facts at the start of a call?**
Today the voice session holds no business data and must hand off every question (F4). In testing, the model sometimes answered from its own head and invented a business. In Codex's replay, the same model given the saved public facts answered correctly. OpenAI's own contract has a documented channel for this (silent background information at session start). OpenAI gives no guarantee that a model hands off every time.
- **(a) Recommended:** give the phone session the public business profile, the same facts a caller is allowed to hear, through the documented channel. The Router stays the owner of everything else. This changes F4's "no business snapshot" for public facts only.
- (b) Keep no snapshot and accept some invented answers, measured.
- (c) Force every answer through the Router (speech-to-text, Router, text-to-speech). Accurate, but it loses the natural conversation F4 chose.
Blocks: S2.

**A2. Does "only OpenRouter and ChatGPT" (F14) also apply to the voice connection?**
The voice session connects to OpenAI directly on our key. That cost sits inside the $250.
- **Recommended:** no. F14 governs the Workers' text inference. Voice stays on our key.
Blocks: nothing if the answer is no.

**A3. Is prod the test environment for real calls and meetings?**
There is no deployed voice worker for dev. The phone harness works on dev only by running the worker on this Mac with simulated callers. A real phone call through Twilio, and a real Meet, can only run against prod; dev has no phone number at all.
- **Recommended:** yes. Iterate on dev with the harness; the check that sets "Machine: yes" is a real call or meeting on prod, in a dedicated test workspace, while there are no customers. Revisit before the first paying business goes live.
- Alternative: deploy a dev voice agent and buy a dev number. More cost, slower loop.
Blocks: how every Sell machine check is run.

**A4. Keep pull-request testing stripped?**
A PR now gets a secret scan and an ARM image smoke test. No backend or web tests run before merge (F22). The only check is the agent's local run and one reviewer.
- **Recommended:** keep it stripped, and make the scripted prod checks in this plan the real gate. Add nothing to PRs.
- Alternative: one non-blocking job that runs only the tests for touched folders.
Blocks: nothing. It is here so the risk is a choice.

**A5. Delete the leftover tables?**
Seven DynamoDB tables for deleted features are still deployed: five marketplace, `provisioning-workflows`, `paperclip-companies`. Deletion cannot be undone.
- **Recommended:** delete after one check that no code reads them.
Blocks: P-cleanup only.

### Product and money

**B1. What does a new business buy?**
Today sign-up gives the old $50 Team plan and a 30-day free trial. The $250 voice plan is a second, opt-in contract an admin must find. Both can be live on one workspace.
- **Recommended:** one plan. A new workspace is offered the $250 voice plan only. The Team plan and seat price stay for existing rows but are not sold. A number is bought only after payment (as built).
Blocks: S8, S9.

**B2. The "Front desk" add-on.** A leftover add-on price exists from the text-agents work; its config says $250 and its description says $100.
- **Recommended:** remove it from checkout.

**B3. Do the owner's own orb minutes count against the 1,500?**
Phone and Meet minutes are metered. The audit found no meter call on the orb route (unverified).
- **Recommended:** count them. One meter, and the orb has real cost.

**B4. One real charge to prove payment.** Proving S9 and S6 needs a real $250 checkout and a real number purchase ($1.15 a month) on prod, then a refund.
- **Recommended:** yes, on the founder's card, refunded the same day.

**B5. Start 10DLC registration now?** Texts (confirmations, reminders) cannot be sent until the carrier registration clears. It is the longest wait in the plan and it is not engineering time.
- **Recommended:** start now. Twilio names the fee at registration.

**B6. Mobile for the first customers.** Mobile has the four tabs, but settings and billing are the old app.
- **Recommended:** Later. For now hide the old purchase screens on mobile and send setup to the web.

**B7. Refunds and failed payments for the voice plan.** No policy exists.
- **Recommended:** refund on request in the first 30 days; three failed payments pause the line and take messages only.

---

## Part 3. Stages and what moves a row

| Stage | Exit test | Evidence kept |
|---|---|---|
| **Built** | Merged to `main` after one reviewer's PASS. | The PR. |
| **Prod** | Every part the row needs is live: backend task definition, web deploy, voice worker version, Hermes image tag on the pool. | The four versions, read from AWS, Vercel and LiveKit. Never from the pin file. |
| **Machine** | The row's scripted check passes on prod, on those versions. | A receipt file under `docs/voice-evidence/<row>/` with the versions, the script, and the result. No customer data, no audio of real people. |
| **Ready for founder** | Every Sell row is Machine: yes, and an agent has done the founder's walk with the founder's tools (Chrome, the Mac app by computer use, a real phone call). | Screenshots and the walk notes. |

**How each kind of row is machine-tested**

| Surface | Tool | Notes |
|---|---|---|
| Phone | `apps/huddle-agent/tests/evals/sim.py`: simulated callers, scored per scenario, pass three runs in a row. Today it seeds two test lines in the **dev** table and runs the worker on this Mac. | Used on dev to iterate. The prod check is a real call through Twilio to the prod number, scored the same way from the call record. |
| Meet | No simulator exists in the tree: the earlier one (#5006) was removed and only the scenario list remains (`apps/huddle-agent/tests/replay/scenarios/`). The check is a real Meet with two signed-in Chrome profiles working through that list. | How the test speaks into a Meet is not solved yet. Task W0-5 picks the cheapest way and says so; it does not rebuild a simulator. |
| Web | Claude in Chrome, signed in with a Clerk sign-in token. | Same clicks the founder will make. |
| Mac app | Computer use on the installed prod app. | Check the screen is unlocked before input. |
| Billing | Stripe and Metronome reads after a real checkout. | See B4. |

---

## Part 4. How the work runs

**Leads.** Two, split by track, each the only writer of its board rows.
- **Sell lead** (Claude): tracks S and M, the machine checks, the founder walk.
- **Platform lead** (Codex, thread `01a1bcde`): track P, and the PRs it already has open. It stays the single release owner for `deploy.yml`.

**Slots.** Each lead runs four workers at a time. Implementers and auditors are small models. Reviewers of money, permissions, credentials and concurrency are a strong model.

**The loop for one task.**
1. The lead writes a brief: the row, the files, the exit test, what is out of scope.
2. One implementer in its own worktree under `.claude/worktrees/`, based on `origin/main`. Test first for logic.
3. One fresh reviewer with the diff and the brief, not the implementer's story.
4. One fix round. A second-round finding becomes a new board row unless it is money, security or data loss.
5. Merge. The PR edits the row to Built: yes.
6. The worktree is removed.

**Trains.** Two prod trains a day, at fixed times. Each train is: `deploy.yml` dispatch, `huddle-agent.yml` dispatch if the worker changed, Hermes pin and roll if the image changed. After the train, the Sell lead reads the four live versions, runs the machine checks for every row the train touched, and updates the board.

**A failed check** becomes a task at the front of the queue. It is fixed at its cause in the architecture (F4, F5): never with a prompt line for one question or a rule for one phrase.

**Disk.** The Mac has under 4 GB free and 53 worktrees. No installs in new worktrees until old ones are removed (task W0-4).

---

## Part 5. The work

Tier: **S** small model, **M** mid, **L** strong reviewer needed.

### Wave 0. Set up the framework (today)

| Task | What | Done when | Tier |
|---|---|---|---|
| W0-1 | Merge the board (`docs/voice-status.md`) and this plan. | On `main`. | S |
| W0-2 | Rewrite `CLAUDE.md` and regenerate `AGENTS.md` from the five code audits: voice product first, Part 1 as the standing decisions, dead sections (Paperclip, marketplace, Stripe Projects, the chat shell) removed, CI and deploy facts corrected. | A fresh agent asked "what is this product and how do I ship" answers correctly from the file alone. | M |
| W0-3 | Send the Platform lead the split, the board and Part 1. Its gitignored ledger is copied into board rows; nothing new is recorded outside the repo. | Codex confirms and edits a row. | S |
| W0-4 | Remove merged and abandoned worktrees. Each removal is listed for the founder first. | At least 20 GB free. | S |
| W0-5 | Stand up the test rig: one prod test workspace; the phone harness scoring a real prod call from its call record; a way to speak into a real Meet; a Chrome sign-in recipe; the daily spend check. | One trivial scenario runs end to end on phone, Meet, web and the Mac app. | M |
| W0-6 | Prod train 1: ship what is on `main` and not on prod (ARM backend, Meet chat image and its roll, provider choice, Spanish core). | Four live versions recorded on the board. | Platform |

### Wave 1. Test what is already live (no new features)

These rows are Built and Prod. The work is the check, then fixing what it finds.

| Task | Row | Check on prod | Tier |
|---|---|---|---|
| W1-1 | S1 | Three calls. The first utterance contains the name, "AI assistant" and "recorded". The call row closes within 5 s of hang-up. | S |
| W1-2 | S3 | One caller books, moves, then cancels. The audio and the stored booking agree at each step. Zero bookings left at the end. | M |
| W1-3 | S5 | A message is taken and appears in Needs you. A transfer reaches an answering number. A transfer to a dead number becomes a "Caller wanted a person" card. | M |
| W1-4 | S10 | With "Book appointments" unticked, a booking request becomes a message (refused by the server, not the prompt). Caller B cannot hear caller A's booking. A caller claiming to be the owner gets nothing more. | L |
| W1-5 | S11 | After W1-1 to W1-4, each Home row shows the right outcome, transcript and steps. | S |
| W1-6 | S12 | Every control on the Agent page and in onboarding is listed with the code that acts on it. Any with none is hidden. First known case: the day-before text. | S |
| W1-7 | M1, M3 | A fresh invite from a normal Google Calendar event: the agent joins, posts one announcement, and notes appear in the app after. | M |
| W1-8 | M2 | Two speakers: unaddressed talk gets silence; a named question gets an answer; an interruption stops it; five idle minutes puts it to sleep and the name wakes it. | M |
| W1-9 | M6 | On the Mac app: hold Fn, ask "what happened today", release. The answer finishes after release. A second press interrupts. | M |
| W1-10 | Web | Chrome walk of Home, Needs you, Agent, Settings: every link, the empty states, one error state. | S |

### Wave 2. Close the Sell gaps

| Task | Row | What | Done when | Depends on | Tier |
|---|---|---|---|---|---|
| W2-1 | S2 | Make a spoken business answer correct. Build per decision A1. Set a latency target for the first useful words (not the acknowledgement) and measure cold and warm. | Twenty question calls across five saved facts: no invented answer, and the target met. | A1 | L |
| W2-2 | S4 | Real Google Calendar booking: create, move, cancel, and the failure path when Google refuses. | The event is seen in Calendar at each step. A forced failure raises a "Line problem" card and keeps the internal hold. | Founder reconnects Google | M |
| W2-3 | S9, B1, B2 | One plan at sign-up; remove the leftover add-on from checkout. | A new workspace sees one offer at $250. | B1, B2 | M |
| W2-4 | S6, S8, S9 | The whole first-customer path on a fresh workspace: sign up, onboarding, pay, Go live buys a number, an inbound call to that number is answered for that business. Time it. | One clean run, under 15 minutes, with the contract, the number and the call row all present. A forced failure at each of the four steps leaves a retry, not a paid business with no line. | B4, W2-3 | L |
| W2-5 | S7 | Forwarding check against a real forward: a second Twilio number forwards to the assistant's number. | The Agent page shows the tick within 70 s, and "not forwarding" when the forward is removed. | B4 (second number) | M |
| W2-6 | S9, B3 | Minutes: phone, Meet and orb all report; the Settings meter matches Metronome. | A 10-minute scripted call moves both by 10. | B3 | M |

### Wave 3. Meetings and the app

| Task | Row | What | Done when | Tier |
|---|---|---|---|---|
| W3-1 | M4 | After train 1, confirm the Meet chat tools on the live image. | In a real Meet the agent reads a chat message and posts a link. | S |
| W3-2 | M5 | Land #5163 (Platform lead has it). | "Lightbulb, draft the holiday post" in a meeting starts one task; "I'll call the supplier" from a person starts none. | Platform |
| W3-3 | M7 | Land #5168, ship, and walk provider choice in Chrome. | Choosing ChatGPT asks for confirmation; cancel leaves the provider unchanged. | Platform |
| W3-4 | M1 | Signed-in bot so nobody has to admit it from the lobby. | A new invite joins with no click. | Founder: Recall login group |

### Wave 4. Platform (Codex, in parallel from day one)

| Task | Row | What | Done when |
|---|---|---|---|
| W4-1 | P4 | Land #5170, the fence that keeps every call on the shared pool. | No invoke reaches an old runtime for 24 h. |
| W4-2 | P5 | Retire the 27 old runtime definitions, their roles and boot secrets, by exact allowlist. Profiles, memory and identity are kept. | AWS lists two runtimes. |
| W4-3 | P7 | OpenSearch: #5156 then #5158. | No collection, no endpoint. |
| W4-4 | P8 | Secrets: follow W4-2 with the boot seeds; classify the rest. | Count and target stated honestly on the board. |
| W4-5 | P10 | Lightbulb NAT and the CloudFront plan. | Decision recorded with the monthly figure. |
| W4-6 | P11, P12 | After W4-1 to W4-5, measure a quiet 24 h and read the bill. | Two numbers on the board. |
| W4-7 | P13 | Confirm one scheduled retention run. | The log line. |
| W4-8 | A5 | Delete the seven leftover tables and the stale Paperclip secret and key wiring. | After decision A5. |
| W4-9 | P9 | AgentCore Identity for OAuth. | Starts only after every Sell row is Machine: yes. |

### Wave 5. Later, in this order, started only when Sell is done

| Order | Row | Why this position |
|---|---|---|
| 1 | L1 texts | Salons expect a confirmation text. Gated on B5, so the registration starts in Wave 0. |
| 2 | L5 staff and holidays | A business with two chairs double-books without it. |
| 3 | L14 minute notices | Needed before the first business reaches its allowance. |
| 4 | L2 recordings | The call says it is recorded; the owner will ask to hear it. |
| 5 | L13 mobile | After B6. |
| 6 | L6, L7 Square and booking sites | Per customer: build the one the next customer uses. |
| 7 | L3, L4, L10, L11, L12, L16, L17 | As asked for. |
| 8 | L18 dental | Its own plan, after the first salons. |

Codex's two open Later PRs (L8 Spanish, L9 receptionist choice) finish, because they are in review. Nothing else in Later starts early.

---

## Part 6. Ready for the founder

The hand-over happens when:

1. Every row in **Sell: one business live on the phone** is Machine: yes.
2. Rows M1, M2, M3 and M6 are Machine: yes.
3. An agent has done the founder's walk on prod with Chrome, the Mac app and a real call, and the screenshots are saved.
4. `docs/superpowers/plans/2026-11-07-end-state-gate-walk.md` is rewritten to match what is live: each step lists what to do, what a pass looks like, and the build it was rehearsed on.

The founder then needs a second phone and about 40 minutes. Anything the founder finds becomes a board row, and the row's Founder column stays "no" until the fix has passed its machine check again.

## Not in this plan

- Selling: the five salons, outreach, pricing tests.
- Dental and medical agreements.
- GooseTown and the CLI.
- Any change to the architecture in Part 1.
