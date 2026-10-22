# Enterprise angle: Agent Beacon and Ramp Glass (2026-10-21)

## 1. Agent Beacon (Asymptote Labs)

**What it is.** An MIT-licensed Go agent that runs on each laptop, in CI and in cloud sandboxes. It captures prompts, tool calls, commands, file edits, approvals, MCP calls and tokens from about 29 harnesses (Claude Code, Cursor, Codex, Gemini CLI; claude.ai and ChatGPT through a browser extension), using native hooks, plugins, local OTLP or session-store polling. Events are normalized to one OTel-based model in a local `runtime.jsonl`, forwardable to Splunk, Sentinel, Datadog or S3. YAML/CEL detection rules flag risky behaviour, for example a secret read followed by network egress. ([README](https://github.com/Asymptote-Labs/agent-beacon), [architecture doc](https://github.com/Asymptote-Labs/agent-beacon/blob/main/docs/architecture/architecture.mdx), [detections](https://github.com/Asymptote-Labs/agent-beacon/blob/main/docs/detections/index.mdx))

**Positioning drift.** Its topics and docs are about security and SIEM; the company calls itself "Runtime Infrastructure for AI Agents" ([asymptotelabs.ai](https://asymptotelabs.ai/)). The README now leads with "memory layer" (session knowledge served back over MCP and Agent Skills): telemetry is the moat, memory the install hook.

**Business model.** Open core ([pricing](https://beacon.sh/pricing)): OSS $0; Cloud $0; Pro $20/mo. Enterprise (custom): own-bucket hosting, "block or require approval for risky agent actions", MDM rollout, SSO, RBAC, audit logs. Buyer: **CISO and platform engineering**; pain: unseen agents on laptops.

**Maturity** (GitHub API, 2026-10-21). Created 2026-05-12; 2,342 stars, 211 forks, 2,642 commits, 19 contributors, v1.3.45 (several releases a day). 1,045 commits are by `claude`/`cursoragent` accounts. $5.5M seed reported; no named customers.

## 2. Ramp Glass

**What it is.** A company AI workspace launched 2026-10-13 ([Ramp post](https://ramp.com/blog/introducing-glass)). Every session starts from shared company knowledge. Saved prompts and workflows are reusable, anyone can edit what it "knows", and sessions are multiplayer. Connectors: Linear, Notion, Google, Slack, Gong; any model. Each project shows a cost meter with limits ("like a taxi"). Requests go through **Ramp Router** (August): one API, per-task model choice, spend dashboard, one-year retention by default ([TechCrunch](https://techcrunch.com/2026/08/20/ramp-launches-its-own-ai-model-router-called-router/)).

**Who, and price.** PMs, CX, sales, marketing, FP&A. Early access, onboarded personally; no published price.

**The savings claim is weaker than the coverage.** The headline "30% off" refers to *Router*: Ramp's own bill after it moved to Router, with no baseline or method given. The $1.5M token week was the motivation, not a result ([Implicator](https://www.implicator.ai/ramp-glass-ai-workspace-token-spending/), [Crypto Briefing](https://cryptobriefing.com/ramp-launches-glass-ai-workspace/)). The real asset is **distribution**: 85,000 customers, $60B valuation, >$2B run-rate, plus "AI Token Spend Management" (spend by provider, model, user, team, key) ([TechCrunch](https://techcrunch.com/?p=3178053)). The buyer is the **CFO**.

**Glass is Lightbulb's nearest direct competitor**; Beacon is adjacent.

## 3. How these map to Lightbulb

| Benefit | What we have | Gap |
|---|---|---|
| Spend visibility | Per-generation cost from `openrouter-settlement`; Metronome ledger; `run.ended` usage; per-owner caps and reservations at the gate | We see only inference routed **through our gate**. An enterprise's Claude Code, Cursor and Codex spend runs on its own contracts, and agent-network messages carry no token cost. |
| Agent inventory | The agent network knows every joined agent, its owner, label and `lbk_` key | Covers only agents that opted in. Beacon finds the agents that did *not* opt in. |
| Policy | The hold, keyed per person; one switch for Lightbulb agents; a 2-hop loop cap | Owner-scoped and binary. Enterprises want org-wide rules (by group, by domain, by tool) set by an admin rather than each session owner. |
| Audit | Immutable, `seq`-ordered messaging table; `admin_actions` rows | `admin_actions` covers only **platform** admins. There is no customer-facing audit view and no export or SIEM stream. |
| "Learn once" | Hermes skills, marketplace, session sharing | Skills belong to each agent's profile. Nothing turns a solved session into an org-wide skill. |

**Where the "easy to adopt" intuition is right.** Spend by agent, person and team is a query over settled data. An inventory page and an export of messages and hold decisions are small extensions of rows that already exist. The hub and hold are the right governance skeleton.

**Where it is wrong:**
1. **"Save big companies money" assumes we sit on their spend. We don't.** Ramp sits on the card and the router, and Beacon on the endpoint. To save a large company money we must route its tokens or ingest its telemetry.
2. **Our inference path is a hard sell to enterprises.** Managed inference is OpenRouter, a second processor between the customer and the model provider, and Bedrock is gone (#3051). Enterprises run committed Azure OpenAI, Bedrock or Anthropic contracts and will want to use them.
3. **The trust stack is missing, and it costs months, not features.** `apps/frontend/src/app/security/page.tsx` says we hold no SOC 2. There is no SAML or SCIM code in the repo. There is one region (us-east-1), no customer audit export, and the only billing path is Stripe self-serve, with no invoicing, MSA or security-questionnaire process. Startups underrate SCIM deprovisioning and the SOC 2 Type II observation window.
4. **Ramp's distribution beats any copied feature.** Selling "shared skills plus a cost meter" to a CFO against Ramp loses.

## 4. Partner, integrate, compete or ignore

- **Beacon: integrate, don't build.** Endpoint telemetry is already free OSS. Emit our audit stream in (or mappable to) Beacon's schema; accept its JSONL as a usage source. Risk: its memory layer drifts into our cross-agent lane.
- **Glass: differentiate, don't mirror.** Glass is one company's people and their AI inside Ramp's walls. Lightbulb's distinct claim is the **cross-vendor network**: anyone's Claude Code, Codex, Cursor or Grok session talking to anyone else's, with consent and loop limits. Integrate at the edges: Ramp Router as a provider choice; spend exported for Ramp's spend tool.

## 5. First moves (value against effort)

1. **Agent spend ledger with budgets** (high value, low effort). Spend per agent, person, team and room from settlement, Metronome and `run.ended` usage, with budgets that reuse the gate's reservation. Ask agent-network sessions to report tokens too (Claude Code and Codex expose them). *Saves money:* exposes runaway loops, idle agents and wrong model tiers. *Proof:* run a pilot for 30 days, set caps, and compare spend per completed task before and after.
2. **Org admin console: inventory, policy and audit export** (high value, medium effort). Every connected agent and `lbk_` key with owner, last seen, revoke; org-wide allow rules by group/domain over the per-person hold; messages and hold decisions streamed as JSONL/OTel to the customer's S3 or SIEM. *Saves money:* revokes orphan keys and duplicate agents; shortens security review. *Proof:* count the keys and agents revoked, and time from questionnaire to signature.
3. **Bring-your-own inference contract behind our gate** (high value, medium effort). Keep caps and metering; route to the customer's Azure, Bedrock, Anthropic or Ramp Router key so committed discounts apply. *Proof:* effective $/1M tokens against list price.
4. **Enterprise trust minimum** (required, high effort, mostly not code). SAML through Clerk enterprise connections, SCIM, SOC 2 Type I and then Type II through a compliance platform, a DPA, and invoice billing. A gate to selling, not a value proposition; start the SOC 2 clock now.
5. **Turn a solved session into a team skill** (medium value, high effort). This is the Glass and Beacon loop on top of session sharing. Defer until 1–3 show pull.

## Not verified

Glass pricing, architecture, admin controls, GA date, and any Glass-specific savings. Beacon's customers, revenue and founders, and whether the 29-runtime coverage holds per runtime. Whether Clerk's plan supports SCIM for us. The "99.5% adoption" and "Inspect writes two-thirds of code" figures appear only in Crypto Briefing.
