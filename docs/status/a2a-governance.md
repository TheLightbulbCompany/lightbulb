# A2A governance work: status

Updated by the governance session at every task completion. Newest first. Everything here is a draft PR until the founder merges it.

## 2 Nov 2026, evening

**Founder decisions today:** all four designs approved with the author's defaults; scope narrowed to phases 1b and 2; phases 3 and 4 wait as drafts.

| Phase | What | Where | State |
|---|---|---|---|
| 1 | Hosted A2A door, authority object, rules, holds, records, Friday's ask_session, replay | monorepo PR #4562, branch `feat/a2a-authority-design` | Built. 81 tests pass. Whole-branch review clean. Waiting on the founder's merge. |
| 1b | OAuth clients, Approve/Deny card in the DM, `when` clauses, re-decide on rules change, signed Agent Card | monorepo PR #4571, branch `feat/a2a-phase1b` (stacked on #4562) | Built. 115 unit + 2 conformance tests pass, 15/15 replay. Six task reviews, one whole-branch review (no bypass found; one injection fix in the hold DM), one fix wave, re-review clean. Waiting on the founder's merge of #4562 then #4571. |
| 2 | The CLI network: signed envelopes with the A2A body, rules on every laptop, real holds, cards, Workers rows as rules, hosted sends via the cloud member | CLI PR #134, branch `feat/a2a-phase2` | Planned: 9 CLI tasks (plan on feat/a2a-phase2) + 3 monorepo tasks (PR #4591, feat/a2a-phase2-bridge on #4571). Building: CLI Tasks 1–2 of 9 done (signed envelopes with an A2A body; the authority verifier and envelope binding, matched to phase 1 check for check; reviews clean), Task 3 (rules module) in progress; Task 9 (Workers rows) waits for the Workers session or the founder. Needs an engine release and a Hermes roll from the founder. |
| 3 | Spend mandates on Link | monorepo PR #4572 | Draft only. Waits. |
| 4 | Adapters (GitHub, Slack, Copilot Studio, Notion), publishing | monorepo PR #4573 | Draft only. Waits. |

**Founder-only steps outstanding:** merge #4562 (publishes the extension page); Hermes roll after it; Clerk Dashboard scopes `lightbulb:a2a:send` and `lightbulb:a2a:read` on dev and prod; one manual CIMD token test on dev to confirm Clerk issues the `/a2a` audience (else set `A2A_ACCEPT_MCP_AUDIENCE`); when phase 2 lands, fill the engine's issuer key list and cut the release.

**Not verified live:** Hermes turn-context `messageId` (needs the roll); delivery through a real computer link.

**Other sessions, as far as this session can see:** Apps session (provisioning approval cards, #4556 merged); Workers session (testing network workers; phase 2 will bring it the assign-envelope format change before touching `src/workers.rs`); a Codex session holds milestones since the orchestrator ended; a second Codex session (super-agent runtime, from the DeepSeek comparison) is designing only, holds the phase-2 assign slot and the tool bodies fixed, and has the phase docs and contracts from this session. The business-plan session ended.

**Open from the founder:** a room for everyone's milestones; whether to add an `observe` rule effect (from the DeepSeek design) to phase 1b.

**Rulings worth knowing:** a person requesting a resource they hold is allowed, not rejected as self; the held-task answer path in phase 1 is the REST route, the DM card arrives in 1b; the origin message id comes only from the runtime's turn context; reply linking requires the dispatched message id.
