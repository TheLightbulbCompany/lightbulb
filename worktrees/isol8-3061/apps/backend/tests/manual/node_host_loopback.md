# Pinned desktop node protocol probe

This is a historical, isolated **local protocol proof**, not production device
enrollment or a human approval test. The native approval implementation now exists,
but this probe did not exercise it; use the [staged acceptance runbook](../../../../docs/runbooks/desktop-execution-staged-acceptance.md)
before enabling a real owner. The old guide's real dev account,
removed `/internal/node/invoke` endpoint and `lightbulb_node` backend instructions
are obsolete; do not restore them.

## Run

Use existing vendored artifacts, read-only. Do not run `vendor-sidecars.sh` or
point the probe at a personal OpenClaw state directory. No AWS/Clerk credentials
or paired device are needed. The tool has no remote/live-device mode and never
chooses a user's device implicitly.

```bash
cd apps/backend
uv run python tests/manual/desktop_execution_probe.py --help
uv run python tests/manual/desktop_execution_probe.py \
  --loopback --policy restrictive \
  --node-bin /absolute/path/to/bin/node-aarch64-apple-darwin \
  --host-dir /absolute/path/to/bin/openclaw-host-aarch64-apple-darwin \
  --output /private/tmp/desktop-probe-restrictive.json
```

Repeat with `--policy default` and a **new** output path. Output files are opened
exclusively; existing files are not overwritten. Exit **2** means the probe
completed but the shell release gate remains blocked; assertion/launch errors
exit **1** and are recorded in the evidence JSON. `--help` starts no host.

The probe generates a temporary HOME/config/state/identity, binds only
`127.0.0.1`, verifies the challenge signature using `node_broker.py`, acknowledges
node requests, and sends ticks. It records version/help before launching
`node openclaw.mjs node run --host 127.0.0.1 --port <ephemeral>` with installed-app
sharing disabled. Only temporary Python scripts and marker/PID files are used.
The restrictive config is `{"tools":{"exec":{"security":"allowlist","ask":"always"}}}`;
the control config is `{}`. Both are synthetic and disposable. No administrative
approval setters or real security-policy writes are performed.

The Python interpreter is the environment running the probe. Python `-c`
**fails prepare** on this pin; use a `.py` operand so the node can bind its digest.
The harness does this automatically. JSON retains every wire frame, local
process-tree snapshot, PID observation, config, CLI output and artifact hash.
It contains temporary keys' public identities, never private keys or real tokens.

## Observed artifact, 2026-09-20 (Apple Silicon)

| Artifact | Observed value |
|---|---|
| Node version | `v22.23.2` |
| Node SHA-256 | `18e387c90ab8a8400183e8bdd396376e1e875b91b4c874b894dcade7b35bf572` |
| CLI version | `OpenClaw 2026.8.1 (ea80657)` |
| Complete OpenClaw host-tree SHA-256 | `3651326affc936c3b2d6bc2acbc4ce84a6d07f349281dffe776ae9c2eaced8ad` |

The host digest hashes each sorted host-relative file path, NUL, file SHA-256
and newline across the complete vendored `openclaw-host-<triple>` directory,
including sibling npm dependencies, native modules and package manifests/locks.
The CLI path is derived from that directory's `node_modules/openclaw/openclaw.mjs`.
Version labels alone do not identify a build.

## Recorded wire contract

Handshake is protocol 4, server-first `connect.challenge`, signed `connect`
request, then `hello-ok`. Client declares `id:node-host`, `mode:node`,
`platform:macos`, `deviceFamily:Mac`. Use the existing broker frame constructors;
the probe loads that pure module without importing backend settings or workers.

Request (`I` is the invocation id; `D` the ephemeral device id):

```json
{"type":"event","event":"node.invoke.request","payload":{"id":"I","nodeId":"D","command":"system.run.prepare","paramsJSON":"{\"command\":[\"/absolute/python3.12\",\"/temp/marker.py\",\"/temp/marker\"],\"cwd\":\"/temp\",\"agentId\":\"probe-agent\",\"sessionKey\":\"probe-session\",\"runId\":\"R\",\"timeoutMs\":2000}","timeoutMs":15000}}
```

Successful prepare result payload, with temporary paths normalized:

```json
{
  "plan": {
    "argv": ["/absolute/python3.12", "/temp/marker.py", "/temp/marker"],
    "cwd": "/canonical/temp",
    "commandText": "/absolute/python3.12 /temp/marker.py /temp/marker",
    "commandPreview": null,
    "agentId": "probe-agent",
    "sessionKey": "probe-session",
    "mutableFileOperand": {"argvIndex": 1, "path": "/canonical/temp/marker.py", "sha256": "c070844e5c4d08b1a0bf510cfe79a6e54c8025e6af2c92bdef8d8008176f9006"},
    "policySnapshot": {"security":"full","ask":"off","askFallback":"deny","autoAllowSkills":false,"allowlistRules":[]}
  },
  "execPolicy": {"security":"allowlist","ask":"always"},
  "allowAlwaysCoverage": {"complete":false,"patterns":[]}
}
```

`policySnapshot` describes the approvals store; `execPolicy` is the effective
config-constrained policy. Do not confuse those two. Forward the returned plan
unchanged as `systemRunPlan`, and use its canonical `argv`/`cwd` in `system.run`.
The harness supplies `approvalDecision:"allow-once"` as a **simulated trusted
gateway decision**. This is not evidence that a human approved the request.

Results are `req` frames with a new transport `id:Q`, method
`node.invoke.result`, and `params.id:I` matching the invocation, plus `nodeId:D`,
`ok` and string `payloadJSON` or `error`. ACK **Q**, never I. Normal payload:
`{"exitCode":0,"timedOut":false,"success":true,"stdout":"","stderr":"","error":null}`.

Cancellation is an event, with **invokeId**, not id:

```json
{"type":"event","event":"node.invoke.cancel","payload":{"invokeId":"I","nodeId":"D"}}
```

There was no cancellation ACK or terminal result during the observation window.
The local process observations prove termination in this test; a remote backend
cannot infer that proof from silence. The timeout inside `paramsJSON.timeoutMs`
actually stops execution; the envelope timeout is not a substitute.

## Observations and remaining gates

| Scenario | Default `{}` | Restrictive `allowlist/always` |
|---|---|---|
| No approval | **Executed** | `SYSTEM_RUN_DENIED: approval required`; no marker |
| `approvalDecision:"deny"` | **Executed** (unrecognized decision is no grant, not explicit refusal) | Denied; no marker |
| Matching prepared `allow-once` | Executed | Executed |
| Changed argv/cwd with same plan | `INVALID_REQUEST: explicit approval requires matching systemRunPlan` | Same; no marker |
| Changed runId with same plan | **Executed** | **Executed** |
| Additional `expiresAtMs:1` | **Executed** | **Executed** |
| Cancel after spawn: child / grandchild | Every PID gone, no late marker | Same |
| 1-second execution timeout: grandchild | Every PID gone, no late marker; exit 124 / timedOut true | Same |
| Disconnect after spawn: grandchild | Every PID gone, no late marker; no result | Same |
| Cancel before request, then request with same id | **Spawned**; a second cancel stopped it | Same |

`expiresAtMs` is a diagnostic extra field, **not an upstream expiry contract**.
The test shows that adding one does not create expiry enforcement. The returned
plan has no runId, expiry or device binding; Lightbulb must enforce those itself.
The probe does not prove one-time consumption or replay resistance.

All marker cases require the documented result as well as the marker outcome:
exact denial/invalid-request code and message, or exit 0/success true/timedOut
false for execution (including the runId/expiry diagnostics). The execution
timeout requires exit 124/timedOut true/success false plus process termination.
A missing result fails these checks; it cannot count as denial or timeout proof.

Process tests record PID/PPID/PGID, observe beyond the leaf's 3-second marker
deadline, and check `kill(pid,0)` fails for each observed PID **before** harness
cleanup. These are ordinary child/grandchild trees, not adversarial daemonization
or detached process-group tests. The stop-before-spawn case records prevention
separately from the successful second cancellation; it is a failed prevention
gate, never a pass.

Native approve/deny/close/expiry and stop-during-approval remain **unproven by
this probe**: the headless host denies immediately under restrictive policy and
supplies no native human prompt here. The backend/native implementation must pass
the live exact device/run/input/deadline and fail-closed cancellation checks in
the staged runbook. The cancel event observed here had no terminal result, so
silence cannot be reported as confirmed termination. Do not bypass the native
prompt by changing policy to full/off.

No real desktop pairing, production calls, browser controls, user-file access,
or security-policy migration are part of this probe.
