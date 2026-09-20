# Task 1 — pinned node contract, 2026-09-20

Status: executable loopback probe and recorded contract complete; **shell release
gate blocked** pending Task 4 native approval and terminal confirmation. No real
desktop enrolled, no production request sent, no personal config/security policy
changed, no vendor script run, and no Rust code changed.

## Changed files

- `apps/backend/tests/manual/desktop_execution_probe.py`: standalone repeatable
  loopback fixture using the repository's pure protocol constructors. Explicit
  artifact paths and `--loopback` required; no live-device selection mode.
- `apps/backend/tests/manual/node_host_loopback.md`: replaced obsolete dev-account
  instructions with observed wire fixtures, exact artifact hashes, safety scope,
  run instructions and failed/unproven release gates.
- This report (working task ledger; remove when the feature ships).

## Exact final verification commands

From `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/desktop-execution/apps/backend`:

```bash
.venv/bin/python tests/manual/desktop_execution_probe.py --help
.venv/bin/python tests/manual/desktop_execution_probe.py --loopback --policy restrictive --node-bin /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/bin/node-aarch64-apple-darwin --openclaw-dir /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/bin/openclaw-host-aarch64-apple-darwin/node_modules/openclaw --output /private/tmp/desktop-execution-task1-restrictive-final.json
.venv/bin/python tests/manual/desktop_execution_probe.py --loopback --policy default --node-bin /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/bin/node-aarch64-apple-darwin --openclaw-dir /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/bin/openclaw-host-aarch64-apple-darwin/node_modules/openclaw --output /private/tmp/desktop-execution-task1-default-final.json
.venv/bin/ruff check tests/manual/desktop_execution_probe.py
.venv/bin/ruff format --check tests/manual/desktop_execution_probe.py
```

Help exited 0 and started no node; lint/format passed. Both final probes completed
all assertions, exited **2 intentionally**, and recorded the shell release gate
as blocked. The JSON files above retain raw wire traffic, help/version, config,
invocation/result IDs and process observations. Temporary homes were removed on
exit. The durable guide carries the normalized fixture and outcome matrix.

From `apps/desktop/src-tauri`:

```bash
cargo test
TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --lib
```

Plain `cargo test` initially failed sandbox DNS; the escalated retry compiled
dependencies then failed the Tauri resource check because the isolated worktree
lacks `bin/lightbulb-browser-service-aarch64-apple-darwin`. Using the controller's
CI-supported bundle-disabled environment, library tests finished **185 passed,
0 failed, 3 ignored** (188 total). No sidecars were installed/copied. Integration
test binaries were not run by `--lib`; no broader success is claimed.

`git diff --check` passed.

## Observed schema and lifecycle

Node `v22.23.2`, SHA-256
`18e387c90ab8a8400183e8bdd396376e1e875b91b4c874b894dcade7b35bf572`.
OpenClaw `2026.8.1 (ea80657)`, package-tree SHA-256
`59d7e1ea38628ea8f8691a6ba721849c3d433c537a00b081b8a9f5a73c1f75af`.
Tree hash algorithm and dependency limitation are documented in the guide.

- Real CLI completed protocol-4 challenge/hello with its generated temporary
  Ed25519 identity; signature verified by existing broker code.
- Prepare accepts an argv array in string `paramsJSON`. It returns `plan.argv`,
  canonical `cwd`, `commandText`, `commandPreview`, `agentId`, `sessionKey`,
  `mutableFileOperand` (script path/index/hash), and `policySnapshot`; siblings
  are `execPolicy` and `allowAlwaysCoverage`.
- A direct Python `-c` prepare was rejected with `SYSTEM_RUN_DENIED: approval
  cannot safely bind this interpreter/runtime command`. A temporary Python file
  operand succeeded. This was a real protocol discovery, not a bypass.
- Matching `systemRunPlan` plus simulated gateway `approvalDecision:allow-once`
  executes. Changed argv/cwd are rejected without a marker. Restrictive config
  rejects no approval and `deny`; fresh default config executes both.
- `runId` is not in the plan: changing it still executes. Extra diagnostic
  `expiresAtMs:1` is ignored; it is not a documented expiry field. The probe does
  not claim device binding, one-use consumption, or replay protection.
- Run result is a node `req` with separate transport and invocation IDs. Final
  restrictive run sample: prepare invocation
  `d792cd72-9cf1-40da-bc59-0df229c16bb1`, successful run invocation
  `d34c9457-1e73-438d-b0ce-fc54a598d7b1`, node id
  `b6859e17be3e0222e11f56509543fc08b69c8b377aecedcb935dabcf36f63f20`.
  ACK the transport request ID. Result payload uses `exitCode`, `timedOut`,
  `success`, `stdout`, `stderr`, `error` inside `payloadJSON`.
- Cancel is `event:node.invoke.cancel`, payload `{invokeId,nodeId}`. No terminal
  result or cancellation ACK was observed. Local observations demonstrate
  ordinary descendant termination but cannot create remote terminal certainty.

Final restrictive process observations, all made **before harness cleanup**:

| Scenario | PIDs | Result |
|---|---|---|
| Cancel child | 6678, 6679 | All gone; no late marker |
| Cancel grandchild | 6773, 6775, 6776 | All gone; no late marker |
| Timeout grandchild | 6850, 6852, 6854 | All gone; no late marker; exit 124, timedOut true |
| Cancel before dispatch | 6888, 6890, 6892 | **Spawned anyway**; second cancel killed all |
| Disconnect grandchild | 6930, 6932, 6934 | All gone; no late marker |

Each tree had a 3-second late-marker deadline and was observed at least 3.5
seconds after the leaf existed. PID/PPID/PGID snapshots are in the JSON. These
fixtures do not test hostile descendants escaping their process group.

## Required follow-up (do not enable shell from this result)

1. Task 4 must supply native human approve-once/deny/close/expiry and
   stop-during-approval behavior, with exact input/device/run/deadline authority.
   Headless restrictive mode denies immediately; no companion prompt was proved.
2. Provision restrictive policy; never rely on the unsafe fresh-state full/off
   default. An incoming literal `deny` is not an explicit refusal under full/off.
3. Prevent dispatch when cancellation wins before spawn; upstream has no pending
   cancel tombstone. Send `{invokeId,nodeId}` only for a dispatched operation.
4. Add observable terminal confirmation or retain requested/unknown cancellation
   status. A successful send/ACK/silence does not prove termination to the backend.

Initial attempts and recoveries are not passing evidence: sandbox rejected the
loopback bind (the authorized retry used escalation); importing
`core.services` initialized backend settings (fixed by loading only pure
`node_broker.py`); inline Python failed prepare (replaced with a bound script).
Final runs above use the finished harness and passed their concrete assertions.
