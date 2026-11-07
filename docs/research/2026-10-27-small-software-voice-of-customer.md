# Voice of the people who build small software for themselves (2024–2026)

Sources: Hacker News (Algolia API), Reddit via the pullpush.io archive, Substack, dev.to, company blogs. Every quote is verbatim (typos kept); "…" marks omissions. Vendor-affiliated voices are tagged. r/lovable thread 1xe2zsf was not recoverable; the same argument is taken from the HN "Lovable raises $400M" thread and r/nocode. pullpush rate-limited after four queries, so Reddit coverage leans on three full threads (r/smallbusiness, r/nocode, r/sysadmin) plus a 100-comment "internal tool" sweep.

## A. Built for one task or one moment

> "I was changing my phone to a new mobile provider, and part of the workflow was entering my IMEI… I went over to Gemini and had it whip me up a Luhn's function for validating the IMEI… Once I validated the number, I threw that code away."
> — kazinator, HN, 2025-04-21, https://news.ycombinator.com/item?id=43753858

> "I build a tool like this with Claude in 2hs, uses Rust with OpenCV crate, egui GUI, PDF output… My brother and I were digitizing medical bills for our mother… This is a throwaway tool as we're now more and more getting bills as digital files anyway… while we were photographing the pages with our smartphones Claude wrote the tool."
> — virtualritz, HN, 2026-08-04, https://news.ycombinator.com/item?id=49176316

> "I've vibe-coded lots of nonsense throwaway apps that helped me with a one time boring task. Wouldn't have been possible."
> — starik36, HN, 2026-09-29, https://news.ycombinator.com/item?id=49900529

> "I vibe coded an app that will extract an epub, translate each of the chapters, and package it back to an epub… It's only ~1000 lines of Rust code, but Claude generated it when I was doing dinner… It lets me do small tools that I just wouldn't have time to do otherwise."
> — drogus, HN, 2026-01-09, https://news.ycombinator.com/item?id=46553666

> "Took me about 30 minutes of wall clock time, maybe 5 minutes of my time to have an LLM write me a simple python script that uses the API in the questionnaire service to pull down the data and insert it into a new Google Sheet… None of these would've been granted the day(s) of engineering time before, now both were something that could be thrown together quickly over a coffee break or done by themselves over a weekend."
> — theshrike79, HN, 2025-09-23, https://news.ycombinator.com/item?id=45344084

> "I have a dozen tiny pieces of software that are used by myself or maybe up to 10 people that that I don't push around or market or release… I built a tool for a PM friend to let Claude Code automatically sync all his meetings and let him query his transcripts for action items… Nobody else uses that."
> — MattGaiser, HN, 2026-03-08, https://news.ycombinator.com/item?id=47294454

> "I've built easily a dozen personal apps in the last few months that I have no plans on releasing or sharing, run locally, and solve very specific productivity (or one-off) issues for me… Sure Claude, build me a MacOS app in Swift that does this exact thing I need. Less than an hour later, it's done."
> — sbarre, HN, 2026-10-26, https://news.ycombinator.com/item?id=50242868

> "I usually create custom disposable playground and lab pages depending on what I'm experimenting with… sometimes I create them as claude artifacts to share a quick link with the team. On these artifact examples the UI details don't really match our exact design system but doesn't really matter."
> — nseckinoral, r/UXDesign, 2026-09-27, https://reddit.com/r/UXDesign/comments/1womr50/no_figma_vercel_cursor_claude_etc/pcd7j6y/

## B. Became recurring, then broke, needed handoff, or nobody maintained it

> "In a single day, I vibe coded an internal tool for my team to run unit, integration tests… In the past, it would have taken me at least 1-2 weeks to build this. It was actually never going to get built because of that."
> — aurareturn, HN, 2025-09-15, https://news.ycombinator.com/item?id=45248754

> "I have vibe coded two different projects for work, one is a slack plugin which basically pushes alerts to a channel based on a people roaster and another one is a gmeet plugin… I actually don't have a good understanding on how it works, but both works without any issue and are deployed in gcp… we used to pay 20$ per person… now it's just 0.07$ per month"
> — sajithdilshan, HN, 2026-08-01, https://news.ycombinator.com/item?id=49133710

> "one of our sales vibecoded an internal tool in the last three weeks using Claude Code that they now use every day. I wouldn't have imagined it a month ago. We still had to take care of deployment for him, but things are moving fast."
> — ElFitz, HN, 2026-04-17, https://news.ycombinator.com/item?id=47803775

> "Not only did person X build this tool by themselves. They did not have a single other person review a pr and every commit was tagged as Claude assisted. When I tried to run it locally the setup instructions not only didn't work… When I asked for help setting it up, person X said ask Claude, it always gets it right."
> — datawrangler017, r/ExperiencedDevs, 2026-10-03, https://reddit.com/r/ExperiencedDevs/comments/1wwkz8p/are_we_back_to_silo_engineering/pdlxu0n/

> "One of the non-devs was vibe coding internal tools and showing them off. Looked great so I gave them the ability contribute to the main internal apps… Big regret… Then I eventually get asked how to fix some bug that Claude got stuck in and drop into the code to have a look and it's this monstrosity of code machinery built to work around a minor flaw."
> — Tiny_Ad_7720, r/singularity, 2026-09-30, https://reddit.com/r/singularity/comments/1wu2bxh/work_i_used_to_give_juniors_now_takes_opus_55/pczexti/

> "I don't have time to manage vibe-coded things… if my time tracker breaks, I can call a company and get shit fixed. If I make every tool I use, all I'll do is manage and support the tools. Works if it is solo, but once you have a big team… My team is like 10"
> — masanon, r/HarvestApp, 2026-10-07, https://reddit.com/r/HarvestApp/comments/1x01x4j/how_does_harvest_flex_billing_work/pei9dy2/

> "There's a founder I spoke to recently who built an internal quoting tool in a weekend. Three months later, nobody was using it. Not because it stopped working. Because it worked well enough, until it didn't. A field got renamed in the CRM… The founder, who had moved on to the next problem, had no idea it had happened."
> — Jake Shelley (co-founder, Jiffi; vendor blog), 2026-05-20, https://www.jiffi.co/articles/vibe-coding-built-it-now-what

> "4. Internal tool is released; the internal team looks at it and says, "uh, I don't want to deal with this, I'm just going to keep forwarding requests to the software team." 5. … Internal tool is never used."
> — mjr00, HN, 2026-09-24, https://news.ycombinator.com/item?id=49834311

> "A lot of internals tools were built where I work, not a lot were used."
> — patchorang, HN, 2026-09-24, https://news.ycombinator.com/item?id=49833402

> "I have started to notice some similarities to MS Access development, where an SME creates a useful app for themselves and begins to share it. I wonder if it will have a similar pattern of creating a mess as the app starts to get uptake and the SME can't scale their attention to be an app owner, as well as an SME at the same time. Also makes me think that an llm-developed-app-friendly shared datastore would be a useful thing to have"
> — monknomo, HN, 2026-02-04, https://news.ycombinator.com/item?id=46889100

> "AI-generated internal tools are just... code. Code that will need updating when APIs change, when requirements shift, when the person who prompted it leaves… Check back in 18 months when half those tools have drifted into unmaintained states and someone has to figure out what handleEdgeCase_v3_final_FIXED.js was supposed to do."
> — agent013, HN, 2026-01-27, https://news.ycombinator.com/item?id=46777573

> "Go over a slack channel used for feedback for an internal tool, triage, open issues, fix obvious ones, reply with the PR link. Some devs liked it, some freaked out. I kept it." (a scheduled Claude Code routine)
> — eranation, HN, 2026-04-14, https://news.ycombinator.com/item?id=47769196

## C. "I just wanted it where the work is"

> "I've recently been a big fan of Claude Code for building lots of these kinds of tools, but then I always ran into the "what now" problem. Our IT team certainly wasn't going to just deploy my randomly-built tool and give it access to our systems, so there was a whole other process to go through to get the things I was building out to the team."
> — kkoppenhaver (Retool DevRel, disclosed), HN, 2026-06-18, https://news.ycombinator.com/item?id=48580701

> "my cofounder who's not technical… loved Cursor, I was convincing Serafim to try Claude Code, but he just couldn't use it. Cause it was a CLI, then I created a first alpha version of this UI for them .... and he loved it!"
> — Bunas, HN, 2026-01-16, https://news.ycombinator.com/item?id=46649984

> "A coworker comes to me with a question about our DB content, Claude gives me a SQL query for what they need, review, copy paste to Metabase or Retool, they now don't have to be blocked by engineering anymore… I wanted my non-engs coworkers to be able to do that whole loop by themselves."
> — dgellow, HN, 2025-07-03, https://news.ycombinator.com/item?id=44453769

> "And I could deploy and share it with a single click." … "That's something that Claude Code can't do as easily." … "use Lovable for prototype testing, Claude Code for building."
> — Thomas Brouwer, Substack, 2026-01-25, https://thomasbrouwer.substack.com/p/claude-code-vs-lovable-which-tool

> "Why am I going to a claude[dot]ai domain to look at my local development artifacts? - Why are contractors sending me paid work in a claude[dot]ai link on their account? - Why can I not easily persist, version, and share these artifacts with my team and other agents?"
> — ramoz, HN, 2026-09-03, https://news.ycombinator.com/item?id=49554725

> "The reason our software projects at our company take longer than a week is not because of code, it's because we have an IT department that blocks production deployment of everything unless you literally get the president of the company to make them do it." (after a non-technical PM built a project in Codex)
> — moron4hire, HN, 2026-05-17, https://news.ycombinator.com/item?id=48170241

> "I'm not a developer but I figured out Claude Code agents and skills and use surge.sh to share things out as needed. In my company I feel like the last to this party,"
> — subpixel, HN, 2026-01-27, https://news.ycombinator.com/item?id=46774793

> "Excel used to be, and probably still is, the primary competitor to enterprise-developed apps… that was a locally deployed phenomenon, with an added ability to deploy it somewhere else by simply emailing the workbook to someone else. In your organization, how do your managers turn their code into working software?"
> — kayo_20211030, HN, 2026-02-04, https://news.ycombinator.com/item?id=46887641

> "we hit almost this exact wall building an internal tool in Claude, backend auth and permissions took longer than the whole UI ever did."
> — kimbaptori, r/MCPservers, 2026-09-10, https://reddit.com/r/MCPservers/comments/1vfr8mx/mcp_for_building_secured_apps_on_your_sql_database/p8xe2dq/

> "yeah the filter breaking login freaked me out more than the credits" … "You're right, my permissions live in the UI, not the database" (OP of "Lovable alternative for internal tools", ~25 users, drivers vs contractors)
> — DifficultWorkers, r/nocode, 2026-08-12, https://reddit.com/r/nocode/comments/1v7mwo2/lovable_alternative_for_internal_tools_tired_of/p36tk7f/

> "Slack was not built as a place to bring your own agent, and there are serious challenges around sharing an agent in a chat environment. Should everyone have their own bot(s)? Do we need thousands of Slack apps for that to work? If not, whose auth does it use?"
> — tlongwell-block (Block, building Buzz), HN, 2026-06-22, https://news.ycombinator.com/item?id=48632992

## D. Why still Lovable when Claude Code / Codex exist, and the counters

> "Yes, I used Claude Code a lot, but Lovable is the fastest way to get a log-in system and database, so a great option for a true MVP."
> — willnz, HN, 2026-08-12, https://news.ycombinator.com/item?id=49279491

> "a huge portion of their user base are not technical. What are they going to do? Use Cc or Codex and then attempt to host their db, frontend and backend somewhere. No chance. Secondly, a lot of the apps built on lovable are for internal purposes and not necessarily used publicly."
> — jvwww, HN, 2026-08-13, https://news.ycombinator.com/item?id=49288541

> "I have a client that prototypes things in Lovable and hands them off to me to get them working and deployed. He would be lost in claude code."
> — hackernud3s, HN, 2026-08-12, https://news.ycombinator.com/item?id=49280015

> "Last month I visited warehouse in Berlin. People who work there(with no technical background) build over 15 lovable apps for themselves -> starting from checkin system, to very sophisticated dashboards. Those were non technical people, and they did all with no engineers or IT. Those apps they were using were better than 90% of internal apps I've seen in my career."
> — podviaznikov, HN, 2026-08-13, https://news.ycombinator.com/item?id=49283393

> "If you can write a clear sentence about what you want, you can build with Lovable." … "If I used Lovable for our core product, my engineers would not be able to integrate it with our existing codebase."
> — Nomiki Petrolla (non-technical founder, Theanna), 2026-02-27, https://theanna.io/building-theanna/lovable-vs-claude-code-when-to-use-each

> "we have many complex internal tools, that run inside of an enterprise Lovable ecosystem… what the PMs tend to do is to connect Claude to Lovable, and prompt Claude to build whatever they need. Claude then writes these dense, highly specific prompts that it sends to the Lovable agent"
> — CryptoSpecialAgent, r/singularity, 2026-10-04, https://reddit.com/r/singularity/comments/1wwzg8r/will_there_be_1_and_only_programming_language/pdvpk9i/

Counters:

> "You can one-shot a website with Claude Code or Codex of the same or better quality than Lovable. I have no idea what the appeal of the product is at this point."
> — paxys, HN, 2026-08-13, https://news.ycombinator.com/item?id=49281545

> "Plug your Lovable project in a Github repo, clone the repo to your machine, open Claude Code on folder and that's it… Youll need to host somewhere tho"
> — neems74, r/nocode, 2026-07-27, https://reddit.com/r/nocode/comments/1v7mwo2/lovable_alternative_for_internal_tools_tired_of/p02c05i/

> "It started as a Claude artifact. Hardcoded queries… It worked, but lacked flexibility. So I upgraded to Claude Code… Then I hit a wall. 30 mins in I was out of API credits… 1/ bolt.new got me moving fast, but stalled when I pushed it. 2/ Lovable got me to a working prototype in the first session."
> — noashavit, HN, 2026-05-02, https://news.ycombinator.com/item?id=47989460

## E. Non-developers inside companies

> "One of my cousins built an ERP for a factory he was managing. It looked great, and seemed to work great. He has no technical background… when I looked underneath the code, it was a mess… it wasn't using foreign keys in database."
> — dpacmittal, HN, 2026-08-13, https://news.ycombinator.com/item?id=49286241

> "i have close to zero experience in programming… i was looking for a software at work to keep track of ips, inventory, switches, patch panels… i started making my own internal tool completely vibe coded… Im not confident at all to put a vibe coded app on the open internet but for an internal tool it has a lot of potential."
> — MrSimpatia17, r/sysadmin, 2026-09-10, https://reddit.com/r/sysadmin/comments/1wcfzlf/ai_coming_do_we_need_crms_itsms_ticketing_systems/p8xjhdy/

> "I've had a colleague (who works in a non-technical part of the company) pitch apps to me fully built in AI for whole company use. He doesn't want to depart with the code, since he did it in his spare time so expects renumeration, but can't answer any questions concerning security, data storage, etc because it's not his field at all. He has no idea how it does what it does."
> — Talt45, r/sysadmin, 2026-10-07, https://reddit.com/r/sysadmin/comments/1wzttxb/users_and_ai/pej2sci/

> "One of my users contacted me for their Claude built email scraper because the way they set it up it was requesting to be registered as an enterprise app in Entra."
> — Nomaddo, r/sysadmin, 2026-10-07, https://reddit.com/r/sysadmin/comments/1wzttxb/users_and_ai/peh6igr/

> "I had a non technical user try to install python and ngrok to run a MCP server that GPT wrote for him. Read the chat history.. this was all AIs idea, and it didn't explain how *any of this* was risky, might violate company policies, etc."
> — nevesis, r/smallbusiness, 2026-09-12, https://reddit.com/r/smallbusiness/comments/1wdsgrt/need_to_build_internal_tools_without_developers/p9azq3q/

> "If your internal tool reads from SharePoint lists, Excel or Outlook, standard connectors cover it and the Microsoft 365 you already pay for includes the Power Apps rights to build it. The second you point at SQL Server… EVERY person who opens the app needs a paid Power Apps license, not just you… you find out in a month or two when you try to roll it past yourself and three people."
> — blud_13, r/smallbusiness, 2026-09-11, https://reddit.com/r/smallbusiness/comments/1wdsgrt/need_to_build_internal_tools_without_developers/p98l5s0/

> "Billy in Accounts can't fire up Claude even if he wanted to, and running Ollama on his work machine is a fireable offense. IT control precisely what can run on their hardware, after all. So he won't ever be able to finally fix those TPS reports even if he is the one guy with the knowledge to do so."
> — kjellsbells, HN, 2026-09-19, https://news.ycombinator.com/item?id=49769627

> "Back then, employees were secretly installing Excel macros and Dropbox just to get work done faster. Now they're quietly running Claude Code in the terminal because the official Copilot can't even forma a CSV properly."
> — veunes, HN, 2026-02-02, https://news.ycombinator.com/item?id=46854187

> "1. Invoice billing review. Automated 80% of what was a manual process… Dev time: 1 day 2. Data dashboards. We use janky saas that does not have APIs… Used to take my associate 3 hours per week to do this in a crappy spreadsheet… Dev time: 4 hours."
> — meetingthrower, HN, 2026-01-19, https://news.ycombinator.com/item?id=46677867

> ""I manually merge these three CSVs every Friday." "We track this process in a spreadsheet and it sucks." "I wish I had a tiny internal tool that did X." Things where someone already has a real workflow, but building custom software for it was never worth the time or money. If they're technical and can fire up Claude Code themselves, awesome — they probably don't need me. I'm more interested in the people who won't."
> — Jese__Ki, r/founder, 2026-09-28, https://reddit.com/r/founder/comments/1ws8wsj/i_want_to_build_real_software_for_real_people/pcjzvlt/

> "Essentially any time someone is like man this sucks, I'll build a tool for it if I can do it in an hour. I've been building our own internal production and quoting tools."
> — rockchurchnavigator (sign shop owner), r/signshop, 2026-09-23, https://reddit.com/r/signshop/comments/1wlzw35/how_are_you_using_ai_or_automation_in_your_sign/pbhdukh/

## Count by tool the speaker used (51 quotes; a few name two tools, counted once by the tool that built the thing)

| Tool | Quotes |
|---|---|
| Claude Code (named) | 10 |
| Claude app / "Claude" unspecified | 12 |
| Claude artifacts | 2 |
| ChatGPT / GPT / Codex | 2 |
| Lovable | 9 |
| Replit | 0 |
| Bolt | 0 (one mention inside the noashavit quote) |
| Cursor | 0 (one mention inside the Bunas quote) |
| Power Apps | 1 |
| Spreadsheet / Google Sheet / Excel | 3 |
| Other (Gemini, generic "vibe coded", Slack, surge.sh) | 11 |

Lovable's count is inflated by the "$400M" thread; no verbatim Replit builder quote surfaced in reachable open sources.

## Patterns

Five phrasings that recur:
1. "would never have been worth building before" / "never going to get built" (aurareturn, theshrike79, sbarre, drogus, MattGaiser).
2. "throwaway" / "disposable" / "one-off" named up front, as the builder's own category (kazinator, virtualritz, starik36, nseckinoral, sbarre).
3. The "what now" problem: "still had to take care of deployment", "host it somewhere", "Youll need to host somewhere tho", "IT department that blocks production deployment" (ElFitz, kkoppenhaver, jvwww, neems74, moron4hire).
4. "permissions live in the UI, not the database" / "auth and permissions took longer than the whole UI" / "EVERY person who opens the app needs a paid… license" (DifficultWorkers, kimbaptori, blud_13).
5. "no idea how it does what it does" / "ask Claude, it always gets it right" / "can't scale their attention to be an app owner" (Talt45, datawrangler017, sajithdilshan, monknomo).

Three most common fates of a one-off tool:
1. Thrown away on purpose, no regret: IMEI check, medical-bill scanner, "nonsense throwaway apps". The builder counts it as a win.
2. Quietly promoted to recurring, then rots in silence: a CRM field renamed, a 2.0 request, the author moves teams; staff drift back to the spreadsheet or to forwarding requests to engineering (Shelley, mjr00, agent013, Tiny_Ad_7720).
3. Built and never adopted by anyone but the author: "not a lot were used", "Nobody else uses that", "He would be lost in claude code" (patchorang, MattGaiser, hackernud3s).

What people say they wanted instead:
- The tool where teammates already are, as a link that works for them: artifacts "to share a quick link with the team", a cofounder who "just couldn't use it. Cause it was a CLI", surge.sh "to share things out as needed", Excel's old trick of "simply emailing the workbook".
- Permissions and data from the team's own systems, not from the generated UI: "an llm-developed-app-friendly shared datastore", IT's existing permissioning so "I can deploy it to the whole team", rules that live next to the table so the next prompt "can't quietly undo it".
- Someone, or something, to own upkeep: the one working example is a scheduled Claude Code routine that triages a Slack feedback channel and answers with a PR link (eranation); the common complaint is "If I make every tool I use, all I'll do is manage and support the tools".
- Pro-Lovable voices reduce to one thing: login + database + hosting in one click, for a person who will never open a terminal, often for an internal tool. Pro-Claude-Code voices reduce to: once hosting is solved, the terminal agent is better and cheaper. Neither camp claims to have solved sharing, permissions, or maintenance for a small internal tool.
