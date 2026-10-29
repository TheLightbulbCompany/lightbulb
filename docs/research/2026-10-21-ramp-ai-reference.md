# Ramp AI products: reference (Glass, Router, AI Token Spend Management, agents)

Compiled 2026-10-21. Facts and mechanisms only, no recommendations. Each claim has a source.
Tags: **[Ramp]** means Ramp's own page, doc, or post. **[3P]** means press or an analyst. **[1 outlet]** means only one source reports it.
Ramp's own figures (savings, adoption, usage) are self-reported. No independent audit of any of them exists.

---

## 0. Summary

| Product | What it is | Public since | Price | Primary source |
|---|---|---|---|---|
| **Glass** | A company-wide AI workspace. It has shared, editable company knowledge, shared "Skills", persistent memory, multiplayer sessions, connectors, scheduled automations, and a cost meter for each project. Every request goes through Router. | Internal from about Jan–Apr 2026. Architecture made public 2026-04-09. External early access from **2026-10-13**. | Not announced | https://ramp.com/blog/introducing-glass |
| **Router** (router.com) | An LLM gateway: one endpoint (OpenAI Responses-style, plus Anthropic-compatible) in front of OpenAI, Anthropic, xAI, DeepSeek, Moonshot, Z.ai, NVIDIA, MiniMax, Qwen and others. Gemini needs BYOK. It has routing strategies, fallbacks, spend logs, and per-request inspection. | First public page **2026-07-20** (ramp.com/router). Relaunched on router.com **2026-08-19**. | Routing is free through 2026. Tokens are billed at list price. $26 credit at signup. | https://ramp.com/blog/router-launch, https://router.com/faq |
| **AI Token Spend Management** (also "AI Spend Management") | A finance-side dashboard. It ingests AI provider usage and billing data through admin/analytics API keys, billing exports and IAM roles. It breaks spend down by provider, model, user, team and API key, and has limits, alerts and weekly briefings. | Expanded launch **2026-07-16** | Free to see total spend, for customers and non-customers. Spend Limits need a paid "AI Spend Management subscription" (price not published). | https://ramp.com/blog/ai-token-spend-launch, https://support.ramp.com/ai-token-spend-management |
| **Ramp agents for MCP/CLI/API** (agents.ramp.com) | A hosted MCP server, a CLI, and the Developer API, so outside agents can act in Ramp. There are two identity models: on behalf of a user, and a standalone agent identity. | 2026 (exact date not found) | Not stated | https://agents.ramp.com/docs/guides/connecting |
| **Finance agents** (Policy Agent, AP agents, Procurement agents) | Agents inside Ramp's finance products. | Policy Agent summer 2025. AP agents 2025. Procurement agents 2026-04-29. | Policy Agent is Ramp Plus only | see §4 |

---

## 1. Glass

### 1.1 Identity and positioning
- Tagline: "The AI workspace where knowledge compounds." Subhead: "AI made every employee faster. Glass makes the entire company smarter." [Ramp] https://ramp.com/glass
- Launch post: "Introducing Glass: Learn Things Once", by Eric Glyman (co-founder and co-CEO), dated 2026-10-13. [Ramp] https://ramp.com/blog/introducing-glass
- The launch post's premise: "Giving every employee AI — the best models, the most tokens — is not the same as becoming an AI-native company. It's like giving every employee a Steinway and expecting a symphony." [Ramp, quoted via search snippet of the post]
- Stated origin: Ramp spent **$1.5M on tokens in one week in May 2026**. A small internal team then took the setups that AI-forward employees had built for themselves and made them the starting point for everyone. [Ramp] (blog). PYMNTS also reports it, via https://www.thestateofai.com/news/ramp-glass
- One newsletter reports that Ramp's AI token bill grew "from a rounding error to more than 10% of payroll" within a year. **[1 outlet]** https://linas.substack.com/p/weeklyfintechpulse420

### 1.2 Feature inventory

The external product (Oct 2026) and the internal product described in April 2026 overlap. The column says which source supports each row.

| Feature | Mechanism as publicly described | Source |
|---|---|---|
| **Shared, editable company knowledge** | Employees edit "what Glass knows". Ramp's example: when someone counts new U.K. customers, Glass leaves out U.S. companies with U.K. offices, because Finance taught it that rule months earlier. Glass then applies that definition for everyone. "323 employees (PMs, CX agents, salespeople, marketers, analysts) have made 6,500+ edits to what Glass knows." | [Ramp] blog |
| **Skills (shared saved prompts and workflows)** | "Saved prompts become shared Skills." Internally, skills live in **Dojo**, a marketplace of **350+** skills. Each skill is a **markdown file** that describes one task, is **Git-backed, versioned, and reviewed like code**. | [Ramp] blog. Dojo, markdown, Git: Goddijn et al. (Apr 9 2026), via https://aicatchup.com/practices/internal-ai-workspaces-playbook and https://departmentofproduct.substack.com/p/how-to-build-powerful-internal-ai |
| **How a change propagates** | Public material gives this much: a knowledge edit is shared company-wide and later sessions use it (the U.K. example). Skills are Git-backed and reviewed, so a skill change goes through a review step. Memory is rebuilt by a **24-hour synthesis pipeline**. Ramp has not published any further detail: no conflict handling, no scoping by team, no approval for knowledge edits. | as above. Pipeline: https://www.fintechbrainfood.com/p/ramp-cracked-ai |
| **Sensei (skill recommender)** | Looks at the user's connected tools, role and recent work, then recommends the skills most likely to matter. One source says it recommends **five** on day one. | https://www.fintechbrainfood.com/p/ramp-cracked-ai, aicatchup |
| **Persistent memory** | Per user. Built from the user's authenticated connections and from previous sessions: Slack, Notion, Linear, Calendar. It holds context on colleagues, active projects, relevant Slack channels, Notion docs and Linear tickets. A synthesis pipeline refreshes it every 24 h. | fintechbrainfood, aicatchup (both citing Goddijn) |
| **Multiplayer sessions** | A user can share a Glass session. A teammate joins, sees everything, and they finish the work together. Ramp's example: a support agent hands a ticket to an engineer, with the ticket context already in the session. | [Ramp] blog |
| **Scheduled automations** | Daily, weekly, or custom cron. Results post to Slack. Internally they ran **locally on the user's laptop** ("turns the user's laptop into a server"). Example: spend anomalies pulled at 8am and posted to Slack. **Headless mode**: set a task once and let it run overnight. When a step needs human approval, the request goes to the user's **phone**. | departmentofproduct, fintechbrainfood, aicatchup |
| **Split-pane UI** | The AI and the user's work appear side by side. | **[1 outlet]** https://cryptobriefing.com/ramp-launches-glass-ai-workspace/ |
| **Connectors** | External launch names **Linear, Notion, Google, Slack, Gong "and dozens more"**. Internal version (Apr): "30+ pre-connected tools" through one **Okta** SSO sign-in, including **Salesforce, Snowflake, Gong, Slack, Notion, Google Workspace, Figma, Zendesk, Linear, Calendar**, plus Ramp internal tools (**Ramp Research**, **Ramp Inspect**, **Ramp CLI**). Access is scoped to each employee's permissions. | [Ramp] blog. Internal list: https://r-beta.slax.com/b/30f3f471-e9f1-43b3-8cb5-f47ed59b7109 (repost of an X post), aicatchup, Built In job listing https://www.builtinnyc.com/job/ai-operations-specialist-enablement/8132921 |
| **Models** | "Works with open and closed models." **Every request goes through Ramp Router**, which picks a model for each job. Ramp says customers can switch when a better model ships. No model list for Glass is published. | [Ramp] blog. https://www.thestateofai.com/news/ramp-glass |
| **Agent runtime** | One repost of an X post says Glass is "built on **Anthropic's Claude Agent SDK**" and is Ramp's own version of a "Cowork-style" product. **[1 outlet; not confirmed by Ramp]** | https://r-beta.slax.com/b/30f3f471-e9f1-43b3-8cb5-f47ed59b7109 |
| **Cost meter and limits** | "Every project has a meter, like a taxi", so cost is visible while work happens. Users can set limits before they are reached. | [Ramp] blog. https://www.implicator.ai/ramp-glass-ai-workspace-token-spending/ |
| **Onboarding** | Self-configuring through SSO. Ramp says it is "personally onboarding the first businesses". Request access at ramp.com/glass. | [Ramp] blog |
| **Permissions and admin controls** | Not published. Internally, connectors are scoped to each employee's own permissions. | aicatchup |
| **Security and data handling** | Not published. No security documentation, SOC 2 statement or retention policy for Glass was found. Note that Router's default is to log inputs, outputs and tool calls for 1 year (see §2.8), and every Glass request goes through Router. Public sources do not say whether Router's default retention applies to Glass traffic. | linas.substack (says "no public security documentation yet") |
| **Pricing** | Not announced. | thestateofai, implicator |
| **Who it's for** | "Every employee": PMs, CX, sales, marketing, analysts, finance. It is not limited to engineers. Public sources do not say whether a Ramp card or account is required. | [Ramp] blog. thestateofai, linas.substack |

### 1.3 Internal history and team
- Ramp published the architecture (not the software) on **2026-04-09**: Seb Goddijn et al., "We Built Every Employee at Ramp Their Own AI Coworker" (LinkedIn/X). The central argument: "the models are good enough; the harness is the bottleneck." aicatchup; X: https://x.com/sebgoddijn/status/2042285915435937816
- Glyman on X, April 2026: "99% of Ramp uses ai daily. but … most people were stuck … terminal configs, mcp servers, everyone figuring it out alone. so we built Glass." https://x.com/eglyman/status/2043362828178841860
- Build: a **team of four built it in under three months**. slax repost. Named team: Sebastien Goddijn, Shane Buchan, Cameron Leavenworth, Calvin Kipperman, Jay Sobel, Caroline Horn (aicatchup). Goddijn has since left Ramp to start Yaklabs / "Bonsai" (search snippet of his post).
- Shane Buchan: "nearly a million views" on the April reveal. He describes it as "an app" used by about 700 people. https://x.com/shanebuchan/status/2044526740299526511
- About **700 DAU within a month** of the internal launch. slax repost **[1 outlet]**
- In about six months of internal use before the external launch, the October launch added **multiplayer sessions, model choice, and the cost meter**. linas.substack **[1 outlet]**
- Job postings name the internal AI stack: **Ramp Glass, Ramp Inspect, Ramp Skills / Dojo, Ramp Research** (a Snowflake research tool built into Glass), the Ramp CLI, Claude Code, and Notion. Example: "AI Operations Specialist | Agentic Workflows", USD 114,200–184,100. https://jobs.generalcatalyst.com/companies/ramp-2/jobs/57190561-ai-operations-specialist-agentic-workflows. Built In NYC version: https://www.builtinnyc.com/job/ai-operations-specialist-enablement/8132921
- **Ramp Research** (Sept 2025): an in-house AI analyst in Slack. It indexes metadata from dbt, Looker and Snowflake. It has no PII access. It handled 1,800+ questions and 300+ users. Over 4 weeks, #ramp-research-beta got 1,476 questions against 66 in the old #help-data channel. "Meet Ramp Research", Ramp Builders blog, 2025-09-18 (via aicatchup).

### 1.4 Glass outcome claims (all Ramp-reported)
| Claim | Source | Caveat |
|---|---|---|
| 323 employees made 6,500+ edits to shared knowledge | Ramp blog | Internal count |
| Feature requests captured: about 300/month (by hand, in Salesforce) went to about 20,000/month, from one account manager's saved prompt (co-founder Karim Atiyeh) | Ramp blog. thestateofai | Count, not quality |
| More than 5,000 "your feature shipped" emails sent by a third chained prompt | Ramp blog | |
| "Moving our own work to Router cut our bill 30% overnight" | Ramp blog | This is a **Router** claim, not a Glass claim. implicator notes it is not baseline-controlled |
| Glass "cut AI costs by 30% in certain accounts" | cryptobriefing, thestateofai | Paraphrase of the above. Wording varies between outlets |

### 1.5 Coverage
- Primary: https://ramp.com/blog/introducing-glass. Glyman's X post: https://x.com/eglyman/status/2110007713396613568
- Press: https://www.implicator.ai/ramp-glass-ai-workspace-token-spending/, https://cryptobriefing.com/ramp-launches-glass-ai-workspace/, https://www.thestateofai.com/news/ramp-glass, https://siliconangle.com/2026/10/15/ramp-raises-1-85b-as-it-expands-beyond-finance-software/ (adds: teams can "upload guidance on how it should interpret requests"; natural-language search of business records), https://www.cpapracticeadvisor.com/?p=191846
- Hacker News: one submission, "Glass – The AI workspace where knowledge compounds", 2 points, 0 comments (2026-10-15, id 50106934).

---

## 2. Router (router.com)

### 2.1 Launch facts
| Item | Value | Source |
|---|---|---|
| First public appearance | **2026-07-20**: page at ramp.com/router. @tryramp: "We cut our own AI bill by 30% with a Router that chooses the right model for the right task. Now you can too." HN post "Ramp Router", 28 pts / 10 comments, 2026-07-21 | https://x.com/tryramp/status/2079274545307889981, HN 48987253, https://enterprisedna.co/resources/ai-pulse/ai-pulse-2026-07-21-ramp-opens-its-internal-model-router-to-the-public-claiming/ |
| Formal launch | **2026-08-19**: "Introducing Ramp Router", by Veeral Patel (Head of Applied AI). HN "Router by Ramp", 99 pts / 64 comments. Same day that Stripe's acquisition of OpenRouter was announced | https://ramp.com/blog/router-launch, HN 49366067 |
| TechCrunch | 2026-08-20 | https://techcrunch.com/2026/08/20/ramp-launches-its-own-ai-model-router-called-router/ |
| Domain | router.com. ramp.com/router 301-redirects there. A Ramp staffer on HN: "we (Ramp) didn't buy anything [i.e. OpenRouter], we built a model router in house". Another HN comment says the domain belonged to Innovation HQ | HN thread 49366067 |
| Internal history | Ramp says it ran the routing layer on its own production traffic for **3 years** | Ramp blog, TechCrunch |
| Beta status | The Aug 4 explainer calls it "currently in beta" | https://ramp.com/blog/what-is-an-llm-router |

### 2.2 API shape
- Base URL `https://api.router.com/v1`. The main endpoint is **`POST /responses`** ("one Responses endpoint for all models across providers"). Auth is a `Authorization: Bearer` key; the docs example uses the env var `RAMP_ROUTER_API_KEY`. https://docs.router.com/
- `GET /v1/models` returns callable IDs, including router aliases (`"owned_by": "router"`, with a `router_alias` object). https://docs.router.com/strategies/benchmark-routing
- FAQ: "compatible with the OpenAI and Anthropic APIs; switching usually means changing your base URL." Any agent that supports the OpenAI Responses API should work. https://router.com/faq
- Router-only request fields that the docs mention: `workload_slug`, `allow_flex_tier`, `fallback_timeout`, `provider_timeout`, `timeout_before_headers`, `service_tier`, and a models list (for an ordered fallback). https://docs.router.com/strategies/workloads, https://docs.router.com/strategies/cost-efficient-routing
- A CLI command exists: `ramp router refresh` (it refreshes coding-agent config). https://docs.router.com/strategies/switchyard-routing
- Coding-agent setup guides: **Claude Code, Codex, OpenCode, Pi, Hermes, Cursor, Conductor**. https://docs.router.com/coding-agents/claude-code

### 2.3 Providers and models (docs snapshot, 2026-10-21)
Source: https://docs.router.com/supported-models. Prices are in USD per 1M tokens (input/output) and are list base rates.

| Provider | Example models (IDs as listed) | Notes |
|---|---|---|
| Anthropic | claude-haiku-5-5 ($0.10/$0.50), claude-sonnet-5-5 ($2/$10), claude-opus-5-5 ($4/$20, Fast), claude-opus-4-6…4-8, claude-fable-5/5-1 ($10/$50) | 1M context on most |
| OpenAI | gpt-6-luna ($0.10/$0.50), gpt-6-sol ($2/$10), gpt-6-astra ($10/$50), gpt-6.1-sol, gpt-5.x family, o3/o3-pro, gpt-4.1, gpt-4o, gpt-oss-120b | "Fast" tier on many. Some legacy models shut down 2026-10-23 |
| DeepSeek | DeepSeek-V4/V4.1 Flash, V4-Pro | includes a `-us` hosted variant at higher price |
| Z.ai | GLM-5.2/5.3, 5.3-Flash | |
| NVIDIA | Nemotron-3-Ultra 550B, nemotron-lightning-3p5-30b | |
| xAI | grok-4.3 … grok-4.7, grok-4.20 variants, grok-build-0.1 | Press calls it "SpaceXAI (formerly xAI)" |
| Google Gemini | gemini-2.5 … 3.8-flash, 3.1-pro-preview, gemini-4-argon | **BYOK required** |
| Moonshot | kimi-k3, kimi-k3-fast | |
| Qwen, MiniMax | qwen3p8-max, minimax-m3 | |
| Hosts | Baseten, Fireworks (muse-spark-open, reflection/Beam-1.0) | Serving partners |
| TypeSafe | jev-1.13.0 / jev-1.14.0 ($0.04 in / $0.00 out, 64K) | The model behind "Auto Jev Routing" |

At launch the press listed OpenAI, Anthropic, DeepSeek, Moonshot, MiniMax, NVIDIA, xAI and Z.ai (TechCrunch), with Gemini, AWS, Together AI, Baseten and Crusoe "coming" (https://ai-tldr.dev/releases/ramp-router/). Anthropic's head of platform engineering, Katelyn Lesse, said Claude was available from day one (ai-tldr, unite.ai).

### 2.4 Routing logic
| Strategy (docs name) | Mechanism | Source |
|---|---|---|
| **Cost-efficient routing** (Beta, **on by default**) | Same model and provider. It only chooses between the **standard and Flex** service tier, based on each tier's recently observed performance. Streamed Flex attempts have a default 25 s `fallback_timeout` to first output, then fall back to standard. It applies only to direct-model requests that do not set `service_tier`. | https://docs.router.com/strategies/cost-efficient-routing |
| **Cross-provider routing** (Beta) | Preference modes: Throughput only / Favor cost / Cost only / Account preference. | https://docs.router.com/strategies/workloads |
| **Switchyard routing** (Beta) | Powered by **NVIDIA NeMo Switchyard**. Per coding-agent turn, it picks a frontier or an efficient model, using signals: tool-output errors, context compaction, passing tests, turn depth. Image input always goes to the frontier model. Sensitivity setting defaults to "Balanced". Launch example: 58 turns on "Luna", the final 5 on "Sol", both caches warm. | https://docs.router.com/strategies/switchyard-routing, launch blog |
| **Auto Jev Routing** (Beta, opt-in) | The user lists 2–6 models, each with a description of the work it is for. **TypeSafe Jev** matches the latest turn to a description. The request is rerouted only when the match is clear; otherwise the requested model is used. Recent turns stay on the model whose prompt cache is warm. It is mutually exclusive with Switchyard. | https://docs.router.com/strategies/auto-routing |
| **Benchmark routing** (Beta) | `model` = benchmark alias. Router samples from a weighted model mix that follows benchmark results. Launch blog: "pick up to three benchmarks, weight them". New models are scored on **Ramp SWE-Bench**, built from Ramp production tasks. | https://docs.router.com/strategies/benchmark-routing, launch blog, sqmagazine |
| **Shadow models** (Beta) | Sends a sample of live traffic to a candidate model while production stays on the current model, then compares cost, latency and quality. | launch blog |
| **Prompt caching** | Listed as a strategy page. | docs |
| **Fallbacks** | Automatic on provider failure or rate limit. A models list gives an explicit fallback order. | FAQ, docs |
| **Workloads** | Presets that bundle strategies: Low latency, General purpose agents, Coding agents, Coding agents (Experimental), Automations, Research and analysis. Selected per request with `workload_slug` or bound to an API key. "Workload profiles change strategies, but they never change models by default." | https://docs.router.com/strategies/workloads |

**Internal algorithm (Ramp engineering):** a failure-aware router. An **EWMA** tracks provider-side failure rates only. **Thompson sampling** runs over log-latency with a Normal-Inverse-Gamma prior; sufficient statistics live in **Redis**. For each candidate it computes P(bad outcome) = failure probability + probability of missing the deadline, weighs that against relative cost, and the result sets the order and the fallback chain. Example: an LLM reranker with a 6 s deadline. Flex latency degraded during 9am–5pm but matched standard off-hours. GPT-5.4 Nano Flex had median/p95 of 1.73 s / 6.22 s against Standard's 1.84 s / 6.34 s, at half the cost. Reported **>25% savings** in that experiment, with error rate down 0.09 pp, and **30%** for a streaming version used by Ramp Inspect. The gateway handles "trillions of tokens per day". https://builders.ramp.com/post/thompson-sampling-model-routing (via https://www.zenml.io/llmops-database/cost-efficient-llm-routing-with-online-learning-and-thompson-sampling)

### 2.5 Billing
- Routing is **free through end of 2026**. Tokens are billed at **list price**, i.e. passthrough with no stated markup. **$26** in free credits at signup. 2027 pricing has not been announced. https://router.com/faq, TechCrunch
- Launch promo: 50% off GPT-5.6 Sol until 2026-09-18. Launch blog.
- BYOK: required for Gemini. ai-tldr says BYOK is supported in general.
- Enterprise: custom plans through sales@router.com. "Enterprise features coming soon." https://docs.router.com/resources/enterprise, FAQ

### 2.6 Keys, eligibility, region
- Sign-in by emailed link. A **business email and business domain are required**: no Gmail, Outlook, iCloud, Proton and similar. .edu domains are onboarded manually. Exceptions: router@ramp.com. https://docs.router.com/resources/signup-eligibility
- **No Ramp account, card or LLC needed.** FAQ
- **US only** ("more countries coming"). FAQ, TechCrunch. An HN user in Sweden was blocked.
- Keys, usage and spend are managed at https://app.router.com/dashboard. Status: https://app.router.com/status

### 2.7 Dashboards and observability
- Per request: model, provider, service tier, tokens, latency, cost, fallback attempts, and whether a strategy changed the model. Launch blog, TechCrunch, docs.
- router.com sections advertise savings "by model, product, and team", plus "spend control: from inference to invoice". https://router.com/llms.txt

### 2.8 Data retention
- **Default: model inputs, outputs and tool calls are logged for 1 year, with opt-out.** Ramp says it removes PII "before using that content to improve the product." TechCrunch, sqmagazine, ai-tldr.
- An HN commenter quoted the docs: Router "stores inputs, outputs, and metadata … may turn this off in settings." HN 49366067.
- No ZDR, SOC 2 or BAA statement was found. An HN commenter asked about BAAs and got no answer.

### 2.9 The savings claims: exact sources
| Figure | Exact wording and where | Method disclosed? |
|---|---|---|
| **30%** (Ramp's own bill) | @tryramp X, 2026-07-20: "We cut our own AI bill by 30% with a Router…". Glyman, Glass post 2026-10-13: moving Ramp's work to Router "cut our bill 30% overnight". Aug 4 explainer: "roughly a 30% cost reduction with no meaningful quality drop, based on three years of production traffic". | No baseline, dollar amount or workload-held-constant comparison (implicator, sqmagazine). The closest method is the engineering post: >25% in the reranker experiment, 30% on Inspect streaming |
| **40%** (customer average) | router.com headline: "cuts inference costs by 40% on average". Launch blog: customers "save 40% on cost without compromising quality or uptime". | Not disclosed (sqmagazine) |
| **92%** | Delphi's Valentin De Matos: model costs cut 92% | Customer quote, via a search snippet of unite.ai **[1 outlet]** |
| **12%** | AI Token Spend launch: "average potential savings: 12% of monthly AI spend" (spend management, not Router) | Not disclosed |
| **17.5%** | One search result attributed "17.5% from an unnamed customer in Ramp's release". **Not found in any primary source** | — |
| Reliability | "99.9%+ across production traffic" | Ramp claim |
| Latency | One search summary attributed "~30 ms added latency" to Ramp's page. **Not seen on the fetched page** | unverified |

---

## 3. AI Token Spend Management

### 3.1 Launch
- Blog: "AI spend moves fast. Know where your next AI dollar is going.", by Siddhi Surana (PM), **2026-07-16**. https://ramp.com/blog/ai-token-spend-launch. Press release: https://www.morningstar.com/news/pr-newswire/20260716ny05901/ramp-launches-ai-token-spend-controls. SiliconANGLE: https://siliconangle.com/2026/07/16/ramp-targets-ais-fastest-growing-cost-expanded-token-spend-tracking/
- SiliconANGLE calls it an expansion of an earlier version that Ramp built first for its own finance team. Forkast lists it in the Q2 2026 release. The earliest version's date was not found.
- Stats at launch: "1,300+ businesses managing 100+ trillion tokens monthly". "Thousands of businesses" have connected providers (press). "One in three customers has access to a cheaper model that performs equally" (SiliconANGLE). AI token spend across Ramp customers is up **20.7x since June 2025** (press). [Ramp]

### 3.2 Ingestion (help center, current)
Source: https://support.ramp.com/ai-token-spend-management

| Provider | Connection method | Requirement |
|---|---|---|
| Claude API | Admin API key **with write** (write also lets Ramp lock keys) | Claude Platform team org |
| Claude Enterprise (Claude.ai, Claude Code) | Analytics API key | Enterprise (Team plan not supported) |
| OpenAI API | Read-only Admin API key | OpenAI Platform org |
| ChatGPT Enterprise (ChatGPT, Codex) | Admin key with Codex analytics read, `chatgpt.enterprise.usage_limit.read`, optional `…usage_limit.write` | Enterprise only |
| Cursor | Read-only Team API key, or "Ramp for Chrome" | may need Enterprise |
| Google Cloud / Gemini | Reads the Cloud Billing **BigQuery export** (no API key) | billing export configured |
| AWS Bedrock | Read-only IAM role | |
| Azure OpenAI | Entra app with federated credential, read-only MCA/EA billing | |
| Fireworks AI | API key scoped to one account | |
| OpenRouter | Listed by SiliconANGLE (Jul 2026). Not in the current help page | unverified |

- Sync is **daily (T-1)**. Recent data backfills within minutes. A full historical backfill takes up to 24 h. The Anthropic API rate limit is 1 req/min, so large accounts load slowly.
- Card transactions: the blog says AI billing is otherwise scattered "across provider dashboards, corporate cards, and invoices". SiliconANGLE says it "reconciles invoices against actual usage". The help center does not describe card-transaction matching.
- **Prompts and message content are not ingested.**
- OpenAI credit-based dollar figures are estimates. Included credits and overage are not split out.

### 3.3 Dimensions
Provider, model, user (name or email), internal user/team label, department, API-key label. Breakdowns appear only when the provider exposes the field. The blog and press also list **project** and **team**. Help center; blog.

### 3.4 Budgets, limits, alerts
| Control | Behavior | Source |
|---|---|---|
| Soft limits | For people, departments, users or API keys. **Notify only**: they do not stop provider charges on Anthropic or OpenAI Platform | help center |
| Hard limits | **ChatGPT Enterprise workspace only**: blocks usage past the threshold | help center |
| Manual key lock | **Anthropic keys only** (uses the write-scoped admin key) | standalone help page |
| API-key Spend Limits | Estimated USD. Recur monthly, quarterly or yearly. Alerts go to the owner or the mapped key owner plus extra recipients. The 100% threshold is always included | https://support.ramp.com/ai-token-spend-management-standalone |
| Suggested limits | Ramp suggests limits from observed trends | blog |
| Anomaly / spike alerts | Slack or email, to key owners, managers and others. Weekly summaries | blog, help center |
| Weekly Briefing | Biggest week-over-week changes, spikes, savings recommendations (model downgrade, prompt caching, reasoning level, turning off Fast Mode), in plain language | blog |
| Gating | Spend Limits need an active **AI Spend Management subscription**. The "AI Token Spend Admin" role alone does not unlock them | help center |

Customer examples in the blog: a $1,600 weekend spike from one user; $3,000+ spent on Opus for work Sonnet could do; Fast Mode caused a 6x cost increase in one week; AngelList found about $10K/month in missed prompt caching.

### 3.5 Packaging
- Free to view total AI spend, for Ramp customers and non-customers. Blog.
- **Standalone app** for businesses that use Ramp only for AI spend. It has AI Spend Management, My AI spend (employees see their own usage), and Company. It does not have cards, expenses, Bill Pay, procurement or treasury. "Explore Ramp" upgrades to Ramp Plus, which starts a financing application. https://support.ramp.com/ai-token-spend-management-standalone
- Roles: Owner, Admin, IT Admin, **AI Token Spend Admin** (can connect). Finance Admin and View-Only can only view.
- Activity logs do not cover provider-connection history. Disconnecting a provider keeps the data already imported; to revoke access, delete the key at the provider.
- Ramp Plus general pricing: $15/user/month plus an unpublished platform fee (third-party). The AI Spend subscription price was not found.

---

## 4. Other Ramp AI agents and features relevant to companies running many agents

| Item | Facts | Source |
|---|---|---|
| **Ramp MCP server** | Live: `https://mcp.ramp.com/mcp`. Demo with sample data: `https://demo-mcp.ramp.com/mcp`. Browser OAuth; the connection acts as the user who authorized it, with that user's role. Custom clients need an allowlisted redirect URI (https, localhost or 127.0.0.1; no wildcards). Query responses are **capped at 100 rows**. Chat attachments are not forwarded, so receipts go through the CLI or dashboard. Admins control MCP access per user on the Integrations page | https://agents.ramp.com/docs/guides/connecting, https://agents.ramp.com/docs/guides/identity |
| **Ramp CLI** | Recommended for agents that can run a shell. Config in `~/.config/ramp/config.toml`. Read-write sessions refresh for 24 h, read-only for 1 week. Default environment is Sandbox; production is chosen with a flag. A one-prompt coding-agent setup checks for an account, installs the CLI and connects it | agents.ramp.com quickstart and connecting guide |
| **Agent identities** | (1) On behalf of a user: available now. (2) **Standalone agent** (limited early access): owned by the business, with its own client ID and secret, least-privilege role, an accountable human owner, its own attribution, and pause, rotate or remove controls. Created under Company → Agents (`agents:write`). "Adding an agent to an approval workflow does not grant authority by itself" | https://agents.ramp.com/docs/guides/identity |
| **Developer API** | For deterministic integrations; the integrator owns OAuth, scopes and webhooks. https://docs.ramp.com/developer-api/v1 | connecting guide |
| **Agent cards / agent payments** | The quickstart mentions "agent cards" that give an agent purchasing power. Ramp and Visa expanded their partnership on 2026-03-31: Visa Intelligent Commerce and the Trusted Agent Protocol, with agents for corporate bill pay. Ramp was a pilot partner in Visa Intelligent Commerce. TechCrunch (Oct 15) mentions "a corporate credit card designed for AI agents". No Ramp product page for agent cards was retrieved | https://finance.yahoo.com/sectors/technology/articles/ramp-visa-expand-partnership-agentic-104008481.html, TechCrunch Oct 15 |
| **Policy Agent** | Checks every card transaction and reimbursement against the written expense policy. Starts in review-only mode. Reviewers keep final authority. Admins upload a policy and Ramp converts it to a structured, citable format. Ramp claims about 85% fewer manual reviews at 99%+ accuracy. **Ramp Plus only**. Launched summer 2025 | https://support.ramp.com/policy-agent-overview, Wikipedia |
| **Agents for AP** | Invoice coding, invoice approval, payment processing. 2025 | https://ramp.com/blog/agentic-ai/best-ai-agents-for-ap-automation (2025-11-03) and search results |
| **Procurement agents** (2026-04-29) | Vendor sourcing (RFx generation and scoring), conversational intake (flags out-of-policy or duplicate requests), compliance agents (vendor due diligence; security, including SOC 2/SSO checks from trust centers; legal and privacy, which flags AI vendors that process customer data), custom agents from templates, renewal agent, plain-English reporting. Integrations: Ironclad, LinkSquares, Docusign, Dropbox Sign, Vanta, Drata, LogicGate, OneTrust, Jira, Asana, Linear. Claims: 16% vendor savings (median), 46 h/month saved ("includes planned features"), 3x faster approvals | https://ramp.com/blog/procurement-ai-agents |
| **Ramp Inspect** | Internal background coding agent. One **Modal Sandbox** per session holds the full dev environment (Postgres, Redis, Temporal, RabbitMQ), **OpenCode**, a VS Code server, a web terminal and Chromium. Repos are snapshotted about every 30 min. Connects to GitHub, Buildkite, Sentry and Datadog. Its share of merged PRs: about 30% (early), then "over half" or "more than two-thirds", depending on source and date. Non-engineers file 12% of human-initiated production PRs. An open-source version is called "Open-Inspect" | https://modal.com/blog/how-ramp-built-a-full-context-background-coding-agent-on-modal, https://infoq.com/news/2026/01/ramp-coding-agent-platform/, thestateofai |
| **Ramp Research** | See §1.3 | |
| **Ramp Intelligence** | 2023-05-17. GPT-powered: vendor price intelligence, expense automation, accounting copilot | https://ramp.com/blog/announcing-ramp-intelligence |
| **Ramp Stack** | An operating system for accounting firms. Q2 2026 release | https://forkast.news/?p=131674, thestateofai |
| **Ramp AI Index** | Monthly, from card and bill-pay transactions. Adoption = share of businesses with a paid AI transaction that month. Published on FRED (series RAMPAI*). In April 2026 Anthropic reached 34.4% business adoption against OpenAI's 32.3%. Undercounts free and personal-account use. Per linas.substack, token spend barely grew in Aug–Sep and the top 1% of firms account for 80% of it **[1 outlet]** | https://fred.stlouisfed.org/data/RAMPAIALL, https://letsdatascience.com/news/anthropic-surpasses-openai-in-business-ai-adoption-0440647d |

---

## 5. Company facts relevant to distribution

| Metric | Value | Date | Source | Note |
|---|---|---|---|---|
| Valuation | **$60B** (pre-money per Bloomberg), $1.85B raised, Dragoneer lead. Co-leads Thrive, Founders Fund, Sands, Avenir | 2026-10-15 | https://techcrunch.com/2026/10/15/ramp-reaches-60b-valuation-as-it-pushes-deeper-into-ai, https://www.gunder.com/en/news-insights/client-news/dragoneer-leads-ramps-dollar185-billion-financing-at-dollar60-billion-valuation | Confirmed by Ramp |
| Prior rounds | $44B ($750M, June 2026, ICONIQ/GIC/OTPP). $32B (Nov 2025). $22.5B (Jul 2025). $16B (Jun 2025). $13B (Mar 2025) | | Wikipedia, TechCrunch, SiliconANGLE | |
| Total raised | about $5B | Oct 2026 | TechCrunch | |
| Annualized revenue | **>$2B** (as of Oct 1, 2026). About $1.5B in June 2026. $1B in Aug 2025 | | TechCrunch, implicator, Wikipedia | Ramp-stated. Cash-flow positive per implicator |
| Revenue mix | About 70% from card interchange | — | Sacra via linas.substack | **[1 outlet]** |
| Purchase volume | >$275B annualized, +175% YoY | Oct 2026 | TechCrunch | |
| Customers | **85,000+** (70,000 in June 2026). Most use 2+ products; about a quarter use 3+ | Oct 2026 | TechCrunch, SiliconANGLE | Named: Visa, Uber, Shopify, Robinhood, Anduril, Notion, Cursor, Figma |
| Employees | 1,200 (2025, Wikipedia). About 800 (thestateofai, 2026). "800+ builders" internally | | | Conflicting; the 800 may be a builder count, not headcount |
| Internal AI adoption | 99% / **99.5%** using AI daily. 84% using coding agents weekly. AI usage +6,300% YoY. 1,500+ internal apps from 800+ builders in 6 weeks | Apr–Oct 2026 | Glyman X, SiliconANGLE, slax repost | 6,300% and 84% are **[1 outlet]** |
| Internal token spend | $1.5M in one week (May 2026). "More than 10% of payroll" | | Glyman blog. linas.substack **[1 outlet]** for payroll | |
| AI Token Spend usage | 1,300+ businesses, 100T+ tokens/month | Jul 2026 | Ramp blog | |
| Customer AI spend growth | 20.7x since June 2025 | Jul–Oct 2026 | Ramp (press release, thestateofai) | |
| IPO | Wants to be "IPO-ready by end of 2026" | | thestateofai **[1 outlet]** | |
| Glyman roadmap (2025) | 2026: agents handle finance busywork. 2027: agents work in parallel. 2028: autonomous under human oversight | | thestateofai **[1 outlet]** | |
| Leadership quotes | CTO Rahul Sengottuvelu: "AI is the fastest-growing line item at most companies, and the one they can least measure." CFO Will Petrie on AI spend. Keith Rabois: wants Ramp "larger … than Anthropic" | | sqmagazine, SiliconANGLE, TechCrunch | |

---

## 6. Timeline of Ramp AI launches

| Date | Event | Source |
|---|---|---|
| 2023-05-17 | Ramp Intelligence (GPT-powered price intelligence, expense automation, accounting copilot) | ramp.com/blog/announcing-ramp-intelligence |
| 2025 (summer) | Policy Agent (expense policy review) | Wikipedia, support.ramp.com |
| 2025 | Agents for AP | search results, Ramp blog 2025-11-03 |
| 2025-09-18 | "Meet Ramp Research" (internal Slack data analyst) | Ramp Builders, via aicatchup |
| ~2026-01 | Ramp Inspect publicized (InfoQ Jan 2026; Modal case study Feb 2026) | infoq, modal.com |
| ~Jan–Mar 2026 | Glass built internally (4 people, under 3 months) | slax repost |
| 2026-03-31 | Ramp and Visa partnership expansion (agentic bill pay, Visa Intelligent Commerce) | Yahoo Finance |
| 2026-04-09 | Glass architecture published (Goddijn et al.). Glyman X post on Glass | aicatchup, X |
| 2026-04-29 | Procurement AI agents | ramp.com/blog/procurement-ai-agents |
| 2026-05 | $1.5M/week token spend (internal) | Glass launch blog |
| 2026-06 | $750M at $44B | TechCrunch |
| 2026-07-16 | AI Token Spend Management expanded launch | ramp.com/blog/ai-token-spend-launch |
| 2026-07-20 | Router first public at ramp.com/router ("cut our own AI bill by 30%") | X, HN |
| 2026-08-04 | "What is an LLM router" explainer (Thompson sampling; beta) | ramp.com/blog/what-is-an-llm-router |
| 2026-08-19 | Router formal launch on router.com (40% customer claim; free through 2026; $26 credit) | ramp.com/blog/router-launch |
| 2026 (date unknown) | agents.ramp.com: MCP server, CLI, standalone agent identities (early access) | agents.ramp.com |
| 2026-10-13 | Glass external early access | ramp.com/blog/introducing-glass |
| 2026-10-15 | $1.85B at $60B. Revenue >$2B ARR. 85k customers | TechCrunch |

One newsletter frames this sequence as "token spend controls in July, Router in August, Glass in October" (90 days). linas.substack

---

## 7. Unverified, conflicting, or unknown

**Conflicts**
- **Router savings: 30% vs 40%.** 30% is Ramp's own bill, with no baseline published. 40% is the "customer average", with no method published. The engineering post supports ">25%" on one reranker workload and "30%" on Inspect streaming.
- **Router launch date:** July 20 (first public page and X post) vs August 19 (formal launch on router.com). TechCrunch dated it August 20.
- **Inspect's share of merged PRs:** ~30%, "over half", or ">2/3", depending on source and date.
- **Internal AI adoption:** 99% vs 99.5%. Glass internal users: ~700 DAU vs "1,000+".
- **Employee count:** 1,200 (2025) vs ~800 (one 2026 outlet).
- **The Glass 30% cost cut:** some outlets attribute it to Glass; Ramp's text attributes it to Router.
- **"17.5%" customer saving** and **"~30 ms added latency"**: seen only in search-summary text. Not found in primary sources.

**Single-outlet claims**
- Glass built on the Claude Agent SDK (slax repost of an X post).
- Split-pane UI (cryptobriefing).
- AI bill is more than 10% of payroll; 70% of revenue from interchange; top 1% of firms account for 80% of token spend (linas.substack).
- IPO-ready by end of 2026; Glyman's 2026–2028 agent roadmap (thestateofai).
- AI usage +6,300% YoY; 84% of staff using coding agents weekly (slax repost).
- Delphi's 92% saving (unite.ai snippet).

**Unknown (not published as of 2026-10-21)**
- Glass: pricing; whether it requires a Ramp account or card; model list; permission model and admin controls; how knowledge edits are scoped, approved, versioned or rolled back (skills are Git-reviewed internally; knowledge edits are not described); conflict resolution; data residency and retention; SOC 2 or other security docs; whether the internal laptop-hosted automations became cloud-hosted for external customers; full connector list; whether Router's 1-year retention default applies to Glass traffic; any external customer results.
- Router: 2027 pricing and any markup after the free period; ZDR, BAA or SOC 2; non-US availability date; what the router.com "spend by product/team" view contains; whether Router spend flows automatically into AI Token Spend Management.
- AI Token Spend Management: subscription price; whether card-transaction data is matched to provider usage; the date of the original (pre-July) version.
- Agent cards: product page, launch date, controls.
- MCP tool list: the reference page did not render (https://agents.ramp.com/docs/reference/agent-tools).
- There are no meaningful Hacker News discussions of Glass. Router HN threads: 49366067 (99 pts) and 48987253 (28 pts). The main concerns raised there were default data logging, unknown future pricing, missing models, US-only availability, and timing next to Stripe's OpenRouter acquisition.
- Primary sources that could not be fetched: X posts (paywalled API), the original Goddijn article full text, the PYMNTS article, The Information (not searched successfully), demo-video transcripts (none found).
