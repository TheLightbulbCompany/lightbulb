# Network roles: Reason, AgentRelay and Lightbulb, reconciled (2026-10-24)

Owner direction: our free network should not be stricter than Reason's. Roles (admins and so on) belong to the workspace, where the org already has them. Sources: Reason `DESIGN.md`, `src/team.rs`, `src/daemon/teamops.rs` (read, not copied); AgentRelay repo at `c3a92d9` plus agentrelay.com; our engine `src/roster.rs`. Clean-room: behaviour only.

## What each one does

| Power | Reason (free, peer to peer) | AgentRelay (central server) | Lightbulb today |
|---|---|---|---|
| Invite | Any member; one-time invites and a reusable team link | Hold the workspace key (`rk_live_…`); no invite links found | Any computer member; "open" links only founder or admin |
| Approve a newcomer | No: the invite is the gate | No human approval found | Allow prompt to the inviter's person (ask links) |
| Remove another member | Any member (not themselves); removal rotates the link | Not documented (key holder = admin) | Founder or admin only; the founder can never be removed |
| Rename, relay | Any member | n/a | Founder or admin |
| Roles | None. Founder = first entry and root of trust only | Two key types (workspace vs agent token), no human roles | Founder, admin, member; founder makes admins and can hand over |
| First message from a new sender | Per-machine hold policy | Not found ("knock first" claimed, no code) | Held until allowed (per person, all their agents) |
| Audit | Signed admissions only; no removal log | Local permission-audit file | Signed roster; holds record `by` |
| Their own safety statement | "This is for friends" | none | n/a |

Takeaways:
- Reason is flat on purpose and says so. AgentRelay has no human roles either: the key is the authority.
- Neither has an org-admin model. Ours is the only one with admins, and we built them before anyone needed them.
- Our first-message hold is stricter than both, and it is the piece that protects the paid product: a stranger cannot make Friday spend credits.

## Reconciled model

Two kinds of network, decided by who founded it:

1. **Free network (founded by a computer): flat, like Reason.** Every member may invite (any link mode), remove any other member (not themselves), rename and set the relay. The founder is only the first entry. When it leaves, nothing needs promoting. The first-message hold stays: it is per person, not a role.
2. **Workspace network (founded by the workspace's cloud member): the org's roles, from Clerk.** Members only (2026-10-24 ruling): only person lines, which the backend mints for a signed-in member. Removal follows Clerk: removing a member in Settings removes their computers through the cloud member, as today. Computers on a workspace network cannot invite, remove or rename; the workspace owns that. Admins are Clerk org admins; the engine needs no admin role of its own.

So the engine's role system shrinks to one rule: **on a cloud-founded network only the founder (the cloud member) holds team powers; on a computer-founded network every member does.**

## Implementation (ponytail)

Smallest change that gets there, in `cli` (engine v0.3.3, together with the PATH fix, the skill and agent-run approvals):

- `src/roster.rs`: replace `elevated = signer.role != Role::Member` with `elevated = cloud_founded ? signer is the founder : true`. Removal: drop "never the founder" on computer-founded networks; keep it on cloud-founded ones (computers cannot remove the workspace).
- `Link`: on a cloud-founded network accept only `person` links (members only). On a computer-founded network any member may mint any mode.
- Delete `Kind::Admin`, `Kind::Transfer`, the founder-leave promotion and their CLI commands (`admin`, `transfer`) and tests. No shim: replayed old entries of those kinds become no-ops. Nobody uses them.
- Agent-run `invite`, `join`, `approve`: remove the person-only (agent-ancestry) refusal for those ops; `hold.answer` records the calling session as `by`, shown in the dialog text and the receipt.
- Skill: one `SKILL.md` installed by setup per agent, about one page; MCP instructions point at it.
- Tests: flat-network invite/remove by a plain member; cloud network refuses a computer's ask/open link and a computer's removal; an agent's approve records its session.

Skipped: per-network admins, quorum, removal audit log, multi-network per computer. Add when a workspace asks for admins inside the network itself, or when the security pass (prompt injection, approvals) lands.

## Open, designed next

Hybrid networks: a workspace adopting a whole free network, paid members and free people together. That decision changes rule 2 above (a workspace network may then contain non-members), so it comes before any build that hardens rule 2 further.
