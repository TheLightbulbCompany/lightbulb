# 13 · Content and social: one founder post a week, per platform rules

## Purpose

Moves the "inbound launched" goal and feeds the Monday numbers (setup requests and trial starts by source). Serves the plan's Go To Market, "Always on, in parallel": one founder-signed post a week each, from now, on the platforms the category reads.

## What we know

| Fact | Source | Confidence |
|---|---|---|
| Owners: Leo X and Reddit, Aden LinkedIn, Prasiddha Hacker News and Product Hunt. Every public post is signed by a named founder; agents draft and never publish | `docs/business-plan.md` §8 and Go To Market "Owners" | High (first party) |
| X posting is manual: the X reply pipeline has been dead since 11 October (API credits) | Plan, Go To Market "Owners" | High |
| The blog has six posts on `origin/main`. Authors are "Lightbulb" or "The Lightbulb team". Two are from July, three from 24 August, one from 20 August. The voice is second-person explainers with bold emphasis. One title says "AI-native", three tag or name "paperclip", one names Hermes and AgentCore, and the posts end in a product link (`/chat`, `/docs/...`) | `apps/frontend/src/content/blog/posts/*.md` | High |
| The best-performing language is specific. "Your agent remembers what you did… no idea what your co-founder" comes from an HN comment by Egregore's founder, who calls existing agent memory "all single-player" | [HN 47027197](https://news.ycombinator.com/item?id=47027197) | High (primary) |
| "Multiplayer" gets mocked. In the Delta launch thread (679 points, 259 comments) replies include "Multiplayer? It's a game?" and "Never found a use for this sort of thing once novelty wore off". The top-quoted pain is "Ive never seen them collaborate ON the chat. they do collaborate on the PR" | [Delta thread](https://news.ycombinator.com/item?id=49276574), comments [49280693](https://news.ycombinator.com/item?id=49280693), [49281058](https://news.ycombinator.com/item?id=49281058), [49279530](https://news.ycombinator.com/item?id=49279530) | High (primary) |
| Show HN is for things people can try, "ideally without signing up". Blog posts, landing pages and sign-up pages do not qualify. Don't ask friends to upvote or comment | [Show HN rules](https://news.ycombinator.com/showhn.html) | High |
| HN: "Please don't use HN primarily for promotion"; "Don't solicit upvotes, comments, or submissions"; no uppercase or exclamation marks in titles | [HN guidelines](https://news.ycombinator.com/newsguidelines.html) | High |
| r/ClaudeAI (1.19M members), Rule 7 "Showcase your project in a way that helps educate and inspire others": promotion is encouraged if you built it with or for Claude, explain how Claude helped, say it is free to try, keep promotional language minimal, use no referral links. "Posts on the feed now require OP karma>100". Rule 10 "Don't manipulate upvotes" is a permanent ban | [r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/) rules sidebar, read 13 Oct | High (first party) |
| r/ClaudeCode (446K): "No Spam or Low-Effort Promotion" (no disguised ads or repeated promotion); "Projects & Showcases": simple sharing goes in the weekly showcase thread, and a standalone post must explain what was built, how Claude Code was used and what you learned | [r/ClaudeCode](https://www.reddit.com/r/ClaudeCode/about/rules.json), read 13 Oct | High |
| r/startups (2.14M), Rule 2: no promotion of any kind outside stickied threads; "You MAY share your startup in the Monthly Share Your Startup thread". Rule 3: posts discuss methods and experiences without naming your own project, with at least 250 characters. Rule 5: links in comments only to content "you have no affiliation with". Rule 6: no DM solicitation. Rule 7: own blog content must be posted in full; "No blogs about your 'startup' journey allowed". Titles carry "I will not promote" by convention | [r/startups](https://www.reddit.com/r/startups/) rules sidebar, read 13 Oct | High |
| r/smallbusiness (2.55M): Q&A only; no links or product mentions that read as promotional (since June 2026); promotion only in the weekly Promote-your-business thread; no market-research posts | [r/smallbusiness](https://www.reddit.com/r/smallbusiness/about/rules.json), read 13 Oct | High |
| r/ChatGPTCoding: Rule 5 "No pure self-promotion / low-value project posts": promotion goes in the weekly Self-Promotion thread ("Delete the link: if nothing is left, it's an ad"). Rule 3: no question posts that indirectly promote. Rule 6: no AI slop | [r/ChatGPTCoding](https://www.reddit.com/r/ChatGPTCoding/) rules sidebar, read 13 Oct; member count not readable | High |
| Site analytics: a pageview fires on every public URL with the query string. `first_touch_landing_path` keeps the UTM, so a blog landing with `?utm_…` is attributable. `/request-setup` returns 404 today | `apps/frontend/src/components/PostHogProvider.tsx`, `lib/__tests__/analytics.test.ts`; `curl` 13 Oct | High |
| SEO target queries with no Lightbulb answer: "share a Claude Code session with my cofounder", "AI employees for a small team", "Slack alternative with AI agents as teammates", "Lightbulb pricing" | `docs/marketing/05-seo.md` Part C, B5, B6 | Medium (one engine, one run) |
| Show HN target Tue 8 Dec, Product Hunt Tue 5 Jan; posts start in plan week 2 (19 Oct) | `docs/marketing/01-marketing-plan.md` §B | High (our plan) |

## Findings (ranked by impact)

1. **"One post a week" has to mean different things per platform.** On HN a weekly self-submission breaks "don't use HN primarily for promotion". Prasiddha's weekly unit is therefore a substantive, disclosed comment. He submits our own writing about once every three weeks, and Show HN happens once. On Reddit the rules are explicit. r/ClaudeAI requires more than 100 karma for any feed post, and r/startups forbids naming your own project outside its monthly thread and forbids linking your own content in comments. Leo's first contact in each subreddit is a link-free answer, and until his karma is over 100 he posts only replies. Every founder replies in his own name with no pitch; a DM only after someone asks for help in the thread.
2. **Proof is the bottleneck, not ideas.** Every strong hook needs a specific artifact: a recorded handoff, a timed first ten minutes, an approval card, Anna's consent, Second Catch's consent. The calendar names the proof for each post. A post without its proof slips a week rather than going out vague.
3. **The voice works when it describes work, not the product.** The Delta thread rewards the PR-level detail ("they collaborate on the PR") and mocks the category word. Our customer language carries the same signal: "starts from zero", "who owns a change", "which actions should stop for approval".
4. **The existing blog speaks a retired voice.** "AI-native", Paperclip, Hermes, an organisation byline, and links to `/chat` instead of the setup door. New posts need a named founder byline and the two doors.
5. **We have honest material no competitor publishes:** running the company on our own agents, agents that talk too much, no public dataset of teams chatting with AI coworkers, and one-person workspaces as the problem. The content-strategy skill ranks this kind of "meta content" as shareable.

## Recommendation

Run one editorial line across three founders. Make the blog the owned flagship and atomize each flagship into a thread, a LinkedIn post, an HN comment or submission, a Reddit answer and a docs paragraph (repurposing rule below). The mix: 60% searchable (the three first posts target 05's queries), 30% shareable (how we run the company, honest numbers, agent behaviour), 10% experimental (a dataset write-up, the Product Hunt day). Agents draft in our own workspace; a named founder edits and posts by hand.

Rejected: separate content per platform ("spray-and-pray" in the skill); reviving automated X replies (unsigned); weekly HN submissions (against the guidelines); scheduling tools wired to agents.

## Deliverable

### 1. The editorial line

We write about real work: what our agents and the founding teams actually did, with the invoice, the PR, the plan and the approval in the post. We write about the moment work moves from one person to another, or from a person to an agent: the handoff, and what gets lost when "your agent remembers what you did" but nobody else's does. We write about how three founders run the company on it, including what broke. We never write feature tours, launch hype, roadmaps dressed as news, or category talk. We never say "multiplayer", "AI-native", "orchestration", "bot" or "prompt". Hermes and Paperclip may be named as "built on", never as the product's name; we name no other internal systems. Every post carries one thing a reader could check, and says plainly what is early.

### 2. The 12-week topic calendar

Weeks start Monday. Hook lines are drafts: the founder posts one only once its proof is true. P1–P3 are the three first blog posts (part 4). "Comment" means a disclosed reply in someone else's thread, with no link unless asked.

| Wk | Founder | Platform | Angle | Hook line | Proof it needs | Draws on |
|---|---|---|---|---|---|---|
| 1 · 19 Oct | Leo | Reddit r/ClaudeAI (comment) | Answer the newest "two devs, several Claude sessions" thread: what context we carry across sessions, what stops for approval | "The agent wasn't what broke for us. Adding the second person was." | Second Catch may be named; a founder vets the post | 02 thread list; plan §5 |
| 1 | Aden | LinkedIn | The single-player problem | "Your agent remembers what you did. It has no idea what your cofounder figured out yesterday." | Credit the HN source; no product claims | Customer language; HN 47027197 |
| 1 | Prasiddha | HN (2 comments) | Agent memory and handoff threads: what we learned building shared sessions | n/a | Disclosure line: "I work on Lightbulb" | 02; 06 |
| 2 · 26 Oct | Leo | X thread | What our agents may do alone and what waits for one of us | "Our agents send email and open PRs. Here is what we decide first: who they may email, and what waits for a yes." | Screenshot of a real approval card from our workspace | 04; approvals blog |
| 2 | Aden | LinkedIn | Anna's invoices | "A one-person food business sends invoices from QuickBooks through an agent now." | Anna is cleared (quotes cleared 12 Oct); a founder vets the post | 16; case study |
| 2 | Prasiddha | HN (comment) | Session handoff: what a teammate needs besides the transcript | n/a | None beyond disclosure | 06; Delta thread |
| 3 · 2 Nov | Leo | Reddit r/ClaudeCode (standalone post under "Projects & Showcases": what we built, how Claude Code was used, what we learned) | How two people hand off a Claude Code session (P1) | "Every new Claude Code session starts from zero. Here is how we stopped re-explaining to each other." | A recorded handoff between two founders; never cross-post the same text to r/ClaudeAI; a reply only until Leo's karma is over 100 | P1; 05 B5 |
| 3 | Aden | LinkedIn (document post) | The handoff in four slides | "The handoff is where agent work gets lost." | Four screenshots from the P1 recording | P1 |
| 3 | Prasiddha | HN (submission) | P1 as a regular story | Title: "Handing a Claude Code session to a teammate" | P1 live, with a founder byline | P1; 05 |
| 4 · 9 Nov | Leo | X thread | The CEO agent's first ten minutes in a new workspace (P2) | "We made a new workspace and wrote down everything the CEO agent did before anyone typed again." | A timed run by a founder in a fresh workspace; numbers from that run only | P2; 08 |
| 4 | Aden | LinkedIn | What you'd want from a new hire on day one, and what the agent did | "I'd judge a new hire by their first ten minutes. So I timed our CEO agent's." | The same run | P2 |
| 4 | Prasiddha | HN (comment) | Threads about AI employees for small teams | n/a | Disclosure | 05 Part C |
| 5 · 16 Nov | Leo | Reddit r/ClaudeAI (Rule 7 showcase if karma >100, else the showcase megathread) | The session-sharing plugin, listed | "A plugin that lets a teammate watch your Claude Code session and pick it up." | Plugin live (14); free to try, said in the post; karma >100 | 14; 06 |
| 5 | Aden | LinkedIn | Why people are free up to five (P3) | "We charge for the work, not the people. Here's why five people cost the same as one." | `/pricing` live, after billing ships | P3; 15 |
| 5 | Prasiddha | HN (comment) | Per-seat pricing threads | n/a | Disclosure | P3 |
| 6 · 23 Nov | Leo | X (single post) | Stop as feedback | "When someone stops an agent mid-reply, that is the most honest feedback we get." | Mechanism only; no rates until the ledger exists | Socially-aware research |
| 6 | Aden | LinkedIn | When should an AI teammate speak in a group chat | "Most AI in group chats speaks only when tagged. We think a good colleague does better than that." | Teams Copilot and Grok Bot docs cited in the research | Socially-aware research |
| 6 | Prasiddha | HN (submission) | Blog: teaching agents when to stay quiet | Title: "Deciding when an agent should speak in a team chat" | The blog post; cite κ = 0.49 human agreement | Socially-aware research |
| 7 · 30 Nov | Leo | Reddit r/startups ("I will not promote" discussion post, no product name or URL, per Rules 2, 3, 7; Lightbulb itself only in the Monthly Share Your Startup thread) | What we hand to agents in a three-person company, as a method | "[I will not promote] What we let agents do alone in a three-person company, and what waits for a founder" | The Monday readout has run 3+ times; 250+ characters; no links; a reply only until Leo's karma is over 100 | 01 §D; 09 |
| 7 | Aden | LinkedIn | Why we don't discount | "We gave our first customers 100% off. Here's what that taught us about price." | Plan §7 figures, with founder OK | Plan §7; 15 |
| 7 | Prasiddha | HN (comment) | Prepare Show HN: answer one thread in the category | n/a | Disclosure | 07 |
| 8 · 7 Dec | Prasiddha | Show HN (Tue 8 Dec) | Share a Claude Code session; the viewer can join the team | Title: "Show HN: Lightbulb – share Claude Code sessions and AI agents with your team" | A session link a reader can open without signing up (item 4); 3+ founding-team handoffs; P answers every comment all day | 07; 06 |
| 8 | Leo | X thread | The build story behind launch day, linking to the product, not the HN thread | "Three founders, six agents, one workspace. What we built and what's still rough." | Launch go/no-go passed | 07 |
| 8 | Aden | LinkedIn | A founding team's first handoff | "The first thing that broke was adding the cofounder. Here's what it looks like now." | Second Catch (may be named) | 16 |
| 9 · 14 Dec | Leo | Reddit r/ClaudeCode (comment-led) | What the launch comments taught us about handoffs | "The most common question was 'who owns the change?'" (only if it was) | The Show HN thread, read and summarised | 07 |
| 9 | Aden | LinkedIn | The week-4 question | "We asked our first teams one question in week four." | Aggregate answers, with each team's consent | 16; 01 §E |
| 9 | Prasiddha | HN (comments) | Follow-ups on Show HN comments | n/a | n/a | 07 |
| 10 · 21 Dec | Leo | X thread | No public dataset of teams chatting with AI teammates | "We went looking for real team chat with AI coworkers in it. None exists." | The datasets research, with the licence table | Datasets research |
| 10 | Aden | LinkedIn | The second person | "We end every setup session the same way: someone invites a teammate." | Setup-session count from the tracking sheet | 04; 08 |
| 10 | Prasiddha | HN (submission) | Blog: the dataset search and the licence traps | Title: "No public dataset has a team chatting with AI coworkers" | The blog post | Datasets research |
| 11 · 28 Dec | Leo | X (single post) | What our agents did for us this quarter | "This quarter our agents opened N PRs and filed N tasks for the three of us." | Counts from our own workspace only | Our workspace |
| 11 | Aden | LinkedIn | What we got wrong | "33 of 38 workspaces that signed up have one person in them. We built for teams." | Plan §9 numbers, refreshed, with founder OK | Plan §9 |
| 11 | Prasiddha | HN (comment) | Year-end "what are you working on" threads | n/a | Disclosure | — |
| 12 · 4 Jan | Prasiddha | Product Hunt (Tue 5 Jan) + one HN comment | The founding teams' handoffs | Tagline per 07 | 07's launch kit | 07; 14 |
| 12 | Leo | X (single post) | Product Hunt day, without asking for upvotes | "We're on Product Hunt today. The best thing you can do is try a session link and tell us what broke." | PH page live | 07 |
| 12 | Aden | LinkedIn | Launch post with Anna's and a founding team's proof | "Two teams, two different jobs, the same pattern: the agent does it, a person says yes." | Anna and Second Catch, vetted by a founder | 16 |

### 3. Per-platform rules

**X (Leo).**
- Use a thread for a sequence: a how-to, or a story with screenshots, 4–8 posts. Post 1 is the hook and must stand alone. The link goes in the last post or a reply, never in post 1.
- Use a single post for one observation or one number.
- Every visual is a real screenshot from our workspace, with no customer content.
- Reply etiquette: answer the question in the reply itself, with no link on first contact. Add "I'm one of the founders" whenever the product comes up. Reply in Leo's own name, no pitch; a DM only after someone asks for help in the thread. Never quote-dunk.
- Reply within the first 30 minutes on large accounts (skill: listening.md).
- Replies are typed by Leo. An agent may draft a reply list each morning (social skill: daily listening, top 10 scored), but there is no automated reply.
- Write 3–5 replies a day in the wedge's threads. That counts as distribution, not as the weekly post.

**LinkedIn (Aden).**
- First person, past tense, one concrete scene. Aden shares three past LinkedIn posts so drafts match his voice.
- Length 1,200–1,500 characters. The hook goes before "see more", with short paragraphs.
- The link goes in the first comment, not the body (platforms.md: "Links in the main post (kills reach)").
- Document posts for step-by-steps; at most 3 hashtags; tag only people who took part and agreed.
- Answer every comment in the first hour.

**Reddit (Leo).**
Rules read from each sub's sidebar on 13 Oct (high confidence); re-read before each post, since mods change them.
- **r/ClaudeAI.** Rule 7, "Showcase your project in a way that helps educate and inspire others", allows our plugin and session-sharing posts: say we built it, explain how Claude helped, say it is free to try, keep the language plain, use no referral links. Feed posts need more than 100 karma; below that, use the showcase megathread. Rule 9: pick the closest flair. Rule 10, "Don't manipulate upvotes", is a permanent ban.
- **r/ClaudeCode.** "Projects & Showcases": a simple share goes in the weekly showcase thread; a standalone post must say what we built, how Claude Code was used and what we learned. "No Spam or Low-Effort Promotion" rules out repeated promotion. "Stay On Topic": Claude Code only. Never post the same text the same day as r/ClaudeAI.
- **r/startups.** Rule 2: no promotion outside stickied threads; Lightbulb may appear only in the Monthly Share Your Startup thread. Rule 3: other posts discuss a method or experience without our name or URL, 250+ characters. Rule 5: links in comments only to content we have no affiliation with, so never our blog. Rule 6: never "DM me". Rule 7: no startup-journey blogs, and our own writing goes in full in the post. Use the "I will not promote" title convention.
- **r/smallbusiness**: not in this calendar (fallback audience; promotion only in its weekly thread).
- **r/ChatGPTCoding** (optional, for Codex users): Rule 5 sends promotion to the weekly Self-Promotion thread; its test is "Delete the link: if nothing is left, it's an ad".
- Leo checks his Reddit karma and posts only replies until it is over 100. In every sub, help first: first contact is link-free, and the product is mentioned only when someone asks. Disclose affiliation in the comment itself. Long, specific answers win (listening.md). Aim for roughly 9 helpful comments for every 1 that mentions us.

**Hacker News (Prasiddha).**
- Show HN happens once (8 Dec), and only when a reader can try a session link without signing up.
- Our own blog posts are submitted as regular stories, at most one every three weeks, under the post's own title, with no adjectives, capitals or exclamation marks.
- Never ask anyone, founders included, to upvote or comment. Never link the HN thread from X, LinkedIn or the team channel with an ask.
- Comment etiquette: disclose, answer the actual question, concede what's rough, and suggest alternatives, including competitors, when they fit better. On launch day, stay in the thread all day.

### 4. The three blog posts to write first

All three get a founder byline, the two doors at the end ("Request a guided setup", "Start free"), no internal names beyond "built on", and screenshots from our own workspace that a founder has vetted.

**P1 · "How a two-person team hands off a Claude Code session"** (Prasiddha with Leo; publish wk 3)
Target queries: "share a Claude Code session with my cofounder", "share claude code session" (05 B5). Searchable; feeds the `/claude-code-session-sharing` page.
1. The problem in their words: "every new Claude Code session starts from zero"; the PR is where collaboration happens today (Delta thread).
2. What a handoff has to carry: the reasoning and open questions, not just the diff. Credit Egregore's idea (plan §6).
3. Our handoff, step by step: share the session, the teammate watches, joins and picks it up, with a screenshot at each step from the recorded run.
4. What stays with a person: who owns the change, and what stops for approval.
5. What's rough today, stated plainly.
6. Alternatives (Lody, Delta, Egregore) in one fair paragraph with links.
7. The two doors.

**P2 · "What the CEO agent did in its first ten minutes"** (Aden with Leo; publish wk 4)
Target query: "AI employees for a small team" (05 Part C). Shareable and searchable.
1. Setup: a fresh workspace, one founder, a stopwatch, and one real ask.
2. The timeline, minute by minute, from the recorded run only.
3. What it handed to the specialists and what it brought back.
4. What waited for a person: the first approval card.
5. The moment it got useful: the second person joins and continues the work.
6. What we'd change; what the first session should produce (plan Engineering/Product items 1–2).
7. The doors.

**P3 · "Why we keep people free up to five"** (Prasiddha; publish when `/pricing` goes live, after seat billing ships)
Target queries: "Lightbulb pricing", "Slack alternative with AI agents as teammates" (05 B1, B6). Decision-stage.
1. The line: "People are free up to five; you pay for the workspace and the work your agents do."
2. Why per-seat pricing taxes the thing we want: the second person.
3. The arithmetic: an 8-person team pays $110, or $13.75 a seat (plan §7). No competitor prices in this post; the comparison pages carry them.
4. What the agents' work costs: provider price × 1.2, or bring your own ChatGPT account or key.
5. No discounts and no coupons. 30 days free with a card on file, first charge on day 31, price locked two years for founding teams.
6. FAQ.
7. The doors.

### 5. The repurposing rule

One piece of real work becomes six things, in this order, within seven days:
1. The founder who did the work drops a two-line note and the artifact (screenshot, recording, numbers) in our `#marketing-drafts` channel.
2. The marketing agent drafts the flagship (a blog post if it holds 600+ words, else a LinkedIn post).
3. From the flagship it drafts an X thread (Leo), a LinkedIn post (Aden), an HN comment or title (Prasiddha), a bank of 2–3 Reddit answers for live threads, and one paragraph for `/docs`.
4. Each founder rewrites their atom in their own words. The same text never goes to two platforms.
5. Stagger the posts: blog day 0, HN day 0–1, LinkedIn day 1–2, X day 2–3, Reddit answers as threads appear.

### 6. The approval and signing rule

- Agents research, draft and prepare. They never log in to a founder's social accounts, never schedule, and never publish. No scheduling tool is connected to any agent.
- Each draft lands in `#marketing-drafts` as a message to the owning founder. The founder edits it, posts it by hand from their own account, and pastes the live URL back into the thread. That URL is the record.
- Before posting, the founder checks:
  - every number has its source;
  - customer consent is on file for any named customer;
  - there are no internal names beyond "built on" and no banned words;
  - a founder has vetted every number and screenshot;
  - the link carries the UTM;
  - affiliation is disclosed on Reddit and HN.
- A post with an unverified claim waits a week.

### 7. Measures

UTM on every link: `utm_source={x|reddit|linkedin|hn|producthunt}&utm_medium=social&utm_campaign=ws13-w{NN}-{slug}&utm_content={leo|aden|prasiddha}`. On HN use the clean URL and rely on the "How did you first hear about us?" field (plan §9).

| Measure | Source | Read |
|---|---|---|
| Setup requests and trial starts whose first-touch landing path carries `utm_campaign=ws13-*`, per post and per founder | PostHog `first_touch_landing_path`; 09's events | Monday readout, weekly |
| Self-reported source on the setup form and at sign-up | 09 / 10 | Weekly |
| Replies and DMs that became a conversation (two-way, about their team's work), then a setup booked | The manual tracking sheet (01 §B), filled from the founders' inboxes by the marketing agent | Weekly |
| Blog sessions by source; Search Console impressions for the P1–P3 queries | PostHog; Search Console (05 B11) | Monthly |
| AI-search mention rate for 05's five queries | 05's monthly check | Monthly |

Likes and impressions are recorded, not targeted. Target: one founding-team setup from content by week 12 (low confidence; re-fit at 01's week-6 check).

## Build tasks

**C1 · Founder byline and two doors on blog posts.**
- Today: author "Lightbulb"; posts end with links to `/chat` or `/docs`.
- Change: add `author` and `authorUrl` to the front matter (as in 05 B8): Prasiddha for product and engineering, Leo for comparisons, Aden for customer stories. Add a shared end-of-post block with "Request a guided setup" (`/request-setup`) and "Start free" (`/sign-up`), which pass through the landing UTM.
- Why: signed posts, and the setup door is the stage-1 conversion.
- Measure: setup requests where the first touch was a `/blog/*` path.

**C2 · Retire the off-voice post.**
- Today: "What is an AI-native company operating system?" is live, with Paperclip tags. Two other posts name Hermes/AgentCore or Paperclip.
- Change: retitle and rewrite to the plan's words, or 308 it to P2 once that exists. Strip internal-name tags from every post; "built on Hermes and Paperclip" is allowed, never as the product's name.
- Why: the voice rules.
- Measure: `git grep -i "ai-native\|paperclip" -- apps/frontend/src/content/blog` returns zero.

**C3 · UTM through to the setup request.**
- Today: `/request-setup` is 404 (10 builds it).
- Change: store `first_touch_landing_path` and the UTM fields on the setup-request record and on the trial-start event.
- Why: the per-post measure.
- Measure: one test request with a `ws13` UTM appears in the Monday readout.

**C4 · `#marketing-drafts` channel and a Friday drafting routine** (no code).
- Today: none; X replies are dead.
- Change: the marketing agent runs a Friday routine that posts next week's three drafts and a Reddit/HN thread shortlist, scored with listening.md's rubric.
- Why: founders edit; they don't start from blank.
- Measure: drafts posted versus posts published per week.

## Founder decisions (13 Oct 2026)
- B4: Our own numbers and founder-workspace screenshots are allowed; a founder vets every number and screenshot before posting.
- B10: Hermes and Paperclip may be named as "built on", never as the product's name.
- C2: Founders reply in public threads in their own names, no pitch; a DM only after someone asks for help in the thread.
- C3: Leo signs X and Reddit, Aden LinkedIn, Prasiddha HN and Product Hunt.
- C11: Aden shares three past LinkedIn posts.
- C12: Leo checks his Reddit karma and posts only replies until it is over 100.
- B1/B2: Second Catch may be named and Anna is cleared; no other team until three teams pay.
- E9: The "free up to five" post goes out when `/pricing` goes live, after billing ships.

## Sources

- `docs/business-plan.md` §5, §6, §7, §8, §9, Go To Market (13 Oct working copy)
- `.agents/product-marketing.md` v7
- `docs/research/2026-10-13-socially-aware-agents.md`; `docs/research/2026-10-13-messaging-datasets.md`
- `docs/marketing/01-marketing-plan.md`, `05-seo.md`
- `origin/main`: `apps/frontend/src/content/blog/posts/*.md`, `apps/frontend/src/components/PostHogProvider.tsx`, `apps/frontend/src/lib/__tests__/analytics.test.ts`, `apps/frontend/src/app/sitemap.ts`
- HN: [Show HN rules](https://news.ycombinator.com/showhn.html), [guidelines](https://news.ycombinator.com/newsguidelines.html), [Delta thread](https://news.ycombinator.com/item?id=49276574), [Egregore comment](https://news.ycombinator.com/item?id=47027197)
- Reddit rules, read from each sub's sidebar or `about/rules.json` on 13 Oct 2026: [r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/), [r/ClaudeCode](https://www.reddit.com/r/ClaudeCode/), [r/startups](https://www.reddit.com/r/startups/), [r/smallbusiness](https://www.reddit.com/r/smallbusiness/), [r/ChatGPTCoding](https://www.reddit.com/r/ChatGPTCoding/)
- Skills: `marketing-skills:content-strategy` (searchable vs shareable, 60/30/10, create once distribute twice), `marketing-skills:social` (platforms.md, listening.md comment tiers)
