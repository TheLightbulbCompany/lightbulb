# Memory scopes: private and team (design)

Status: phase 1 implemented (feat/memory-scopes); phases 2-4 open, so this doc stays. Owner
defaults taken on the open questions: private recall only in the 1:1 DM; a leaving member's
private scope is deleted at once; the boundary is soft (product, not security). Two corrections
from implementation: the owner wipe also lists records by the `namespace` prefix
`/owners/<owner>:user_`, because `ListActors` stops listing a member once their events expire
(7 days) while their records stay; and the private IAM conditions are separate Allow statements,
because two operators in one Condition block are ANDed. Owner-approved direction: split agent memory into a
private scope (one person with the agents) and a team scope (the whole workspace).
Pattern: Grok Bot. Facts stay private unless a person promotes them to the team.

## Today (verified in code, 2026-10-09)

- One `CfnMemory` "WorkspaceMemory" per env, shared by every tenant
  (`apps/infra/lib/stacks/container-stack.ts` ~226). `eventExpiryDuration: 7`. One
  `semanticMemoryStrategy` named `workspace_facts`, namespaces `["/owners/{actorId}/"]`.
- The runtime plugin (`apps/infra/hermes/plugins/memory/agentcore/__init__.py`) writes
  every turn with `CreateEvent(actorId=<owner>)` and recalls with
  `RetrieveMemoryRecords(namespace="/owners/<owner>/")`.
- Isolation between tenants is IAM: the per-owner inline policy `owner-memory`
  (`apps/backend/core/billing/bedrock_aip.py` `ensure_owner_memory_policy`) allows events
  only for `actorId == <owner>` and records only for `namespace == "/owners/<owner>/"`.
  The CDK permissions boundary (`container-stack.ts`, Sid `BoundaryAgentCoreMemoryUse`)
  is the ceiling.
- The wipe (`apps/backend/core/services/workspace_memory.py` `delete_owner_memory`)
  deletes records in `/owners/<owner>/` and events under `actorId == <owner>`.

The leak: a fact told to an agent in a private DM is extracted into the workspace
namespace. It is then recalled for every member, in every room.

A second, smaller leak: Hermes's built-in memory (`MEMORY.md` / `USER.md` in each agent
profile, `hermes_profiles.py` `memory.memory_enabled: True`) stays on beside the
AgentCore provider. The agent itself chooses what to save there, and it shows in every
room. Phase 1 only adds a prompt line for it (see below).

## 1. Scope rule (phase 1, no new UI)

Rule: **a turn in a 1:1 DM between one human and an agent is private to that human. Every
other turn is team.**

- Write: a DM turn whose speaker is a human (`user_` id) writes to that human's private
  scope. Channel, group DM, mail, Paperclip runs and agent-authored turns write to team.
- Recall in a DM with human H: team scope + H's private scope.
- Recall everywhere else: team scope only.

Why private recall is DM-only, not "everywhere for that speaker": in a channel the reply
is read by everyone in the room. If the agent recalls H's private facts because H spoke,
it can repeat them to the room. DM-only recall keeps a private fact inside the one room
where only H reads the answer.

Alternative weighed and rejected: "private by default everywhere, team only on promote".
It is the end state of the Grok pattern, but with no promotion UI it empties team memory
overnight. Shared channel knowledge is what makes the team useful. Phase 1 keeps today's
behaviour for shared rooms and closes only the DM leak.

What the plugin knows: Hermes passes `user_id` and `chat_type` to
`MemoryProvider.initialize` (`hermes-agent agent/agent_init.py` ~1264-1274). The bridge
already sets `user_id` = sender id and `chat_type = "dm" if room.kind == "dm" else "group"`
(`apps/infra/hermes/bridge/turn.py` ~3225). A `group-dm` maps to `group`, so it is team.
Adding a person to a DM turns it into a group DM in place (ISOA-548), so later turns
become team turns. Earlier DM facts stay private.

## 2. AgentCore mechanics

Options from the AgentCore docs:

- Namespace templates support `{actorId}`, `{sessionId}`, `{memoryStrategyId}` and up to
  5 custom keys (`namespaceKeys`), with values sent per event in
  `CreateEvent.extractionConfig.namespaceVariables`. If a strategy's custom key is missing
  from an event, that strategy does not extract from it. (Source: devguide "Specify
  long-term memory organization with namespaces".)
- `CreateEvent.extractionMode: SKIP` stores an event with no extraction. (API reference,
  CreateEvent.)
- `actorId` pattern: `[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*`, max
  255. A `:` separator is allowed. (API reference, CreateEvent.)

**Chosen: encode the scope in `actorId`. No change to the memory resource.**

- Team turn: `actorId = <owner>` (unchanged).
- Private turn: `actorId = <owner>:<userId>`, for example `org_abc:user_xyz`.
- The existing template `/owners/{actorId}/` then extracts a private turn into
  `/owners/org_abc:user_xyz/`, a separate namespace. Team records stay in
  `/owners/org_abc/`.
- Recall in a DM makes two `RetrieveMemoryRecords` calls (team, private), run in
  parallel, and merges them, private first. Each call keeps today's 1.5 s read timeout and
  fail-to-empty behaviour.

Why not a second strategy with a custom `{member}` key: the existing `workspace_facts`
strategy has no custom key, so it would still extract every DM event into team scope. To
stop that we would have to change its template. That moves all future team records to a
new path and splits them from the old ones. The `actorId` encoding needs no resource
change, which is the safest path for prod data.

Replace vs update (the critical question). The CloudFormation reference for
`AWS::BedrockAgentCore::Memory` lists `MemoryStrategies`, `NamespaceKeys`, `IndexedKeys`
and `EventExpiryDuration` as **"Update requires: No interruption"**, and `Name` and
`EncryptionKeyArn` as **"Update requires: Replacement"**. So a strategy edit should not
replace the resource. The docs do not say what happens to existing records when an
existing strategy's namespace changes. Phase 1 does not touch `memoryStrategies` at all,
so the question does not arise. Any later strategy change must be add-only (a new
strategy beside `workspace_facts`), proved on dev first, and must never touch `Name` or
`EncryptionKeyArn`.
Source: https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-memory.html

## 3. IAM

The `owner-memory` policy changes from exact match to "owner, or owner plus `:`":

- Events: `bedrock-agentcore:actorId` matches `<owner>` (StringEquals) or `<owner>:*`
  (StringLike).
- Records: `bedrock-agentcore:namespace` matches `/owners/<owner>/` or `/owners/<owner>:*/`.
- Keep the Deny on the unconditionable actions as it is.

Prefix safety: owner ids are Clerk org ids (`org_` + alphanumerics) and contain no `:`.
So `org_abc:*` cannot match a different owner such as `org_abcd`. Add an assert that an
owner id has no `:` beside the existing `/` and `..` checks in `owner_namespace`.

Unverified, and the first step of phase 1: the dev spike of 2026-09-22 proved StringEquals
on both keys. It did not test StringLike. The service-authorization page did not render
for this review. Test StringLike on dev (allow own, deny a different owner's `org_x:*`)
before anything else. If StringLike is not honoured, fall back to sending a custom
namespace key, which needs a strategy change and a new design pass.

Plain statement: **this scope is a product boundary between members, not a security
boundary between agents.** Every agent in a workspace runs in one runtime under one role.
The agent's shell can read that role's credentials (see the IMDS/MMDS credential note).
An agent that is told to, or is prompt-injected, can call `RetrieveMemoryRecords` on any
member's private namespace in its own workspace. IAM still keeps each workspace out of
every other workspace. Inside a workspace, the scope protects members from casual recall,
not from a hostile agent.

## 4. Existing data

Records already in `/owners/<owner>/` stay there and stay team scope. That includes facts
that came from DMs before this change. No backfill: the records carry no source room, so
a backfill would have to guess. Events expire in 7 days anyway. The later "Memories"
review list (phase 2) is how a person finds and deletes an old DM fact.

The wipe must also cover private scopes. `delete_owner_memory` lists actors
(`ListActors`, which the backend task role must be granted in `service-stack.ts`), keeps
those that start with `<owner>:`, and deletes their records and events the same way.
Open question 2 covers a member leaving the workspace.

## 5. Later phases (not phase 1)

- Phase 2: a "Memories" list in the agent panel. It shows "Only you" and "Team" records,
  each with delete. The backend reads and deletes (`ListMemoryRecords`,
  `BatchDeleteMemoryRecords`) because the runtime role is denied record delete on purpose.
  A member sees only their own private scope and team.
- Phase 3: human-ticked "Share with team". The backend copies a private record into the
  team namespace (`BatchCreateMemoryRecords`, backend-only) and deletes the private one.
  Agents never promote.
- Phase 4: evidence-cited synthesis. Team facts keep a link to the source message, and
  a person verifies a fact before it is shown as confirmed.

## 6. Phase-1 change list

1. Dev spike (no code): prove StringLike on `actorId` and `namespace` for CreateEvent and
   RetrieveMemoryRecords, with a cross-owner deny check.
2. `apps/infra/hermes/plugins/memory/agentcore/__init__.py`
   - `initialize`: read `user_id` and `chat_type`. Private when `chat_type == "dm"` and
     `user_id` starts with `user_`.
   - `sync_turn`: `actorId = f"{owner}:{user_id}"` when private, else owner.
   - `prefetch`: in a private turn, query both namespaces in parallel; merge, private first,
     de-duplicate, cap at `_MAX_RESULTS`.
   - `system_prompt_block`: in a DM, say that recall includes this person's private memory,
     and do not save what they say here with the memory tool unless they ask, because the
     memory tool is shown in every room.
3. `apps/backend/core/billing/bedrock_aip.py` `ensure_owner_memory_policy`: the two
   conditions in section 3. `roll_runtime_image` and `provision_hermes_runtime` already
   re-put it (`hermes_provisioning.py` ~894, ~1445), so the roll in step 6 applies it.
4. `apps/backend/core/services/workspace_memory.py`: `owner_namespace` refuses `:`;
   `delete_owner_memory` also wipes `<owner>:*` actors and their namespaces.
5. `apps/infra/lib/stacks/service-stack.ts`: add `bedrock-agentcore:ListActors` to the
   backend's memory grant. Run `npx cdk synth` and commit `deploy.yml` if it changes.
6. New image, pin flip, `roll_hermes_runtimes.py` (a pin flip alone changes nothing live).
   Deploy the backend (step 3) before the roll: the roll re-puts the policy before the new
   image runs. Under the old policy a private write is denied and the plugin drops it with a
   warning, so a wrong order loses DM memory, not data.

Test plan (CI is the gate; no local runs):

- `apps/infra/hermes/tests/test_memory_agentcore.py`: DM + human writes the private actor;
  channel, group DM, agent sender, missing `user_id` write the team actor; DM recall calls
  both namespaces and merges; channel recall calls team only; one failing call still
  returns the other.
- `apps/backend/tests/unit/services/test_workspace_memory.py`: wipe deletes `<owner>:*`
  actors and not `<owner>x:*`; `owner_namespace` refuses `:`.
- A policy-document test for `ensure_owner_memory_policy`: the exact conditions, and the
  Deny is unchanged.
- CDK test: backend memory grant includes `ListActors`.
- Dev acceptance, by an agent in Chrome with two test accounts (A, B) in one workspace:
  A tells the CEO a unique fact in a DM. After about 2 minutes (extraction lag), A asks in
  the DM and the agent recalls it. B asks the CEO in `#general` and in B's own DM, and the
  agent does not recall it. A says a different unique fact in `#general`, and B recalls it
  in B's DM. Then wipe the dev workspace and confirm both namespaces are empty
  (`list-memory-records`). Put screenshots and CLI output on the PR.

## Open questions for the owner

1. Private recall in a DM only (chosen), or for that person in every room? "Everywhere"
   is more helpful but lets an agent repeat a private fact in a channel.
2. When a member leaves the workspace, delete their private scope at once (proposed), or
   keep it for a grace period?
3. Is "product boundary, not security boundary" acceptable for phase 1? A hard boundary
   needs the memory calls to go through the backend instead of the runtime role.
