# Can the `openclaw-host` sidecar be retired?

2026-10-22. Investigation only, read from `origin/main` at `8a1a3944c`, plus read-only prod AWS checks and the installed `/Applications/Lightbulb.app`.

## Verdict

**Do not remove it yet. Retire it after a Rust port of the CLI node's commands.** In the meantime, prune it now to save about 400 MB.

`openclaw-host` has nothing to do with the retired OpenClaw *gateway/runtime*. It is the **second desktop node** (clientId `node-host`), and it is the only thing that serves the Mac's **Shell, Files and local MCP** tools to Hermes agents through `lightbulb_desktop`. Hermes did not replace it. Hermes calls it: the backend's own node broker terminates the OpenClaw node protocol, and both desktop nodes dial it. Prod has one connected right now with the full Shell, Files and MCP grant. Removing the sidecar today breaks `lightbulb_desktop invoke system.run`, the `file` actions (`file.fetch/write`, `dir.list/fetch`), `mcp_list`/`mcp_call`, and the Settings → Desktop "Local MCP servers" pane.

The Rust app already speaks the node protocol natively (`node_client.rs`), so the dependency is about commands, not protocol. The Node.js host can go once those ~8 commands are ported to Rust.

## 1. What it is and what runs it

- **Build:** `apps/desktop/src-tauri/scripts/vendor-sidecars.sh:83-108` runs `npm install openclaw@2026.8.1` (vanilla public npm) into `bin/openclaw-host-<triple>/`. `write_launcher` (`:113-140`) writes `bin/lightbulb-browser-service-<triple>`, a bash shim that execs `node … openclaw.mjs node run --host 127.0.0.1 --port $LIGHTBULB_PROXY_PORT`. It ships as a resource through `tauri.conf.json` `"bin/openclaw-host-*/**/*"`.
  - The name "browser-service" is a scar. The sidecar was added for `browser.proxy` in #313, and that path was deleted in #3436/#3438.
- **Spawn chain:** `lib.rs:553 run_bootstrap` (and `start_from_persisted_state`, about `:1040`) calls `bootstrap.rs::bootstrap_desktop_node_host` → `start_from_pair`. That starts the loopback proxy (`proxy.rs`) and then `subprocess::spawn` (`subprocess.rs`, about `:104-215`).
  - The proxy dials `pair_resp.relay_url` (the `bv-` gateway relay) with clientId `node-host` (`lib.rs:594,1056`). It also mints a fresh device token on every reconnect from the Keychain **Cli** slot.
- **Restarts:** `lib.rs:746 restart_node_host_subprocess_locked` restarts the sidecar. It is called by `local_mcp_set` (`:842`), `local_mcp_remove`, and `repair_if_permissions_changed` (`:655`). `forget_pairing` (`:455`) kills it.
- **The other node:** the Rust **Mac node** (`node_client.rs`, `CLIENT_ID = "openclaw-macos"` at `:642`) is a separate socket. It is native Rust and does not use the sidecar. `node_invoke.rs:1-14` states the split: "the cross-platform commands (`system.run*`, `system.which`, `system.execApprovals.*`, `browser.proxy`, `file.fetch`/`file.write`, `dir.list`/`dir.fetch`) now live in the openclaw subprocess".

### Which features depend on which node

| Feature | Served by | Depends on openclaw-host? |
|---|---|---|
| `lightbulb_desktop invoke system.run` (Mac shell) | `node-host`: `system.run.prepare` + `system.run`. The Mac node gives native approval through `system.run.approve.v1` | **Yes** |
| `lightbulb_desktop file` (`file.fetch/write`, `dir.list/fetch`) | `node-host`. The Mac node supplies `file.access.policy.v1` | **Yes** |
| `lightbulb_desktop mcp_list` / `mcp_call`, Settings "Local MCP servers" (`LocalMcpServers.tsx`, `local_mcp.rs`) | `node-host`: `mcp.tools.call.v1` + the `node.pluginTools.update` catalog | **Yes** |
| `screen.snapshot`, `system.notify`, `device.*` | Mac node (Rust) | No |
| Mac agent Chrome (`use_browser`, `browser.cdp.v1`/`cast`/`wipe`) | Mac node (Rust, `agent_chrome.rs`) | No. `browser.proxy` only survives as a grant marker in the backend row |
| Remote session start (`session.start.v1`) | Mac node (Rust) | No |
| Local sessions / session sharing | `ws-` relay, clientId `lightbulb-local-sessions` (Rust) | No. The backend only checks the CLI *pairing record* (`local_sessions.py:1016-1025`, `local_sessions_repo.py:76-81`), not a live process |
| Session browser MCP (`chrome-devtools-mcp`) | the bundled `node-<triple>` binary | No, but it needs the 107 MB `node` binary, which must stay |

## 2. Backend use

- `core/services/desktop_pair.py`:
  - Its docstring (`:1-11`) describes the two-node design.
  - `DESKTOP_CLI_SHELL_COMMANDS` (`:71-96`) and `DESKTOP_CLI_FILE_COMMANDS` (`:107-111`) are granted to the `node-host` device.
  - `ALLOWED_CLIENT_IDS = {"openclaw-macos","node-host"}` (`:137`).
  - `paired_physical_mac` (`:157`) requires exactly two linked entries, one of each clientId.
  - The Browser grant is stored as the `browser.proxy` marker on the CLI entry (`:104`, `granted_commands` `:273`).
- `core/services/desktop_execution.py`:
  - `_execute_before_deadline` (about `:421-426`) routes `system.run` to `_shell` (`:491-584`) and file/MCP commands to `desktop_resources.execute`.
  - `_resolve_companion` (`:986-1040`) refuses shell/file unless the target's clientId is `node-host` (`:1001`).
  - `_shell` depends on openclaw's exact `system.run.prepare` contract: the `plan` object, `execPolicy == {"security":"allowlist","ask":"always"}`, and `systemRunPlan` + `approvalDecision: "allow-once"` on the run.
- `core/services/desktop_resources.py:26` maps `mcp_list`/`mcp_call` to `mcp.tools.call.v1`. The catalog comes from `node_broker.NodeSession.mcp_catalog` (`node_broker.py:302-360`, pluginId `node-mcp`).
- `routers/gateway_relay.py:40-46` keeps both sockets per Mac, keyed `(owner, device)`. `node_broker.py:1-6` says the broker is built "against the CLI node-host's contract, which is a strict superset of our Rust client's". `machine_auth.py:23` sets `NODE_CLIENT_IDS = {"node-host","openclaw-macos"}`.
- Hermes: `apps/infra/hermes/plugins/lightbulb/desktop.py:43-125` exposes `system.run`, the file actions and `mcp_call`, and `desktop_resources.py:39-43` handles the file actions.
- **Answer to Q2:** the backend implements the gateway side of the node protocol. Both clients speak it. The *command implementations* for shell, files and MCP exist only in the openclaw-host process.

## 3. Prod today (read-only, `isol8-admin`, 2026-10-22)

- `lightbulb-prod-ws-connections` has 3 live `connectionType=node` relay rows: `node-host` ×2 (users `…3I8s15…` and `…3IROHY…`) and `openclaw-macos` ×1, all connected 01:04-02:12Z today.
- Ready rows (`desktop-ready#…`) show what is live:
  - `node-host`: `dir.fetch, dir.list, file.fetch, file.write, fs.listDir, mcp.tools.call.v1, system.execApprovals.get/set, system.run, system.run.prepare, system.which, terminal.upload`.
  - `openclaw-macos`: the Mac set (browser.*, screen.snapshot, session.*, system.run.approve.v1, …).
- `/ecs/lightbulb-prod` traffic to `/api/v1/desktop-execution/{admission,operations}` over the last 30 days is low: 18 submits on 10-10, 1 on 10-13, 6 on 10-14, plus 1 admission on 10-20. Access logs do not show which command each submit carried, so the split between shell/file/MCP and screen is unknown. **The path is live and lightly used, not dead.**

## 4. What breaks if it is removed, and what replaces each part

| Breaks | Replacement |
|---|---|
| `system.run.prepare` / `system.run` / `system.which` | Rust handlers. Process exec with argv/cwd/env/timeout, in the shape `_shell` checks: a `plan {argv,cwd,agentId,sessionKey,…}`, `execPolicy {"security":"allowlist","ask":"always"}`, honouring `systemRunPlan` + `allow-once`. Output and exit shape should match what `desktop.py` renders. `node_invoke.rs` already has a bounded invoke table and `tokio::process` |
| `file.fetch/write`, `dir.list/fetch` | Rust handlers matching the payloads `desktop_resources.py:218-305` checks: `base64`/`size`/`sha256`; `tarBase64`/`tarBytes`/`entries` (gzip tar); `maxBytes`/`maxEntries`. `file_access.rs` already holds the approved-folder policy |
| `mcp.tools.call.v1` + catalog (`node.pluginTools.update`, pluginId `node-mcp`) | A Rust stdio MCP client (for example the `rmcp` crate) that launches the servers `local_mcp.rs` already stores in `openclaw.json` → `nodeHost.mcp.servers`, publishes the catalog, and forwards calls |
| `local_mcp_*` restarting the sidecar | Restart the Rust MCP host task instead |

**Lowest-risk shape: keep the backend unchanged.** Run a second Rust `NodeClient` with the Keychain **Cli** identity and clientId `node-host`, dialing the relay directly. Make `CLIENT_ID` (`node_client.rs:642`) a parameter. That removes the loopback proxy (`proxy.rs`), `subprocess.rs`, the identity-file write, the `retire_legacy_node_host_config` sweep and the `OPENCLAW_*` env.

In that shape the pairing record, device ids, `paired_physical_mac`, the grants and existing pairs stay valid, and nobody re-pairs. The four "unconditional" commands (`system.execApprovals.*`, `fs.listDir`, `terminal.upload`) exist only because openclaw declares them. Our broker intersects declared commands with granted ones, so the Rust node can simply not declare them. Merging both nodes into one device is a larger backend change (disjoint-set assert, `ALLOWED_CLIENT_IDS`, `_resolve_companion`, the local-session pairing conditions, the Browser marker) with no user benefit, so do not do that.

**Git history:** `git log -S openclaw-host` shows no planned removal, and no doc on main plans one. Relevant past changes: it was added for `browser.proxy` (#313); the two-node split came in #667; local MCP was added in #2260; the 8.1 bump in #2247; respawn avoidance in #2821; browser.proxy was removed (#3436/#3438) without removing the sidecar.

### Removal plan

1. Parameterize `NodeClient` by clientId and identity. Add CLI-node handlers for shell, files and MCP in Rust behind the same grants (`desktop_permissions.rs` already gates `system.run`/`file.*`).
2. Contract tests: replay the backend's `_shell` and `desktop_resources` checks against the Rust handlers. Use `apps/backend/tests/manual/desktop_execution_probe.py` and `node_host_loopback.md`, retargeted from `--host-dir` to the app.
3. Ship with both paths present. A hidden flag picks the Rust CLI node and skips spawning the sidecar. Run dev acceptance per `docs/runbooks/desktop-execution-staged-acceptance.md`: shell with native approval, each file action in and out of approved folders, a local MCP server's list and call, unpair and repair.
4. Delete: `vendor_arch`'s openclaw install and `write_launcher` (keep `node` + `chrome-devtools-mcp`), the `bin/openclaw-host-*` resource glob, `subprocess.rs`, `proxy.rs`, `verify-browser-pin.mjs`, `tests/launcher_process_topology.cjs`, the Info.plist sidecar wording, and the `openclaw-state` dir handling (keep `local_mcp.rs`'s config file, or move it).
   - Backend comments that cite upstream `node-host/runtime.ts` reasons should be updated.
   - The `node-host` clientId string stays, because it is stored data in `paired_devices`.
5. Update the CLAUDE.md/AGENTS.md Desktop "Sidecars" line.

### Risks

- **Contract drift:** `_shell` rejects anything not shaped exactly like openclaw's prepare/plan. A partial port fails closed (`shell_prepare_denied`), so it is safe but broken.
- **Local MCP parity:** openclaw's node MCP host handles stdio servers, env and lifecycle. Users' existing `nodeHost.mcp.servers` entries must keep working.
- **Native addons:** openclaw ships `node-pty` and similar for a reason. If any current behaviour relies on a PTY (`terminal.upload`), the Rust port needs a PTY too. `system.run` itself does not.
- **Update path:** an older app still runs the sidecar against the same backend, which is fine because the backend does not change.

## 5. Size options until then (measured on the installed v0.4.x app)

`Contents/Resources/bin/`: `openclaw-host` 881 MB, `node` 107 MB, `chrome-devtools-mcp` 15 MB. Inside `openclaw-host/node_modules`:

| Package | Size | Used by a node host? |
|---|---|---|
| `@anthropic-ai/claude-agent-sdk-darwin-arm64` (a bundled `claude` binary) | **310 MB** | No. Only `dist/extensions/anthropic/agent-sdk.runtime.js` loads it, and `local_mcp::restrict_node_integrations` disables that plugin's feature |
| `openclaw/dist` | 210 MB | Partly (it holds the node host, plus the gateway/agent code we never run) |
| `@trycua/cua-driver-darwin-arm64` | 48 MB | Probably not (computer-use driver) |
| `@mistralai` 27, `typescript` 24, `openai` 21, `@google` 17, `tree-sitter-bash` 19, `playwright-core` 13, `@opentelemetry` 12 | ~133 MB | Provider/agent SDKs, unlikely on the node path |
| `openclaw/docs` | 12 MB | No |

- **Prune now (low effort, about 320-400 MB):** after `npm install` in `vendor_arch`, `rm -rf` `@anthropic-ai/claude-agent-sdk-darwin-*` and `openclaw/docs`. This also cuts the signing and notarization work in `desktop-build.yml`'s pre-sign step. Verify with the existing loopback probe, a real `system.run`, a file fetch and a local MCP call before shipping.
  - Removing the provider SDKs and cua-driver gains about 180 MB more, at higher risk, because openclaw's bundled chunks may import them eagerly. Test each one by starting `node run` and exercising each command.
- **`--omit=optional`:** this only drops `sqlite-vec`, so it saves little. The real bulk is platform optional deps of transitive packages; prune them by name as above.
- **Lazy download on first use:** possible (fetch a pinned, signed tarball the first time Shell, Files or MCP is switched on). It adds a signed-artifact host, integrity checks and offline failure modes, and macOS Gatekeeper/notarization applies to downloaded executables. Not worth it when pruning recovers ~40% and the Rust port removes all of it.
