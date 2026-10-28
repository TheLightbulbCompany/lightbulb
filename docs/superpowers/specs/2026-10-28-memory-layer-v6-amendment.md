# Memory layer: v6 amendment (28 October 2026)

Amends `2026-10-27-memory-layer-design.md` (v5). Everything in v5 stands unless this file says otherwise. Facts about what is built come from `docs/research/2026-10-28-inventory-cli-code.md` and `-workspace-code.md`; the comparison from `docs/research/2026-10-28-memory-parity.md`.

## 1. What changes and why

v5 drew the free/paid line at search: keyword on the free network, meaning in the workspace. The founder's decisions today move the line to extraction and always-on:

1. The free engine gets good at $0 to us, with nothing leaving the machine.
2. BEAM is the target for both tiers, measured by one runner with two backends; the spend is approved.
3. The paid tier sells what needs a server: memory that is up when laptops are closed, extraction from room conversations, consolidation across the team, the evaluator, the Memory tab, usage, admin wipes.
4. Functionality at par or better than Beacon, past.dev and Reason Network.

Nothing old in dev or prod matters: ship and fix forward (founder).

## 2. Free engine v2 (the CLI)

### 2.1 Recall by meaning, on the laptop
- A small embedding model downloaded once at `lightbulb setup` into the state dir (int8 ONNX, about 25–35 MB; `setup --no-model` keeps keyword-only). Builder's call on the model: the smallest of `bge-small-en-v1.5` / `all-MiniLM-L6-v2` that embeds an entry in under 20 ms on a 2020 laptop CPU.
- Vectors in `state.db` beside the entry (one table, 384-float blob). Search is a linear cosine scan in Rust. `ponytail:` linear scan, a vector extension when a team passes ~50,000 entries.
- Fusion: reciprocal rank fusion (k=60) of the FTS5 bm25 ranks and the cosine ranks, per arm. Page composition, `recall`'s API and the start block are unchanged.
- The five inventory fixes ride along: walk up to the repo root for the start block; repo key `owner/name`; fill the block line by line instead of dropping whole sections; contested within a scope; the secret check on entries accepted from peers.

### 2.2 The session's own turns, indexed locally
- Capture adds the turns: the person's prompt (UserPromptSubmit) and the agent's reply (Stop; Claude Code's transcript gives both) — `turns(session, seq, role, text ≤ 4 KB, at)` with FTS5 and vectors. Secrets per turn: a matching turn is stored as `[refused: <name>]`.
- Recall gets a third arm, turns, returning excerpts, fused like the others; the start block does not include turns.
- **Default on, local only, never uploaded** (builder's call, reversible). `lightbulb sessions turns off` stops recording; hidden folders skip as today; turns are kept 90 days (`[capture] turns_days`). `full` upload keeps sending tool summaries only.
- Docs and the security page change one line: capture records your prompts and replies on your computer only; they are never uploaded.

### 2.3 Extraction by the harness
- The end-of-turn hook already makes an agent continue (Claude Code, Codex, Cursor: Stop `decision: block` with a reason; OpenCode: a new prompt; Grok: its stop hook). Once per session, when a turn ends after at least one tool call and nothing was remembered yet, the hook answers with a reason that asks the agent to call `remember` for the facts, decisions and lessons of the session, with `replaces` where a fact changed, then stop. One nudge per session (a flag on the session row); never when the session has unread messages waiting (that path keeps priority); composable with any other Stop decision (one JSON answer; a guard that must block wins, the nudge waits).
- Measure: anonymous count of sessions that wrote at least one entry, in the existing usage counts.
- Fallback, v2.1: `lightbulb memory extractor ollama:<model>` runs over unextracted turns in the background through a runtime the person already has; same `remember` path. Not built until the nudge's numbers say so.

### 2.4 Start block for every harness
Cursor, Grok CLI and OpenCode get the block through their session-start hook's context field where the harness supports one (verify per harness from its own docs and a captured payload, as the 23 Oct research did); where none exists, unchanged.

## 3. The workspace

As merged in #4459 plus the follow-up PR (tab lists the team's log entries; plugin refuses instead of scrubbing; mirrored records link back to their entry; owner wipe tombstones the workspace's entries on laptops; credential-shaped values refused before queueing). Consolidation across the team, extraction from rooms, the evaluator and always-on stay paid. Dev deploy on merge, prod by the founder's dispatch; no import step (nothing old matters).

## 4. Measurement

One runner, forked from past.dev's MIT BEAM runner, with two backends:
- **free**: a note-taking model (Haiku for loops) plays the harness over each conversation, calling `remember` with `replaces` through the daemon's MCP; the raw turns go into the turns index directly; recall through the daemon. Measures "a frontier note-taker plus local hybrid recall".
- **workspace**: the backend's ingest and recall.
Published numbers use their answer model and judge (hash-checked prompts); tuning uses the free "gold on the page" signal and Haiku answers. Budget: v5 §9 plus about $10–30 per 10M ingest through the note-taker.

## 5. Order of work

1. Follow-up PR (§3). 2. Plan `2026-10-28-memory-free-engine-v2.md`, built by subagents. 3. Runner plan, built after v2 lands. 4. Runs: 100K free and workspace first; publish whatever they show. 5. v2.1 fallback only if the nudge's numbers ask for it.

## 6. Calls I made (check these)

- Turns recorded by default, local only, never uploaded, 90 days.
- Linear cosine scan, no vector extension yet.
- One nudge per session, after a tool call, skipped when messages wait.
- No bundled local model; Ollama-style runtime as a later fallback.
- `as_of` recall on the CLI deferred (past.dev has it; the log has `when`, so it is a filter away).
