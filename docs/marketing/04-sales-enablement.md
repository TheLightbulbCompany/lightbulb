# 04. Sales enablement: the setup-call battle card

## Purpose
Moves the goal of ten founding teams paying $50 at list, with a second person inside 48 hours and a handoff in week one. Serves plan §5 (qualification), §6 (competition), §7 (offer) and Go To Market "The setup session".

## What we know
- The buyer is a founder of a 3 to 15 person team who already runs two or more agents and relays context by hand (plan §5; confidence high: plan, product-marketing context, quoted public threads).
- Activation is the second person invited, not the first reply. 33 of 38 outside workspaces have one person (plan §5; first-party data, 12 Oct).
- Proof we may use today: Anna's invoices and her quote (cleared 12 Oct); Second Catch's plan waiting on a roughly $5,000 approval (Second Catch may be named now). Nobody pays list yet; every active subscription is on the internal coupon. Do not imply otherwise.
- The offer (founders, 13 Oct): 30 days free with a card on file from the setup session, first charge on day 31, then $50 a month for up to 5 people and $20 a member from the 6th, price locked two years, a shared `lightbulb-<slug>` channel, no discounts, no coupons, no refund promise.
- One plan: Base and Ultra are retired. Seat billing and the trial checkout are built before the first setup (plan §7, item 5); do not sell a setup until they ship.
- Competitor facts are as the plan §6 states them; Zed Delta's current state is not in the sources. Check its page before quoting anything. The one Slack price we use is the price-for-8 line (workstream 11).
- Data facts we can state from the codebase (CLAUDE.md, confidence high): each workspace runs in its own isolated runtime; its agents' files sit under a per-workspace prefix in our AWS account; a bring-your-own API key is stored in AWS Secrets Manager. Say what the Privacy Policy says: isolated AWS environment per workspace; content not sold or used to train models. Add the provider path the team chose. Not established anywhere in sources: SOC 2, a vendor-side training guarantee, a retention schedule. Say "unverified" and Prasiddha answers in writing within a day.

## Findings
1. The objections that kill a setup are about habit and control, not features. Both are answered by doing the work in the call, not by arguing.
2. "Good enough" is the real competitor (plan §6). The pitch is a finished thing in 45 minutes.
3. The wedge is narrow. Disqualifying early is worth more than a friendly call that ends with one person in a workspace.
4. The product's first session is single-player today, so the founder must do by hand what items 1 and 2 will do later.

## Recommendation
One card, used verbatim on calls and replies. Lead every call with the second person, not the tour. Disqualify in the first five minutes. Never discount; the free month is the risk reversal. Alternatives rejected: a deck (a setup call is a working session, not a pitch); a per-competitor battle card (the one-liners are enough, and the comparison pages are workstream 11).

## Deliverable

### The card

**30 seconds.** "Lightbulb is where your team and your AI employees work in the same rooms. You mention an agent like a colleague, the work comes back where everyone can see it, and a teammate can pick it up. Each agent has its own memory, email and tools, and anything that spends money waits for a yes from a person, and you decide who your agents can email. We set it up with you in 45 minutes and you stop being the one who carries context between people and agents."

**Three claims, with proof.**
| Claim | Proof to show or say |
|---|---|
| Agents are members, not bots. | Six named agents with a job, memory, email address and approval rules. Show the roster and the team email. |
| The whole team sees the work and can continue it. | One record in Inbox and Library; a teammate continues a task; shared Claude Code and Codex sessions. Second Catch's plan on an approval card. |
| Any model, one price. | Three paths: our managed credits, your ChatGPT account, your own key. $50 flat for up to 5 people, then $20 a member. |

**Objections.** Two sentences each. Ask the follow-up; do not recite.
| They say | You say | Then ask |
|---|---|---|
| "We already have Slack." | Keep it. Lightbulb is where the agents do the work; Slack added agents to chat, we built the company around them. For 8 people Slack Business+ is $120 a month billed yearly; Lightbulb is $110, plus AI usage or your own key. | "Where does an agent's work live today if one teammate is offline?" |
| "Claude already does this for me." | For you alone, yes, and keep using it. It stops when a second person needs to see, continue or approve what it did. | "Last time your cofounder needed that context, how did they get it?" |
| "I don't need 5 agents." | You talk to one, the CEO agent; the others are its team and you can switch them off. Most teams use two or three at first. | "Which one job would you hand off first?" |
| "Will it act without asking?" | Spend, deploys and hires wait for a person on an approval card, you decide who your agents can email, and every action is on the shared record. We will watch the first card answered together today. | "What is the one thing you would never let it do alone?" |
| "What happens to our data?" | Your workspace runs in its own isolated AWS environment, and our Privacy Policy says your content is not sold or used to train models. Which model provider sees it depends on the path you chose. I will not claim a certification I cannot show you; Prasiddha will send the exact answer in writing within a day. | "Which data worries you most: code, books, customer email?" |
| "A chat tool alone is cheaper." | Yes, and we are not selling you a cheaper chat tool. If you want agents that do the work, $50 covers up to five people; compare it with the hours you spend relaying context. | "What does the relaying cost you in a week?" |
| "We self-host OpenClaw / our own glue." | Good, you already know the problem. Stop wiring it together: bring the agent, get the team, with approvals and one record you do not have to build. | "Who maintains the glue when it breaks?" |
| "We use Grok Bot." | Grok Bot gives each person a team of bots that no one else can reach. Lightbulb gives the team one set of AI employees everyone can see, on any model. | "Can your cofounder see what your bot did today?" |
| "We are waiting for Zed Delta." | It shares the coding session; we share the company, and it works with Claude Code and Codex now. Try both; ours takes 45 minutes. | "Is code the only thing you need handed off?" |

**Competitor one-liners.**
- Slack and Teams: "Slack added agents to chat. Lightbulb built the company around them."
- Grok Bot: "A team of bots for each person. We give the team one set everyone can see."
- Session tools (Delta, Lody, Egregore): "They share the coding session. We share the company."
- DIY glue: "Stop wiring it together. Bring the agent, get the team."
- Atlassian AMP: "Great if you run on Jira. We work outside it, with any model."
- Claude Code, Codex, ChatGPT: partners. "Keep them. We make their work shareable."

**The offer in one breath.** "Thirty days free. We take a card at this session and the first charge is on day 31. Then fifty dollars a month for up to five people and twenty dollars a member from the sixth. The price is locked for two years, and you get a shared channel with us. We do not discount, so there is no coupon to ask for."

**Qualify (all must be true; ask in the first five minutes).**
1. How many people, and who would join? (3 to 15.)
2. Which agents do you run today, and for which two functions?
3. When did a handoff last get dropped or re-explained? (Within 30 days.)
4. Who carries context between people and agents now? (One person, by hand.)
5. Who decides and pays? (On the call, no procurement gate.)
6. Will you invite one more person and connect one real workflow today?

**Disqualify (say so kindly, end the call early, log it).**
- Solo operator whose tasks one ChatGPT tab handles.
- A pair whose work does not overlap ("we have different scopes of work").
- A team shopping for a cheaper Slack.
- A buyer who wants deterministic automation (an n8n job).
- Needs SSO, procurement or a security review before trying (stage 3, not now).

### The 45-minute run sheet
Rule: the session ends with one thing finished, not a tour. You drive, they click. The founder who sourced the team runs it, Aden by default. Ask for the second person at booking; book anyway if they insist, and the first approval card is the invite.

| Min | Step | Do | If it fails |
|---|---|---|---|
| 0-5 | Qualify | Ask the six questions. Say what the next 40 minutes produce. | Fails a disqualifier: stop, thank them, offer the free sign-up. |
| 5-12 | Name the company | They sign up, name the workspace, add the card (30 days free, first charge on day 31) and say what the company does in one sentence. Watch the CEO agent respond. | Workspace is slow to be ready: use the wait to ask question 2 and 3 again in more detail; if it passes 5 minutes, reprovision from the debug route and note it. |
| 12-20 | Connect one tool | The tool they named in question 2 (QuickBooks, Gmail, GitHub), from the card in chat. | Connection fails: switch to the next tool on their list; if none connects, use email, which needs no setup, and log the failure. |
| 20-32 | Wire one workflow | One real task: "draft the three invoices due this week", "triage this week's issues". Hand it to the right agent in a room. | The agent stalls or answers badly: restate it as one outcome; if still bad, do the task by hand beside it and say so. Never hide it. |
| 32-40 | Invite the second person | Invite by email now, on the call, while the first result is visible. Keep the room open until they appear. | They cannot join live: send the join link into their phone, and book a 10-minute slot within 48 hours before you hang up. |
| 40-45 | First approval card | The agent's output becomes a card; the customer answers it, never the founder. Name what happened: "an agent did it, you decided." | No card appears: ask the agent to propose the next step needing a yes. If the product still shows none, file it as a bug before ending the call. |

Close: agree the week-4 check, confirm the `lightbulb-<slug>` channel, and write down what broke. Record the five numbers' inputs: setup done, second person in, handoff done.

**Never promise.**
- A discount, coupon, free month beyond the 30 days, or a different price for "being early". No refund promise of any kind.
- A certification (SOC 2 or similar), uptime or response-time terms, or that no model vendor sees your data. Say what the Privacy Policy says and send the rest in writing.
- Features not built: SSO, an App Store app, Delta-style inline comments.
- That an agent will do something without approval, or never make a mistake. Say that spend, deploys and hires wait for a person on an approval card, and that for email you decide who your agents can email.
- Revenue or growth results. Quote only Anna and Second Catch, and only as what happened to them.
- Any word from the avoid list ("multiplayer", "AI-native", "bot", "orchestration"). The one exception is quoting Grok Bot by name.
- A date for roadmap items, or that "AI employees" replace anyone's job.

## Build tasks
1. **Title: CEO agent asks who should see the first result.** Today: nothing asks (plan item 1). Change: the agent's next line after the first reply invites a teammate. Why: step 5 of the run sheet is done by hand. Measure: share of new workspaces with two or more people in 48 hours.
2. **Title: A first approval card in the first session.** Today: the first run lands on a board. Change: the first run ends with one approval card (plan item 2). Why: the run sheet's last step must not depend on luck. Measure: share of first sessions with a first approval.
3. **Title: A written data page.** Today: no page says where data lives or which model vendors see it. Change: a `/security`-style page limited to verified facts. Why: "what happens to our data" gets a link, not a promise. Measure: share of setup calls that raise it and still book.

## Measures
- Setup booked → set up, and set up → second person within 48 hours (Monday numbers).
- Share of calls that end with a completed approval card.
- Disqualifier rate by source; objection counts per call (founders tick the table after each call).
- Week-4 active and the "very disappointed" share.

## Founder decisions (13 Oct 2026)

- A1: No guarantee and no refund promise. The free month is the risk reversal; "week-4 active" stays as a metric only.
- B1: Name Second Catch now. Anna is cleared. No other team is named until three teams are paying.
- B6: Outbound email is already rule-based server-side; copy says "you decide who your agents can email". No per-message approval is promised.
- B8: No Slack Pro price. Slack's price appears only in the answer to "we already have Slack" (the price-for-8 line), nowhere else on calls.
- B9 (provisional): Say what the Privacy Policy says (isolated AWS environment per workspace; content not sold or used to train models) plus the provider path the team chose. Prasiddha answers in writing within a day. A data page is a build task. Revisit when the first call asks.
- C1: The founder who sourced the team runs its setup, Aden by default; the customer answers the first approval card.
- C4: Ask for the second person at booking; book anyway if they insist; the first approval card is the invite.
- "AI employees" on calls: yes (settled 13 Oct).

## Sources
- `docs/business-plan.md` §5, §6, §7, §12, Engineering/Product items 1-2 and 5, Go To Market "The setup session" and "Weekly rhythm".
- `.agents/product-marketing.md` v7: Objections, Switching Dynamics, Proof Points, Customer Language.
- `docs/research/2026-10-09-grok-bot-teardown.md`.
- CLAUDE.md (Workspace Runtime, Billing, Marketplace sections) for the data facts. Not independently re-opened against `origin/main` in this pass.
- Competitor price and product claims are the plan's; not re-checked live in this workstream (unverified).
