# Browser v2 implementation

Spec: `../specs/2026-09-20-browser-v2-design.md` (workspace sharing approved).

Use the existing Browser profile, containers row, S3 bucket, runtime RPC, released CLI and DCV viewer. No new dependency, infrastructure service, per-human profile, CDP relay or CLI fork.

## Tasks

- [x] Backend: conditional `browser_state` map in reserved `__browser__#<owner>` row in the existing containers table; session start, controller transitions, verified ownership, checkpoint and stop. Keep the pointer independent of disposable runtime rows; transactionally require the parent workspace to exist when publishing. Internal runtime routes authenticate the workspace hook token; human routes resolve Clerk workspace and user. Save immutable `browser-state/<owner>/<operation>.json` and conditionally publish its pointer.
- [x] Runtime gate: supported pre-tool hook plus fixed loopback CDP discovery; room, native Paperclip and runless paths acquire/release unique run identities. Never deliver signed endpoints through unscoped process environment. Block stale browser actions until a fresh snapshot/open after handoff.
- [x] Persistence RPC: bounded `browser.state` save/load through released CLI, mode-0600 temp files, presigned S3 transfer, deterministic cleanup and sanitized failures. Explicit state load after CDP attach, not startup `--state`.
- [x] UI: explicit Start, Take control, Done/save and Stop; retain URL while refreshing controller metadata; non-controller input guard and shared-login disclosure. Mounting or polling never starts a browser.
- [x] Infrastructure/runtime env: backend session lifecycle and automation signing grants, backend-only state prefix, static nonsecret CLI discovery URL; compatibility and teardown verification.
- [ ] Focused tests, pinned-image test, independent diff review, CI and dev integration. No production promotion.

## Interface contract

`GET /browser/live-view` and human mutations return the v1 fields plus `sessionId`, `generation`, `mode`, `controllerId`, `isController`, `canControl`, `savedAt`, `error`. Modes: idle, agent, human, transition, error. Start has an empty body; navigation takes `{generation,url}` at `/browser/navigate`; control takes `{action: take|release, generation}`; stop takes `{generation}`.

Internal `/internal/browser/{owner}/acquire` accepts `{runId}` and returns `{generation, cdpUrl}`. Release accepts `{runId,generation}`. The hook token binds the owner; clients never choose a session/profile or S3 key.

RPC `browser.state` accepts `{action: save|load, cdpUrl, stateUrl, previousStateUrl?}`. Signed URLs and browser state must never appear in errors/logs. RPC returns safe metadata only.

## Verification / recovery

Test racing claims and stale releases, workspace mismatch, failed/late save publication, read-only polling, unsupported runtime RPC, failed AWS stream transition and session expiry. An expired transition must not reuse a session that an old in-flight AWS operation could still mutate. Failed save retains the previous committed pointer and does not stop the browser. An abrupt session expiry restores the last completed checkpoint, not unsaved changes.

The backend owns AWS lifetime. Generic CLI close disconnects without stopping AWS. Viewer input guards coordinate trusted workspace members; they are not private-profile isolation.


## Verification completed before PR

Backend browser, repository and provisioning checks; Hermes unit suite and exact pinned-image smoke; frontend panel regressions, installed TypeScript and ESLint; scoped CDK tests. Disposable live AWS testing restored the saved cookie/localStorage into a new profile-bound session, while removing stale cookies and stale localStorage (including an origin whose saved storage was empty). All probe resources were removed. CI and the deployed dev UI sequence remain rollout gates.

Runtime compatibility uses image-local setup of the static CDP discovery env, not an unconditional provisioning-env change. Ordinary turns bind a unique identity but acquire lazily at the first browser tool. Turn cleanup runs inside its Hermes profile scope and closes its automation client before checkpointing. S3 state publication uses SigV4 with matching JSON Content-Type; saves retain the previous pointer on failure and remove replaced snapshots after commit.
