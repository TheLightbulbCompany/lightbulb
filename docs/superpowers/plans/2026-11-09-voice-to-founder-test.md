# Voice Product to Founder Test: Program Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task. This is the program plan. A task here moves one or more rows of `docs/voice-status.md` one stage. The lead of each track writes the step-level brief for a task when it starts, from the task's entry here plus the spec.

**Goal:** Close the founder's phases 0 to 7: every promise on the status board built, live on prod, and passed by a machine check, so that the founder's own walk (Chrome, the Mac app, a phone) finds a product that already passed the same walk run by an agent.

**Architecture:** Unchanged, and fixed by Part 1. A GPT-Live voice session talks and listens; it hands every business request to the one Router, which starts Hermes Workers on the shared pool; approvals are card taps. This plan adds no architecture. It adds a board, a stage discipline and an order of work.

**Tech stack:** FastAPI on ARM Fargate, Temporal Cloud, DynamoDB and S3, Bedrock AgentCore (shared Hermes pool), OpenAI GPT-Live through LiveKit Cloud, Twilio, Recall.ai, Photon (iMessage), Next.js on Vercel, Tauri Mac app, Expo, Clerk, Stripe and Metronome.

**Spec:** `docs/superpowers/specs/2026-11-07-voice-agents-design.md`, with the master plan `2026-11-07-voice-agents-master-plan.md`, as amended by the founder decisions in Part 1. Where they differ, Part 1 wins. This plan does not replace the master plan: it is the master plan's remaining work, row by row.

**Board:** `docs/voice-status.md`. Row ids here (3.7, 5.12) are board rows.

**Coverage:** on 9 Nov three read-only audits compared the board with the spec, the end-state UX plan and the phase plans. The first board covered about 16 of roughly 80 master-plan items. The board was rebuilt by phase to hold every promise found. Rows marked `?` have not been checked against code or prod yet.

## Global constraints

- One business is one workspace.
- No per-business compute, secret or process while nothing is happening.
- First words of every call contain the assistant's name, "AI assistant" and "recorded". The wording is the model's own.
- No outbound contact beyond consented callbacks, confirmations and opt-in reminders. No card capture.
- Approvals are authenticated card taps. A spoken "yes" is a message.
- A caller or texter gets the business profile and their own bookings and messages. Never owner credentials or team memory.
- No migration code, no backfills, no compatibility layers: fix forward. There are no real customers.
- No hand-written speech rules: no canned lines, no word lists, no code that decides when the model talks.
- Golden path, never a hack: the vendor's documented way first, with the docs cited in the PR.
- Limit guards and features. No new tools unless a scenario needs them.
- Review and security are not skimped. Phase gates are run (Part 3).
- Shortest diff that works. Reuse what exists. Delete over add.
- A name is not proof that something is dead. Before deleting a resource, prove nothing reads it.
- Never store customer data on this Mac. Never print a secret.
- Test spend per day: Twilio at most $5, Recall at most 3 hours, OpenAI voice at most $5. Anything else that costs money is named and waits for a go.
- UI is driven through Chrome and computer use, never DevTools or CDP automation.
- Prod changes go through `deploy.yml` dispatch, `huddle-agent.yml` dispatch and the Hermes pin and roll. Never by hand.

## Review focus

What the tests will not catch, most likely first:

1. **The voice answers a business question without asking the Router.** A caller hears a confident wrong answer. The rate is unmeasured. (Task 3-A.)
2. **A row is marked tested on an old stack.** Booking passed before GPT-Live replaced the voice layer. Each receipt names the build it ran on.
3. **A merge to main is not on prod.** Three things deploy separately: backend and web, the voice worker, the Hermes image. A row is "Prod: yes" only when every part it needs is live.
4. **A business pays and has no number.** Payment, enrolment, number purchase and SIP routing are four steps with four failure points. (Task 6-C.)
5. **A control on the Agent page saves and does nothing.** The owner believes texts are being sent. (Task 4-B.)
6. **Something live is deleted because of its name.** `paperclip-companies` is the workspace identity table. (Constraint above; Phase 1 tasks.)

---

## Part 1. Decisions already made, and why

Settled. Tasks build on these and do not reopen them. Source: the founder's own words in the build sessions (4 to 9 Nov), with the reason given at the time.

| # | Decision | Why | What it rules out |
|---|---|---|---|
| F1 | Pivot to a voice assistant per business: phone receptionist, Meet agent, in-app orb. "All in on only voice agents." | Low traction for the messaging product. | Chat-product and email features. |
| F2 | Keep the engine: one Router that only decides, toolful Hermes Workers per task on Temporal, DynamoDB as the record. | Already built, and it is the part that does real work. | A bounded voice bot with its own small tool list. |
| F3 | The Router decides only. Facts it cannot see go to a Worker thread. | Accuracy first ("Choose A", 8 Nov). | Giving the Router the full tool set. |
| F4 | GPT-Live is the voice on all three surfaces. It holds no business data and hands off to the same Router typed messages use. | The founder heard "Let me check" twice in the Mac app. Hand-built speaking logic kept producing bugs like that. | Canned lines, classifiers, mute gates, question-type routing in code. |
| F5 | The model owns conversation: when to speak, when to hand off, what to ask. Code enforces permissions, settings, effect safety, identity and duplicate prevention. | "Leave it up to the model and give it the tools." | Fixing a behaviour problem with a keyword rule or a prompt line for one question. |
| F6 | Phone uses the same Router and Workers, under a caller principal. | Accuracy first; measure latency instead of assuming the shape is too slow. | A separate fast path for callers. |
| F7 | Approvals are card taps only. | A voice can be anyone's. | "Yes" approving a spend. |
| F8 | In a meeting everyone acts with the inviting member's authority. Spend, send and publish need that member's tap. | There is no guest and member split in a Google meeting. | A guest authorising work. |
| F9 | One workspace per business: the agent, phone, billing and permission boundary. | Stated 7 and 9 Nov. | A workspace per worker or per call. |
| F10 | Keep the six-agent starter roster. One existing agent is named the receptionist. All are workers. | 9 Nov. Existing names and data are kept. | Reseeding or renaming rosters. |
| F11 | One shared Hermes runtime pool per tier; a session per owner or per task. | Cost must follow use. | Per-business runtimes, roles, secrets. |
| F12 | Paperclip, Aurora, marketplace, Apps and the Stripe Projects rail are deleted. | They belonged to the old product and held the fixed cost. | Rebuilding on them. |
| F13 | OpenSearch goes. | "We aren't a messaging app any more." | A replacement search engine. |
| F14 | OpenRouter stays as the inference and billing gate. | "It's how all inference works and billing for models goes through that." | Deleting the gate, as the spec first said. |
| F15 | Meeting notes are kept in the app only. | Notes are secondary; live answers and finished work matter more. | A notes email. |
| F16 | Meeting follow-ups start only work clearly given to the agent. Human commitments are recorded. | A summary must not invent the owner's authorisation. | Every action item becoming a task. |
| F17 | A Meet session may sleep after five idle minutes and wake on the name. | Bounds the cost of long meetings. | An always-open voice session. |
| F18 | The Mac app stays. The orb does the general agent work the old product did. | The orb is the third use case. | Removing desktop. |
| F19 | Every existing integration is kept as a live-information source. | Nobody reconnects something already connected. | Dropping a connector to simplify. |
| F20 | Booking conflicts are left to the model and the booking tool. | The founder did not want deterministic double-booking logic. | A scheduling rules engine. |
| F21 | Numbers: "keep my number" (forwarded) or "get a new number". Either way a dedicated number is bought at Go live. Porting is later. | 7 Nov. | A shared line for customers. |
| F22 | Missed-caller and waitlist texts stay off until a consent review. | 7 Nov. | Sending them because the code is ready. |
| F23 | Speed: merge without waiting on CI, deploy, test, fix forward. CI was stripped on purpose. | "Implementation is cheap." No customers yet. | Waiting on a pipeline. |
| F24 | Review and security are kept: "Don't skimp on review or security." | Same day as F23. | Reading F23 as "skip the phase gates". |
| F25 | A phase that is built, deployed and machine-verified is "done, acceptance pending". | 9 Nov. | Blocking engineering on the walk. |
| F26 | Real calls and meetings may be tested on prod. | 9 Nov. Dev has no number. | n/a |
| F27 | One plan is sold: the voice agent, presented as the receptionist. | 9 Nov. It is what customers are buying. | Selling the old $50 plan to new businesses. |
| F28 | Voice stays on our OpenAI key. Orb minutes count. | 9 Nov. | n/a |
| F29 | Payment is proven in the Stripe sandbox on dev. | 9 Nov. | A real prod charge as a test. |
| F30 | Start vendor administration now (Twilio and similar). | 9 Nov. | Waiting until code is ready. |
| F31 | iMessage agents are a channel of the product: plain iMessage, not Apple business messaging. | 9 Nov. "The direction we wanted to go in." | Apple Messages for Business registration. |

---

## Part 2. Decisions that need the founder

### Open

**Q1. Minutes or credits?** The founder said "voice + OpenRouter work on a credits model". The built plan is $250 for 1,500 minutes, then $0.35 a minute.
- (a) Minutes stay the unit for voice; text inference draws on credits as today.
- (b) One pool of credits covers both.
Blocks: 6.1, 6.4, 6.5.

**Q2. Who may choose Sign in with ChatGPT?** The founder said "ChatGPT is fine for now as it's only for us."
- (a) Customers can still choose it (the 9 Nov ruling to the Platform lead).
- (b) Internal only for now; customers are always on credits.
Blocks: 4.9.

**Q3. Price and metering for iMessage.** A Photon line costs $250 a month. Today it is a flat "Front desk" add-on at the same price.
- (a) Included in the plan for every business (doubles our cost per business).
- (b) An add-on a business turns on.
- (c) One shared Lightbulb line that routes by contact. Not designed; needs a contact-to-workspace map.
Blocks: I.8, and how iMessage is built.

**Q4. Phase order.** The master plan says Phase 1 and 2 finish before 3, 4 and 5 start. In practice all are running. Proposal: accept that, and order by the board's **1st** marker so the phone receptionist reaches "sellable" first.

**Q5. Review rounds.** Proposal from the Product lead: one fix round per task, later findings become new rows unless they are money, security or data loss. The Platform lead disagrees and keeps real findings as release blockers. The founder picks.

**Q6. Refunds and failed payments for the voice plan.** No policy exists. Suggested: refund on request in the first 30 days; three failed payments pause the line and take messages only.

### Answered on 9 Nov

Test on prod (F26). Delete the leftovers that are truly dead (see row 1.13). Orb minutes count (F28). One plan (F27). Payment in the dev sandbox (F29). Start vendor admin (F30). No change to the voice design before measuring (task 3-A).

---

## Part 3. Stages, gates and checks

| Stage | Exit test | Evidence |
|---|---|---|
| **Built** | Merged to `main` after a reviewer's PASS. | The PR. |
| **Prod** | Every part the row needs is live. | The backend task definition, the web deploy, the voice worker version, the Hermes image tag on the pool, read from AWS, Vercel and LiveKit. |
| **Machine** | The row's scripted check passes on prod, on those versions. | A receipt under `docs/voice-evidence/<row>/`: versions, script, result. No customer data. |
| **Phase gate** | Code review of the phase's range against `docs/standards/aws-agentic-lens.md`; security review; over-engineering audit where code was deleted. Findings fixed, checks re-run. | The three review reports. |
| **Ready for founder** | Every row in the phase is Machine: yes, the gate has passed, and an agent has done the founder's walk with the founder's tools. | Screenshots and walk notes; the gate-walk document rewritten to match what is live. |

**How each surface is checked**

| Surface | Tool | State today |
|---|---|---|
| Phone | `apps/huddle-agent/tests/evals/sim.py`: simulated callers, each scenario must pass three runs in a row. | Works on dev only, with the worker on this Mac. Needs a mode that scores a real prod call from its call record. |
| Meet | The scenario list in `apps/huddle-agent/tests/replay/scenarios/`, run in a real Meet with two signed-in Chrome profiles. | No simulator exists; the earlier one was removed. How the test speaks into a Meet is unsolved. |
| Web | Claude in Chrome, signed in with a Clerk sign-in token. | Ready. |
| Mac app | Computer use on the installed prod app. | Ready. |
| Billing | Stripe sandbox on dev, then Metronome reads. | Passed once before; re-run. |
| iMessage | A real message to the dev Photon line. | No sandbox exists; one real line is needed. |

---

## Part 4. How the work runs

**Leads.** Two, each the only writer of its rows.
- **Platform lead** (Codex, thread `01a1bcde`): phases 0, 1 and 2, the rows marked Platform, and the single release owner for `deploy.yml`, `huddle-agent.yml` and the Hermes pin and roll.
- **Product lead** (Claude): the rows marked Product in phases 3 to 7 and iMessage, the machine checks, the founder walk.

**Files reserved by the Platform lead until its open work lands:** `voice_booking.py`, `voice_phone.py`, `business_profile.py`, the receptionist messaging and member services, the voice GET route, mobile `AgentSettings`, and `billing_service.change_provider`. Product tasks that need them wait or go through the Platform lead.

**Slots.** Each lead runs four workers. Implementers and auditors are small models. Reviewers of money, permissions, credentials and concurrency are a strong model.

**The loop for one task.**
1. The lead writes a brief: the rows, the files, the exit test, what is out of scope.
2. One implementer in its own worktree under `.claude/worktrees/`, based on `origin/main`. Test first for logic.
3. One fresh reviewer with the diff and the brief.
4. Fixes. (How many rounds: Q5.)
5. Merge. The PR edits the rows.
6. The worktree is removed.

**After every prod train** the Platform lead posts the four live versions. The Product lead runs the checks for every row the train touched and updates the board.

**A failed check** becomes a task at the front of the queue, fixed at its cause in the architecture.

**Disk.** Under 4 GB free and 53 worktrees. Each lead removes the worktrees of its own merged work. No installs in new worktrees until there is room.

---

## Part 5. The remaining work, by phase

Each task lists the board rows it moves. "Check" tasks build nothing: they run a machine check on what is already live and file what fails.

### Set-up (first)

| Task | What | Done when |
|---|---|---|
| 0-A | Merge the board and this plan. | On `main`. |
| 0-B | Rewrite `CLAUDE.md` and regenerate `AGENTS.md`: the voice product first, Part 1 as the standing decisions, dead sections removed, deploy and CI facts corrected. After the founder confirms Part 1, and after #5166 lands (it edits the same files). | A fresh agent asked "what is this product and how do I ship" answers correctly from the file alone. |
| 0-C | Fill every `?` on the board from code and prod. | No `?` remains. |
| 0-D | The test rig: one prod test workspace; a phone check that scores a real prod call; a way to speak into a real Meet; the daily spend check. | One trivial scenario runs on phone, Meet, web and the Mac app. |
| 0-E | Vendor administration, started in Chrome and stopped at anything needing the founder's signature: 10DLC (3.26), Google calendar-only verification (2.19), BAA requests (2.18), the Recall login group (5.2). | Each has a filed date or a named blocker. |

### Phase 0 and 1 (Platform)

| Task | Rows | What | Done when |
|---|---|---|---|
| 1-A | 0.3, 1.3 | Remove the nightly CEO references and the feature switch, after proving nothing reads them. | Guard tests pass; no reference remains. |
| 1-B | 1.5, 1.7 to 1.11 | Remove the dead residue: unused components, comments, `marketplace_safety.py`, the leftover comms and Ideas components. Keep the three live Paperclip-named resources. | An over-engineering audit of the range finds nothing dead. |
| 1-C | 1.12 | OpenSearch: #5156 then #5158. | No collection, endpoint or indexer. |
| 1-D | 1.13 | Retire the five marketplace tables, staged: change the Retain policy, then remove. | AWS lists none. |
| 1-E | 1.14 | Delete the unrouted chat shell (`workspace`, `teams`, `control`), keeping what `/voice/lightbulb` uses. | The web build passes and the Chrome walk still works. |
| 1-F | 1.2, 1.4, 1.15, 1.16 | Check: an old mobile build, the CLI and A2A routes, every connected integration, and any opted-out workspace still work. | One receipt each. |
| 1-G | 0.4, 1.18 | Read the bill: RDS, OpenSearch and Paperclip lines gone. | The figures on the board. |
| 1-H | Gate 1 | Phase 1 code review, security review and over-engineering audit. | Three reports, findings fixed. |

### Phase 2 (Platform)

| Task | Rows | What | Done when |
|---|---|---|---|
| 2-A | 2.1 | Land #5170; prove one tenant cannot reach another's session. | A cross-tenant attempt is refused, with a receipt. |
| 2-B | 2.4, 2.5 | Retire the 25 old runtime definitions and roles by exact allowlist. Move the 28 identity seeds to their replacement first. | AWS lists two runtimes; network identities unchanged. |
| 2-C | 2.6, 2.7 | Verify live credential use from the envelopes; classify the remaining secrets. | The count and the honest target on the board. |
| 2-D | 2.10, 2.11 | ARM to prod; profile idle CPU; decide one task or two. | Prod task definitions read ARM64. |
| 2-E | 2.12, 2.13 | Lightbulb NAT, CloudFront plan, background loops. | A decision with the monthly figure for each. |
| 2-F | 2.2, 2.3, 2.9, 2.16, 2.17, 2.20 | Checks: no prewake, the curator stays cold, a scheduled retention run, the cold-start number, zero retention at each vendor, the Twilio key rotated. | One receipt each. |
| 2-G | 0.2, 0.5, 2.14, 2.15 | After 2-B to 2-E: a quiet 24 hours and the full bill. | Two numbers on the board. |
| 2-H | 2.8 | AgentCore Identity for OAuth. | Existing grants keep working without a reconnect. |
| 2-I | Gate 2 | Phase 2 code review and security review. | Reports, findings fixed. |

### Phase 3: phone (Product, except where marked)

| Task | Rows | What | Done when |
|---|---|---|---|
| 3-A | 3.2, 3.3, 3.4, 3.5 | Check: 20 question calls across five saved facts, and 20 openings. Count missed hand-offs, wrong answers, missed disclosures, and time to a useful answer. Set the latency target from what is measured. | The four rates on the board. If hand-off misses are above zero, the cause is found in the voice-to-Router seam and brought to the founder; no prompt patch. |
| 3-B | 3.6, 3.8, 3.9, 3.12 to 3.15, 3.20 | Check: book, move, cancel; a message; a hand-off that answers and one that does not; returning caller and Forget; a "didn't know" answered and reused; a "next time" note; Pause. | Each scenario passes three runs; the Home row is right each time. |
| 3-C | 3.10, 3.11 | Check: an unticked action is refused by the server; caller B cannot hear caller A's booking; a caller posing as the owner gets nothing more. | Three receipts. Strong reviewer. |
| 3-D | 3.7 | Google Calendar: the Platform lead lands its fixes; then a real booking, move and cancel, and a forced failure. | Each step seen in Calendar; the failure raises a Line-problem card. Needs the founder to reconnect Google. |
| 3-E | 3.16, 3.17 | Make number buying safe: no double purchase on two Go-lives, the line lookup, no orphan when the row write fails. | Three tests that fail before the fix. Then one real purchase. |
| 3-F | 3.18, 3.19 | Check: the forwarding codes for the big carriers; the forwarding check against a real forward from a second number. | The tick appears within 70 s and clears when the forward is removed. |
| 3-G | 3.23 | Staff and holidays limit the slots offered. | A booking outside a holiday or beyond a staff member's time is not offered. |
| 3-H | 3.21 | Store the call recording and play it in the call pane. | A real call's recording plays for the owner and for no one else. |
| 3-I | 3.25 | Texts: confirmations and opt-in reminders from the assistant's number. | After 10DLC clears. Missed-caller and waitlist texts stay off (F22). |
| 3-J | 3.24, 3.28, 3.29, 3.33 | Waitlist, booking sites by browser, owner PIN, and the row actions the UX plan deferred. | Built in the order customers ask. |
| 3-K | 3.22, 3.32, 3.34 | Checks: the per-call warm specialist, the metric and alarm on prod, a Google Voice forward. | One receipt each. |
| 3-L | 3.27, 3.30, 3.31 | Platform lead finishes Square, Spanish and voice preview, and the receptionist choice. | Each on prod with its check. |
| 3-M | 3.35, Gate 3 | The sellable bar: every scenario three runs in a row, 20 real calls from people who did not build it, then the code and security reviews. | The scorecard and the reports. |

### Phase 4: web, Mac, mobile (Product)

| Task | Rows | What | Done when |
|---|---|---|---|
| 4-A | 4.1 to 4.4, 4.8 | Check: a Chrome walk of Home, Needs you, Agent and Settings: every link, the empty states, one error state. | Walk notes with screenshots; failures filed. |
| 4-B | 4.5 | List every control on the Agent page and in onboarding with the code that acts on it. Hide any with none. | No control saves and does nothing. |
| 4-C | 4.6, 4.7 | Check: onboarding on a fresh workspace at phone width, timed. | Under 15 minutes to an answered call. |
| 4-D | 4.10, 4.11, 4.12 | Check on the Mac app: the orb's states and caption, a real piece of agent work by voice, Fn hold and release, an interruption. | Receipts by computer use. |
| 4-E | 4.14 | Mobile: voice settings and billing; old purchase screens removed. | A simulator walk passes. |
| 4-F | 4.15, 4.16 | Push for cards and first calls; the ten-second undo. | Each on prod with its check. |
| 4-G | 4.17, Gate 4 | The design critique of the built product, then fixes. | Critique report; material issues closed. |

### Phase 5: Meet (Product, except where marked)

| Task | Rows | What | Done when |
|---|---|---|---|
| 5-A | 5.12, 5.14, 5.20 | Platform lead's train carries Meet chat, follow-ups and minutes to prod. | Four versions posted. |
| 5-B | 5.1, 5.4, 5.5 | Check: a fresh invite from a normal calendar event; a recurring series with one occurrence moved. | One bot per occurrence; one announcement. |
| 5-C | 5.6 to 5.10 | Check in a real Meet with several people: silence when unaddressed, an answer when named, an interruption, sleep and wake, a reconnect. | The scenario list passes. |
| 5-D | 5.11, 5.13 | Check: the spec's six live asks, each answered from the right source, and a drafted page arriving in chat. | Six receipts. |
| 5-E | 5.15 | Check: a guest's spend request becomes the inviter's card; nothing is spent until the tap. | Strong reviewer. |
| 5-F | 5.17, 5.18 | Structured decisions with recall; per-person transcription opt-out. Unpark #5018 and #5009 or rebuild on the current stack. | "What did we decide last week" is answered by voice. |
| 5-G | 5.19, 5.21, Gate 5 | The per-meeting warm specialist; the whole scenario list green; code and security reviews. | Reports. |

### Phase 6: billing and go to market

| Task | Rows | What | Done when |
|---|---|---|---|
| 6-A | 6.3, 6.7, 6.9 | One plan: a new business is offered the voice agent only, presented as the receptionist. Close the old purchase doors on web, mobile and Apple. Remove the leftover add-on from checkout unless Q3 keeps it for iMessage. | A new workspace sees one offer. After Q1. |
| 6-B | 6.1, 6.2, 6.4 | Check in the dev sandbox: pay, entitlement, a 10-minute session on each surface moves the meter by 10. | Receipts, environment stated. |
| 6-C | 6.8 | The first-customer path on prod: a paid workspace, Go live buys a number, a call to it is answered for that business. A forced failure at each step leaves a retry. | One clean run. Needs a prod workspace that counts as paid without a charge. |
| 6-D | 6.5, 6.6 | Notices at 80% and 100%, the run-out choice, cancel, failed payments, refunds per Q6. | Each path exercised in the sandbox. |
| 6-E | 6.10, Gate 6 | Reviews, then the founder sells. | Five paying salons. |

### Phase 7: dental

| Task | Rows | What |
|---|---|---|
| 7-A | 7.1 | NexHealth adapter against its sandbox. |
| 7-B | 7.2 | The medical tier: its own pool, audit logging, model choices safe for health data. |
| 7-C | 7.3, 7.4 | Agreements, then the first practice. Founder and vendors. |

Starts after Phase 3's sellable bar, per the master plan's order.

### iMessage (Product)

The October code is a working rail with the old product's assumptions. The work is to make a texter the same kind of contact as a caller.

| Task | Rows | What | Done when |
|---|---|---|---|
| I-A | I.1 | Check: send one real message to the dev Photon line and follow it to a reply. | Either a reply, or the exact place it stops. |
| I-B | I.2, I.5 | A text thread gets a contact room like a call's, with the caller principal and the same Router path. The line belongs to the receptionist. | A texter can do exactly what a caller can, and no more. Touches reserved files: coordinate with the Platform lead. |
| I-C | I.3 | One memory per contact across calls and texts, respecting Forget. | Call, then text from the same number: the agent knows the name. |
| I-D | I.4, I.6 | Agent page switch; Pause applies; STOP and HELP; group chats detected; attachments read. | Each with a test. |
| I-E | I.7 | Text after a call. Decide the fallback when Photon will not allow a first message. | A confirmation arrives after a booked call. |
| I-F | I.8, I.9 | Price and metering per Q3; delete the Sendblue rail. | |

---

## Part 6. Ready for the founder

A phase is handed over when its rows are Machine: yes, its gate has passed, and an agent has rehearsed the walk. `docs/superpowers/plans/2026-11-07-end-state-gate-walk.md` is rewritten for what is live: each step says what to do, what a pass looks like, and the build it was rehearsed on.

The phone receptionist (Phase 3 rows marked **1st**, with 4.1 to 4.8 and 6.1 to 6.4, 6.8) is the first hand-over. The founder needs a second phone and about 40 minutes.

Anything the founder finds becomes a board row, and its Founder column stays "no" until the fix has passed its machine check again.
