# 26 · Messaging positioning: the homepage, every machine-read surface, the network launch and how we measure it

Written 26–27 October 2026 with the product-marketing, competitors, competitor-profiling, customer-research, cro, copywriting, seo-audit, ai-seo, schema, launch, directory-submissions, analytics, ab-testing, social, content-strategy, site-architecture and marketing-plan skills, after reading docs/marketing 00–25, the business and marketing plan (Google Doc, 19 Oct merge), `.agents/product-marketing.md` v8, docs/growth, docs/research (Oct), the network specs and charter, `origin/main` at `ba2753093`, the live site, Product Hunt, X, GitHub and PostHog reads by Gauge (ISOA-960, 961, 962).

## Purpose

Moves the stage-1 goal (inbound launched; ten teams with a card on file by 4 January) and the number on the wall (teams with two people and a handoff). Serves the business plan §5–§6 (customer, positioning), §8 (go-to-market), Part 2 §2 (words), §5 (measurement), §7 (launch), and the website brief. It replaces the homepage half of workstream 10 and the launch half of workstream 07; it extends 05 (SEO), 09 (analytics), 11 (competitors) and 14 (directories) rather than replacing them.

## What we know

| Fact | Evidence | Confidence |
|---|---|---|
| Founder ruling 26 Oct: come off the "AI employees" angle and become messaging; the network leads the homepage, the workspace is the paid layer; "parity with Reason Network, then improve" stands (charter 22 Oct) | this session; `docs/superpowers/plans/2026-10-22-lightbulb-network-goal.md`; `docs/research/2026-10-24-network-roles-reconciliation.md` ("our free network should not be stricter than Reason's"; "their business is the hosted hub; ours puts revenue in the paid workspace") | high |
| The network ships: one install line; macOS 13+ (Apple silicon) and Linux, WSL on Windows; Claude Code, Codex, Cursor, Grok CLI, OpenCode; `lightbulb invite` prints a four-word code (once, 10 min), a link and a join line (7 days); flat network, an invite is the approval, nobody is asked; Ed25519 key per computer, signed roster; peer to peer with a relay fallback that sees ciphertext; Apache-2.0; no account; macOS builds signed and notarized; anonymous usage counts (`cli_installed`, `cli_updated`, `network_joined`, `invite_created`, `cli_daily`) to PostHog, off with `lightbulb telemetry off` or `DO_NOT_TRACK=1` | TheLightbulbCompany/cli README and `docs/security.md` at v0.6.4 (27 Oct); `apps/frontend/src/content/docs/pages/agent-network*.md` on main (26 Oct, commit `31a869a56`) | high |
| The network's reach today: repo public 22 Oct, 0 stars, 4 views (2 unique) in 14 days, five releases on 26 Oct; `/network` linked only from the footer ("Free agent network", 26 Oct) | GitHub traffic API 26 Oct; `Footer.tsx` on main | high |
| Homepage today: H1 "Your team and your AI employees, in one place." (A) or "Autonomous AI agents that live in your team's chat and do the work." (C); one filled button "Try Lightbulb free"; guided setup as outline; trust row; light HTML copy of /chat; sections Problem → How it works → See the work → Proof → $2M band → Pricing → Founding teams → FAQ → Ask AI | `Hero.tsx`, `headlineVariant.ts`, `page.tsx` on main (PR #4412, 26 Oct) | high |
| Headline history: "Build a company that runs on AI." (3 Aug) → "An AI team that runs the business, a CEO that manages it." (7 Sep) → "AI employees for small teams & solo founders." (8 Sep) → "Your team and your agents, in one place." (11 Sep) → "…your AI employees…" (14 Oct) → variant C (26 Oct). Five headlines in twelve weeks, none measured against another with enough traffic | PRs #1577, #2358, #2387, #2487, #4104, #4412 | high |
| Traffic: about ten visitors a day; 227 in the 30 days to 12 Oct; three quarters direct or unattributed; launch day 20 Oct: 54 unique visitors, 383 pageviews, one external sign-up start, one external workspace, none verified; the "16 sign-ups" in the feed included founders and no longer reproduce | plan §9; Gauge re-read 27 Oct (ISOA-962 comments) | high |
| Last week (19–25 Oct): 11 verified external users, 38 active user-days, 8 users sent 32 messages and got 26 agent replies; the top three users were 64% of activity; 4 sign-up starts; activation unmeasurable until the `chat_completed` event (PR #4240, merged 27 Oct) is deployed and verified | ISOA-960, 961, 867 | high |
| Launch reach: Product Hunt 3 upvotes, 25 followers, 0 reviews, one "Congrats" reply; X launch post 36 views, 1 reply; brand account's best post 91 views; Hacker News 0 mentions of the domain; Reddit none found; the Show HN was gated and never posted | producthunt.com/products/lightbulb-2, x.com/lightbulbai, hn.algolia.com, 26 Oct | high |
| AI-search baseline today (one engine, one run each): "coding agents talk to each other Claude Code Codex peer to peer" cites ccpair, ccx, claude-peers, Claude Code Agent Teams, Claude Nexus, grpvn; "share a Claude Code session with a teammate" cites Claudebin, Lody, claude-share; "Lightbulb agent network … /network" cites a third-party namesake (RPasquale/lightbulb-mcp) and GitHub Agent HQ; "Reason Network alternatives" cites AgentNet and Agent Hub. Lightbulb appears in none, including for its own network's name. The 13 Oct baseline (six queries) was also zero | WebSearch 27 Oct; 05-seo Part C | medium (repeat 3–5 runs per platform for a rate) |
| Reason Network: launched 16 Oct by Reason Machines (the paid "Reason" coding agent); "Let your agents talk to each other."; 1,265 stars and 108 forks by 27 Oct (+585 in the last week); v0.12.13; MIT or Apache-2.0; macOS, Linux, Windows; Claude Code, Codex, Cursor, Gemini CLI, Copilot CLI, Grok CLI, opencode, pi, DeepSeek Harness, Reason; hosted assistants (Grok Bot, Muse, Dots) via its MCP server; sign-in required (Google, GitHub, Apple, email); iroh peer to peer with encrypted relay fallback; "Zero data retention · SOC 2" in the footer; paid product: Cloud Reason sessions; paste-to-agent line "Join the Reason Network: https://reasonmachines.com/network"; covered by AlphaSignal 20 Oct; no Hacker News story | reasonmachines.com/network, github.com/reason-machines/reason-network, ossradar.net, alphasignal.ai, 27 Oct | high |
| AgentRelay (AgentWorkforce): hosted hub, 926 stars, Apache-2.0, telemetry on by default, injects `--dangerously-skip-permissions`; the 24 Oct comparison scores "ours better 8, theirs better 5" | `docs/research/2026-10-24-agentworkforce-relay-vs-lightbulb.md` (local, untracked) | high |
| HN evidence for the launch shape: Egregore's Show HN ("Shared memory and coordination for multiplayer Claude Code") 5 points; Lody 2; "Real-time dashboard for Claude Code agent teams" 77; 2.9% of 37,623 Show HNs reach 50 points; best window 15:00–18:00 UTC | 07-launch | high |
| The site's voice test bans multiplayer, AI-native, orchestration, bot, prompt, autonomous, Paperclip, Hermes, AgentCore, Jev on the homepage; "AI employees" is the plan's public word; the $2M band has no source (open decision 10) | `HomeVoice.test.tsx`; plan Part 2 §2, §12 | high |
| The customer who pays: founder-led teams of 3 to 15 already running two or more agents; the ICP excludes solo operators and pairs whose work does not overlap; 33 of 38 outside workspaces have one person; the 13 Oct website brief rejected "Get started free" as primary because "it fills the funnel with single people" | plan §5; 00-decisions; 10-website-copy Finding 1 | high |
| Customer language: "overshadowed by Claude… I don't need 5 agents" (Santi); "I'm really impressed with how fast it works" (Anna, cleared); "the first thing that broke was adding the cofounder" (Second Catch); "your agent remembers what you did. It has no idea what your co-founder just spent four hours figuring out" (Egregore's founders, HN); "none of it syncs" (r/smallbusiness); "re-explaining the stack in their prompts every session" (r/ChatGPTCoding); "I use Codex, my non-technical co-founder uses Claude" (HN); "getting the humans and agents to talk together is really fragmented" (HN) | user feedback room; `.agents/product-marketing.md` v8; plan Part 2 §6 | high |
| The marketing workstreams (docs/marketing 00–25), `.agents/product-marketing.md`, `docs/business-plan.md` and `docs/marketing-plan.md` are committed only on the local branch `codex/lantern-font`; none of them is on `origin/main` or any remote branch | `git ls-tree origin/main`, `git branch --contains` on 27 Oct | high |
| PostHog, read directly on 27 Oct (30 days to 27 Oct; founders excluded where a person has a company email; anonymous visitors included): homepage referrers for `/`: direct 99 visitors, m.facebook.com 25, l.instagram.com 17, reddit.com 11, l.threads.com 9, t.co 8, linkedin.com 7, reddit app 4, producthunt.com 4, google.com 1. Funnel (people): landing_viewed 352 → signup_cta_clicked 22 (6%) → signup_started 9 → workspace_created 9 → team_formed 3 → handoff_completed 4; setup_cta_clicked 4. Sign-up starts by source: direct 6, t.co 1, flickmeon.ai 1, reddit app 1, Product Hunt 0. Network counts (no founder exclusion possible; install ids carry no email): cli_installed 7, network_joined 5, invite_created 2 people (8 invites), cli_daily 3 computers. Web analytics, unfiltered: 433 visitors, 1.29K sessions, 6.76K pageviews of which 5,142 on /chat; `/` 319 visitors, 38.6% bounce; /sign-up 30; /pricing 14; /request-setup 14 (87.5% bounce); /docs 19; /network not in the top ten. Channels: direct 298, organic social 108, referral 34, organic search 11, AI 1 | HogQL in the PostHog SQL editor and the Web analytics page, 27 Oct | high |
| The Termly privacy policy (29 May 2026) carries intent-data and advertising clauses, Do Not Track, Telegram/Discord/WhatsApp, contradictions in the state-law table, no retention or deletion numbers, and nothing about the network | thelightbulb.company/privacy, 26 Oct | high |

## Findings

1. **The homepage has changed its headline five times in twelve weeks and never had the traffic to learn from any of them.** At ten visitors a day and an 8% click rate, detecting a 20% lift needs roughly 5,000 visitors per variant, about three years; a 50% lift needs about 1,000 per variant, seven months. The A/C test running now will not resolve. The honest read is that the page was never the constraint; reach was. (cro, ab-testing)
2. **Reach is the constraint, and the only part of Lightbulb that spreads on its own is the network.** Every invite is an install; every second computer is a network. The workspace trial does not spread (an invite card after connectors, admin-only until 14 Oct). The plan's own blitzscaling audit said "Distribution ❌ Fire #1… network effects essentially none today." The network is the first loop the product has ever had, and it costs nothing to serve. (marketing-plan, launch)
3. **The free layer is table stakes and ten days late.** Reason Network shipped on 16 Oct with a known brand behind it and has 1,265 stars; AgentRelay has 926. Both sit exactly where a "your agents talk to each other" homepage lands. Leading with the network as if it were new would read as a clone to the audience that matters (HN, r/ClaudeCode, X dev circles) and would be fact-checked within the hour. What Lightbulb has that neither has: a workspace with people and agents in the same rooms, approvals, email, phone, non-code work; no account on the free layer; and a stricter trust model than AgentRelay (end-to-end, no permission bypass, telemetry off by default). The page makes those points about Lightbulb without naming either competitor. (competitors, competitor-profiling)
4. **"Your agents can talk to each other" is also a weak Show HN.** HN's own numbers in 07-launch: category pitches for agent coordination got 5 and 2 points; a thing you can watch working got 77. The network launch must show two agents talking in a way a reader can see or try in a minute, not announce a category. (launch)
5. **The category word the founder reached for is right, and the page should not say it.** "AI-native communication" names the shelf accurately (messaging, not workspace; agents as first-class senders and receivers, not add-ons) and the plan, the deck and interviews can use it. On the page it is a label; the 12 Oct ruling that it confuses buyers stands, and the voice test enforces it. The page says it in concrete words: people talk to people, people talk to agents, agents talk to agents. (product-marketing, copywriting)
6. **The ICP objection to a free developer door is real and answerable.** 10-website-copy rejected "Get started free" as primary because the funnel filled with single people who then had nothing to do in a team product. On the network a single person has something to do on day one (their own Claude Code and Codex talking), is never counted as a team, costs nothing, and the second person is still the activation event the plan defines. The founding-teams offer and the qualification criteria stay exactly where they are, one level down. (customer-research, cro)
7. **Lightbulb is invisible to AI search even for its own product names**, and a third-party "lightbulb-mcp" package shows up instead. Citation requires third-party pages; today there are none. The GitHub README, the plugin directories, the MCP registries and Hacker News are the surfaces that produce citations for this category (ccpair, ccx, claude-peers are all cited from GitHub and PyPI pages). (ai-seo, directory-submissions)
8. **Launch-day measurement counted founders as sign-ups and the feed repeated it.** Every routine needs one shared exclusion filter. The network's usage counts already flow to PostHog with their own install id; they must join the Monday readout as aggregate rows, never linked to a person. (analytics)
9. **The privacy policy would fail the audience the network attracts.** A developer deciding to install a daemon that handles their agents' messages reads the policy; this one is a template with contradictions and says nothing about the network. The CLI's `docs/security.md` is excellent and unreachable from the site. (customer-research, cro)
10. **The founder-led setups stay the stage; the network runs beside them.** 01-marketing-plan's "never cut" list is the 30-touch floor and the setup sessions, and 17-ideas found nothing that "beat founder-led setup for the first hundred". Nothing here cuts them. The network is the parallel loop that the plan put in stage 2 and that now exists for free; the 28 named leads in Part 2 §6 all run Claude Code, Codex or Cursor, so the first touch can hand them the install line instead of asking for a call. (marketing-plan)
11. **The demo has a hard constraint from 06-referrals.** Only the Mac app shares sessions, a public link holds 20 viewers, and 06 warned that "a Show HN post would turn most visitors away" at that cap. The network's demo is therefore a recording unless T18 raises the cap first; the transcript on the homepage is HTML, not a live link. Codex sessions can be messaged on the network even though they cannot be picked up, so the hero's transcript is accurate.
12. **We still have counts and no reasons for churn** (16-customer-research: "every current explanation of the 24 is a guess"). The alpha-by-hand step below doubles as the ask: each lead who installs is asked one question, in their words, and the answers feed the voice-of-customer bank before the Show HN copy is final.
13. **The planning documents are not on main.** The 13 Oct workstreams, the plan copies and the product-marketing context live only on a local branch. Agents and teammates reading `origin/main` cannot see them, which is how the launch monitor, the hero teardown and this review each had to rediscover the same facts.

## Recommendation

**Reposition to messaging for people and their AI agents, with the network as the front door and the workspace as the product. No competitor is named on the site (founder, 27 Oct); the workspace carries the difference.** The homepage leads with the install line and a visible two-agent conversation, then shows what the workspace adds when the laptops close. The sentence is the same on every surface a machine reads. The headline test is removed, not paused. The network launches on its own on Hacker News and Product Hunt once a fresh Mac can go from the install line to a delivered message in under ten minutes, with a demo a reader can watch. The privacy policy is rewritten for both products and `/security` gains a network section copied from the CLI's security doc. The Monday readout gains network rows. The workstreams get pushed to main.

Alternatives considered and rejected:

- **Keep the workspace front door and add a network section.** (The 13 Oct website brief's reason for rejecting a free primary button, that it "fills the funnel with single people", was right for a workspace trial and does not apply to a network where a single person has something to do on day one and is never counted as a team.) Keeps the Slack-shelf headline that five rewrites have not moved, keeps the solo-filled trial funnel, and hides the only loop. Rejected.
- **Name Reason Network on the page** (an FAQ answer crediting it and stating the difference). I proposed it because the HN audience will raise it; the founder ruled on 27 Oct that naming a competitor on the homepage sends them traffic and frames Lightbulb as the follower. Rejected. The workspace band and the trust section carry the difference without a comparison; if a prospect raises Reason in conversation, the answer is one sentence about the workspace, not a comparison.
- **Build a Reason comparison page now.** The comparison would be mostly "same", and the facts move weekly (five Reason releases in the week of 20 Oct). One FAQ answer now; a page when a founder can keep it current. Rejected for now.
- **Use "AI-native communication" as the headline.** Jargon on a page that has already lost readers to jargon; banned by the voice test for good reason. Rejected; kept as the category name in the plan.
- **A relaunch on Product Hunt under the existing listing.** A product page can relaunch; the old listing's three upvotes and "AI employees" tagline would carry over into the page's history. A new listing under the network's name keeps the story clean. Chosen: new listing, with the workspace in the maker comment.

## Deliverable

### A. Positioning (replaces §6's statement; the context doc goes to v9)

**Category sentence (plan, deck, interviews):** Lightbulb is AI-native communication: messaging for people and their AI agents. People talk to people, people talk to agents, and agents talk to agents, across vendors and computers, with one record of who did what and who said yes.

**Positioning statement.** For people who already run two or more AI agents, and the teams they work in, Lightbulb is messaging for people and their AI agents: the agents talk to each other and to the people, across vendors and computers, with one record of who did what and who approved it. Unlike Slack and Teams (messaging for people, with a vendor's assistant added), Reason Network and AgentRelay (agent-to-agent only, for code) or Grok Bot (a computer per person nobody else sees), Lightbulb joins a free, open-source network to a workspace where agents from any vendor keep working when every laptop is closed, so work assigned in chat gets finished and the first five people are free.

**Three claims, each with proof.** 1. *Your agents can talk to each other.* Any agent, any vendor, any computer; free, encrypted, no account. Proof: the install line, the live directory, a message from Claude Code answered by Codex. 2. *The work keeps going when you log off.* Workspace agents with their own runtime, memory and email run routines overnight. Proof: Anna's invoices, the nightly CEO run. 3. *A person stays in the loop.* Approval cards on the workspace; on the network every member you admit is trusted and `lightbulb remove` takes it back. Proof: Second Catch's $5,000 plan waiting on a yes.

**Contrast lines (for calls and the plan; the site names no competitor).** vs Slack and Teams: "Slack is messaging for people. Lightbulb is messaging for people and their agents." vs agent-to-agent tools: "Add a workspace and your agents keep working after you close the laptop." vs hosted hubs: "Ours runs between your computers; nothing in transit is readable." vs Grok Bot: "A team of bots per person, or one set of agents the whole team can see." vs DIY: "Stop being the switchboard."

**Words.** Use: your agents, talk to each other, message, network, invite, join, session, keeps working when you log off, your whole team, approve, continue, "who owns a change". "AI employees" for the workspace's ready-made agents, never a headline. Never on the page: multiplayer, AI-native, orchestration, bot, prompt, autonomous, internal names. No competitor is named in public copy. If a prospect raises Reason Network or AgentRelay, answer in one sentence about the workspace (agents that keep working when the laptop closes, rooms the team reads, a person's yes), without comparing their product.

### B. Homepage (final copy; the design spec carries the component plan)

| Element | Copy |
|---|---|
| H1 | Your agents can talk to each other now. |
| Subhead | Lightbulb is messaging for people and their AI agents. One line to install, and your Claude Code talks to your cofounder's Codex, your Cursor, and the agents in your team's workspace. Peer to peer, end-to-end encrypted, free. |
| Primary control | The install line in a monospace box with Copy: `curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh \| sh`. Under it: *or tell your agent:* `Join the Lightbulb network: https://thelightbulb.company/network` with Copy. |
| Secondary | **Start a workspace** (outline) → `/sign-up`. Text link: *or request a guided setup* → `/request-setup`. |
| Trust row | Free · Apache-2.0 · No account · macOS and Linux · Claude Code, Codex, Cursor, Grok CLI, OpenCode |
| Hero art | The two-terminal transcript (HTML). Desktop: beside the copy. Phone: below the buttons. It appears once on the page. |
| Problem H2 | Your agent remembers what you did. It has no idea what your cofounder's agent worked out an hour ago. |
| Problem body | So you carry it. You paste one session's findings into another, write the summary for your cofounder, and check three tools to learn what the agents did. Every agent can only talk to the person who runs it, and you are the switchboard. |
| How it works | **Install.** One line. It wires Claude Code, Codex, Cursor, Grok CLI and OpenCode, and starts a small background service. Homebrew and npm work too. · **Invite.** `lightbulb invite` prints a four-word code, a link and a join line. Send any of them; your teammate runs `lightbulb join`. An invite is the approval; nobody is asked. · **Talk.** Ask your agent to message theirs. Replies come back into the session. A message to a closed session waits on that computer. |
| Workspace H2 | When the laptops close, the workspace keeps working. |
| Workspace body | Add a workspace to your network and you get agents that never log off: a CEO, Engineering, Finance, Growth and your own, in rooms your whole team can read. They email, run routines overnight, and anything that spends, sends or deploys waits for a yes on your phone. Your Claude Code and Codex sessions sit in the same rooms, so a teammate can watch one and pick it up. |
| Workspace captions | **Rooms.** People and agents in the same channels, with one record. · **Approval cards.** A plan, a spend or a new hire asks in the room and waits for a person. · **Shared sessions.** Watch a teammate's Claude Code or Codex session live and pick it up where they stopped. |
| Proof | Anna's cleared quote; the Second Catch story. The $2M band comes off. |
| Pricing H2 | The network is free. The workspace is one price. |
| Pricing columns | Network $0 (unlimited computers and people · peer to peer, end-to-end encrypted · open source, no account) · Workspace (price from the billing API; up to 5 people then $20 each; agents that keep working, rooms, approvals, a phone in the loop; any model; 30 days free, no card) · Founding teams (unchanged). |
| Trust lead | What we can see: nothing in transit. Messages between computers are encrypted end to end; a relay, when one is needed, carries ciphertext only. Usage counts are anonymous and off with one command. Workspaces run in their own isolated AWS environment; your content is never sold or used to train models. |
| FAQ | We already have Slack. / Claude already does this for me. / Will it do things without asking? / What happens to our data? (answers in the design spec, FAQPage JSON-LD word for word; no competitor named) |
| Ask AI question | Go to thelightbulb.company and read the site, then read github.com/TheLightbulbCompany/cli. Lightbulb is messaging for people and their AI agents: a free, open-source network where the coding agents people already run talk to each other and to teammates' agents, peer to peer and end-to-end encrypted, plus a paid workspace that joins the network with agents that keep working when nobody is logged in, rooms the whole team can read, and approval cards for anything that spends, sends or deploys. Who is this for, and what would a team of three to fifteen that already uses two or more agents get out of it? |
| Footer H2 | Your agents can talk to each other. Bring your team. |

Headline alternatives, for the founder to pick from (the "Now you can" test and the Human Action Model from the copywriting skill; all pass the voice test):

- A. **Your agents can talk to each other now.** Outcome first, present tense, implies the pain (they could not before). Recommended.
- B. **Messaging for people and their AI agents.** The category, plain. Clear, less alive; good as the title tag and the OG image line.
- C. **One line, and your Claude Code talks to your cofounder's Codex.** The most concrete; developer-only; loses the founder reading over a developer's shoulder.

### C. Site architecture

Header: Network (`/network`) · Workspace (`/#workspace`) · Docs · Pricing · GitHub (external) · Log in · **Start a workspace**. Six items plus the button, inside the 4–7 rule. Blog and Download move to the footer. Footer columns: Product (Network, The workspace, Pricing, Docs, Compare, Download for Mac, Marketplace) · Company (Blog, Your data, Privacy Policy, Terms, Support, GitHub) · Legal. `/network` keeps its URL and becomes the canonical install page; the homepage carries the same install constant and links to it as the full guide. The agent-network docs (overview, quickstart, teams, security, troubleshooting, one page per agent, CLI reference) are the hub for the developer cluster; `/claude-code-session-sharing` and the six `/compare/*` pages stay. Every page in that cluster links to `/network` and the hub links to each page.

### D. Search and AI search

- **One sentence everywhere.** `HOME_TITLE` "Lightbulb — Messaging for people and their AI agents"; `HOME_DESCRIPTION` "Your coding agents talk to each other and to your teammates' agents, free and end-to-end encrypted. Add a workspace and your agents keep working when you log off." The same sentence opens the CLI README, the GitHub repo description, the Homebrew and npm descriptions, `llms.txt`, the Product Hunt tagline and the X bio. The `/llms.txt` route today says "messaging workspace where humans and AI employees share channels" and never mentions the network; it is generated from `seo.tsx` and the docs index, so the fix is in those two sources plus a network line in its Key facts.
- **Entity graph.** Keep one `#organization` and one `#software`. `sameAs` gains github.com/TheLightbulbCompany, producthunt.com/products/lightbulb-2 and x.com/lightbulbai (all return 200; LinkedIn stays out until E5 is done). Keep the `disambiguatingDescription`; add "and not the lightbulb-mcp package". The SoftwareApplication gets two Offers (Network, price 0, url `/network`; Workspace, price from the billing API, url `/pricing`) and a `featureList` that opens with the network. FAQPage from the five homepage questions, word for word. No ratings or reviews until real ones exist.
- **Pages that can rank for the wedge's queries**, all already in the docs cluster or cheap to add: "message between Claude Code and Codex" (quickstart), "share a Claude Code session with a teammate" (exists), "Cursor and Claude Code in one team" (agents-cursor), "end-to-end encrypted agent messaging" (security), "lightbulb invite" (CLI reference). Each needs the exact question as its H2 and a 40–60 word answer under it. Add a machine-readable `/pricing.md` with both products (the ai-seo skill's agent-readiness file; `/pricing.md` exists as a static file on main and must say Network $0).
- **Third-party presence, which is where citations come from.** The GitHub README's first three lines; repo topics (`claude-code`, `codex`, `cursor`, `mcp`, `p2p`, `agents`); the hosted MCP in the MCP registries (Glama and the others in 14-directories tier 6); the plugin in the Anthropic and Codex directories (14's tiers 1 and 3); awesome-claude-code; a Dev.to or Hashnode post of the security doc with canonical back to `/docs/agent-network-security`. Claim Crunchbase and Wikidata (14's tier 14). These are also how the namesake (`lightbulb-mcp`) stops outranking the product.
- **AI-visibility baseline**, to repeat monthly, 3–5 runs per query per platform, logging the rate: the four queries in What we know plus "what is Lightbulb (thelightbulb.company)", "Reason Network vs Lightbulb", "best way to share Claude Code session with cofounder", "AI agents in Slack alternative", "free open source agent network", "Lightbulb workspace pricing".

### E. Competitor profile: Reason Network (quick scan, 27 Oct 2026)

| | |
|---|---|
| Tagline | "Let your agents talk to each other." |
| Company | Reason Machines; the paid product is Reason, an autonomous software engineer; Cloud Reason sessions bill as usual on the network |
| Launched | 16 Oct 2026 (repo created); AlphaSignal coverage 20 Oct; no Hacker News story |
| Traction | 1,265 stars, 108 forks, +585 stars in the week to 27 Oct; v0.12.13 (releases most days) |
| Licence | MIT or Apache-2.0 (engine); the Reason CLI is free but not open source |
| Platforms | macOS (menu-bar app), Linux, Windows |
| Agents | Claude Code, Codex, Cursor, Gemini CLI, Copilot CLI, Grok CLI, opencode, pi, DeepSeek Harness, Reason; hosted assistants (Grok Bot, Muse, Dots) through its MCP server |
| Install | `curl … install.sh \| bash -s -- network`, PowerShell on Windows, or the paste line "Join the Reason Network: https://reasonmachines.com/network" |
| Identity | Sign in with Google, GitHub, Apple or email; your other computers join by signing in; friends join by invite link; "names are verified by Reason sign-in" |
| Transport | iroh (QUIC) peer to peer; encrypted relay fallback; a team can run its own relay; hosted bots connect through Reason's server |
| Trust claims | "Zero data retention · SOC 2" (footer); received files inert in `~/.reason/network/inbox`; debug logging off by default |
| Strengths | Brand and an existing audience; ten agents and Windows; hosted assistants; shipping speed; SOC 2 language |
| Weaknesses | An account is required; hosted assistants and name verification run through their server; agent-to-agent only, for code; no rooms, approvals, email or non-code work; it exists to feed a paid coding agent |
| Where Lightbulb is stronger | No account; the workspace (people and agents in rooms, approvals, email, phone, non-code work) joins the network as a member; the CLI never touches agent permissions |
| Where Reason is stronger | Reach (1,265 stars vs 0), agents supported (10 vs 5), Windows, a company that already sells to this audience |
| If a prospect asks (never raised by us) | One sentence: "The difference is the workspace: agents that keep working when your laptop is closed, rooms your team reads, and a person's yes on anything that spends, sends or deploys." |

AgentRelay, one line: a hosted hub (their server reads the messages, telemetry on by default, agent permissions bypassed), 926 stars; the 24 Oct comparison scores Lightbulb better on eight axes, mostly trust. The contrast line in A covers it.

### F. The network launch

**Readiness gate (SLC, from the launch skill, plus the charter's phase-7 gate).** Simple: one thing, your agents message each other. Lovable: a developer would choose it over copy-paste. Complete: install, invite, join, send, receive, leave, update, remove, all from the public docs. Gate items: (1) the homepage above is live; (2) a non-founder on a fresh Mac goes from the install line to a message delivered from their Codex to a teammate's Claude Code in under ten minutes using only the public docs, and everything that confused them is fixed; (3) `/security` carries the network section and the Termly policy is rewritten; (4) the GitHub README, topics, CHANGELOG and SECURITY.md are in place; (5) a demo a reader can watch: a public session link (the existing "anyone with the link" live terminal, viewer cap raised after the load test) or a 45-second recording of two Macs, embedded on the homepage and the HN link; (6) one shared PostHog exclusion filter for founders in every routine.

**Sequence (ORB: owned → rented → borrowed).**

| When | What | Owner |
|---|---|---|
| Week 0 (this week) | Homepage, surfaces, README, `/security`; the fresh-Mac test; founders' own networks dogfooded daily | Prasiddha (build), Aden or Leo (test) |
| Week 1 | Alpha by hand: the 28 named leads in Part 2 §6 get the install line in a personal note instead of a setup-call ask ("your Codex can message your cofounder's Claude; one line; no account; tell me what breaks"); GitHub topics live; plugin directory submissions (Anthropic, Codex) and MCP registries; Homebrew and npm descriptions; awesome-claude-code PR | Aden (email), Leo (X, Reddit DMs), Prasiddha (GitHub, directories) |
| Week 2 | Show HN, Tuesday to Thursday, 08:00 PT (the 15:00–18:00 UTC window): title in Prasiddha's own words, shape "Show HN: Lightbulb – your Claude Code can message your cofounder's Codex (open source, p2p, no account)", link to the demo, first comment with the real numbers; HN rules: no vote asks, founder-written text, stay in the thread all day | Prasiddha |
| Week 3 | Product Hunt, new listing under the network's name, 12:01 AM PT Tuesday to Thursday; three-week warm-up starts in week 0 (comment on three launches a day, Upcoming page, notify subscribers); first comment from Prasiddha, a second from a non-founder user; ask for feedback, never votes; the workspace in the maker comment as "what's next" | Prasiddha (maker), Aden (LinkedIn), Leo (X) |
| Week 3–4 | Launch recap post (numbers and lessons) on the blog, Indie Hackers and r/SaaS; Dev.to cross-post of the security doc; the founder X thread; r/ClaudeCode showcase ("what we built, how Claude Code was used, what we learned"), karma rules from 13-content observed | Leo, Prasiddha |

**What the launch must not do.** Ask for votes. Coordinate amplification. Say "multiplayer", "bot", "prompt" or "orchestration". Claim a hold or Allow step. Claim Windows. Name a competitor. Post agent-written text on HN.

**Directories (14-directories stands; the network changes the ranking).** Tier 1: Product Hunt (new listing), Show HN, DevHunt. Tier 4 (the moat): Glama and the MCP registries for the hosted MCP; the Anthropic plugin directory; the Codex directory if hooks-free. Tier 8: GitHub topics, Dev.to, Hashnode. Tier 2 waits: G2 needs twenty users who could review; AlternativeTo and SaaSHub once the homepage is live. Skip paid directories and BetaList (14's ruling stands).

### G. Measurement

Tracking plan additions (PostHog project 380894; every query filters production hosts and the founder exclusion list; no person-level rows in any post):

| Event | Where | Properties | Trigger |
|---|---|---|---|
| `install_line_copied` | web | `surface` (hero, network, footer), `kind` (curl, agent_line, brew, npm), first-touch (automatic) | Copy pressed |
| `network_guide_opened` | web | `surface` | "full guide" link from the hero |
| `cli_installed`, `cli_updated` | CLI (exists) | version, os, cpu, install-id | first `setup`; `setup` after a version change |
| `network_joined`, `invite_created` | CLI (exists) | `via` | join; invite |
| `cli_daily` | CLI (exists) | messages sent and received, computers on the network, agents connected | once a day |
| `signup_cta_clicked`, `setup_cta_clicked`, `signup_started`, `workspace_created`, `team_formed`, `handoff_completed`, `chat_completed` | web and backend (exist; `chat_completed` merged 27 Oct, prod verification pending) | unchanged | unchanged |

Definitions for Appendix C: **network installed** = `cli_installed`; **network with two computers** = a `cli_daily` with computers ≥ 2; **network activated** = a `cli_daily` with messages sent ≥ 1 and computers ≥ 2; **network to workspace** = a workspace whose `lightbulb member` joins a network that reported activation (counted from the bridge, never from the install id). Monday rows added: installs, networks with two computers, activated networks, invites created, install line copies, and workspace links; counts, not rates, until n is above fifty. Remove the headline test (`headlineVariant.ts` and its five uses) and its super property; let the `lb_hv` cookie expire. Keep first-touch UTM (it already rides every `capture()`); every link we post carries one. Search Console and Bing verification remain open (ISOA-848).

### H. Privacy and the data page

`/security` ("Your data") gains **The network**: what is on your computer (`~/.lightbulb/connect/identity.key`, `login.json` if a workspace is linked, the signed roster, an outbox for offline computers); what travels (your messages, encrypted end to end; a relay only when a direct route fails, carrying ciphertext); what we never see (message text, names, keys, codes, folder paths); usage counts (the exact event list, the random install id, geoip off, the three ways to turn it off); how to leave (`lightbulb leave`, `lightbulb setup --remove`). Copy the facts from the CLI's `docs/security.md` and README; never looser. Termly rewrite (Prasiddha signs, decision B9): remove intent data, advertising partners, Do Not Track, Telegram/Discord/WhatsApp, the state-table contradictions; one subprocessor list (AWS, OpenRouter, OpenAI, Anthropic, Stripe, Clerk, PostHog, Termly); retention and deletion in days; a network paragraph that matches `/security`; a name and date at the top. The homepage's trust lead says "a founder signed it" only after that is true.

### I. Content and social (13-content stands; three pieces move to the front)

1. **"How two Macs let their agents talk, and what the relay can see"** (searchable; the security doc as a story; Prasiddha's byline; the HN and Dev.to companion).
2. **"We shipped a free agent network ten days after Reason did. Here is what is the same and what is not."** (shareable; honest; the recap after launch; Leo signs the X thread).
3. **"Share a Claude Code session by link, then hand it off"** (exists as a page; becomes the third post with a recording).

Pillars stay the handoff and real work. A statistics page for the category ("how many agents a small team runs", from the setup discovery guide's answers, n stated) is the one link-earning format the content skill ranks above all others; start it when ten setups have answered the question.

### J. The plan

The section-by-section redlines are drafted at claude.ai/artifact/7Ne5GiseUy66PYPbUoSxNr (Part 1 §1, §2, §3, §5, §6, §8, §9, §12, §13; Part 2 §1, §2, §4, §5, §7, §10; Appendices A, C, D). They are applied by the founders in the Google Doc.

### K. What this supersedes in the earlier workstreams

| Workstream | Ruling there | Now |
|---|---|---|
| 10-website-copy | Primary button "Request a guided setup", secondary "Start free"; headline A/B; the seven-section order | Primary action is the install line; guided setup one level down; no test; the order in B |
| 05-seo C.1 | One definition sentence, "the team workspace where AI employees…", verbatim everywhere | The messaging sentence in D, verbatim everywhere |
| 07-launch | One Show HN, built around the session link and a handoff, gated on billing and two named teams | The Show HN is the network's; its gate is the ten-minute fresh-Mac test and a watchable demo; the session-link story becomes the second post, not the submission |
| 14-directories | Plugin surfaces first, G2 later | Unchanged order; MCP registries and GitHub topics move up; Product Hunt is a new listing |
| 11-competitors | Six comparison pages; Bloome closest in shape | Reason Network and AgentRelay added to the internal table; not named on the site (founder, 27 Oct); no page |
| 01-marketing-plan | Week-9 Show HN (8 Dec), week-13 Product Hunt (5 Jan), headline test on the cut list | Dates move to the gates in F; the test is cut, as 01 itself allowed |
| `.agents/product-marketing.md` v8 | Category "the Slack / Teams shelf, AI-native version"; conversion action "request a guided setup" | v9 (drafted on this branch): messaging for people and their AI agents; conversion action "copy the install line", then "start a workspace" |

## Build tasks (Forge-ready; sizes S under a day, M one to three days)

| # | Title | Today | Change | Why | Measure | Size |
|---|---|---|---|---|---|---|
| T36 | Homepage says messaging | Hero A/C, "Try Lightbulb free", painting, $2M band, workspace-first order | The copy and order in B; install line with Copy; transcript once; workspace band keeps the `lb-mock` frame and drops the painting; ProofBand deleted | Findings 1–6 | `install_line_copied` per visitor; sign-up starts; founder sign-off | M |
| T37 | Nav, footer, pricing column | Product · Docs · Blog · Pricing · Download; no network link | C; Network $0 column on home and `/pricing`; prices from `getPricing()` | Finding 2; the 4–7 rule | `/network` referrals from nav | S |
| T38 | Remove the headline test | `headlineVariant.ts`, cookie, super property, form field, tests | Delete all five uses; `page.tsx` back to static if nothing else needs per-request | Finding 1 | PostHog reads without the property | S |
| T39 | One sentence on every machine-read surface | Title, description, JSON-LD, `llms.txt`, OG image, `/pricing.md` say "AI employees" | D; `og-lightbulb-v3.png` with the H1 on the homepage and layout; the mark stays elsewhere | Finding 7 | Rich Results Test clean; `llms.txt` opens with the sentence | S |
| T40 | `/network` install page | `<pre>` blocks, no copy control, no npm line | Shared install constant; Copy buttons (client component); npm line; backlink line to the homepage; test updated | B, G | `install_line_copied` surface=network | S |
| T41 | Website events and the Monday rows | No install events; network counts not in the readout | `install_line_copied`, `network_guide_opened`; readout rows per G; confirm the CLI's events land in 380894 | Finding 8 | Monday page shows the rows | S (web), M (readout) |
| T42 | `/security` network section | No network section | H, copied from the CLI security doc | Finding 9 | Page live; linked from the FAQ and the README | S |
| T43 | GitHub, directories, registries | README leads with install; no topics; not listed | README first three lines = the sentence; topics; CHANGELOG; plugin directories; MCP registries; Homebrew and npm descriptions; Crunchbase and Wikidata | Finding 7 | Listings live; repo views and stars weekly | M (founder time) |
| T44 | Fresh-Mac ten-minute test | Never run by a non-founder | A non-founder, public docs only, install to delivered message; each confusion fixed | Launch gate | Minutes to first message, written on the PR | M |
| T45 | The demo a reader can watch | None | A public live session link (viewer cap raised after the load test, T18) or a 45-second recording of two Macs | Finding 4 | Plays on the homepage and the HN link | M |
| T46 | One founder exclusion filter | Each routine filters differently; the launch monitor filtered nothing | One shared filter (hosts, environment, founder ids, test workspaces) used by every routine and stated in every post | Finding 8 | No routine posts an unfiltered count | S |
| T47 | Push the planning docs to main | docs/marketing 00–25, the plan copies and the context doc exist only on `codex/lantern-font` | Commit them to a docs-only branch off main and merge | Finding 10 | `git ls-tree origin/main docs/marketing` lists 26 files | S |
| T48 | Termly rewrite | Template policy, 29 May | H; Prasiddha signs | Finding 9 | New policy live and dated; the homepage trust line turns on | M (founder time) |

## Measures

- Reach: repo views, stars, `install_line_copied`, installs per week, against the 26 Oct baseline (2 unique repo visitors, 0 stars, about ten site visitors a day).
- Loop: networks with two computers; activated networks; invites created per install.
- Bridge: networks that link a workspace; sign-up starts; teams formed; first handoff; the number on the wall, unchanged.
- Search: the AI-visibility rate per query per platform, monthly; Search Console impressions for the wedge queries once verified.
- Launch: HN points and comments; PH comments (quality over upvotes); recap post reach; qualified replies and setup requests in the week after, by source.

## Open questions for the founders

1. **Who runs the fresh-Mac test, and whose Mac?** It must not be a founder's daily machine.
2. **Relays.** Will Lightbulb run any, and who approves the cost? The policy has to say what a relay sees either way.
3. **Retention and deletion in days** for the Termly rewrite. The numbers are the founder's to pick; the page cannot say "in accordance with our retention practices".
4. **The 90-day network targets.** My placeholders: 100 computers on two-computer networks by 4 January and a quarter of activated networks trying a workspace. Replace them.
5. **Product Hunt: a new listing under the network's name, or a relaunch of lightbulb-2?** Recommendation: new.
6. **Push the planning docs to main (T47).** This is a one-line decision and it unblocks every agent that reads `origin/main`.

## Sources

Internal: docs/marketing 00–25 (13 Oct, local branch); the business and marketing plan (Google Doc 107VLG…kVTM, 19 Oct merge); `.agents/product-marketing.md` v8; docs/growth/2026-07-10 and 2026-08-17; docs/research (Oct 2026); `docs/superpowers/specs/2026-10-22-lightbulb-network-p2p-design.md`; `docs/superpowers/plans/2026-10-22-lightbulb-network-goal.md`; `docs/research/2026-10-24-network-roles-reconciliation.md` and `-agentworkforce-relay-vs-lightbulb.md`; PRs #1577, #2358, #2387, #2487, #4104, #4240, #4412; commits `31a869a56`, `bed24c6b8`; ISOA-850, 867, 893, 906, 960, 961, 962; the GTM Strategy, user feedback, Humans + Friday and feed rooms (29 Sep–27 Oct).

External, read 26–27 Oct 2026: thelightbulb.company, /network, /privacy, /security; producthunt.com/products/lightbulb-2; x.com/lightbulbai and the 20 Oct post; github.com/TheLightbulbCompany/cli (README, docs/security.md, releases, traffic API); reasonmachines.com/network; github.com/reason-machines/reason-network; ossradar.net; alphasignal.ai (20 Oct); hn.algolia.com; WebSearch runs listed in What we know.
