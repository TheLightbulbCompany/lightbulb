# A2A authority extension, phase 1b: design

**Status:** draft for the founder's approval. Nothing is built.
**Builds on:** phase 1 (branch `feat/a2a-authority-design` at `82fddd7bb`, PR #4562, not merged). This branch (`feat/a2a-phase1b`) stacks on it and is rebased onto `main` when #4562 merges; a branch from `origin/main` cannot carry phase 1b because phase 1 is not there yet (controller ruling, founder may object).
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
- Phase-1 invariants hold: the door mints and signs; deny always wins; every transition is a conditional write; records on every state change; the requester never answers their own hold.
- The OpenUI card press mechanism posts the pressed label as a normal human reply from the person (`apps/frontend/src/components/messaging/openui/CardContext.tsx`, `useCardPressState`); it calls no backend route. Phase 1b uses that as the answer channel and verifies on the backend.
- Fresh worktree, draft PR, no merge or release without the founder; no production data; no secrets printed.

## 4. Third-party OAuth clients

- Generalize `hosted_mcp.ClerkOAuthVerifier` so the audience and the required scopes are constructor arguments (today `RESOURCE` and `READ_SCOPE` are module constants, `hosted_mcp.py` ~57-130). Instantiate a second verifier for `RESOURCE_A2A = f"{PUBLIC_API_BASE}/a2a"` with required scopes `{"lightbulb:a2a:send", "user:org:read"}`; `lightbulb:a2a:read` for GetTask and records only.
- Metadata: `GET /.well-known/oauth-protected-resource/a2a` beside the `/mcp` one (`main.py:1180`), listing the Clerk issuer and the two scopes.
- Dashboard (founder's step): add `lightbulb:a2a:send` and `lightbulb:a2a:read` on dev and prod; default scopes for dynamic clients include `lightbulb:a2a:read` but not `send`.
- The JSON-RPC route accepts either a Lightbulb session JWT (phase 1 behaviour, via `get_current_user`) or an opaque Clerk OAuth token verified by the A2A verifier; the resulting `AuthContext` carries `user_id` and `org_id` the same way, plus `client_id` recorded as `chain[0].agent` for OAuth callers (the client, not a person, is the first hop's software) and `verified_by="clerk-oauth"`.
- The Agent Card's `securitySchemes` becomes OAuth2 (authorization code with PKCE) pointing at the Clerk issuer, scopes listed, plus the bearer scheme for Lightbulb sign-ins.
- Rules: an OAuth client's requests match `requesters` by person id and `org:` as before, and additionally by `client:<client_id>`.

## 5. Approve/Deny in the app

The hold notification carries an OpenUI buttons card (the fence `roomMarkdown.tsx` already renders for agent messages when cards are enabled): two buttons, labels `Approve <short id>` and `Deny <short id>` where `<short id>` is the first 8 characters of the task id, plus the phase-1 text (chain, intent, request text, the REST route as the fallback).

The press posts the label as the holder's own message in that DM. The backend hooks human messages (the same place `create_human_message` persists a message, the way `post_agent_message` got the agent-reply hook): when a human message in a DM with an agent matches `^(Approve|Deny) ([0-9a-f]{8})$`, resolve the task whose id starts with that prefix in that org, and call `a2a_tasks.decide(auth=<the message's author>, verdict=..., reason="in-app")`. All phase-1 checks apply unchanged: holder only, never the requester, held and not expired, revision unchanged. The hook never raises into the message path; failures are logged and the agent posts a one-line "could not record your answer: <reason>" in the same DM.

Why this and not a Teams inbox card: the Paperclip approval card is answerable by any org admin, not by a specific holder, and needs a Paperclip company. The OpenUI press is authenticated as the person who pressed it, which is exactly the check the decide route already makes.

Feed rooms and archived rooms disable cards; holds are only ever notified in a DM, so this does not apply.

## 6. `when` clauses

`Rule` gains `when: When | None` with `When(ContractModel)`: `operands_equal: dict[str, str] | None`, `intent_matches: str | None`. `decide` gains `operands: dict` and `intent: str` parameters, passed from `_decide_and_store` and `ask_session` (`claims["operation"]["operands"]`, `claims["intent"]`). A rule with `when` matches only if every `operands_equal` key is present with an equal value and `intent_matches` equals the intent after case-folding and collapsing whitespace. No regex, no model. Evaluation order is unchanged. The replay cases gain two `when` cases.

## 7. Re-deciding held tasks on a policy change

`put_rules` keeps cancelling only tasks that are already expired. For each other held task it runs `decide` again under the new document with the stored claims (operands, intent, requester, chain, session person): `allow` → dispatch as an allow-by-policy (record `allow_by_policy`, no exception row); `deny` → `rejected` (record `deny_by_policy`); `hold` → stays held, `holders` and `policyRevision` updated, holders notified again only if the holder set changed. The revision check in `decide` stays as the backstop for a task the loop missed.

## 8. Agent Card signatures

`agent_card()` returns a signed card: `a2a.utils.signing.create_agent_card_signer(signing_key=<PyJWK from the Ed25519 private key network_proof uses>, protected_header=ProtectedHeader(kid=network_proof.kid(), alg="EdDSA", jku=f"{PUBLIC_API_BASE}/a2a/keys.json", typ="JOSE"))`. The SDK canonicalizes with its bundled JCS, so our card signature is verifiable by any A2A client with `create_signature_verifier`. The conformance test verifies the signature with the SDK verifier against `keys.json`. `network_proof._sign` stays as is for authority objects and records (a compact JWS over the payload bytes needs no canonicalization).

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

Exact verifier refactor; where the human-message hook sits; the short-id collision rule (two held tasks sharing 8 hex characters in one org: refuse with a logged warning); the OpenUI fence syntax for two buttons (read `openui/renderers/ask.tsx` `ButtonsView`); the PyJWK construction from the raw Ed25519 seed; the dedupe of re-notification.

## 12. Questions for the founder

1. Clerk Dashboard scopes are a manual step on dev and prod that only you can do. Do you want 1b to ship with the session-JWT path still accepted (recommended) so the door works before the Dashboard step?
2. The press-to-answer path means the holder's answer is a visible message in their DM with the agent. Acceptable, or must the answer be invisible to the agent?
3. 1b stacks on the unmerged phase-1 branch. Merge #4562 first, or let 1b proceed stacked and rebase later?
