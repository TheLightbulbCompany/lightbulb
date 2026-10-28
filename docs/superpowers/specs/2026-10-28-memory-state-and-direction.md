# Memory: what is built, what it meets, where it goes (28 October 2026)

Status: written for the founder after the first two memory PRs. Spec of record: `2026-10-27-memory-layer-design.md` (v5). This note does not change it; a v6 amendment follows the decisions in §3.

## 1. Built and on `main` of the CLI (`cli` 5b2e2c5 + #85; v0.6.6, v0.7.0 not yet released)

| Area | What ships | Verified by |
|---|---|---|
| Memory log | Signed entries per author: subject, fact, kind (fact / decision / lesson / preference), scope `me` or `team`, `replaces` for history, `forget` as a signed tombstone, two different current values on one subject shown as contested, never settled. Secrets refused by seven patterns (credential labels, private keys, AWS, `sk-`, vendor keys, URL credentials, JWT) | 12 store tests; two-node run |
| Sync | Over the existing peer links, gated by a `memory` capability in `hello`: an older build keeps messaging. `team` entries reach every member; `me` entries reach only the same person's signed-in computers | two-node run (team, forget while offline, share); `me` cross-computer sync unit-tested only (needs sign-in) |
| Agent tools | `remember` (shows the existing facts on the subject first) and `recall` over MCP for Claude Code, Codex, Cursor, Grok CLI, OpenCode. Recall is SQLite FTS5 with stemming and prefix terms, BM25, page: contested first, then current with history | two-node run (paraphrased questions, control) |
| Session start | A block of about 400 tokens for the repository's git origin, Claude Code and Codex only; `lightbulb memory last` shows it; `forget` clears it | two-node run |
| `lightbulb memory` | list, show, forget, share, export, last, team on/off, drafts, approve, dismiss. forget / share / team / upload refused from an agent's shell | two-node run |
| Capture | Sessions and tool calls kept locally: tool, 200-character summary with secrets refused, duration, success; tokens for Claude Code from its transcript at session end. The hook never waits more than 10 ms. `lightbulb sessions list / show / forget / export / upload off|metadata|full` | capture tests; two-node run |
| Upload and drafts | Nothing leaves unless signed in and `upload` is set; drafts from the evaluator listed, approved (`--team`) or dismissed from the CLI | unit tests; not yet run against a deployed backend |
| Setup | Suggests the free sign-in; installs the `lightbulb` skill | setup tests |

Not done on the CLI: the v0.7.0 release and the two-Mac CI acceptance (founder; needs an invite secret).

## 2. What that meets, against the spec's free column (§2 of the spec)

Met: personal memory across every harness on this computer; team memory with the people on the network with no server; keyword search; `lightbulb memory`; capture on this computer with nothing leaving; upload as a choice at sign-in; the evaluator's drafts reachable from the CLI. Built but not yet exercised end to end: `me` sync between a person's own computers (needs two signed-in computers), upload to a deployed backend, drafts from a real evaluator.

Workspace (monorepo PR #4459, review clean, not merged): AgentCore extraction with our instructions and metadata; sibling namespaces for facts, turns and the mirrored log; five-stage recall (expand, retrieve, fuse, rerank, compose); sync in both directions with idempotent mirrors; routes, hosted MCP and the room plugin; the evaluator on an episodic memory of its own; approve into memory; wipes with retry; session upload, timeline, share; the Memory tab; the security page. Reviewed by one Opus whole-branch review plus two fix rounds, and two Codex passes plus two fix rounds; 12,633 backend tests. Not done: dev deploy, runtime image roll, the one-time import, smoke on dev.

Not measured anywhere: no benchmark has been run on either engine.

## 3. Direction (founder, 28 October 2026)

1. The free engine gets good, at $0 to us and with nothing leaving the machine: recall by meaning on the laptop (a small embedding model beside the keyword index, fused), a local index of the session's own turns, and extraction by the harness itself through the end-of-turn hook that already makes the agent continue. A small local model (a runtime the person already has, such as Ollama) is the fallback for background extraction and for harnesses whose hook cannot prompt the agent.
2. BEAM is the target for both tiers, measured by one runner with two backends; the spend (about $250 for the programme plus the ingest runs) is approved.
3. The paid tier sells what needs a server: memory that is up when laptops are closed, extraction from room conversations, consolidation across the team, the evaluator over uploaded sessions, the Memory tab, usage and timeline, admin wipes.
4. Functionality at par or better than Beacon, past.dev and Reason Network; the matrix is `docs/research/2026-10-28-memory-parity.md`.

Open: whether local recording of the person's own prompts and replies is on by default or opt-in. Then: v6 spec amendment, plan, CLI recall v2, the runner, the runs, publish.

Order of work: merge #4459 → dev deploy, runtime roll, import, smoke → v6 amendment → plan → build → benchmark → publish.
