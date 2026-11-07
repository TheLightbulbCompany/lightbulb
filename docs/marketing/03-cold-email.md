# 03. Cold email: the two asks and the three-touch sequence

## Purpose
Moves the stage-1 goal: ten founding teams set up by 31 Jan 2027. Serves business plan Go To Market, "How we reach them" (two asks split-tested, three to five touches, a founder sends, agents draft).

## What we know
- The wedge, the qualification list and the offer are in the plan (§5, §7; high confidence, first-party). The ask in the plan: "we'll get your first [plan / PR / invoice run] approved by them this week."
- Customer words we may echo: "Every new Claude Code session starts from zero" (r/ClaudeCode, Aug 2026); "who owns a change, and which actions should stop for human approval" (r/ClaudeAI, Sep 2026); the founder-pair HN comment (Feb 2026). From `.agents/product-marketing.md`; the plan carries the same quotes (medium: one thread each).
- Proof we hold: Second Catch, a two-founder team (may be named now) whose CEO agent wrote a seller-acquisition plan that now waits on a ~$5,000 approval card (plan §9). Anna's quote is cleared for public use; her QuickBooks invoices are sent by the CEO agent since 11 Oct. Nothing else is proof: no customer pays list yet, so we never claim results, only what happened.
- Skill benchmarks (cold-email references, Belkins/Lavender/Gong, second-hand, medium): 2-4 word lowercase subjects; 55% of replies come from follow-ups; best single email 8.4%; 3 days then 7-8 then 14+ spacing. We use them as defaults, not promises.
- The lead list (workstream 02) is not in the repo yet, so observation lines are written as templates with `{{slots}}`.

## Findings (ranked)
1. The only thing that can make a stranger reply is that we read their words. The observation must quote or name something public they wrote or built, and the next sentence must be the problem it shows.
2. The ask carries the email. "Worth a chat?" asks for time; "your first plan approved by your cofounder this week" asks for a result. The split test therefore changes only the cost to the reader: A is a 45-minute call we run, B is a one-line reply and we set it up async.
3. Proof is thin and must stay honest. Use what happened (a plan waiting on an approval card, invoices sent). Second Catch may be named; no other team is named until three teams are paying.
4. Public replies (X, Reddit) must give before they ask; no link in the first one.

## Recommendation
Send A to half the list and B to the other half, alternating by row, same observation, same proof. Founder sends from their own address; an agent drafts. Decide the winner on setups booked per send, then replies per send, after 60 sends per arm (see Measures). Rejected: a meeting-request ask ("15 minutes?"), a link or calendar link in touch one, a discount, a feature list, any claim about week-4 results.

## Deliverable

Slots: `{{first}}`, `{{observation}}` (section 3), `{{teammate}}` ("cofounder" if their bio or thread says so, otherwise "teammate"), `{{work}}` ("plan / PR / invoice run": pick the one that matches what they build), `{{founder}}`.

### 1. First-touch emails (variant A and B differ only in the last paragraph)

**Subject (both):** `handoffs` (alternates in section 4)

**Variant A, the call ask**

> Hi {{first}},
>
> {{observation}}
>
> When two people run agents, the founder ends up carrying context between them.
>
> Second Catch, a two-founder team, set up in Lightbulb last week. Their CEO agent wrote a seller plan, and it now waits on a $5,000 approval card both founders can see.
>
> Bring your {{teammate}} and the agents you already use, and we'll get your first {{work}} approved by them this week. 45 minutes, I run it.
>
> {{founder}}

**Variant B, the reply ask**

> Hi {{first}},
>
> {{observation}}
>
> When two people run agents, the founder ends up carrying context between them.
>
> Second Catch, a two-founder team, set up in Lightbulb last week. Their CEO agent wrote a seller plan, and it now waits on a $5,000 approval card both founders can see.
>
> Reply with one handoff that still goes through you, and we'll set it up so your {{teammate}} approves the {{work}} from a channel this week. No call needed.
>
> {{founder}}

Signature under the name: "Lightbulb, thelightbulb.company" as plain text, no image, no link in the body. The sender is the founder who sourced the lead and runs the session. When a call is booked, ask for the second person; book anyway if they insist, and the first approval card is the invite.

### 2. Follow-ups (each stands alone; send in the same thread, which is a real reply, not a fake "Re:")

Day 3, the problem in their world:

> {{first}}, one more angle. The hard part with agents on a team isn't the code. It's who owns a change, and which actions stop for a person's yes.
>
> In Lightbulb the agents sit in the same channels as your people, and spend, sends and deploys wait for an approval you can see.
>
> {{A: Bring your {{teammate}} and one agent you use, and your first {{work}} gets approved this week. / B: Name one handoff that still goes through you and we'll wire it this week.}}

Day 8, proof from a different kind of team:

> {{first}}, a food business asked its CEO agent to match photographed checks to open invoices in QuickBooks. It marked them paid and has sent invoices since 11 October, asking first. The owner told us: "I'm really impressed with how fast it works."
>
> Same shape for your {{work}}: the agent does it in the real tool, and a person says yes.
>
> {{A: Want us to set up yours with your {{teammate}} this week? / B: Which handoff would you want it to do first?}}

Day 16, breakup (last email; honour it):

> {{first}}, I'll stop here. If handoffs between your people and your agents aren't a problem right now, ignore this and good luck with {{their project}}.
>
> If it becomes one, reply "handoff" and we'll get your first {{work}} approved by your {{teammate}} that week.

### 3. Observation line, three source types (it must lead into the problem line)

| Source | Observation | Why it connects |
|---|---|---|
| HN / Reddit comment author | `Your comment on {{thread}} stuck with me: "{{quote, 10 words or fewer}}".` | Their own sentence is the problem. Quote exactly or paraphrase without quote marks; never invent. |
| Plugin publisher | `I used your {{plugin}} plugin. It {{what it does, one verb phrase}}, which tells me you run agents for real work.` | They already extend an agent; the gap is the second person. Use only if we actually looked at the listing. |
| Accelerator founder | `{{Company}} is in {{batch}}, and your bio says you build with Claude Code.` | Public bio and batch page only. Add "with {{n}} of you" only if the team page shows it. |

### 4. Subject lines (2-4 words, lowercase, no product, no name, no numbers)

| Use | Subject |
|---|---|
| Default, both variants | `handoffs` |
| HN / Reddit | `your {{thread-topic}} thread` (e.g. `your shared memory thread`) |
| Plugin publisher | `your plugin` |
| Accelerator founder | `agents on a team` |
| Follow-ups | same thread, same subject |
| Breakup | same thread |

Alternate `handoffs` and the source-specific line in a second, later test; do not stack it on the A/B.

### 5. X / Reddit reply (public; one reply, no link, under 60 words)

> Same here: the agent remembers my work, not my cofounder's. What helped was putting the agents in the same channel as the people, so a handoff is a mention and anything that spends money waits for a yes. Curious what you do when a change needs an owner.

(48 words.) Rules: reply only where the poster asked for or described the problem; first-person, no brand name; the link and the offer come only if they answer, and a DM only after the person asks for help in the thread. Leo signs and posts X/Reddit by hand, in his own name.

### 6. LinkedIn DM for Aden (about 45 words, sent after a connection request is accepted)

> Hi {{first}}, I saw your post on {{topic}}. I'm Aden, one of the founders of Lightbulb. We put a team's people and its AI agents in the same channels. If you and your {{teammate}} run agents, I'll get your first {{work}} approved by them this week. Want it?

### 7. Quality checklist applied

Test for every line: would a founder say this to another founder's face? Words banned in public copy scanned: multiplayer, AI-native, orchestration, bot, prompt, "I hope this finds you well", exclamation marks (none present).

| Piece | Reads aloud | Would I reply | Serves reader | Personalisation fuels the problem | One low-friction ask | Face test |
|---|---|---|---|---|---|---|
| Email A | yes, 4 short paragraphs | yes if the quote is real | yes | via section 3 | yes, a result, costs 45 min | yes |
| Email B | yes | yes, cheaper than A | yes | via section 3 | yes, a one-line reply | yes |
| Day 3 | yes | yes, new angle | yes | not repeated, stands alone | yes | yes |
| Day 8 | yes | yes, new proof | yes | proof matches "real tool, person says yes" | yes | yes; Anna's line is cleared |
| Day 16 | yes | yes, no guilt | yes | one door left open | yes | yes |
| X/Reddit | yes | n/a | gives a method first | their thread | a question, no link | yes |
| LinkedIn | yes | yes | short | their post | yes | yes |

Word counts (counted by script; the observation slot counted at 15 words, the cap for a filled line): email A about 85, email B about 88, both under 90. Keep every observation line to 15 words or fewer.

## Build tasks
1. **Title:** Tag the setup request with the email variant. **Today:** the plan's setup form asks "How did you first hear about us?" but cannot tell A from B. **Change:** read `utm_source`, `utm_medium=email`, `utm_content=A|B` into the request, and put `?utm_content=` links only from touch two on. **Why:** the winner is decided on setups per send. **Measure:** every request in the Monday readout carries source and variant.
2. **Title:** One-row-per-send tracking sheet. **Today:** none. **Change:** columns: lead, source type, variant, touch, sent date, reply, setup booked. **Why:** the Monday review counts touches, conversations and set-ups per source. **Measure:** sheet row count equals sends logged.

## Measures
- Sent per week (floor ten per founder), reply rate, setups booked per send, per variant and per source type (the plan's Monday numbers: touched, conversation, setup booked).
- Sample size is the constraint: at ten to twenty sends per founder per week, 60 sends per arm takes about three to four weeks. At a plausible 5 to 10% reply rate that is three to six replies per arm, so a small gap is noise. Rule: after 60 per arm, keep the variant with more setups booked; if equal, keep B (cheaper to say yes); if neither has booked a single setup after 60 each, stop and change the offer, not the wording.
- The better variant also becomes the ask in follow-ups and in the public post.

## Founder decisions (13 Oct 2026)

- B1: Name Second Catch now. Other teams stay unnamed until three teams are paying.
- C2: Public replies in each founder's own name, no pitch. A DM only after a person asks for help in the thread.
- C3: The channel owner signs: Leo X and Reddit, Aden LinkedIn, Prasiddha HN and Product Hunt.
- C4: Ask for the second person at booking; book anyway if they insist; the first approval card is the invite.
- C1 (from 04): The founder who sourced the team runs the 45 minutes, Aden by default. "I run it" holds because that founder is the sender.
- F3: Aden answers setup requests within one business day from his own address; the Growth agent drafts.
- Not answered: the HN/Reddit quote policy (under 15 words, thread named, deleted comments dropped), and whether `{{teammate}}` suits teams of 3 to 15.

## Sources
- /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/docs/business-plan.md (§5, §6, §7, §9, Go To Market)
- /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.agents/product-marketing.md (customer language, objections, voice)
- marketing-skills:cold-email references (subject-lines, follow-up-sequences), benchmarks second-hand.
- Not opened: the live site and the lead list; no claim above depends on them.
