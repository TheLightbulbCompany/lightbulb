# Agent Beacon (Asymptote Labs): the 27 October 2026 pass

Refreshes `docs/research/2026-10-21-agent-beacon-reference.md` (the 21 Oct baseline, still accurate unless stated here). Sources: the repo cloned at `90b19ae` (27 Oct), its `docs/`, beacon.sh, asymptotelabs.ai, GitHub API, Hacker News (Algolia), press. Markers: **[code]** read in source, **[docs]** in-repo docs, **[site]** live page, **[press]**, **[unverified]**.

## 0. What changed since 21 Oct

| Item | 21 Oct | 27 Oct | Source |
|---|---|---|---|
| Stars / forks | 2,347 / 211 | 2,872 / 259 (+525 stars in six days) | GitHub API 27 Oct |
| Repo description | telemetry wording | "The cross-harness, self-improving memory layer for AI agents"; topics add `memory`, `knowledge-base` | GitHub API |
| README H1 | mixed | "Self‑improving memory for coding agents"; the loop diagram Run → Capture → Evaluate → Extract → Review + approve → Reuse | README.md **[code]** |
| Releases | v1.3.45 (20 Oct) | v1.3.46 (22 Oct) plus the first separate browser-extension release `ext-v0.1.0` (22 Oct, unsigned zip, retention defaults to full) | releases API |
| Commits | 962 in 30 days | the API's first 100 results are all dated 26–27 Oct: about sixty detection-rule matching fixes in one day, Harness² (google-research) added as a harness that reads benchmark rollouts, Cursor/Codex "neutral hook extension points", redaction fixes (Google OAuth tokens, URL passwords) | commits API |
| v1.3.46 memory fixes | | regenerating a candidate keeps its review; concurrent approve/reject serialized; evaluations sample the tail of sessions over 2,000 events; candidate/evaluation `--query` paging fixed | `docs/changelog/cli.mdx` **[docs]** |
| Funding | "$5.5M" banner | $5.5M seed announced 20 Oct, led by Tower Research Ventures with Lytical, 1984 Ventures, South Park Commons; Tower's own security team is testing it; no customers named | TNW 20 Oct, SecurityWeek 21 Oct **[press]** |
| Press framing | | Press calls it runtime security ("blocks risky actions"); the launch posts and the site call it memory | TNW, SecurityWeek, X posts 20 Oct |
| Hacker News | May Show HN, 21 points | 21 Oct "Open-source, cross-harness memory for AI agents": 18 points, 14 comments | hn.algolia 50182883 |
| Site | | Homepage: three steps "Capture the full story · Dream it into a skill · Reuse it everywhere"; a wall of X posts dated 20 Oct; "Loved by thousands of developers" with no number; `/enterprise` and the "Announcing Beacon" post 404 | beacon.sh **[site]** |
| Public adoption surfaces | | Leaderboard ("Agent token leaderboard") shows 9 public profiles; ranks 3–5 are staff; #1 is 385.6B lifetime tokens. Discover shows 3 public traces, all by the CEO, 0 downloads each | beacon.sh/leaderboard, /discover **[site]** |
| asymptotelabs.ai | | still "Runtime Infrastructure for AI Agents. Observe, govern, optimize."; no SOC 2 claim on the page (the in-repo blog claims SOC 2 Type II) | **[site]**, `docs/blog/` |
| Third-party check | | eraofminds (21 Oct): "self-improving" overstates the free CLI, memory is curated by hand; installer preselects Cloud and backfills 30 days | **[press]** |

## 1. How it works end to end

**Capture.** `beacon endpoint install` (TTY wizard: sign in, Beacon Cloud preselected, Local opt-out, Standard or Metadata-only privacy, MCP and Skills preselected). Hooks, plugins, loopback OTLP or session-store polling write one normalized event per line to `~/.beacon/endpoint/logs/runtime.jsonl`; a 30-day backfill of session stores runs after install and connect. 39 runtime docs; the README table says "20+". **[docs, code]**

**Evaluate (optional, local CLI only).** `beacon memory evaluations run [--dry-run] [--trace <id>] [--harness x] [--limit n] [--evaluator jev|subq]`. `BuildProjection` sends at most 80 events (40 head + 40 tail, a marker for the omitted middle), each text field cleaned to 1,200 characters, redacted (`evaluator.go:25-26,240-250`) **[code]**. The request goes to TypeSafe's "System One" API (`https://api.typesafe.ai/v1/systemone`, model `jev-latest`) or SubQ (`https://api.subq.ai/v1/systemone`, `subq-snap-preview`), question type `noul`. Three questions, verbatim from `evaluator.go:72-76`:
1. "Did the trace complete the user's engineering task successfully?" (`task_success`)
2. "Does the trace contain a correction or debugging pattern that future agents should reuse?" (`reusable_correction`)
3. "Is the reusable lesson supported by concrete events in the trace?" (`evidence_supported`)
The evaluator returns three probabilities and no text. Dry-run cost estimate defaults to $0.00035 per trace (`DefaultCostPerTrace`) **[code]**.

**Candidate.** A trace becomes a candidate when `task_success ≥ 0.50` (precondition) and the mean of the three ≥ 0.60 (`candidate.go:13,24`). The comment records why: 0.27/0.86/0.69 averaged 0.6067 and cleared the mean alone (#649) **[code]**. The candidate's `kind` is guessed from title words: convention/standard, gotcha/pitfall, workflow/process (`candidate.go:300-304`). A scored candidate's body is the placeholder "no lesson text was extracted". Alternatively `beacon memory candidates create --trace <id> ...` files a lesson a person or agent wrote after reading the trace; no evaluator call.

**Review.** `beacon memory candidates list|show|approve|reject|supersede`; approve supplies the reviewed text. v1.3.46 made reviews transactional **[docs]**. In Beacon Cloud (Pro): `beacon_memory_candidate_create` after the user confirms, then `beacon_memory_candidate_review`; "Beacon Cloud has no evaluator" **[docs]**. Cloud "Findings" of kind Skill (hourly LLM analysis of forwarded sessions, 50 lifetime free, unlimited Pro) draft a `SKILL.md` for "steps you walk an agent through more than once" (`docs/cloud/findings.mdx:18`); this is what the site calls "dreaming".

**Serve.** Local: `beacon mcp serve` (stdio or `127.0.0.1:8766`) with `search_memory`, `get_memory`, `get_memory_context` (limit forced to ≤5, `server.go:367`). Matching is lowercase substring AND over whitespace-split terms against the joined fields (`store.go:767-773`); no ranking, no embeddings; `get_memory_context` is the same list filter **[code]**. Skills: `beacon memory skills preview|install <candidate>` writes `.agents/skills/<slug>/SKILL.md` with provenance in frontmatter. Cloud: `beacon mcp connect` registers `beacon-managed` (Streamable HTTP + OAuth, or `bcn_mcp_` read tokens) with seven memory tools (Pro) **[docs]**.

**Workflow skills** (`agent-skills/skills/`): `beacon-memory-recall` (recall at most once per task, 2–3 keywords, "terms are ANDed everywhere"), `beacon-memory-distill` (pick up to 10 traces, score only with consent, draft the lesson yourself, user confirms, "never approve a candidate with its placeholder body"), `beacon-memory-promote` (install only workflow/convention/recurring debugging_pattern; one-off gotchas stay in memory), plus `beacon-session-handoff`, `beacon-trace-share`, `beacon-lens-create` **[code]**.

**Scope.** Memory is per project (resolved repo; worktree → main checkout). Cloud memory is per account ("You see only your own history"). Team memory: the founder on HN (21 Oct) says the open-source tool targets individuals and "the enterprise product provides shared memory and findings across a company", an "engineering brain"; no doc, page or code describes it **[unverified]**. Local and Cloud stores never sync.

## 2. Tech stack

| Layer | What | Where |
|---|---|---|
| CLI, hooks, collector | Go 1.27 (toolchain 1.27.3); cobra 1.8.1; bubbletea 1.3.10; `modernc.org/sqlite` 1.59.0; cel-go 0.30 (indirect) | `cli/beacon/go.mod` **[code]** |
| Event schema | `beacon` / `endpoint-agent` / `schema_version 1.0`, OTel GenAI attributes under `gen_ai.*`, deterministic `event.id`, `collection_method` and `fidelity` on every event | `pkg/asymptoteobserve`, `docs/telemetry-schema/event-schema.mdx` |
| Collector | custom OpenTelemetry Collector distro with `beaconjson` exporter; loopback 4317/4318/13133 | `collector-builder/` |
| Memory store | `memory.db` SQLite, tables `evaluations`, `candidates`, `memories`; record = kind, title, body, applicability, tags, evidence event ids, provenance (`beacon.learning.v1`) | `cli/beacon/internal/learning/store.go`, `pkg/asymptoteobserve/learning.go` |
| Evaluator | HTTP POST to TypeSafe Jev or SubQ "System One"; probabilities only | `cli/beacon/internal/learning/evaluator.go` |
| Local MCP | hand-rolled JSON-RPC, read-only, 7 tools | `cli/beacon/internal/mcpserver/server.go` |
| Cloud MCP | `https://mcp.beacon.sh`, Streamable HTTP, OAuth (RFC 9728) or `bcn_mcp_` tokens | `docs/deployment/managed-mcp.mdx` |
| Forwarding | Vector 0.56.0 tail of `runtime.jsonl` → `/v1/ingest/*` gzip NDJSON, per-device key; Metadata-only mode is a VRL transform on the device | `packaging/vector`, baseline §7 |
| Plugins / extension | TypeScript (bun) one-file plugins for 7 runtimes; MV3 browser extension posting OTLP to 127.0.0.1:4318 | `plugins/`, `browser-extension/` |
| Detection | YAML + CEL rules (RE2, cost cap 5M), 77-rule corpus plus 6 embedded | `rules/`, `pkg/asymptoteobserve/threatrules` |
| Cloud backend (closed) | `beacon-ingest`, dashboard, Findings analysis "in an isolated sandbox per account", ClickHouse ClickPipes readers for customer buckets | referenced only; `terraform/*-asymptote-reader` |
| Enterprise (closed) | signed enforcement package downloaded from the dashboard; OSS ships a fail-open 2 s policy seam | `cli/beacon/cmd/endpoint_enforcement.go` |
| Test harness | `beacon-sandbox`: Modal sandbox runs a real Claude Code session and checks capture with markers; "~3 min" per scenario, "~30 min" for all, "a few cents" each | `beacon-sandbox/README.md` |
| Authorship | about 74% of commits name an AI agent (baseline §10) | git log |

## 3. Claims and benchmarks

| Claim | Where | Metric | Method | n | Reproducible? |
|---|---|---|---|---|---|
| "Self‑improving memory for coding agents" | README H1, beacon.sh | none | none | — | No. The free CLI is review-gated; "Nothing in this workflow runs from hooks" (`docs/concepts/cross-harness-memory.mdx`). eraofminds: "improves when you review it" |
| "so every agent starts smarter than the last" | beacon.sh sub | none | none | — | No |
| "A problem solved by one agent shouldn't need to be learned from scratch by another" | README | none | none | — | Slogan |
| "20+ harnesses" / README table 29 local runtimes | README, site | count | read the table | — | Yes; 39 files in `docs/runtimes/` incl. index pages |
| "Loved by thousands of developers" | beacon.sh | users | none | — | No public count. Leaderboard: 9 profiles; Discover: 3 traces; stars 2,872 |
| "#1 trending on X" | founder, HN 21 Oct | rank | none | — | No |
| Stars "grew more than 460% in September" | TNW 20 Oct | growth | no base given | — | Partly: 2,642 commits and 29 releases in Sep (baseline) |
| Candidate thresholds 0.50 / 0.60 | `candidate.go` | cut-offs | constants, one anecdote (#649) | 1 | They are constants; no precision/recall data published |
| Evaluator cost $0.00035 per trace | `evaluator.go:24` | USD | default estimate | — | Estimate only |
| Jev "making it dramatically cheaper to evaluate what actually happened inside an agent run" | beacon.sh post | cost | none | — | No |
| Hook latency "about 250 ms" reduced | changelog v1.3.42 | ms | not stated | — | Only performance number found; method absent |
| SOC 2 Type II | `docs/blog/asymptote-achieves-soc-2-type-ii-compliance.mdx` | — | — | — | Not shown on asymptotelabs.ai; **[unverified]** |
| Harness² benchmarks (JobBench, LAB, WorkBuddy-Bench) | `docs/runtimes/harness2.mdx` | — | Beacon only ingests Google Research's rollouts as telemetry | — | Beacon publishes no results on them |

**Plain statement:** there is no benchmark anywhere public of what Beacon's memory does to task success, tokens, time or repeated corrections. No eval harness or dataset for memory exists in the repo. The one instrument they publish, `beacon-sandbox`, measures capture fidelity (did the log record what the agent did), not memory. The "thousands" and "self-improving" claims are unsupported by any number they show.

## 4. Weak points and gaps

1. **No team memory.** Per project locally, per account in Cloud; founders say shared memory is an enterprise feature; nothing documents it. The one HN comment asking for it ("debugging issues that another engineer's agent already figured out") got the enterprise answer.
2. **Retrieval is substring AND, capped at five, with no ranking.** A sentence returns nothing (their own skill warns so). "Context for the task" is a list filter. Nothing learns which memories helped.
3. **The evaluator reads nothing and writes nothing.** Three probabilities from a third party (TypeSafe or SubQ) over a redacted 80-event, 1,200-char-per-field projection; a person or the agent writes the lesson. Cloud has no evaluator; Cloud "dreaming" is an hourly LLM finding. The thresholds are two constants with no validation.
4. **Memory is a store beside the record, not the record.** Local and Cloud are separate and unsynced; `memory.db` is durable while the log rotates at 60 MiB.
5. **Privacy defaults favor the cloud.** Cloud preselected in the wizard, 30-day backfill on connect, Standard mode ships every redacted line, data used "for research and AI training" unless opted out, extension retention `full` by default, HEC forwarding only redacted since v1.3.46, system log 0666 (#957 open), DNS-rebinding on the OTLP receiver (#911 open).
6. **Blocking is sold, not shipped.** The OSS seam fails open on timeout (2 s), error or malformed output; enforcement is a signed enterprise package for Apple Silicon system mode only.
7. **Breadth costs.** About sixty rule-matching fixes in a single day and 100+ commits in two days; 39 runtime docs to keep current; three-quarters of commits are agent-authored.
8. **Adoption evidence is thin.** Nine public profiles, three public traces by the CEO, no customers named, press and launch disagree on what the product is.
9. **No live agent-to-agent path.** Handoff writes a Markdown brief; cross-harness means "the next session can read a lesson", not "two agents talk".

## 5. What a better version would look like: targets Lightbulb could publish

Each is a number Beacon does not publish and cannot easily, because of its scope. Measure with founders excluded, the metric pre-registered in the repo, and the sample sizes stated.

| # | Target | Metric and honest measurement |
|---|---|---|
| T1 | Memory that crosses people | Share of agent tasks whose first retrieval returns at least one record written by a different person's agent in the same workspace. Count from events (recall events carry author id and requester id). Beacon's figure is 0 by construction (project and account scope). Report weekly per workspace with two or more people. |
| T2 | Fewer repeated questions | Fraction of prompts that re-ask something answered in the team's rooms or memory in the prior 30 days. Sample 200 prompts per month, two human raters plus one model judge with a published rubric, report agreement. Target: halve it within four weeks of the second person joining. |
| T3 | Retrieval quality | Precision@5 and recall@5 on a labeled set of (task, relevant record) pairs built from real rooms by the team that owns them. Publish the set and the scorer. Run Beacon's substring-AND matcher on the same set as the baseline. |
| T4 | Cross-vendor read and write | Distinct harnesses (Claude Code, Codex, Cursor, OpenCode, Grok CLI, Lightbulb agents) that both read and wrote the shared record in the week, per workspace. Beacon is read-only across harnesses and project-scoped. |
| T5 | Memory hygiene | Share of records with a person's confirm, share or delete action; median record age; deletion latency in seconds (AgentCore deletes are eventually consistent, about 22 s today, so state it). |
| T6 | What leaves the machine | Bytes of plaintext leaving a laptop per session on the network (relay sees ciphertext only) and per workspace message (only what a person or agent posts). Publish the four-step table and a reproducible capture (tcpdump on a fresh Mac). Beacon Standard mode forwards every line after connect. |
| T7 | Time to first useful context | Seconds from a session's first prompt to its first message citing a prior room, session or memory; median per workspace. Beacon's skill recalls at most once per task and only on request. |
| T8 | Review gate without a third party | Share of memory writes that passed through an approval in the room (a person said yes) versus silently written; no external evaluator in the loop; zero egress to a scoring API. Beacon sends a redacted projection to TypeSafe or SubQ. |
| T9 | Effect on work, not on memory | Handoffs completed per person per week before and after memory is on, by cohort of workspaces joining in the same week; same for approvals waiting time. Report the confidence interval, not a point. |

## 6. Sources

Repo and docs, cloned 27 Oct 2026 at `90b19ae`: github.com/Asymptote-Labs/agent-beacon (README.md; `cli/beacon/internal/learning/{evaluator,candidate,store}.go`; `cli/beacon/internal/mcpserver/server.go`; `cli/beacon/go.mod`; `agent-skills/skills/beacon-memory-{recall,distill,promote}/SKILL.md`; `docs/concepts/cross-harness-memory.mdx`; `docs/cli/memory.mdx`; `docs/deployment/managed-mcp.mdx`; `docs/cloud/findings.mdx`; `docs/changelog/cli.mdx`; `docs/runtimes/harness2.mdx`; `beacon-sandbox/README.md`; `docs/blog/`). GitHub API (repos, releases, commits), 27 Oct 2026.
Live pages, 27 Oct 2026: beacon.sh, beacon.sh/pricing, beacon.sh/discover, beacon.sh/leaderboard, asymptotelabs.ai; beacon.sh/enterprise and beacon.sh/blog/announcing-beacon returned 404.
Hacker News via hn.algolia.com: items 50182883 (21 Oct 2026, 18 points, 14 comments) and 48183793 (18 May 2026, 21 points, 10 comments).
Press: thenextweb.com/news/asymptote-labs-5-5m-seed-ai-agent-security (20 Oct 2026); securityweek.com/asymptote-labs-lands-5-5-million-seed-for-ai-agent-runtime-security (21 Oct 2026); eraofminds.com/blog/beacon-memory-coding-agents (21 Oct 2026); helpnetsecurity.com (22 Jun 2026). X posts dated 20 Oct 2026 by @jqdsouza, @AIStackLabX, @TradedVC, @wallstengine (via search results; not fetched directly).
Baseline: `docs/research/2026-10-21-agent-beacon-reference.md` and `docs/research/2026-10-21-enterprise-agent-beacon-ramp-glass.md`.
