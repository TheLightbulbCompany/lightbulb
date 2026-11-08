# Founder gate walk — voice MVP (prod, after the last deploy)

**Before you start (founder-only):**
- (a) Reconnect Google once if it was connected before 28 Oct, so free/busy can be read.
- (b) Optional:
  - a Recall Google Login Group plus `RECALL_GOOGLE_LOGIN_GROUP_ID`. Without it, admit the bot from the Meet lobby.
  - a LiveKit outbound trunk plus `VOICE_OUTBOUND_TRUNK_ID`. Without it, the forwarding check says "isn't switched on yet".

| # | Surface | Do | Pass looks like |
|---|---|---|---|
| 1 | Sign-in / Mac app | Sign in on thelightbulb.company. Then open the Mac app. | Both land on **/voice Home**: orb, the one Needs-you line, the timeline, and the chips All · Calls · Meetings · Yours · Needs you. /chat still opens. |
| 2 | Onboarding | /voice/setup. Step 1: business plus website. Step 4: **Keep my number**, choose your carrier and "Missed and busy". Then step 5, **Go live**. | Step 4 shows your carrier's code with the assistant's number and the off code. Go live succeeds on the prod number, or shows the retry message if it can't get one. On the Agent page, **Check forwarding** rings your line. With the trunk set, a tick appears within 70 s; without it, "isn't switched on yet". |
| 3 | Agent page | /voice/agent. Leave Greeting style empty. Uncheck **Book appointments**, then call the line and ask to book. Re-check it. Press **Pause** and call again. | Saves as you type. With Book off, Nova takes a message instead of booking (refused server-side). While Paused, it takes messages only. The missed-caller and waitlist texts show off ("consent review"). |
| 4 | Phone receptionist | From a phone that isn't yours, call **(484) 207-9294**. Book a gel manicure on Saturday afternoon. | The opening says, in Nova's own words, its name, that it's an AI assistant, and that the call is recorded. Slots are in your time zone. It reads the booking back and books it, and the booking appears in **Google Calendar**. A Home row appears within about 20 s with the steps and the transcript. |
| 5 | Phone, harder | Call again from the same phone and ask something it can't know (a bridal party of 6). Ask to pay a deposit. Ask for a person. | It knows you as **returning**, and the pane's "remembers" section has a **Forget** button that works. The unknown question becomes a **"Nova didn't know"** card in Needs you; answer it and the answer appears in Knowledge, then the next caller gets it. The deposit is refused and a message is taken. A failed hand-off becomes a **"Caller wanted a person"** card. |
| 6 | In-app orb | Hold the orb: "What happened today?" With a Needs-your-OK card open, say "Nova, yes". | The orb goes Listening, Thinking, Speaking, with a caption. A **Yours** row is kept with its transcript. "Yes" gets "sent to your phone to confirm", the card shows **You said yes · Confirm**, and one tap approves it. |
| 7 | Meet agent | Invite the agent address (Agent page, Meetings) to a Meet. | It joins (signed in, or admitted from the lobby) and posts **one** announcement ending "This meeting is recorded and notes are saved to …'s workspace". Unnamed talk gets **silence**. |
| 8 | Meet, live asks | "Nova, how many bookings this month versus last?" · "Nova, what does our website say about group bookings?" · "Nova, pull October's revenue" (Stripe connected) · "Nova, draft the holiday-hours post and put the link in chat". | Spoken answers to the first three come from calls, Knowledge or the web, and the connected provider. For the draft, the tile shows **Drafting…**, then "Draft ready: … link" appears in chat and the tile shows **Page ready**. The meeting row's follow-up shows **Done**. |
| 9 | Meet, spend | Anyone says "Nova, order the gel colours" → then someone says "Nova, yes". | A Needs-your-OK card goes to the **inviter** whatever the approval mode. "Yes" sends a one-tap confirm, and nothing is spent until the tap. |
| 10 | Settings | /voice/settings | Members, Connections (every integration still listed), Approvals (Ask/Auto/Never ask), Notifications, and **Billing & minutes** ("N of 1,500 min", "$0.35/min after", run-out choice). |
| 11 | Ops | CloudWatch after the infra deploy | The `VoiceCallOutcome` metric appears for your test calls, and alarm W85 exists. |

**Not in this walk (Later):** texts and confirmations (needs KYC/10DLC), Ring me and call back, owner mode by phone, spam, warm transfer, the recording player, minutes metering, noise and accent evals, meeting notes and decisions (#5018 parked), per-guest opt-out (#5009 parked). The "sellable" bar is still open: pass^3 on the eval card plus 20+ real dial-ins.
