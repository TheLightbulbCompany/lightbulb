# Chaos Testing — Sub-project 1: Hermetic Logic Invariants

**Date:** 2026-06-15
**Status:** Approved design, pending implementation plan
**Goal:** Catch latent business-logic, idempotency, and config-bricking bugs **before launch**, on every PR, without touching AWS.

---

## Program overview (all four tracks)

"Chaos testing isol8" decomposes into four tracks split by where they can safely run.
**Nothing chaos-related ever runs against prod** — this is a hard constraint.

| Track | Runs where | Tooling | PR gate? | Spec |
|-------|-----------|---------|----------|------|
| **SP1. Logic invariants** | Hermetic CI | `hypothesis` + `moto[dynamodb]` | ✅ every PR | **this doc** |
| **SP3. API contract fuzzing** | Hermetic CI | `schemathesis` (already a dep) | ✅ every PR | later |
| **SP2. Infra fault tolerance** | Dev only | AWS FIS experiment templates (CDK) + CloudWatch stop-conditions | ❌ on-demand game-day | later |
| **SP4. Load / concurrency** | Dev only | load harness (locust/k6) + FIS | ❌ on-demand | later |

Sequencing is **Approach A — hermetic-first**: SP1 and SP3 are safe, repeatable, gate every
PR, and reuse dependencies already present. SP2/SP4 require live AWS, IAM roles, and careful
blast-radius control, so they come after the hermetic foundation exists and the invariants are
trusted. Each sub-project gets its own spec → plan → implementation cycle.

This document specifies **SP1 only**.

---

## SP1 §1 — Scope & goal

A **hermetic** pytest suite (no AWS, no live containers) that fuzzes inputs and asserts
**invariants** over isol8's stateful business logic, run on every PR. It targets the failure
classes the project's own history keeps hitting: illegal state-machine transitions, idempotency
holes, usage-counter drift, and **config bricking** (an invalid `openclaw.json` value crash-loops
the container at boot — the Isol8 config-write path currently has no schema validation).

This is *property / stateful* testing — generate many random-but-valid input sequences and assert
a property holds after every step — **not** more example-based unit tests. That property-driven
generation is what makes it "chaos" rather than additions to the existing ~249 example tests.

**New tooling:** add `hypothesis` (stateful `RuleBasedStateMachine` + `@given`). **Reuse**
`moto[dynamodb]` (already a dev dependency) so invariants execute against real repository code over
a real-shaped in-memory DynamoDB, not hand-written mocks.

### Why hermetic-first serves "catch issues before launch"

The highest-value bug classes here (state machines reaching illegal states, idempotency holes,
double-billing, config that bricks the box at boot) are all reachable without AWS. Property tests
surface them by exhaustively exploring call sequences a human would never script by hand, and shrink
any failure to a minimal repro — exactly the pre-launch safety net wanted, at PR-gate speed.

## SP1 §2 — Architecture

```
apps/backend/tests/chaos/
  conftest.py                 # moto dynamodb + all isol8 tables created from the CDK table shape
  strategies.py               # shared hypothesis strategies (owner_ids, listings, token events, config inputs)
  machines/
    marketplace_machine.py    # RuleBasedStateMachine: create → submit → approve/reject → publish → retire/takedown
    provisioning_machine.py   # RuleBasedStateMachine: provision_org / member-add / duplicate-signup
  invariants/
    test_usage_counters.py    # @given token-event sequences → counter arithmetic invariants
    test_config_schema.py     # @given config inputs → emitted openclaw.json validates against vendored zod schema
  _schema/                    # vendored upstream zod schema + Node validator (see §4)
    zod-schema*.ts            # copied from the fork at the pinned openclaw-version.json#upstream tag
    validate.mjs              # reads JSON on stdin, validates against the schema, exits non-zero on failure
    PINNED_TAG                # the upstream tag the vendored schema was copied from (staleness guard)
```

Each `RuleBasedStateMachine` declares **rules** (the operations under test, e.g. `publish`,
`provision_org`) and **`@invariant()`** methods (properties that must hold after *every* rule).
Hypothesis drives random valid call sequences and shrinks failing sequences to minimal repros.

Tests run the **real service + repository code** against moto's in-memory DynamoDB. No service is
re-implemented; the suite is a harness around production code paths.

## SP1 §3 — Invariant catalog

### Marketplace machine (`core/services/marketplace_service.py`)
- Exactly **one** row per `listing_id` is ever `published` (the core `publish_v2` guarantee).
- Transitions follow `draft → review → published → retired | taken_down` only — no skips, no
  reversals, except the documented `review → draft` auto-demote on `update_draft`.
- Artifact replacement (`replace_artifact`) succeeds **only** in `draft` state.
- `publish_v2` is all-or-nothing: an interrupted `TransactWriteItems` never leaves a half-published
  state (two published rows, or a retired-with-no-replacement).
- Buyer entitlements still resolve after the listing is retired or taken down.

### Provisioning machine (`core/services/paperclip_provisioning.py`)
- `provision_org` is idempotent on `owner_id`: N calls ≡ exactly one `status="active"` row.
- Only the `status="active" AND org_id == owner_id` path short-circuits; other statuses redo the chain.
- Duplicate signup / member-add 409s are treated as success and **never corrupt** the row.
- A failed Atlas seed never undoes a successful Friday seed.
- No member-join sequence produces the orphan-422 loop (redundant-approve must be skipped).

### Usage counters (`core/services/usage_service.py`)
- Owner period total == Σ per-member period totals == sum of recorded events.
- Counters are monotonic (never decrease).
- Concurrent / interleaved `record_usage` calls never lose an increment.
- `check_budget` is consistent: never both blocks under budget and allows over budget.

### Config builder — the brick class (`core/containers/config.py`)
- For **any** generated valid input (providers, agents, channels, security modes, MCP servers),
  `build_openclaw_config_dict` emits JSON that **passes upstream's zod schema**.
- Never emits an invalid enum (e.g. `tools.exec.security` ∉ {`deny`, `allowlist`, `full`}).
- Strict-schema unknown-key rejection holds (no stray keys that crash boot).

This is the highest-value invariant: it has bricked containers in prod before, and a generative
test over the config inputs is the only way to cover the combinatorial surface the builder produces.

## SP1 §4 — Config validation mechanism (vendored zod + Node validator)

The authoritative validator for `openclaw.json` is upstream's **TypeScript zod** schema, and CI will
not have the local `~/Desktop/agent` fork checkout. Decision: **vendor the schema + ship a tiny Node
validator.**

- Copy the fork's `zod-schema*.ts` (at the tag pinned in `openclaw-version.json#upstream`) into
  `apps/backend/tests/chaos/_schema/`.
- A small `validate.mjs` imports the schema, reads candidate JSON on stdin, and exits non-zero with
  the zod error on failure. `test_config_schema.py` shells out to `node validate.mjs`.
- This is faithful to the **real** schema (catches the real brick class), needs **no GHCR/repo auth**
  in the hermetic CI job (CI already has node/pnpm for the frontend), and aligns with the project
  rules "vendor over rewrite" and "verify the full emitted config against upstream's zod schema."

**Refresh contract:** when `openclaw-version.json#upstream` is bumped, re-copy the vendored schema and
update `_schema/PINNED_TAG`. Enforced by the staleness guard in §5.

## SP1 §5 — CI integration & runner

- New pytest dir under a `chaos` marker, run in the **backend CI job on every PR** (hermetic, no AWS creds).
- Two Hypothesis profiles:
  - **`ci`** — bounded `max_examples` and a strict `deadline` so PR runtime stays sane.
  - **`dev`** — deep (many examples, no deadline) for local "really try to break it" runs.
- Failing sequences auto-shrink to a minimal repro printed in the CI log.
- **Staleness guard:** a one-line test asserts `_schema/PINNED_TAG == openclaw-version.json#upstream`.
  A bump-without-revendor becomes a red test instead of silent drift.
- Per project convention: write the test files per task, defer running the full suite to a single
  end-of-plan verification; run only targeted chaos tests during implementation.

## SP1 §6 — Out of scope (other sub-projects)

Real AWS / FIS / network faults (SP2); load & concurrency-at-scale against dev (SP4); API contract
fuzzing via `schemathesis` (SP3); frontend chaos; Paperclip **Aurora SQL** invariants; live Stripe.
SP1 is purely hermetic Python + the vendored zod validator.

## Open risks / notes

- **moto fidelity:** moto's DynamoDB approximates `TransactWriteItems` / `ConditionExpression`
  semantics; the implementation plan must confirm moto enforces the conditional writes the
  marketplace/provisioning invariants depend on, and fall back to an explicit in-harness check if a
  given condition isn't faithfully emulated.
- **Concurrency invariants under moto:** moto is single-process; "concurrent `record_usage`" is
  modeled as interleaved sequential calls, which exercises the repository's update-expression logic
  but not true wall-clock races. True races belong to SP4.
- **Vendored-schema maintenance:** the staleness guard catches drift but the re-vendor step is manual;
  document it in the OpenClaw bump runbook.
