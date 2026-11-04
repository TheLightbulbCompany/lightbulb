# A2A phase 2b: the runtime signs in as the workspace — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A workspace's runtime presents a backend-minted proof naming its workspace, and a laptop accepts a hosted authority note relayed by a computer signed in as that note's workspace.

**Architecture:** The backend mints a workspace proof for the runtime's own member key (it derives the key from the boot secret it wrote) and serves it on a hook-token route; the Hermes bridge fetches it at start and renews it, writing `login.json` into the engine's home. The CLI engine presents the proof from an instance, verifies `org_` proofs, keeps workspace senders out of the person path, and binds a two-hop hosted note to a sender signed in as the note's workspace.

**Tech Stack:** Python (FastAPI backend, Hermes bridge), Rust (CLI engine).

**Spec:** `docs/superpowers/specs/2026-11-03-a2a-workspace-signin-design.md` (approved 4 Nov 2026, 30-day proof life).

**Branches:** monorepo `feat/a2a-phase2b-workspace-signin` from `origin/main` (worktree `.claude/worktrees/a2a-phase2b`); CLI `feat/a2a-phase2b-workspace-signin` from `feat/a2a-phase2` until CLI #134 merges, then rebased onto `origin/main` (worktree `~/Desktop/lightbulb-network.nosync/.claude/worktrees/a2a-phase2b`).

## Global Constraints

- Workspace proof claims exactly `{iat, exp, sub: "org_<id>", org: "org_<id>", computer: <member key>}`, signed by the proof issuer key, `exp - iat` = 30 days (`30 * 86_400`).
- **Ruling (supersedes the spec's nonce step):** the route takes no key from the caller; the backend derives the runtime's member key with `agentcore_runtime.network_member_key(owner_id)` and mints for that key only. The hook token proves the caller is that workspace's runtime. Same guarantee, no round trip.
- Never log a proof, the hook token, the boot secret or a JWS. No new dependency; no CLI `PROTOCOL` bump; schema only through `ADDED_COLUMNS`.
- The person-link route stays the fallback; `A2A_DISPATCH_VIA_MEMBER` stays `False` in this plan (Task 6 flips it, dispatched only after the founder releases a CLI that has Tasks 4–5).
- Backend tests from `apps/backend`: `uv run pytest <files> -q --no-cov -p no:cacheprovider`. Bridge tests from `apps/infra/hermes`: `uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/<file> -q`. CLI: every cargo command with `CARGO_TARGET_DIR=/Users/prasiddhaparthsarthy/Desktop/lightbulb-network.nosync/target-a2a CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0`; fmt and `clippy --all-targets -- -D warnings` clean.

## Review Focus

1. A workspace that has no runtime yet (no boot secret): the route answers 404 `no_runtime`, never a 500 (Task 1).
2. A bridge that cannot reach the backend at start: the engine still starts without `login.json`, exactly as today, and the next renewal tick retries (Task 2).
3. A proof whose `org` claim differs from its `org_` subject: refused at verify (Task 3).
4. A workspace sender with no bound note: judged as `member:<short>`, never as a person, so the holder rule never fires for it (Task 4).
5. A runtime signed in as workspace W relaying a note minted for workspace V: refused `bad_request`, nothing stored (Task 5).

---

### Task 1: Backend mints the runtime's workspace proof

**Files:**
- Modify: `apps/backend/core/services/network_proof.py` (add `mint_workspace`)
- Modify: `apps/backend/routers/` — the router that already hosts hook-token internal network routes (find with `grep -rn "_require_hook_token" apps/backend/routers`; if none fits, `routers/a2a.py` beside `ask-session`)
- Test: `apps/backend/tests/unit/routers/test_runtime_proof.py`

**Interfaces:**
- Produces: `def mint_workspace(member_key: str, org_id: str, now: int | None = None) -> str` in `network_proof.py`; route `POST /api/v1/internal/network/{owner_id}/runtime-proof` (dependency `_require_hook_token`) → `{"proof": <jws>, "exp": <int>}`; 404 `{"detail": {"code": "no_runtime"}}` when `network_member_key(owner_id)` is None; 409 `{"detail": {"code": "not_on_network"}}` when the workspace has left its network (`network_member.stored_team_id(owner_id)` is None), so a retired or departed runtime stops being renewed (spec §4 Revocation).

- [ ] **Step 1: Write the failing tests:** `test_mint_workspace_claims` (decode the payload: `sub == org == "org_1"`, `computer == key`, `exp - iat == 30 * 86_400`, header `kid == network_proof.kid()`); `test_route_mints_for_the_runtimes_own_key` (mock `agentcore_runtime.network_member_key` → `"k"*52`; the proof's `computer` is that key whatever the request body says); `test_route_without_a_runtime_is_404` (Review Focus 1); `test_route_needs_the_hook_token` (401 without it); `test_route_refuses_a_workspace_off_its_network` (409 `not_on_network`).
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement.** The owner id is the workspace id the backend uses for `org_` (check how `network_login.mint` passes `org=ctx.org_id` and use the same value). — [ ] **Step 4: Run** the file plus `tests/unit/routers/test_network_login.py` → PASS.
- [ ] **Step 5: Commit** `feat(network): the backend mints a workspace proof for its own runtime`

### Task 2: The bridge fetches, writes and renews the proof

**Files:**
- Modify: `apps/infra/hermes/bridge/network.py` (`_start` and the existing sweep/renewal loop)
- Test: `apps/infra/hermes/tests/test_network_runtime_proof.py`

**Interfaces:**
- Consumes: Task 1's route; `bridge.API`, `bridge.HOOK_TOKEN`, `bridge.OWNER` (as `browser_control.py` uses them).
- Produces: `def refresh_runtime_proof(now: float | None = None) -> bool` in `network.py`: POSTs the route, writes `NET_HOME / "login.json"` as `{"proof": ..., "route_url": null, "name": "workspace"}` mode 0600, then asks the engine `login.changed`; returns False on any error without raising.

- [ ] **Step 1: Write the failing tests:** `test_start_writes_login_json_before_the_engine_starts` (the file exists with mode 0600 and the proof from a mocked POST); `test_start_proceeds_when_the_backend_is_unreachable` (Review Focus 2: no file, the engine still starts); `test_renews_when_less_than_15_days_remain` (exp within 15 days → POST again and `login.changed` sent; otherwise no POST); `test_the_proof_is_never_logged` (caplog contains no part of the token).
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement**: call it in `_start` after `identity.key` is written and before the engine launches; call it from the existing periodic sweep. — [ ] **Step 4: Run** the file, then the full bridge suite → PASS.
- [ ] **Step 5: Commit** `feat(bridge): the runtime fetches and renews its workspace proof`

### Task 3: The CLI verifies workspace proofs and an instance presents its own

**Files:**
- Modify: `src/proof.rs` (`Proof.org: Option<String>`; `verify` accepts `sub` starting `user_` or `org_`; an `org_` sub must equal `org`)
- Modify: `src/net/mod.rs` (`own_proof`: drop the `d.cloud.is_none()` gate)
- Test: unit tests in `src/proof.rs`; `tests/a2a_workspace.rs` (new)

**Interfaces:**
- Produces: `Proof { .., org: Option<String> }`; `pub fn is_workspace(p: &Proof) -> bool` (`sub` starts with `org_`).

- [ ] **Step 1: Write the failing tests:** unit `workspace_proof_verifies` (test issuer, `sub = org = "org_1"`); `workspace_proof_with_a_different_org_is_refused` (Review Focus 3); `a_user_proof_still_verifies_and_has_no_org`; integration `an_instance_presents_its_login_json_in_its_hello` (start an embedded instance with `login.json` in its home holding a test-issuer workspace proof; a dialled `Peer` receives a hello whose `proof` verifies as `org_1`).
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement.** `proof::admitted` and `presence::claimed_user` keep their `user_`-only meaning (check they do not now admit an `org_` proof as a person). — [ ] **Step 4: Run** `--lib proof --test a2a_workspace --test net_two --test embed` → PASS.
- [ ] **Step 5: Commit** `feat(proof): workspace proofs verify and an instance presents its own`

### Task 4: A workspace sender is never a person; `bind` accepts its hosted notes

**Files:**
- Modify: `src/mail.rs` (`Judge.sender_workspace: Option<&str>`; the `requester_of` mapping), `src/net/mod.rs` (fill `sender_person` only for a `user_` proof and `sender_workspace` for an `org_` one, around line 1358), `src/authority.rs` (`bind(.., sender_person, sender_workspace)`)
- Test: unit tests in `src/authority.rs`; append to `tests/a2a_workspace.rs`

**Interfaces:**
- Consumes: Task 3's `Proof.org`, `is_workspace`.
- Produces: `bind(a, kind, to, body, meta, sender_person: Option<&str>, sender_workspace: Option<&str>) -> Result<()>`: accepts when `sender_person == requester.person`, or (unchanged) the person relay rule, or `sender_workspace == Some(requester.org)` AND `chain.len() == 2` AND `chain[1].agent.is_some()`.

- [ ] **Step 1: Write the failing tests:** unit `workspace_sender_binds_its_own_workspaces_hosted_note`; `workspace_sender_cannot_bind_another_workspaces_note` (Review Focus 5); `workspace_sender_cannot_bind_a_one_hop_note`; `workspace_sender_cannot_bind_a_note_without_an_agent_hop`; integration `a_workspace_sender_without_a_note_is_judged_as_its_member` (Review Focus 4: a plain message from an instance signed in as `org_1` is allowed by `members-message`, and an `a2a` ask without a note is held with requester `member:<short>`, never `person:org_1`). Workers' check: `a_workspace_senders_assign_to_new_still_matches_its_member_row` (an instance signed in as `org_1` assigns to B's `:new` worker address; B has an `always` row for the instance's member → the assign starts without a hold; with a `refused` row → refused `declined`).
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `--lib authority --lib mail --test a2a_workspace --test a2a_envelope --test a2a_hold` → PASS.
- [ ] **Step 5: Commit** `feat(a2a): a laptop accepts a hosted note relayed by its workspace's runtime`

### Task 5: The hold and the record name the relay

**Files:**
- Modify: `src/store.rs` (`ADDED_COLUMNS` += `("holds", "relayed_by", "TEXT")`), `src/mail.rs` (store it at hold time from `Judge.sender_workspace` when the note was bound by the workspace rule), `src/holds.rs` (dialog line and record field)
- Test: append to `tests/a2a_workspace.rs`

**Interfaces:**
- Consumes: Task 4's `Judge.sender_workspace`.
- Produces: the dialog's first line `"<agent> for <person>, via your workspace's agent: <intent>"` (each field through `frame::preview`) when `relayed_by` is set, for Authority holds only: the Worker hold's dialog text (`holds::dialog` Worker arm) is unchanged; the signed record gains `"relayed_by": "org_<id>"` (null otherwise).

- [ ] **Step 1: Write the failing test:** `a_relayed_hosted_ask_is_held_and_names_the_workspace` (an instance signed in as `org_1` sends a two-hop note for `user_p` to B's session; B holds it; `shown` contains "via your workspace's agent"; Allow delivers; the approvals record has `relayed_by == "org_1"`). And: the same note from an instance signed in as `org_2` is refused `bad_request`, nothing stored.
- [ ] **Step 2: Run** → FAIL. — [ ] **Step 3: Implement.** — [ ] **Step 4: Run** `--test a2a_workspace --test a2a_hold --test holds`, then the full `cargo test` once → PASS.
- [ ] **Step 5: Commit** `feat(holds): a hold relayed by the workspace's runtime says so`

### Task 6: Turn the member route on (dispatch only after the founder releases a CLI with Tasks 3–5)

**Files:**
- Modify: `apps/backend/core/config.py` (`A2A_DISPATCH_VIA_MEMBER: bool = True`)
- Test: `apps/backend/tests/unit/routers/test_a2a_dispatch_bridge.py` (the default-on test; the off test sets it False explicitly)

- [ ] **Step 1:** flip the default and update the two tests. — [ ] **Step 2: Run** the file and `-k a2a` → PASS. — [ ] **Step 3: Commit** `feat(a2a): hosted asks ride the workspace's runtime by default`

## Not in this plan
Per-agent identities; a peer-revocation list on laptops (the 30-day life is the bound); the backend's hold notice wording.
