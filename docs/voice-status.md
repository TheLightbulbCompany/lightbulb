# Voice product status board

The one place that says what is built, what is live, and what has been tested.
If a capability is not a row here, nobody owns it: add the row.

Last full check: 9 Nov 2026, against `origin/main` `42a8f8aa8`, prod backend task
definition 416 and dev 966. "Tested" entries come from the receipts of the build
sessions unless the note says "config read 9 Nov".

## The four states

| State | Means | Who sets it |
|---|---|---|
| **Built** | The code is on `main`. | The PR that merges it |
| **Prod** | It is deployed to prod and switched on there. | The session that ran the prod deploy |
| **Machine** | A real call, meeting, payment or scripted run passed **on prod**. Unit tests do not count. | The session that ran it, with the receipt named |
| **Founder** | The founder walked it on prod and said yes. | The founder |

Values: `yes`, `no`, `part` (say which part in the note), `n/a`.

## Rules

1. Every PR that changes a capability edits its row in the same PR.
2. One owner per row. Only the owner's track edits the row.
3. A row moves one state at a time. "Built" never implies "Prod"; "Prod" never implies "Machine".
4. One implementer, one fresh reviewer, one fix round, merge. A finding from a second
   round becomes a new row, unless it is about money, security or data loss.
5. New ideas go to **Later** as a row. Nothing in Later is started while a **Sell** row is not `Machine: yes`.
6. Prod trains leave twice a day. After each, the machine checks run, then the founder walks what passed.

## Tracks and leads

| Track | Lead | Scope |
|---|---|---|
| **Sell** | Sell lead (Claude) | What one paying business needs to take real calls |
| **Platform** | Codex (thread `01a1bcde`) | Infrastructure, cost, retirement, and the PRs it already has open |
| **Later** | Nobody until Sell is done | Everything else that was promised |

---

## Sell: one business live on the phone

| # | Capability | Built | Prod | Machine | Founder | Next step |
|---|---|---|---|---|---|---|
| S1 | Call is answered; opening says name, AI, recorded | yes | yes | yes | no | Founder walk |
| S2 | Answers a business question correctly, out loud, in useful time | yes | yes | part | no | One call invented facts; the handoff was fixed but no call has yet proved a correct **spoken** answer. Run the phone eval on prod and set a latency target |
| S3 | Book, move, cancel on our own calendar | yes | yes | part | no | Passed on the older voice stack. Re-run on GPT-Live |
| S4 | Book into Google Calendar | yes | yes | no | no | Founder reconnects Google; then one real booking with the event checked in Calendar |
| S5 | Take a message; hand off to a person | yes | yes | no | no | One transfer to an answering number, one to a number that does not answer |
| S6 | Each business gets its own number at Go live | yes | yes | no | no | `VOICE_BUY_NUMBERS=true` on prod (config read 9 Nov). No number has ever been bought. Needs a fresh paid workspace. Costs $1.15/month per number |
| S7 | Forwarding check from the Agent page | yes | yes | no | no | Outbound trunk is set on prod (config read 9 Nov). Never run against a real carrier forward |
| S8 | Onboarding on a fresh workspace, sign-up to first answered call | yes | yes | no | no | Never run end to end. Time it against the 15-minute goal |
| S9 | Pay $250, get the plan, minutes counted | yes | yes | part | no | Sandbox passed. 0 of 33 prod billing rows hold a voice contract (read 9 Nov). One real checkout on prod |
| S10 | Caller cannot do what the owner unticked; no owner data reaches a caller | yes | yes | no | no | Scripted calls: forbidden action, second caller, spoofed owner |
| S11 | Home shows the call with transcript and steps | yes | yes | part | no | Check a fresh prod call row for truth |
| S12 | The Agent page shows no control without a working feature behind it | no | no | no | no | "Day-before text" is ticked by default with no sender. Hide it until texts exist |
| S13 | A new business is offered one plan, the $250 voice plan | no | no | no | no | Today sign-up gives the old $50 Team plan and a 30-day trial; the voice plan is a second opt-in contract. A leftover "Front desk" add-on is also in checkout. Needs founder decisions B1 and B2 |
| S14 | Phone, Meet and orb minutes all count toward the 1,500 | part | part | no | no | Phone and Meet report minutes. No meter call was found on the orb route (unverified). Needs founder decision B3 |

## Sell: meetings and the app (demo quality, not a gate for the first phone customer)

| # | Capability | Built | Prod | Machine | Founder | Next step |
|---|---|---|---|---|---|---|
| M1 | Invite the agent to a Meet and it joins | yes | yes | part | no | Joined once after a manual repair. A normal new invite must work. `RECALL_GOOGLE_LOGIN_GROUP_ID` is empty on prod, so the bot waits in the lobby |
| M2 | Speaks when named, stays quiet otherwise, sleeps when idle | yes | yes | part | no | One real meeting had flaws; the fixes are not re-tested with several people |
| M3 | Notes in the app after the meeting | yes | yes | yes | no | Founder walk |
| M4 | Reads and writes Meet chat | yes | no | no | no | Prod image pin merged (#5169); prod not deployed or rolled |
| M5 | Starts follow-up work it was clearly asked to do | no | no | no | no | #5163 in review (Codex) |
| M6 | In-app orb: talk, interrupt, keep speaking after release | yes | yes | part | no | Founder test on the Mac with Fn |
| M7 | Provider choice: managed or Sign in with ChatGPT | yes | no | no | no | #5162 merged, dev only; settings route fix #5168 in review (Codex) |

## Platform (Codex)

| # | Capability | Built | Prod | Machine | Next step |
|---|---|---|---|---|---|
| P1 | Router and Workers on Temporal are the only agent path | yes | yes | yes | n/a |
| P2 | Paperclip and Aurora removed | yes | yes | part | Confirm the bill lines are gone |
| P3 | GooseTown parked, its NAT, load balancer and DNS removed | yes | yes | yes | n/a |
| P4 | One shared Hermes runtime pool per tier | yes | yes | part | Tenant isolation check; #5170 fences calls to the pool |
| P5 | Old per-business runtimes, roles and boot secrets retired | no | no | no | 27 old runtime definitions remain |
| P6 | Backend, workers and Lambdas on ARM | yes | no | no | Live on dev (#5167). Prod train |
| P7 | OpenSearch removed | part | no | no | #5156 then #5158, after the image roll |
| P8 | Secrets under 20 | no | no | no | About 130 today |
| P9 | AgentCore Identity holds OAuth tokens | no | no | no | Not started |
| P10 | Lightbulb NAT and CloudFront plan | no | no | no | Not started |
| P11 | Nothing wakes a business that is idle (quiet 24 h) | yes | yes | no | Needs a clean 24 h measurement |
| P12 | Fixed cost at or under $250/month | no | no | no | Needs a full bill after P5 to P10 |
| P13 | Memory retention (90 days) runs on a schedule | yes | yes | no | Confirm a real scheduled run |

## Later (promised, not started or parked)

| # | Capability | State | Blocked on |
|---|---|---|---|
| L1 | Texts: confirmations, reminders, callbacks | not built | 10DLC registration (vendor, paid) |
| L2 | Play back a call recording | not built | n/a |
| L3 | Waitlist: join and fill | not built | L1 for the fill texts |
| L4 | Owner calls in and proves who they are (PIN) | not built | n/a |
| L5 | Staff and holiday closures limit booking slots | fields saved, not used | n/a |
| L6 | Square booking | started (Codex) | n/a |
| L7 | Booking sites with no API, by browser | not built as a product | n/a |
| L8 | Spanish and voice preview | core merged (#5171), controls not live | Codex to finish, then stop |
| L9 | Choose which agent is the receptionist | #5166 draft (Codex) | Codex to finish, then stop |
| L10 | Structured meeting decisions, recallable | not built (#5018 parked) | n/a |
| L11 | Per-person transcription opt-out in a meeting | #5009 parked | n/a |
| L12 | Shared `agents@` invite address | backend built | Google Workspace mail forwarding (founder access) |
| L13 | Mobile: voice settings, billing and onboarding match the web | not built | n/a |
| L14 | 80% and 100% minute notices, run-out forward | part, dev only | n/a |
| L15 | Refunds, disputes, dunning for the voice plan | not decided | Founder decision |
| L16 | Push for the first calls on Home (today it polls every 20 s) | not built | n/a |
| L17 | Ten-second undo on send or spend | not found | n/a |
| L18 | Dental: NexHealth, medical tier, BAAs | not started | Vendor agreements |

## Waiting on the founder

| Item | Unblocks |
|---|---|
| Reconnect Google on the founder workspace | S4 |
| A second phone and 20 minutes after each prod train | Every `Founder` column |
| Google Workspace admin access, or someone who has it | L12 |
| Recall Google Login Group | M1 without the lobby |
| Go or no-go on 10DLC registration cost | L1, L3 |
| Activate Founders application | Credits |
| Refund and dunning policy | L15 |
