# Browser v2: human control and persistent sign-ins

Status: measured feasibility complete for the core primitives; workspace-sharing decision approved on 2026-09-20. Implementation in progress; this is not a shipped feature.

## Intended behavior

A person can open the remote browser without an active agent turn, use websites, take control from an agent, and release control for later agent work. Ending an agent turn must not destroy that browser. Saved website authentication and localStorage should survive a fresh browser session.

Approved access model: one shared browser and saved-login store per workspace. Workspace members and workspace agents use the same website sign-ins. Keep one AWS Browser Profile per workspace. Sharing does not remove control arbitration: only one controller may drive the browser at a time, and every request must still be authorized to that workspace. The UI must explain that website sign-ins are shared with the workspace.

## What was measured

All probes used synthetic form/cookie/storage data. No production workspace or website login was changed.

| Probe | Result |
| --- | --- |
| AWS SaveBrowserSessionProfile, then fresh session | Cookie restored; localStorage absent at first completed page read. |
| Released agent-browser 0.38.1 `state save`, then startup `--state` | Neither synthetic value restored in the tested provider path. |
| Explicit post-open `state load`, then reload | Both cookie and localStorage restored. A final readback was correlated to a real READY AgentCore session through the data-plane API; that final case did not attach an AWS Browser Profile. |
| Real Lightbulb dev Browser panel, automation enabled | Human keyboard input produced `abc`, independently read back through the CLI. |
| Real dev panel, native automation disabled | Human input and button click produced and visibly displayed `abcdef`. |
| Native AgentCore CLI provider after re-enable | CLI created a different AWS session and lost the original page context. This path is unsuitable for transparent handback. |
| Generic released CLI attached to signed existing automation endpoint | Worked when the URL scheme was `wss://`. Passing the signed `https://` URL caused HTTP discovery against the wrong endpoint. |
| Disable/enable and generic reattach | Reattached to the same AWS session and retained the synthetic page/value. |
| Generic CLI close, then fresh CLI reattach | AWS session remained READY with the same profile; the new client recovered the existing page/value. |

Cleanup stopped the recorded AWS sessions and deleted disposable profiles. The pre-existing Blue Caller dev Browser Profile was preserved. CLI cleanup is namespace-scoped in pinned `cli/src/connection.rs` (`walk_daemons` reads only `get_socket_dir`).

## Architecture recommendation

The backend owns the AWS Browser session lifecycle. The released CLI is a client of an existing session, using its generic CDP connection; it must no longer create a new AWS session on reconnect. AWS receives automation traffic directly: no CDP proxy or forked CLI is needed.

Reuse existing components:

- `core/services/browser_sessions.py`: profile ownership checks, AWS SDK clients, URL signing, and live-view discovery.
- Existing DynamoDB claim patterns in `container_repo.py`: durable, token-fenced controller ownership and expiry. Do not add a generic workflow engine or Step Functions browser state machine.
- Native AWS `UpdateBrowserStream`: disable automation while a human controls the browser, then re-enable before an authorized agent reconnects.
- Released `agent-browser` CDP and state-file commands: same-session continuation and explicit storage export/import.
- Pinned Hermes `pre_tool_call` hooks: block browser actions while the human owns control, rather than relying on an observer callback or instructions to the model.

Workspace sharing is the authorization boundary. The installed `bedrock-agentcore@0.4.4` viewer has no read-only permission prop: a live-view URL permits human interaction. Non-controller input guards are UI coordination, not per-member security isolation. `UpdateBrowserStream` gates automation globally; it does not revoke another human viewer. Signed URL expiry limits new connections and must not be assumed to terminate an established connection. Per-viewer security enforcement belongs with the deferred isolation feature.

The lease and AWS state must agree before the UI reports a successful handoff. Use a transitioning state around external calls; a timeout is not proof of success. Fence stale releases and reject stale page actions after a human handoff until the agent obtains a fresh snapshot.

Use the AWS-returned session viewport. The earlier standalone viewer's hardcoded viewport and incomplete target setup produced misleading blank/stale-view results. The actual app displayed and accepted input correctly.

## Persistence contract

Treat native browser state JSON as a credential. Keep it out of messages, logs, analytics, packages, screenshots of developer consoles, and public attachments. Store it in owner-scoped encrypted storage and use mode-0600 local files for CLI import/export.

There must be one authoritative state writer. A stale turn must not overwrite a newer human session checkpoint. Commit a saved-state revision only while the matching lease/generation still owns the browser; use an immutable operation-key plus a conditional pointer unless a conditional object write provides the same guarantee. A fixed `latest.json` upload followed by a metadata check is insufficient: the stale upload may already have overwritten newer state.

Export before planned session termination, at a suitable human Done boundary, and after successful agent browsing. Explicit post-open import is the measured working path; do not substitute the startup flag without a new passing test. Test the final profile-bound connection and persistence sequence together before claiming integration success.

Do not promise IndexedDB, service workers, arbitrary in-memory page state, or every site's login behavior from the cookie/localStorage probe. Clarify session expiry, retry/save failures, and the distinction between closing the panel and ending the remote browser.

## Remaining integration checks

1. Enforce the approved workspace-shared model across human and agent entry points, including cross-workspace denial.
2. Implement and test controller claims: two agents, two humans, expiry, stale release, backend restart, and an AWS request that times out after taking effect.
3. Prove the pinned Hermes integration without a second CDP observer stealing the page. `BROWSER_CDP_URL` enables Hermes's own supervisor; `AGENT_BROWSER_CDP` belongs to the CLI. They are different controls.
4. Prove endpoint delivery through the existing subprocess credential filter. A simple per-turn secret-scope overlay did not reach the browser subprocess in the isolated pinned-image check. Do not fix this by indiscriminately exposing all profile secrets.
5. Decide the smallest supported integration seam: scoped browser environment support or standard local CDP discovery from the existing bridge. A discovery response carries connection metadata only; browser WebSocket traffic must remain direct to AWS.
6. Verify the full real-app sequence: open, restored login state, human input, takeover, handback, fresh agent snapshot, agent completion, later reattach, expiry/save/restart, and cross-workspace denial.
7. Roll out through the existing backend/CDK/image build, dev pin, runtime-roll and verification procedures before any production promotion.

Browser-v2 source implementation is in progress on `codex/browser-v2`; no deployment has been made. Migration closeout PR #2808 is separately merged and deployed to dev; its production jobs were skipped.

## Backlog: optional per-human browser isolation

Deferred by the user on 2026-09-20. Add private browser profiles and saved-login stores only when requested. That feature must define which agents may use a person's private sessions, how access is revoked when they leave a workspace, and how existing shared state is handled. Browser v2 does not add private-profile tables, flags, or migration scaffolding.
