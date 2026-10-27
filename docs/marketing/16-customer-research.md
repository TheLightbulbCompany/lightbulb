# 16. Customer research: setup-session guide, churn script, week-4 check-in

## Purpose

Moves the founding-team goal (ten teams, a second person in 48 hours, a handoff in week one) by telling us whether the wedge is real before we spend more founder time. Serves plan §5 ("What we know and don't"), §12 (the wedge risk), GTM "The setup session" and "Weekly rhythm", and Appendix C.

## What we know

- **Wedge:** founder-led teams of 3 to 15 running two or more agents, where one person routes context by hand. One customer inside it (Second Catch). Confidence: low (n=1). Source: plan §5.
- **Day-one loss:** 24 of 50 outside accounts never returned after day one; 33 of 38 outside workspaces have one person. Confidence: high (first-party counts, plan §9).
- **Who stuck:** small-business back-office users (Anna, Brooks), not the wedge. Santi left with "I'm not the right avatar" (`.agents/product-marketing.md`, quoted by the founder). Confidence: medium.
- **Week-4 question** is already set in the plan: "How would you feel if you could no longer use Lightbulb?" Aden owns the activation and churn interviews; the founder who sourced a team runs its setup session, Aden by default.
- **Method** (customer-research skill, `references/interviews-and-surveys.md`): keep calls casual, do not say "research study", prove yourself wrong, ladder with 5 whys, $50 per call, aim for 10 and be happy with 5, close with "who else should we talk to?", 40% "very disappointed" benchmark.
- **Not known (unverified):** why the 24 left; whether any have a teammate who would have joined; contact routes for them (ISOA-121 stalled here).

## Findings

1. We have counts, no reasons. Every current explanation of the 24 (free-mail, wrong avatar, "too many complications") is a guess from one to three people.
2. The two assumptions the wedge rests on are testable in a call: (a) someone is the human router and feels it; (b) a second person is needed and was not there on day one.
3. The sample is small, so each call must be able to return an unwelcome answer. The scripts below put the disconfirming questions first.
4. Contact routes are the real blocker. Rule: a founder (not an agent) contacts people, through the email the person gave us at sign-up or a public business route (Aden emails the 24 once each, from his own address, with the consent line), and agents never read the messages or the list contents.

## Recommendation

Run three conversation types on a fixed cadence, all casual, all by founders: (A) discovery inside every setup session, (B) 20-minute day-one-churn calls, (C) the week-4 check-in. Log one row per conversation as a Paperclip task. Report three measures on the Monday readout. Rejected: a survey blast to the 24 (low response, no ladder, and it reads as research); interviewing only happy customers (it confirms, it does not test); recording or transcribing calls to laptops (breaks the customer-data rule).

## Deliverable

### 1. Setup-session discovery guide (first 15 of the 45 minutes)

Frame: "Before we touch anything, tell me how work gets done at your company today." Do not pitch. Write answers in their words.

1. Walk me through the last thing your team shipped. Who touched it, human or agent?
2. Which agents or AI tools does each person use day to day? Where do their outputs end up?
3. When one person's agent learns something, how does the rest of the team find out?
4. Tell me about the last time a handoff was missed or work got done twice. What happened?
5. Who routes context between people and agents? What happens when they are away for a day?
6. Where does that routing live today (a repo, Linear, a doc, a chat, someone's head)?
7. What have you already tried to fix it? Why did it stop working?
8. If we could hand off one thing this week, what would you pick first, and who would pick it up?
9. What would you never let an agent do without a yes from you?
10. Looking back in a month, what would make you stop using this?

Disconfirming checks inside the list: Q3 and Q5 can return "it is fine" (then they fail the §5 list and the session should say so); Q10 invites the exit.

**Ladder for the two that matter most** (Q4 the missed handoff, Q8 the first handoff). Ask "why" three to five times, in a casual voice ("what made that the one?").

- Q4 ladder: What happened? → Why did it get missed? → Why was nobody holding it? → What did that cost you (time, money, a customer)? → What would have had to be true for it not to happen? *Root we want:* a named cost and a named owner gap, not "we should communicate more".
- Q8 ladder: Why that task? → Why not something else? → Who has to see the result? → Why them? → What happens if it comes back wrong? *Root we want:* the first person who must be in the room, and the approval they would insist on. That person is the second invite.

### 2. Day-one churn interview (the 24 who never returned)

**Outreach note** (Aden sends, one person at a time, from his own address):

> Subject: Quick favor, 20 minutes, on us
>
> Hi [first name],
>
> You signed up for Lightbulb on [month] and I can see you didn't get far. That's useful to me, not a mark against you. I'd like 20 minutes to hear what you were hoping for and what got in the way. No pitch, nothing to sign up for.
>
> I'll send you a $50 gift card as thanks, whatever you say. It is for your time, not a price offer. Are you free [day] or [day]? Calendar: [link]
>
> Thanks either way,
> Aden

**Consent line** (said at the start, once): "I'll take a few notes by hand, not record. Nothing goes outside the company with your name on it unless you say yes. You can skip any question or stop any time, and the gift card is yours either way. OK?"

**Script (20 minutes).** Do not defend the product. Silence is fine.

1. (Warm-up, 2 min) What does your company do, and what were you working on the week you signed up?
2. What made you try it? What was going on that week?
3. **What did you expect to happen after sign-up?**
4. What actually happened in the first ten minutes? Where did you stop?
5. **Who else would have needed to be in it for it to be useful?** Were they there? What stopped you inviting them?
6. **What did you do instead?** (The real alternative: a chat, one ChatGPT tab, a person, nothing.)
7. Who on your team uses AI agents for work, and how do they share what they make?
8. Was there a task you wanted to hand off? Why didn't you?
9. What would have had to be different for you to come back the next day?
10. What would make you try again, and what would make sure you never do?
11. Who else should I talk to?

Ladder the answers to 3, 5 and 9 (5 whys). Assumptions we are trying to break, logged before the first call: (a) they left because the product was too hard; (b) they were alone and needed a team; (c) they are not the wedge at all (free-mail, student, curiosity). Any answer that kills one of these is written down as a disconfirmed assumption.

### 3. Week-4 check-in (founder, in the team's `lightbulb-<slug>` channel or a short call)

Ask only after a real week of use. Core question, with its four options:

> How would you feel if you could no longer use Lightbulb?
> 1. Very disappointed 2. Somewhat disappointed 3. Not disappointed 4. I no longer use it

Follow-up by answer (two each, one sentence of context at most):

| Answer | Follow-up 1 | Follow-up 2 |
|---|---|---|
| Very disappointed | What is the main thing you'd miss? (their words become our copy) | Who on your team would miss it most, and who else should use it? |
| Somewhat disappointed | What would make it something you'd really miss? | What do you do today when it doesn't cover that? |
| Not disappointed | What were you hoping it would do that it didn't? | Who is the person it would have worked for? |
| No longer use it | When did you stop, and what happened that week? | What do you use instead? |

Also ask everyone: "What kind of team gets the most from this?" Segment the answers: the "very disappointed" cohort is the real market.

### 4. Notes schema and synthesis rule

One row per conversation, written by hand within an hour, no transcripts, no names in the shared file (use the task id).

| Field | What goes in |
|---|---|
| id / date / type | Paperclip task id; A setup, B churn, C week-4 |
| segment | Wedge (3-15, 2+ agents) / small-business back office / solo / other |
| trigger | What made them look or sign up |
| pain quote | One verbatim line, under 15 words |
| alternative | What they do or did instead |
| objection | What stopped or would stop them |
| disappointed | C only: the answer 1-4 |
| assumption tested | Which written assumption, and held / disconfirmed |
| next action | Owner and date, or "none" |

**Synthesis rule.** After every five conversations, cluster by theme, score each theme frequency × intensity (intensity = said unprompted or with strong words), and label confidence: High = 3+ independent conversations, unprompted, across segments; Medium = 2, or prompted, or one segment; Low = 1. Do not change positioning, product or the wedge on Low. Do not average across segments (wedge and small business are read separately). Under five data points in a segment, report "too few", not a conclusion.

### 5. Cadence

| Conversation | When | Who runs it | Where notes go |
|---|---|---|---|
| Setup-session discovery (A) | Every setup session, first 15 min | The founder who sourced the team; Aden by default | Paperclip task, one per session |
| Day-one churn calls (B) | 3 per week until 10 are booked (be happy with 5) | Aden | One task per call |
| Week-4 check-in (C) | Thursday check-in, week 4 for each team | The founder who set the team up | One task per team |
| Synthesis | Monday pipeline review, 10 minutes | Whoever holds the notes; Aden reads | Comment on the weekly readout task |

Rules: no recordings, no transcripts or personal data on laptops or in the repo; notes live only in the Paperclip task. Every call ends with "who else should we talk to?" and the answer becomes a lead for stream 02.

## Build tasks

1. **Research task template in Paperclip.** Today: none. Change: a task template with the notes-schema fields. Why: one row per call without improvising. Measure: 100% of calls have a task within an hour.
2. **Week-4 check-in trigger.** Today: manual. Change: a Thursday reminder to the setup founder when a team reaches day 28 (Clerk workspace date; no message content). Why: no team is missed. Measure: check-ins done / teams at week 4.
3. **"How did you first hear about us?" at sign-up** is already in plan §9; confirm it lands so churn calls can be segmented by source. Measure: share of new accounts with a source.

## Measures

| Measure | Target | Monday number it feeds |
|---|---|---|
| Interviews completed per week | 3 churn + every setup session + every week-4 | Pipeline review (conversation stage) |
| "Very disappointed" share | 40% or more of week-4 answers; reported separately for wedge teams and small businesses, with n beside each | Week-4 active |
| Disconfirmed assumptions logged | At least 1 per 5 conversations; zero means we are not testing | Wedge risk (plan §12) |

## Founder decisions (13 Oct 2026)

- C7: A $50 gift card for churn interviews is allowed (research, not acquisition); company card.
- C8: Aden emails the 24 day-one churners once each, from his own address, with the consent line.
- C9: The "very disappointed" share is reported separately for wedge teams and small businesses, with n.
- C10: Churn calls are 20 minutes.
- C1: The founder who sourced a team runs its setup session; Aden by default; the customer answers the first approval card.

## Sources

- `docs/marketing/README.md` (brief); `docs/business-plan.md` §5, §9, §12, Appendix C, Go To Market "The setup session" and "Weekly rhythm"; `.agents/product-marketing.md` (customer language, objections, anti-persona).
- Customer-research skill and its `references/interviews-and-surveys.md` (casual calls, disconfirmation, 5-why, $50 incentive, Sean Ellis 40%).
- No customer messages, transcripts or personal data were read.
