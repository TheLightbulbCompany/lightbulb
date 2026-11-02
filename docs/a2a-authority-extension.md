# A2A authority extension

Extension URI: `https://thelightbulb.company/a2a/ext/authority/v1`

This page describes what Lightbulb's A2A endpoint does today (phase 1). Source: `core/services/a2a_authority.py`, `a2a_rules.py`, `a2a_tasks.py` and `routers/a2a.py` in `apps/backend`.

## What it is

An A2A extension that attaches a signed statement of authority to every task: who asked, through which agents, for whom, what operation on which resource with which exact operands, and in the person's own words. The endpoint decides each request with a deterministic rule document (no model), and signs a record of every decision.

Receiving a message grants nothing. Only a verified authority object, passed through the rules, does. Message text is an input to the decision and never changes the rules.

## The extension

- **Declared** in each Agent Card under `capabilities.extensions` with `required: true`. The card also says `streaming: false` and offers two skills, `message` and `ask_session`.
- **Activated** by listing the URI in the message's `extensions` array. A message without it is rejected with `-32008`.
- **Carried** in `Message.metadata`, under the key equal to the URI, as an unsigned authority request:

```
{ "operation": { "skill": "message", "resource": "agent:<id>", "operands": { "text": "..." } },
  "intent": "<the person's words, up to 2048 characters>",
  "on_behalf_of": null }
```

For `message`, `operands` must equal `{"text": <the message's text parts joined by newlines>}`. The signed operands are exactly what the agent receives. `resource` must be `agent:<id>` of the card's agent.

## The authority object

The endpoint turns the request into this object and signs it. A client never supplies a signed object in phase 1.

| Field | Meaning |
|---|---|
| `v` | `1` |
| `id` | UUID, one per request; a second use is rejected as a replay |
| `iat`, `exp` | Unix seconds; `exp - iat` is at most 86400 |
| `requester` | `{person, org, verified_by}`: the person at the origin, their workspace, and how they were verified (`clerk-session` for the A2A door, `origin-message` or `agent-runtime` for a hosted agent's ask) |
| `chain` | Hops, origin first. Each hop is `{agent, session, person, at}`. The first hop's `person` equals `requester.person`. The backend writes the whole chain in one signature; there is no per-hop re-signing |
| `on_behalf_of` | `null` or the requester. Anyone else is invalid |
| `operation` | `{skill, resource, operands, operands_hash}`. `operands_hash` is `sha256:` plus the hex digest of the operands as JSON with sorted keys, no whitespace, UTF-8 |
| `intent` | The person's words, at most 2048 characters |
| `policy` | `null` in phase 1 |

**Signing.** A compact JWS, EdDSA, header `typ` `lightbulb-a2a-authority`, signed with the same issuer key that signs Lightbulb sign-in proofs. Public keys are at `GET /a2a/keys.json`. **Who mints.** Only the Lightbulb backend, from the authenticated caller. `requester` and `chain[0]` come from the verified identity, never from the request.

## Verification

Checked in this order; the first failure names the check:

1. `signature`: valid EdDSA signature by the issuer key, matching `kid`
2. `typ`: `lightbulb-a2a-authority` for an object, `lightbulb-a2a-record` for a record
3. `shape`: `iat` is an integer
4. `iat`: not in the future
5. `exp`: present, not passed, at most 24 hours after `iat`
6. `shape`: required fields present with the right types
7. `intent_length`: at most 2048 characters
8. `on_behalf_of`: null or the requester
9. `origin`: `chain` is non-empty and its first hop is the requester
10. `operands_hash`: matches the operands

Errors on `POST /a2a/{org}/{agent}` are JSON-RPC errors:

| Code | When |
|---|---|
| `-32008` | extension not listed in `Message.extensions`, or no authority request under the URI key |
| `-32602` | invalid params: `invalid authority: shape`, `resource`, `operands`, or a failed check above; also `replay`, an unknown agent, a message with no text part, or `params` not an object |
| `-32004` | `CancelTask` (unsupported) |
| `-32601` | unknown method |
| `-32001` | `GetTask` for a task that does not exist or that the caller may not read |

Authentication is a Lightbulb sign-in (Clerk session token); the caller must belong to `{org}` (403 `wrong_workspace` otherwise).

## The rule document and evaluation

One JSON document per workspace:

```
{ "revision": "<string>",
  "holders": { "agent:*": ["user_..."], "session:*": ["@session-person"], "room:*": ["user_..."] },
  "rules": [ { "id": "r1", "resource": "agent:*", "skill": "message",
               "requesters": ["org:org_..."], "effect": "allow" } ],
  "default": "hold" }
```

`resource` matches exactly or by a trailing `*`. `requesters` entries are a person id, `org:<id>`, or `agent:<id>` (matches any agent in the chain). `effect` is `allow`, `deny` or `hold`. `@session-person` stands for the person who owns the target laptop session. Holders are chosen by the exact key, else the longest wildcard prefix.

Evaluation, in this order:

1. Requester's org differs from the workspace: `deny`, rule `outside_org`.
2. Any matching `deny` rule wins, wherever it sits.
3. A stored exception for the same resource, skill and `operands_hash`: `allow`, rule `exception:<id>`.
4. Otherwise the first matching rule in document order.
5. No match: `default`.
6. A `hold` whose requester is one of the holders, with a person at the origin: `reject`, rule `self`. Nobody approves their own request. An ask that starts from an agent run with no person behind it skips this check.

With no stored document, the default is one rule: anyone in the workspace may `message` any agent; everything else holds, for the admins (agents, rooms) or the session's person (sessions).

A hold with no holder is refused with rule `no_holder`. An exception is created when a holder allows: one day, only for the same resource, skill and operands hash.

Changing `revision` cancels every held task with reason `policy_changed`. Held tasks are not re-decided.

## Task states and the record

Task states (A2A names): `working` is `TASK_STATE_WORKING`; `held` is `TASK_STATE_AUTH_REQUIRED`; `completed`, `rejected`, `canceled` and `failed` map to their `TASK_STATE_` names. `Task.metadata[URI]` carries `state`, `rule`, the latest signed `record`, and `reason` when there is one.

- A hold expires after 24 hours: `canceled`, reason `expired`.
- A `working` task not finished 24 hours after dispatch becomes `failed`, reason `undeliverable`. Both expiries are computed when the task is read.
- A failed dispatch is `failed`, reason `dispatch_failed` (or `undeliverable` for a laptop session).
- A new policy revision cancels held tasks with reason `policy_changed`.
- When the agent replies, the reply becomes the task's artifact and the task `completed`.

Each state change appends a signed record (JWS, `typ` `lightbulb-a2a-record`, same issuer key):

```
{ "task", "authority_id", "authority_hash", "decision", "rule", "policy_revision",
  "holder", "answered_by", "decided_at", "iat",
  "input": { "requester", "chain", "operation", "intent" } }
```

`decision` is one of `allow`, `deny`, `hold`, `allow_by_holder`, `deny_by_holder`, `completed`, `failed`, `canceled`. A rejection by rule, by `self` or by `no_holder` is recorded as `deny`. `rule` is a rule id, `default`, `exception:<id>`, `self`, `no_holder`, or a reason (`expired`, `policy_changed`, `dispatch_failed`, `undeliverable`). `input` is the snapshot the decision saw. A record is readable by the requester, the task's holders and workspace admins.

## Routes

| Route | Use |
|---|---|
| `GET /a2a/keys.json` | public issuer keys |
| `GET /a2a/{org}/{agent}/agent-card.json` | Agent Card (public, rate limited) |
| `POST /a2a/{org}/{agent}` | JSON-RPC: `SendMessage` (returns the task under `task`, does not block), `GetTask` |
| `GET /a2a/{org}/{agent}/tasks/{task}/record` | every signed record for the task, oldest first |
| `POST /a2a/{org}/{agent}/tasks/{task}/decide` | a holder answers a held task: `{"verdict": "allow" or "deny", "reason": ""}` |
| `GET`, `PUT /a2a/{org}/rules` | read or replace the rule document (workspace admins) |
| `POST /api/v1/internal/a2a/{owner}/ask-session` | a hosted agent asks a laptop session; authenticated by the runtime's hook token, not by users |

`decide` errors: 404 `not_found`; 403 `self` (the requester), 403 `not_holder`; 409 `expired`, 409 `not_held` (already answered, canceled or never held). A second answer to the same task loses.

An allowed `message` is posted to the agent in the requester's direct-message room and the task is `working` until the agent replies. An `ask_session` is delivered to the laptop session with a first line naming who asked, for whom, why, and the record URL.

## What phase 1 does not do

- Third-party OAuth clients: callers need a Lightbulb sign-in for the workspace.
- Streaming (`SendStreamingMessage`) and the REST binding; JSON-RPC only.
- `CancelTask` (returns `-32004`).
- Agent Card signatures.
- `when` clauses in rules (such as matching on intent or operands). Rules are resource, skill, requesters and effect.
- Re-deciding a held task when the revision changes; it is canceled.
- An Approve or Deny button in the app; a holder is asked by direct message and answers at the `decide` route.
- Chain re-signing and narrowing checks across hops; the backend signs the whole chain once.
- Autonomous asks (no person behind the turn) need a stored rules document, because the stand-in requester is the agent's first holder and none exists without one.
- Replies from Hermes agents carry no reply id, so a reply links to its task only when exactly one task for that agent in that room is `working`.
