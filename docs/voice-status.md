# Voice product status board

The one place that says what was promised, what is built, what is live, and what has been tested.
It follows the founder's phases 0 to 7 (`docs/superpowers/plans/2026-11-07-voice-agents-master-plan.md`).
If a promise is not a row here, nobody owns it: add the row.

**Sources for the rows:** the design spec (`docs/superpowers/specs/2026-11-07-voice-agents-design.md`), the master plan,
the Phase 1 and Phase 2 plans, the end-state UX plan, the founder goal, and the build sessions' ledger. Three read-only
audits checked this board against those documents on 9 Nov 2026.

**Last state check:** 9 Nov 2026. `origin/main` `42a8f8aa8`. Prod backend task definition 416, dev 966.
Prod voice worker: LiveKit agent `CA_HTFFgrTj24NH`, version `QJcdFMeCBBJF`. Prod Hermes pool image `h-2026.11.9-df8c100`.

## The four states

| State | Means | Who sets it |
|---|---|---|
| **Built** | The code is on `main`. | The PR that merges it |
| **Prod** | Every part it needs is deployed to prod and switched on. | The session that ran the deploy |
| **Machine** | A real call, meeting or scripted run passed **on prod**, on the versions named in the receipt. Unit tests do not count. Payment is the one exception: it is proven in the Stripe sandbox on dev, and the note says so. | The session that ran it |
| **Founder** | The founder walked it on prod and said yes. | The founder |

Values: `yes`, `part` (the note says which part), `no`, `?` (**not checked yet**: do not read it as yes or no), `n/a`.

The **1st** column marks rows a first paying receptionist customer needs. It is an order of attack, not a cut.

## Rules

1. Every PR that changes a capability edits its row in the same PR.
2. One owner per row. **Platform** and **Product** are the two tracks. Since 9 Nov late, Codex is out of usage and the Claude lead holds both.
3. A row moves one state at a time. Built never implies Prod. Prod never implies Machine.
4. A pass is valid only for the build it ran on. The receipt names the backend task definition, the voice worker
   version and the Hermes image tag.
5. A phase is "done, acceptance pending" when every row in it is Machine: yes and its gate rows have passed
   (founder ruling, 9 Nov). The founder walk is then its own task.

---

## Phase 0. Stop the bleed

| # | Promise | Built | Prod | Machine | Owner | Note |
|---|---|---|---|---|---|---|
| 0.1 | GooseTown services at zero; its NAT, load balancer, WAF and DNS removed | yes | yes | yes | Platform | Verified by CloudFormation receipts on both envs |
| 0.2 | Dormant per-business sessions stopped; nothing wakes them | yes | yes | no | Platform | Needs the quiet 24 h (row 2.14) |
| 0.3 | Nightly CEO timer off | yes | ? | no | Platform | Nothing arms or runs a nightly timer. `should_arm_nightly_ceo` is dead code called only by tests; it can be deleted |
| 0.4 | Paperclip off in dev and prod; Aurora gone in both regions | yes | yes | part | Platform | No clusters found. Bill lines not yet confirmed gone |
| 0.5 | 24 h of about 0 GB-hours outside live use | n/a | n/a | no | Platform | Same measurement as 2.14 |
| 0.6 | Activate Founders application sent | n/a | n/a | n/a | Founder | No receipt |

## Phase 1. The engine on prod, the old product out

| # | Promise | Built | Prod | Machine | Owner | Note |
|---|---|---|---|---|---|---|
| 1.1 | Router and Workers on Temporal are the only agent path on prod | yes | yes | yes | Platform | |
| 1.2 | Workspaces that had opted out (`super_agent=False`) are moved, not silently broken | yes | ? | no | Platform | No code branches on the field any more: every workspace is treated as on. Only an audit script reads it. Which prod rows still hold `False` is not checked |
| 1.3 | The feature switch is retired | yes | yes | n/a | Platform | It is a constant (`flags_for` returns True). Left over: the helper file and its callers, which can be deleted |
| 1.4 | Roster served from `agent_registry`; old mobile builds (TestFlight 0.4.8) still work against `/teams/agents` | yes | yes | no | Platform | Master plan review focus 2. Never exercised with an old build |
| 1.5 | Paperclip code removed | part | yes | n/a | Platform | No module remains. Name residue in comments and one unused component (`AgentPaperclipFields.tsx`). **Three Paperclip-named things are live and stay:** the `paperclip-companies` table (the canonical workspace identity row), `PAPERCLIP_SERVICE_TOKEN_KEY` (signs session shares, digests and Needs-you links), and the bridge's run route. A name is not proof something is dead |
| 1.6 | Paperclip stack and Aurora removed | yes | yes | yes | Platform | Guard test `no-paperclip-stack.test.ts` |
| 1.7 | Marketplace removed | part | part | n/a | Platform | Routers and services gone. Five tables and `marketplace_safety.py` remain |
| 1.8 | Apps backend removed, with its secrets | yes | yes | n/a | Platform | Routes gone. No Apps-named secret remains under the dev or prod prefix (names read 9 Nov) |
| 1.9 | Stripe Projects rail removed | part | part | n/a | Platform | Code gone. The `provisioning-workflows` table is still read by `owner_facts` and the wipes; it stays until that coupling is removed |
| 1.10 | Comms features removed (channels, threads, reactions, pins, mentions) | part | part | n/a | Platform | `EmojiPicker`, `RoomMessageSearch`, `RoomLinkPreviews` remain |
| 1.11 | Ideas, Feed, Daily, Brief UI removed | part | part | n/a | Platform | `components/creations` (9 files) and `components/activity` (6) remain |
| 1.12 | OpenSearch removed | part | no | no | Platform | Phase A (#5156, the search API and its consumers) merged 10 Nov. Phase B (#5158, the AWS resources) ships in the train right after; no workspace wipe runs between the two |
| 1.13 | The five marketplace tables retired | part | no | n/a | Platform | Stage 1, the delete policy (#5175), merged 9 Nov. Stage 2 removes the tables; Codex's draft is saved on branch `finish/retire-leftover-table-wiring` and must not deploy before stage 1 is on prod |
| 1.14 | Old chat UI deleted once the voice UI is live | no | no | n/a | Product | `components/workspace` (54 files), `teams` (73), `control` (37) are unrouted. `messaging` (174) is still used by `/voice/lightbulb` |
| 1.15 | Every existing integration kept as a live source; the CLI and A2A routes kept | yes | yes | no | Platform | Founder ruling. Never walked after the deletions |
| 1.16 | Tasks export replaced before deletion | yes | ? | no | Platform | `GET /tasks/export` exists with a test |
| 1.17 | The Lightbulb CLI's future is settled with GooseTown | no | no | n/a | Founder | Master plan open item |
| 1.18 | The bill shows RDS, OpenSearch and Paperclip Fargate lines gone | n/a | n/a | no | Platform | |

## Phase 2. Shared infrastructure, cost, medical-ready

| # | Promise | Built | Prod | Machine | Owner | Note |
|---|---|---|---|---|---|---|
| 2.1 | One shared Hermes runtime pool per tier; a session per owner or per task | yes | yes | part | Platform | Tenant isolation not yet proven. #5170 fences every call to the pool |
| 2.2 | Idle timeout 5 min; typing and new-room prewake deleted; the curator runs only when warm | yes | yes | no | Platform | |
| 2.3 | The curator works without waking a business's session | yes | ? | no | Platform | When the session is cold it returns without an invoke. It then waits for the next turn; a phone-only business is curated only when it next has a turn |
| 2.4 | Old per-business runtimes and roles retired | part | no | no | Platform | 27 old definitions remain (25 prod, 2 dev). The dry-run tool and workflow are built (`retire_legacy_runtimes.py`, `retire-legacy-runtimes.yml`). The allowlist is empty: every owner row still names its old runtime and every runtime was invoked in the last 7 days. Roles are kept. See `docs/ops/2026-11-10-legacy-runtime-inventory.md` |
| 2.5 | Boot identity seeds replaced, then retired | no | no | no | Platform | 28 seeds are KEPT until network identity custody has a replacement |
| 2.6 | Per-business secrets moved to encrypted DynamoDB envelopes | yes | yes | part | Platform | Unit tests and live provider use exist in earlier receipts. Still to do: classify it per connector |
| 2.7 | Secrets Manager under 20 | no | no | no | Platform | About 130 today. 14 platform OAuth client secrets are required and stay |
| 2.8 | AgentCore Identity holds OAuth tokens | no | no | no | Platform | Not started |
| 2.9 | Memory retention (90 days) runs on a schedule | yes | yes | no | Platform | No scheduled-run receipt |
| 2.10 | Backend, workers and Lambdas on ARM | yes | no | no | Platform | Live on dev (#5167) |
| 2.11 | One backend task, or two with a reason; idle CPU profiled | part | part | part | Platform | Prod runs 2 tasks for availability, dev 1. Measured over 24 h: CPU average 9%, 95th percentile about 42%, peak 99%; memory peak 18%. The Platform lead reads this as "keep the resources"; no downsizing claimed |
| 2.12 | No NAT for Lightbulb; CloudFront free plan in place of WAF | no | no | no | Platform | Two NAT instances remain |
| 2.13 | Backend background loops reduced | no | no | no | Platform | Ten loops start at boot: scheduled worker, trial-idle, run-ledger, browser autosave, voice-call, session pick-up, memory, daily digest, needs-you email, running gauges |
| 2.14 | Quiet 24 h: nothing runs for an idle business | n/a | n/a | no | Platform | After 2.2 to 2.4 |
| 2.15 | Fixed cost at or under $250 a month; per-business cost about $25 to $30 | n/a | n/a | no | Platform | Needs a full bill |
| 2.16 | AgentCore cold start measured | no | no | no | Platform | Master plan open item |
| 2.17 | Zero retention set at every speech and model vendor | part | part | no | Platform | Recall is set to zero retention in code. Nothing is set for OpenAI voice, LiveKit or Twilio in code; their account settings are not checked |
| 2.18 | BAA requests filed with dates: Twilio, Google Cloud, LiveKit | n/a | n/a | n/a | Founder | Phase 2 gate. Paid plans |
| 2.19 | Google calendar-only OAuth client created; verification started | no | no | n/a | Founder | Takes 4 to 6 weeks. Not started |
| 2.20 | Twilio API key rotated | ? | ? | n/a | Platform | The founder goal says the key's secret was shown once in a tool result when it was made. Find the key id and date before acting |

## Phase 3. Phone

| # | Promise | Built | Prod | Machine | 1st | Owner | Note |
|---|---|---|---|---|---|---|---|
| 3.1 | Call answered through Twilio, SIP and LiveKit | yes | yes | yes | 1st | Product | |
| 3.2 | The business owns the opening. We ship an editable default (name, business, "AI assistant"). Nothing in code enforces its wording. The assistant never claims to be a person when a caller sincerely asks | no | no | no | 1st | Product | Founder ruling 9 Nov, replacing "every call says AI and recorded". To change: delete the worker's disclosure guard and the backend's greeting fallback; make the default greeting editable; keep one prompt rule about not claiming to be human. The medical tier defaults the AI notice on |
| 3.3 | The voice hands every business question to the Router and speaks its answer | yes | yes | part | 1st | Product | One call invented facts with no hand-off; one later call handed off. Rate not measured |
| 3.4 | Latency: 1.4 s caller-to-reply on simple turns; a stated target for a useful answer | no | no | no | 1st | Product | No target for a useful answer exists yet |
| 3.5 | Knowledge from the website scan answers callers, with where each fact came from | yes | yes | part | 1st | Product | The Router received the right facts in one recovered call |
| 3.6 | Book, move, cancel on the assistant's own calendar; time zone saved at onboarding; overlaps conflict | yes | yes | part | 1st | Product | Passed on the older voice stack. Re-run on GPT-Live |
| 3.7 | Book, move, cancel in Google Calendar; a Google failure keeps the hold and raises a Line-problem card | yes | no | no | 1st | Platform | #5176 merged 10 Nov after four review rounds: safe through slow or lost Google writes, never stuck pending, older bookings read correctly, one Line-problem card per workspace, owner edits in Google no longer block a move. Next prod train. Then the founder reconnects Google for the real check |
| 3.8 | Take a message | yes | yes | no | 1st | Product | |
| 3.9 | Hand off to a person, only during opening hours; a missed hand-off becomes a card | yes | yes | no | 1st | Product | |
| 3.10 | A caller cannot do what the owner unticked: refused by the server, and a message is taken | yes | yes | no | 1st | Product | Master plan review focus 3 |
| 3.11 | A caller gets only the business profile and their own bookings; no owner data; cannot pose as the owner | yes | yes | no | 1st | Product | Unit tests exist |
| 3.12 | Per-caller memory keyed by phone number; "what it remembers" with a working Forget | yes | yes | no | | Product | |
| 3.13 | "Didn't know" becomes a card; the owner's answer goes into Knowledge and the next caller gets it | yes | yes | no | 1st | Product | |
| 3.14 | "Next time…" note on a call reaches later calls | yes | yes | no | | Product | |
| 3.15 | Pause and not-live take messages only; the owner's test call still works before Go live | yes | yes | no | 1st | Product | |
| 3.16 | Each business gets its own number at Go live | yes | yes | no | 1st | Product | Buying is switched on in prod. No number has ever been bought |
| 3.17 | Number buying is safe: no double purchase, no orphaned number, the line lookup works | part | part | no | 1st | Product | A lease stops two Go-lives buying at once, and a receipt lets a retry reuse a bought number. Gap: a number whose final row write fails is kept and paid for with no alert, and the 30-day release of unused numbers has no caller |
| 3.18 | Keep-my-number or new-number choice, with the carrier's forward and stop codes | yes | yes | no | 1st | Product | |
| 3.19 | Forwarding check from the Agent page | yes | yes | no | 1st | Product | The outbound trunk is set in prod. Never run against a real forward |
| 3.20 | Call record: transcript, outcome tag, steps, one-line "what it did", booking card | yes | yes | part | 1st | Product | Home polls every 20 s by design |
| 3.21 | Call recording stored and playable | no | no | no | | Product | Moved to Later by the founder (10 Nov): salons, spas and barbers are served by the transcript, which every call already keeps. Audio adds consent and retention weight for no clear gain. Build when a customer asks |
| 3.22 | Front-desk specialist pre-warmed when a call connects | part | part | no | | Product | A Router prewarm exists. The per-call specialist is not proven |
| 3.23 | Staff and holidays limit the slots offered | no | no | no | 1st | Product | Both fields are saved and ignored. A business that enters "closed 26 to 27 Nov" is still booked on those days |
| 3.24 | Waitlist: join and fill | no | no | no | | Product | The UX plan cut the tool; the spec promised it |
| 3.25 | Texts from the assistant's number: confirmations, opt-in reminders, callbacks | no | no | no | | Product | Needs 10DLC. Founder ruling: missed-caller and waitlist texts stay off until a consent review |
| 3.26 | 10DLC registration, per-business brand path | n/a | n/a | n/a | | Founder | Founder said start (9 Nov) |
| 3.27 | Square booking | part | no | no | | Platform | #5173, hidden adapter |
| 3.28 | Booking sites with no API, by browser, with steps and screenshots in the call record | no | no | no | | Product | Generic browser tool exists; no product path |
| 3.29 | Owner calls in and proves who they are (PIN) | no | no | no | | Product | |
| 3.30 | Spanish, and a voice choice with preview | yes | part | no | | Platform | The voice worker with Spanish and voice choice is on prod (version `QJcdFMeCBBJF`). Web and backend controls are on dev until the next prod train. Mobile controls are in #5183 (type-check and voice tests pass; not yet seen on a device) and reach phones by OTA when it merges |
| 3.31 | Choose which agent is the receptionist | yes | no | no | | Platform | #5166 merged 9 Nov; on dev, next prod train |
| 3.32 | `VoiceCallOutcome` metric and an alarm on worker and route errors | yes | yes | no | | Platform | The metric is emitted. The alarm is W85 (voice route errors), present in prod and dev. No alarm reads the outcome metric itself |
| 3.33 | Mark as spam; call back and text back from a row; warm transfer; ring me | no | no | no | | Product | Deferred by the UX plan |
| 3.34 | Whether a Twilio number can be a Google Voice forward target | n/a | n/a | no | | Product | Master plan open item |
| 3.35 | **Sellable bar:** every scenario passes three runs in a row, plus 20 real calls from people who did not build it | n/a | n/a | no | 1st | Product | |

## Phase 4. Web, Mac and mobile

| # | Promise | Built | Prod | Machine | 1st | Owner | Note |
|---|---|---|---|---|---|---|---|
| 4.1 | Voice Home is where sign-in lands; `/chat` redirects | yes | yes | no | 1st | Product | Sign-in, sign-up and the Mac app all open `/voice`. A signed-in person who types `/` stays on the marketing page |
| 4.2 | Home timeline by day, filter chips, footer counts | yes | yes | no | 1st | Product | |
| 4.3 | Needs you: four card kinds with labels and fix links ("Reconnect Google") | yes | yes | no | 1st | Product | |
| 4.4 | Agent page: one page, saves as you type, Pause, greeting style, Knowledge with "questions to add", instructions, hours, services, calendar status, number panel | yes | yes | no | 1st | Product | |
| 4.5 | No control without a working feature behind it | part | no | no | 1st | Product | Audited 9 Nov. Saved and ignored: "Text clients the day before" (on by default), Holidays, service "Who does it", mobile "Offer the waitlist" (on by default). The notes-email flag is removed by #5163, in the queued train |
| 4.6 | Onboarding: website read, review what was learned, booking destination, number, name and try a booking | yes | yes | no | 1st | Product | Never run end to end on a fresh workspace |
| 4.7 | Fresh business to first answered call in 15 minutes, at phone width | n/a | n/a | no | 1st | Product | |
| 4.8 | Settings: Members, Connections, Approvals (Ask, Auto, Never ask), Notifications, Billing and minutes with the run-out choice | yes | yes | no | 1st | Product | |
| 4.9 | Customers are always on managed credits; Sign in with ChatGPT is internal only for now | no | no | no | | Platform | Founder ruling 9 Nov. What is built (#5162, #5168, on dev) offers both to every workspace admin |
| 4.10 | The orb: connecting, listening, thinking, speaking, with a caption; each talk kept as a "Yours" row | yes | yes | part | | Product | |
| 4.11 | The orb does the general agent work the old product did | yes | yes | no | | Product | Founder ruling: the orb is a full third use case |
| 4.12 | Mac app opens voice Home; mic works; Fn hold-to-talk; signed update | yes | yes | part | | Product | v0.4.1031 installed. Physical Fn test pending |
| 4.13 | Mobile: four tabs, voice Home | yes | yes | no | | Product | Shipped by OTA |
| 4.14 | Mobile: voice settings, billing and onboarding match the web; old purchase screens gone | no | no | no | | Product | |
| 4.15 | Push: Needs-you cards, and the first three calls after Go live | yes | no | no | | Product | Card pushes already existed (one per card, retry-safe). #5188 trims their text to the kind and a first name (no caller number or question) and adds a push for the owner's first three answered calls. Not seen on a device. A tap opens the task, not the Needs-you list |
| 4.16 | Ten-second undo on send or spend | no | no | no | | Product | Not in the code |
| 4.17 | Design pass to a consumer bar; accessibility; empty and error states | part | part | no | | Product | A polish pass shipped (#5084). No final critique |

## Phase 5. Meet

| # | Promise | Built | Prod | Machine | Owner | Note |
|---|---|---|---|---|---|---|
| 5.1 | Invite the workspace address to a Meet and the agent joins; only a signed-in member's invite counts | yes | yes | part | Product | Joined once after a manual repair |
| 5.2 | Joins signed in, with no lobby click | yes | no | no | Founder | `RECALL_GOOGLE_LOGIN_GROUP_ID` is empty on prod |
| 5.3 | Shared `agents@` address routes by sender | part | part | no | Founder | Backend built. Needs Google Workspace mail forwarding |
| 5.4 | Recurring series; one occurrence moved or cancelled | yes | yes | no | Product | |
| 5.5 | One announcement on joining, with the recording notice | yes | yes | part | Product | |
| 5.6 | Speaks when named, stays quiet otherwise; the model decides, no code gate | yes | yes | part | Product | One real meeting had flaws; fixes not re-tested with several people |
| 5.7 | Sleeps after five idle minutes, wakes on its name with recent context | yes | yes | no | Product | |
| 5.8 | Hears each person separately and knows who asked | yes | yes | no | Product | |
| 5.9 | Leaves only when Recall says the call ended; survives a reconnect | yes | yes | no | Product | Master plan review focus 4 |
| 5.10 | Tile shows what it is doing | yes | yes | no | Product | |
| 5.11 | Live answers from Knowledge, the web, past calls and meetings, and every connected provider | yes | yes | no | Product | The spec's six-ask acceptance list has never been run |
| 5.12 | Reads and writes Meet chat; long answers go to chat as a link | yes | no | no | Platform | Prod image pinned (#5169); not deployed or rolled |
| 5.13 | A Worker's result comes back into the meeting; the follow-up shows its status | yes | yes | no | Product | |
| 5.14 | Starts follow-up work it was clearly given; records human commitments without acting | yes | no | no | Platform | #5163 merged, on dev |
| 5.15 | Everyone acts with the inviter's authority; spend, send and publish need the inviter's card tap | yes | yes | no | Product | |
| 5.16 | Notes in the app after the meeting; no email | yes | part | part | Product | Notes in the app are verified on screen on prod. The removal of the automatic email reaches prod with the queued train |
| 5.17 | Structured decisions, recallable by voice ("what did we decide") | yes | no | no | Product | #5186: the notes run also returns a Decisions section; stored on the meeting, shown in the pane, each written once to the workspace's team memory for recall. Not yet seen in a real meeting or recalled live |
| 5.18 | A person can opt out of transcription; the tile says so | part | no | no | Product | #5009 parked. Built in #5185, unverified live: GPT-Live hands the request to the Router, whose `meeting_stop_transcribing` excludes only the recorded speaker; the worker adopts it from `/events`; the tile shows the count. Needs a real-meeting run. |
| 5.19 | Office specialist pre-warmed for the meeting | part | part | no | Product | |
| 5.20 | Meeting minutes counted | yes | part | part | Platform | #5155, verified on dev |
| 5.21 | The scenario list (`tests/replay/scenarios/`) passes in a real Meet | n/a | n/a | no | Product | No simulator exists; the earlier one was removed |

## Phase 6. Billing and go to market

| # | Promise | Built | Prod | Machine | 1st | Owner | Note |
|---|---|---|---|---|---|---|---|
| 6.1 | One pool of credits covers voice and text | no | no | no | 1st | Platform | Founder ruling 9 Nov, replacing the built plan ($250, 1,500 minutes, $0.35 after; sandbox billed 1,600 minutes at $285). How one pool works across OpenAI voice and OpenRouter text is being looked into |
| 6.2 | Pay, get the plan | yes | yes | part | 1st | Platform | Passed in the Stripe sandbox on dev. 0 of 33 prod billing rows hold a voice contract |
| 6.3 | A new business is offered one plan, the voice agent | no | no | no | 1st | Product | Sign-up still gives the old $50 plan and a free trial. A leftover "Front desk" add-on is in checkout. Founder ruling 9 Nov: one plan |
| 6.4 | Phone, Meet and orb usage all draw on the customer's credits | part | part | no | 1st | Platform | Today only the orb debits credits. Phone and Meet minutes are sent to the voice plan's meter and debit nothing, so a business with no voice contract is not charged for them. A business on the voice plan gets text inference free, capped only by a daily key limit |
| 6.5 | 80% and 100% notices; run-out choice; never a silent cut-off | part | no | no | | Platform | |
| 6.6 | Cancel, renewal, failed payments, refunds | part | part | no | | Platform | Cancel at period end exists. No refund or dispute handling |
| 6.7 | Old plans and purchase doors closed for new buyers (web, mobile, Apple) | no | no | no | | Product | |
| 6.8 | Pay, then Go live buys the number, then the first call is answered | yes | yes | no | 1st | Product | Four steps, never run together |
| 6.9 | Pricing page presents the receptionist | no | no | no | 1st | Product | Founder ruling 9 Nov |
| 6.10 | Five paying salons live | n/a | n/a | n/a | | Founder | Phase 6 gate |

## Phase 7. Dental

| # | Promise | Built | Prod | Machine | Owner | Note |
|---|---|---|---|---|---|---|
| 7.1 | NexHealth integration | no | no | no | Product | |
| 7.2 | Medical tier: its own runtime pool, audit logging, model choices safe for health data | no | no | no | Platform | |
| 7.3 | Practice BAA template; vendor BAAs signed | n/a | n/a | n/a | Founder | Follows 2.18 |
| 7.4 | One practice live under BAA | n/a | n/a | n/a | Founder | Phase 7 gate |

## Added channel: iMessage agents

Named in the spec as an optional add-on (section 9). Founder direction 9 Nov: plain iMessage agents, not Apple business messaging.

| # | Promise | Built | Prod | Machine | Owner | Note |
|---|---|---|---|---|---|---|
| I.1 | A customer texts the business's iMessage line and the agent replies | yes | part | no | Product | Built in October on Photon. The route is mounted and the three Photon secrets exist in prod and dev. Not known whether Photon points at it or whether a real message has ever arrived |
| I.2 | The texter is treated like a caller: same contact identity, same limits, same Router path | no | no | no | Product | Today a text has no contact room and no caller scope. The phone path's contact model is the thing to reuse |
| I.3 | One memory per contact across calls and texts, with Forget | no | no | no | Product | Caller memory is written only at the end of a call |
| I.4 | Agent page switch for texts; Pause applies | no | no | no | Product | |
| I.5 | The texting line belongs to the business's receptionist, not the old Customer Operations agent | no | no | no | Product | |
| I.6 | STOP and HELP handled; group chats detected; photos and files read | no | no | no | Product | None of the three exists |
| I.7 | Text the caller after a call (confirmation, answer) | no | no | no | Product | Photon only allows a first message to someone who texted first. May need SMS as the fallback, which needs 10DLC |
| I.8 | iMessage is an add-on | part | ? | no | Product | Founder ruling 9 Nov: an add-on, built out later. A flat add-on exists in checkout today. A Photon line costs $250 a month |
| I.9 | The leftover Sendblue rail deleted | no | no | n/a | Product | Mounted and inert |

## Phase gates

Required by the master plan before a phase reaches prod. Founder ruling: "Don't skimp on review or security."

| Phase | Code review against `docs/standards/aws-agentic-lens.md` | Security review | Over-engineering audit | Founder walk |
|---|---|---|---|---|
| 1 | no | no | no | no |
| 2 | no | no | n/a | no |
| 3 | no | no | n/a | no |
| 4 | no (design critique instead) | no | n/a | no |
| 5 | no | no | n/a | no |
| 6 | no | no | n/a | no |

Individual PRs were reviewed. No whole-phase review has been run.

## Superseded: do not rebuild

| Was promised | Replaced by | When |
|---|---|---|
| Gemini Live as the voice and the Router | GPT-Live talks; it hands off to the same Router typed messages use | 8 Nov |
| A spoken "yes" approves | Card taps only | 8 Nov |
| Two seeded specialists | The six-agent roster, one named the receptionist | 9 Nov |
| Notes emailed to the organiser | Notes in the app only | 7 and 9 Nov |
| Every action item becomes a task | Only work clearly given to the agent | 9 Nov |
| Delete the Mac app | It stays | 7 Nov |
| Delete the chat UI entirely | A conversation page stays at `/voice/lightbulb` | 8 Nov |
| Always-open Meet voice | May sleep and wake on its name | 8 Nov |
| Code rules for when the agent speaks (3 s pause, 20 s stale drop, wake gates, canned lines) | The model owns conversation; code enforces permissions and safety only | 8 Nov |
| A scheduling rules engine or speech rules for double booking | The model judges what to offer. The booking tool still reserves a slot atomically, so two bookings cannot take one slot. This ruling does not remove that safety | 8 Nov |
| Delete the OpenRouter gate | OpenRouter stays as the inference and billing gate | 9 Nov |
| First words of every call say the name, "AI assistant" and "this call is recorded", enforced by a guard | The business owns the opening; no enforcement in code; the assistant never claims to be a person when sincerely asked | 9 Nov |

## Waiting on the founder or a vendor

| Item | Unblocks |
|---|---|
| Reconnect Google on the founder workspace | 3.7 |
| Recall Google Login Group | 5.2 |
| Google Workspace admin access | 5.3 |
| 10DLC registration (company details, fee) | 3.25, 3.26 |
| Google calendar-only OAuth verification (4 to 6 weeks) | 2.19 |
| BAA requests (paid plans) | 2.18, 7.3 |
| Activate Founders application | 0.6 |
| A second phone and 40 minutes after the checks pass | Every Founder walk |
