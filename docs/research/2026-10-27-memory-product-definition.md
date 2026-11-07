# Memory as a product: what we build to compete with past.dev and Beacon, alongside the network (27 October 2026)

Founder ruling, 27 Oct: build, do not buy. "I want to copy and become a competitor, I want to offer the same products alongside the network." This file defines the products, the surfaces, the positioning and the measurement. The architecture and the effort estimate follow in `2026-10-27-memory-building-blocks.md` (the open-source systems and benchmark harnesses we copy from, in progress). Facts about the competitors are in `2026-10-27-agent-beacon-pass.md` and `2026-10-27-past-dev.md`; what we have today is in `2026-10-27-network-memory-inventory.md`.

Copying means the product and the API shape, never code, prompts or names. Everything we ship is ours or Apache-2.0/MIT upstream with attribution.

## 1. The three products

| Product | Who buys | What it is | The competitor it answers |
|---|---|---|---|
| **A. Memory for your coding agents** (part of the network) | The developer on the network, then their team | Sessions captured on the laptop by the hooks we already install for Claude Code, Codex, Cursor, Grok CLI and OpenCode; lessons drafted at session end; recall at session start and on demand over the MCP tools the agent already has; memory follows the person across computers once signed in; shared with the team through the workspace | Beacon (capture, lessons, MCP, skills); past.dev's Claude Code and Codex plugins |
| **B. Team memory in the workspace** (the head) | The 3–15 person team | Every room, DM, handoff, approval and uploaded session becomes memory scoped by room; the Memory tab shows current and previous values with their sources; any member confirms, corrects or deletes; every agent the team admits recalls it, including laptops and hosted apps | Nobody ships this. Beacon says "enterprise"; past.dev is an API |
| **C. The memory API** (a developer product) | Developers building their own agents and apps | Ingest timestamped text with an audience; recall as an identity; dated sources; current versus previous values; as-of queries; delete erases derived memory; MCP server; SDKs for the AI SDK, LlamaIndex, CrewAI; self-hostable; the same engine that runs A and B | past.dev, Mem0, Zep, Hindsight, Honcho, Supermemory |

One engine, three doors. A and B are why the network and the workspace are better than anyone else's; C is a second revenue line and the public proof (benchmarks) that the engine is real.

## 2. The API, as a shape (copied from the category, with our additions)

Endpoints and semantics every competitor converges on, so developers can switch with a base-URL change:

- `POST /v1/ingest` and `/v1/ingest/batch`: data points `{id, content, occurredAt, audience, author?, metadata?}`; async; `GET /v1/ingest/{id}` reports `completed`; a project-level `settled`.
- `POST /v1/recall`: `{query, identity, queryTimestamp?, occurredFrom?, occurredTo?, limit?, maxTokens?}` → `{asOf, usedEvidenceTokens, results:[{content, currentValue?, previousValues?[], sources:[{id, date, excerpt}]}]}`.
- `PUT /v1/audiences/{slug}` (fixed list or rule over identity traits), `PUT /v1/identities/{id}` with traits, bulk variants.
- `POST /v1/data-points/delete` erases derived memory; replacing a data point under the same id replaces its memory.
- MCP: `recall_memory`, `ingest_memory`, `explain_ingestion`, `estimate_ingestion`, plus our `remember` (a lesson with kind and evidence) and `recall_context` (a budgeted brief for a task).

Our additions, which are the reasons to pick us:

1. **Audiences derived from the product, not declared by hand.** A room is an audience; a DM is an audience of two; a network is an audience of its members; membership changes propagate. The API customer can still declare audiences by hand.
2. **Lessons as a first-class record type** beside facts: kind (workflow, correction, gotcha, convention), evidence (event ids or message ids), provenance (agent, computer, repo, branch), review state (drafted, approved by whom, superseded by). Beacon has this locally; past.dev has facts only.
3. **The review gate is in the product.** Approval in a room, or an explicit API call; nothing becomes team memory silently.
4. **Local mode.** The same engine runs inside the CLI daemon on the laptop with SQLite, so the no-account tier has memory that never leaves the machine, and the API customer can self-host. past.dev is hosted only (own cloud at Enterprise); Beacon's local memory is a substring index.
5. **End to end on the network.** Memory that moves between a person's computers moves over the network's encrypted channel, not through a server that can read it; the cloud holds it only for signed-in accounts.
6. **Open source engine.** Recommendation, not yet ruled: the engine under Apache-2.0 like the CLI, hosted API as the paid service (Mem0's model). It is the trust story the network already tells, and it is how a fourteen-month-old company gets stars, contributors and third-party benchmark runs. Closed is past.dev's choice; open is Beacon's and Mem0's.

## 3. Where each tier gets what

| Tier | Memory |
|---|---|
| Network, no account | Capture on your disk; personal lessons and recall on this computer (local engine); nothing leaves |
| Cloud, free account | Memory follows you across computers; the hosted apps recall it; a monthly allowance of drafted lessons and recalls; the API with a starter allowance |
| Workspace ($50 for five, $20 a member after) | Team memory by room with review and provenance; unlimited drafting; every admitted agent recalls it |
| API (new line) | Usage-priced like the category: a credit per ingested unit and per recall, with a free tier that undercuts past.dev's; self-host free | 
| Enterprise, later | Own AWS account, SSO, retention controls, audit export |

## 4. Positioning, one line per competitor (internal; none named on the site)

- **vs past.dev:** same API, plus the room, the review, local mode and open source; they have the benchmark lead today, which §6 is for.
- **vs Beacon:** same capture and lessons for coding agents, plus team memory, agents that talk to each other, nothing shipped to a third party, no scorer, no cloud default.
- **vs Mem0 / Zep / Hindsight / Honcho:** they sell memory to developers; we sell memory that already knows who is in the room. The API is the door for their customers; the workspace is what they cannot copy without building Slack.

## 5. Measurement from day one

Two kinds of numbers, both published, both pre-registered in the repo before the first run:

1. **The category benchmarks, run by us on our engine, with the same discipline past.dev used:** BEAM at every split, DolphinBench, LongMemEval, LoCoMo on corrected keys; one answer model, pinned judge prompts, cost and latency in the table, failed requests counted, harness open on GitHub. The first run is the baseline before any tuning, and it will be low; it is published anyway. Target ladder: Mem0/Honcho level (BEAM 10M ≈ 45–50%) → Hindsight level (64%) → past.dev level (85%).
2. **The team numbers nobody else can publish** (memory decision §5): memory that crosses people, repeated-question rate, review without a third party, cross-vendor read and write, bytes of plaintext leaving the laptop, time to first useful context, effect on handoffs.

Cost is a benchmark too: credits per recall and per ingested KB at list price, next to past.dev's.

## 6. What waits for the building-blocks pass

The engine's architecture (temporal fact model, extraction, retrieval, reranking, budgets), which open-source systems to start from and what to rewrite, the effort by milestone, and the hard parts (contradiction resolution, event ordering, 10M-token splits, judge variance). That file names the stack; this one does not guess it.

## 7. Open for the founder

1. Open-source the engine (recommended) or keep it closed like past.dev.
2. The API as a public product from day one, or after the workspace memory ships; it changes the first milestone.
3. Name: "Lightbulb Memory" under the company, or its own name for the API audience.
4. API pricing: copy the credit model and undercut, or flat tiers.
