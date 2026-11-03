# A2A authority extension, phase 1b: design

**Status:** APPROVED by the founder 2 Nov 2026 with the author's calls as written. Plan: docs/superpowers/plans/2026-11-02-a2a-authority-phase1b.md.
**Builds on:** phase 1 (branch `feat/a2a-authority-design` at `82fddd7bb`, PR #4562, not merged). This branch (`feat/a2a-phase1b`) stacks on it and is rebased onto `main` when #4562 merges; a branch from `origin/main` cannot carry phase 1b because phase 1 is not there yet (controller ruling, founder may object).
**Builder check (2 Nov):** run; nine contradictions and eleven questions folded in below; calls marked "my call".
**Scope (from the phase-1 spec §2a cut list):** third-party OAuth clients, an Approve/Deny button in the app for held tasks, `when` clauses on rules, re-deciding held tasks on a policy change, Agent Card signatures. Everything else stays in phase 2 and later.

## 1. What phase 1 taught, and what this phase fixes

- A held task can only be answered by calling a REST route with a sign-in token. Nobody answers a hold from a DM today. The notification already lands in the holder's DM with the agent; the answer has to land there too.
- The hosted door takes Lightbulb sign-ins only. The Agent Card advertises a bearer scheme no outside client can obtain. The hosted MCP already admits dynamic third-party clients through Clerk (CIMD clients, scopes set in the Clerk Dashboard); the A2A door needs the same, with its own audience and scopes.
- Rules match on resource, skill and requester only. A rule like "allow Friday to ask my session for status" cannot be written. `decide` never sees operands or intent.
- A rules change cancels every held task. Admins who tighten one rule lose unrelated holds.
- The Agent Card is unsigned; the A2A spec allows signatures and the SDK ships a signer with JCS canonicalization.

## 2. Goals, non-goals, anti-goals

Goals: an outside A2A client can obtain a token through Clerk and call the door; a holder answers a hold with one press in the app; rules can condition on operands and intent; a rules change re-decides holds instead of dropping them; the Agent Card carries a verifiable signature.

Non-goals: streaming, cancel, chain re-signing, the P2P migration, Workers rules, spend mandates, adapters (phases 2 and later). A new UI component. A new message kind.

Anti-goals: a button whose press can be forged by anyone but the holder; a `when` clause evaluated by a model; a signature that only our own code can verify.

## 3. Constraints

- No new dependency beyond `a2a-sdk`; the `signing` extra of `a2a-sdk` (PyJWT, already present) is allowed.
- Clerk has no API for custom scopes or default scopes; they are Dashboard-only, on each instance (dev, prod). Someone with Dashboard access adds them by hand: this is the founder's step, not the builder's.
- Phase-1 invariants hold: the door mints and signs; deny always wins; every transition is a conditional write; records on every state change; the requester never answers their own hold. One phase-1 ruling is overturned by this phase's scope: "a hold is never re-decided under rules its holder did not see" (`a2a_tasks.py:563-566` docstring) becomes "a hold is re-decided under new rules, and its holder is notified again if the holder set changed" (§7).
- The OpenUI card press posts the pressed label as a human reply from the person, with `mentions: [agent]`, `replyToMessageId` = the card's message id and `threadRootMessageId` (`packages/shared/src/ui/reply.ts:18-25`; `ButtonsView` presses `label.trim()`, `renderers/ask.tsx:50-52`); it calls no backend route. Phase 1b uses that as the answer channel and verifies on the backend. Cards render only for agent-authored messages (`RoomMessageItem.tsx:91`); the hold notification is one.
- Fresh worktree, draft PR, no merge or release without the founder; no production data; no secrets printed.

## 4. Third-party OAuth clients

- A second verifier instance, not a module constant: generalize `hosted_mcp.ClerkOAuthVerifier` (`hosted_mcp.py:57-130`) so audience, required scopes and the returned `resource` are constructor arguments. The A2A instance keeps the other hard-coded checks as they are: opaque tokens only, `subject` starting `user_`, live org membership through `clerk_admin`, `_registered_client` lookup. Two differences, my call: a free account (`org_id` None) is refused with 403 `workspace_required`, and per-agent keys (`LightbulbVerifier`) are NOT accepted on the A2A door; hosted agents use the hook-token route.
- Audience: the A2A verifier requires `RESOURCE_A2A = f"{PUBLIC_API_BASE}/a2a"` in the token's `aud` list. Clerk's docs do not say whether one instance issues a second resource audience for CIMD clients (checked 2 Nov: the OAuth and MCP pages cover DCR, CIMD, scopes and endpoints, not RFC 8707). The builder verifies against the dev instance's `/.well-known/oauth-authorization-server` metadata and one real CIMD token request; if Clerk does not echo the requested resource, the fallback is: the A2A verifier accepts the `/mcp` audience and discriminates on the A2A scopes alone, and the metadata route advertises `/mcp` as the resource. Report which.
- Scopes: `lightbulb:a2a:send` (SendMessage), `lightbulb:a2a:read` (GetTask and the record route), plus `user:org:read`. Dashboard (founder's step, dev and prod): create both scopes on the Scopes tab and assign them; default scopes for dynamic clients include `read`, not `send`.
- Metadata: `GET /.well-known/oauth-protected-resource/a2a` beside the `/mcp` one (`main.py:1180`), listing the Clerk issuer and the two scopes.
- The JSON-RPC and record routes accept either a Lightbulb session JWT (phase 1 path, `get_current_user`) or an opaque Clerk OAuth token verified by the A2A verifier (a token with a `.` is a JWT and goes to the session path; an opaque token goes to the verifier). The resulting `AuthContext` carries `user_id` and `org_id` as before. For OAuth callers the first hop records the client: `chain[0].agent = client_id`, `verified_by = "clerk-oauth"`; rules match it with the existing `agent:<client_id>` requester spelling (my call: no new `client:` kind).
- The Agent Card lists two `securityRequirements` entries, alternatives (OR): the bearer scheme for Lightbulb sign-ins, and an OAuth2 authorization-code-with-PKCE scheme with `authorizationUrl = {CLERK_ISSUER}/oauth/authorize`, `tokenUrl = {CLERK_ISSUER}/oauth/token` (the paths Clerk's authorization-server metadata publishes; the builder reads them from the live metadata rather than hard-coding) and the two scopes.

## 5. Approve/Deny in the app

The hold notification carries an OpenUI buttons card with two buttons, labels `Approve` and `Deny`, plus the phase-1 text (chain, intent, request text, the REST route as the fallback). The press posts the label as the holder's own message in that DM with `replyToMessageId` = the notification message id.

The backend recognizes an answer in exactly one place: the web person-JWT post path of `create_human_message` (`routers/messaging.py:986`: `label is None`, no `via`, no `author_override`). Posts through the hosted MCP (`hosted_mcp.py:792`), coding sessions (`via="lightbulb_mcp"`, `routers/messaging.py:996-1005`) and phase 1's own dispatch (`a2a_tasks.py:399`) are human-accountable but software-written and can never answer a hold (builder-check contradiction 1; my call: web path only in 1b, iMessage and others later if wanted). The hook runs before the message is dispatched: if `reply_to_message_id` is a hold-notification message id (stored on the task row as `notificationMessageIds[holder]` when the notification is posted) and the text is `Approve` or `Deny`, it calls `a2a_tasks.decide(auth=<the post's AuthContext>, verdict, reason="in-app")`, then persists the message with `wake=False` so the agent is not dispatched to answer "Approve" (builder-check contradiction 2). All phase-1 checks apply unchanged: holder only, never the requester, held and not expired, revision handled per §7. The task is found by the notification id, so no short-id prefix, no collision rule. A failed decision never raises into the message path; it is logged and the agent posts one line in the same DM: "could not record your answer: <reason>".

Why this and not a Teams inbox card: the Paperclip approval card is answerable by any org admin, not by a specific holder, and needs a Paperclip company. The OpenUI press is authenticated as the person who pressed it, which is the check the decide route already makes.

Feed rooms and archived rooms disable cards; holds are only notified in a DM.

## 6. `when` clauses

`Rule` gains `when: When | None` with `When(ContractModel)`: `operands_equal: dict[str, str] | None`, `intent_matches: str | None`. `decide` gains `operands: dict` and `intent: str` parameters, passed from `_decide_and_store` and `ask_session` (`claims["operation"]["operands"]`, `claims["intent"]`). A rule with `when` matches only if every `operands_equal` key is present with an equal value and `intent_matches` equals the intent after case-folding and collapsing whitespace. No regex, no model. Evaluation order is unchanged. The replay cases gain two `when` cases.

## 7. Re-deciding held tasks on a policy change

Held rows store what `decide` needs to run again: from 1b on, `requesterOrg`, `sessionPerson`, `originIsPerson` are written beside `requester`, `authority`, `holders`, `text`, `policyRevision` (builder-check contradiction 4). A row held before 1b lacks them and is canceled with `policy_changed` as phase 1 does today.

One function, `_redecide(org, row, rules, now)`, is used in two places: `put_rules` stores the new document FIRST (so a press during the loop is judged against the stored revision, contradiction 5), then re-decides every held task; and `decide`'s revision-mismatch path calls it inline instead of canceling, continuing with the holder's answer if the task is still held afterwards. Outcomes: `allow` → dispatch, record `allow_by_policy`, no exception row (my call: policy allowed it, an exception is only for a holder's answer); `deny` → `rejected`, record `deny_by_policy`; `hold` → stays held, `holders` and `policyRevision` updated, record `redecided`, holders notified again only if the holder set changed; `hold` with no holders → `rejected`, `no_holder`, as phase 1 does. Already-expired holds still cancel as `expired`.

## 8. Agent Card signatures

`agent_card()` returns a signed card: `a2a.utils.signing.create_agent_card_signer(signing_key=<PyJWK from the Ed25519 private key network_proof uses>, protected_header=ProtectedHeader(kid=network_proof.kid(), alg="EdDSA", jku=f"{PUBLIC_API_BASE}/a2a/keys.json", typ="JOSE"))`. The signer accepts a `PyJWK` (PyJWT 2.15 unwraps it) and writes the protected header `{"typ":"JOSE","alg":"EdDSA","kid","jku"}`; the SDK canonicalizes with its bundled JCS, so the signature is verifiable by any A2A client with `create_signature_verifier` against our `keys.json`, which already serves the OKP JWK with the same `kid`. The conformance test verifies the signature with the SDK verifier against `keys.json`. `network_proof._sign` stays as is for authority objects and records (a compact JWS over the payload bytes needs no canonicalization).

## 9. Records and the page

New record decisions: `allow_by_policy`, `deny_by_policy`, `redecided`. New `verified_by` value: `clerk-oauth`. The extension page gets a section per feature; the "What phase 1 does not do" list shrinks accordingly.

## 10. Tests (acceptance)

1. An OAuth token with audience `/a2a` and scope `send` passes; one with only `read` can GetTask but not SendMessage; a `/mcp`-audience token is refused; the card advertises both schemes.
2. A holder's `Approve <id>` message in the DM completes the hold; `Deny <id>` rejects it; the same message from the requester or a non-holder records nothing and the agent posts the refusal line; a malformed label is ignored.
3. A `when` rule with `intent_matches` allows the matching intent and holds a different one; `operands_equal` likewise; a rule with `when` never matches when `decide` is called without operands.
4. A rules change re-decides three held tasks into allow (dispatched), deny (rejected) and hold (kept, holders updated), with one record each; an expired hold still cancels as expired.
5. The signed card verifies with the SDK verifier against `keys.json`; a tampered card fails.
6. Phase-1 suites still pass unchanged.

## 11. Left to the builder

The exact verifier refactor; the Clerk audience check and its fallback (§4); the OpenUI fence syntax for two buttons (`renderers/ask.tsx` `ButtonsView`); the PyJWK construction from the raw Ed25519 seed; the dedupe of re-notification; the `wake=False` plumbing on the web post path.

## 12. Questions for the founder

1. Clerk Dashboard scopes are a manual step on dev and prod that only you can do. 1b keeps the session-JWT path accepted so the door works before that step (my call). Confirm, or ask for OAuth-only.
2. The press-to-answer path makes the holder's answer a visible message in their DM with the agent, and the agent is not woken to reply to it. Acceptable, or must the answer be invisible to the agent?
3. 1b stacks on the unmerged phase-1 branch (#4562). Merge #4562 first, or let 1b proceed stacked and rebase later?
4. §7 overturns a phase-1 ruling (holds are now re-decided under new rules rather than canceled). This is the scope you set for 1b; confirm.
