# bedrock-gate — break-glass & operations runbook

The bedrock-gate is the **standard and only** Bedrock inference path: owner
roles carry **no `bedrock:Invoke`** (the fence), a workspace's AgentCore runtime
authenticates to the gate with a per-owner bearer token read from its boot
secret, and the gate pre-flight-checks the budget
before forwarding each call. **Fail-closed is a deliberate choice**: if the gate
hard-fails, Bedrock inference fails with it (requests error; nothing corrupts,
nothing overspends). A budget door that fails open is not a door. This runbook
is the escape hatch and the health checklist. Design:
`docs/superpowers/specs/2026-07-15-bedrock-gate-design.md`.

## 1. EMERGENCY: gate down / erroring — restore inference (~15 min)

Both steps are required (config AND IAM):

```bash
# Step 1 — flip the fleet to direct mode (SSM forbids empty strings; the
# literal value "direct" is the break-glass sentinel bedrock_gate_url.py reads):
aws ssm put-parameter --name /lightbulb/prod/bedrock-gate-url \
    --value direct --overwrite --profile isol8-admin

# Step 2 — re-grant direct invoke on every Bedrock owner role and reprovision
# (re-renders the owner's Hermes profiles + boot secret in direct mode; the
# endpoint the runtime reads is baked into its AgentCore env, so the reprovision
# is what actually moves it). Dry-run first:
cd apps/backend
uv run python -m scripts.bedrock_gate_break_glass --env prod
uv run python -m scripts.bedrock_gate_break_glass --env prod --apply
```

During direct mode the async meter remains the enforcement (invocation-log
counter -> kill), exactly the pre-gateway architecture — spend stays bounded.
⚠️ Direct mode is the ONE mode where the meter's IAM revoke still bites: with
the gate in the path, `bedrock:Invoke` is denied anyway and the deny-flag on the
bearer is the whole kill (the meter's `desiredCount=0` leg has no per-user ECS
service to stop).

**REVERT (gate healthy again):**

```bash
aws ssm put-parameter --name /lightbulb/prod/bedrock-gate-url \
    --value <the GateUrlProd CfnOutput / Function URL> --overwrite --profile isol8-admin
uv run python -m scripts.bedrock_gate_break_glass --env prod --revert --apply
```

`--revert` re-puts the fence policy (no invoke) and reprovisions back through
the gate. Single-owner surgery: add `--owner <owner_id>` to either direction.
(Per-owner deny/restore/direct controls in the admin site are a planned
fast-follow; the script is deliberately laptop-runnable with admin AWS creds so
it works even when the platform itself is sick.)

## 2. Health: what to watch

- **Gate alarms** (`LightbulbAI/BedrockGate`, per-env): `gate.error` and
  `gate.hold_release_failed` PAGE; Lambda `Errors`/`Throttles` warn.
- **Reconciliation**: `meter.reconciled_gateway_miss` (namespace `LightbulbAI`,
  dims service=`lightbulb-bedrock-meter`, env) counts invocations the gate failed
  to settle (client aborts are legitimate contributors; a sustained rate means
  a gate accounting bug):

```bash
aws cloudwatch get-metric-statistics --namespace LightbulbAI \
  --metric-name meter.reconciled_gateway_miss \
  --dimensions Name=service,Value=lightbulb-bedrock-meter Name=env,Value=prod \
  --start-time $(date -u -v-24H +%FT%TZ) --end-time $(date -u +%FT%TZ) \
  --period 86400 --statistics Sum --profile isol8-admin
```

- **Deny-flag stuck** (owner blocked at the gate after their budget reset):
  the daily restore path clears it automatically (`maybe_restore_trial_bedrock`
  -> `deny_gate_token(owner, False)`); manual clear:
  `restore_owner_bedrock(owner_id)` from a backend shell, or flip `denied` on
  the owner's row in `lightbulb-{env}-bedrock-gate-tokens` (key = the containers
  row's `bedrock_gate_token_hash`).

## 3. Invariants (do not regress)

- The meter Lambda stays the **sole Metronome writer** — paid billing is fed
  from AWS's invocation logs, never from the gate.
- The enforcement counter has exactly TWO writers joined on Bedrock's
  `requestId` in the dedup table: gate (cashier, writes first) and meter
  (auditor, counts only what the gate missed).
- `CAP_MICROCENTS` is **3_000_000 ($3.00)** in three lockstep copies:
  `core/services/trial_cap.py`, `bedrock-usage-meter/index.py`, and the gate
  stack env. Never move one alone.
- Invocation-log **text delivery stays enabled** (disabling it stops logging
  Claude records entirely and blinds the meter); bodies are bounded by 3-day
  log retention + DLQ redaction.

## 4. Byte-identical passthrough is load-bearing

The gate forwards the eventstream **verbatim, content-type included**. A client
that reframes or re-chunks hard-fails with `Truncated event message received`,
so any change to the forwarding path has to preserve the bytes, not just the
JSON. (The acpx / Claude Code harness this section used to cover is gone with
the extended OpenClaw image — a Hermes profile advertises no coding harness, so
`build_profile_config` writes no ACP block.)
