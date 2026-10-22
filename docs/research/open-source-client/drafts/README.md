# Lightbulb desktop

This is the source of everything [Lightbulb](https://thelightbulb.company) runs on your
computer: the macOS app and the `lightbulb` command. The hub it connects to is hosted by
us and is not in this repository. You can read here what the app does on your Mac, what
it sends, and where it sends it.

This repository is a mirror. Development happens in our private monorepo, and each sync
commit names its source commit in a `Source:` line. The paths keep their monorepo layout
(`apps/desktop/…`, `packages/…`) so the code builds unchanged.

## What it is

- **The app** (`apps/desktop/src-tauri`, a [Tauri 2](https://tauri.app) app in Rust). Its
  window loads `https://thelightbulb.company/chat`. Everything in this repository is the
  native half that the web page cannot do by itself.
- **The `lightbulb` command** (the same binary, linked at `~/.local/bin/lightbulb`):
  `lightbulb join`, `lightbulb claude|codex …`, `lightbulb continue '<link>'`, and
  `lightbulb-desktop serve` to run session sharing with no window.
- **The Claude Code / Codex plugin** (`packages/claude-plugin`, MIT, also published as
  [lightbulb-plugin](https://github.com/TheLightbulbCompany/lightbulb-plugin)). The app
  compiles some of its files in and installs them with your own `claude`.

| Area | Source | What it does |
|---|---|---|
| Pairing | `bootstrap.rs`, `pair_client.rs`, `keychain.rs`, `device_token.rs` | Makes two Ed25519 keys in your Keychain, registers their public halves, and signs short-lived (~90 s) tokens with them. |
| Sign-in for the command | `headless.rs` (`join`) | Device-code sign-in: prints a code and a URL, waits for you to approve in a browser. |
| Agent access to this Mac | `node_client.rs`, `node_invoke.rs`, `subprocess.rs`, `proxy.rs`, `desktop_permissions.rs`, `file_access.rs`, `screen.rs` | Runs what your workspace's agents ask, limited to what you switched on at pairing (Shell, Browser, Files, Screen) and to the folders you approved. |
| Agent browser | `agent_chrome.rs`, `agent_cast.rs` | A separate "Lightbulb agent" Chrome profile. Your everyday Chrome profile is never opened. |
| Session sharing | `local_sessions/…`, `holder/…`, `cli.rs`, `shell_integration.rs` | Opt-in. Finds Claude Code, Codex, Cursor and Grok CLI sessions, shares the ones you choose, relays a live terminal while a teammate watches, and delivers admitted messages and keystrokes. |
| Pick up / continue | `session_pickup.rs`, `session_continue.rs`, `local_sessions/snapshot.rs` | Copy a shared session, and its git working tree, onto another Mac. |
| Notifications | `notify.rs`, `local_sessions/held_notice.rs` | Native macOS notifications. Their Allow / Decline buttons answer only when a person presses them. |
| Context export | `claude_export.rs` | When you click it: packages `~/.claude/CLAUDE.md`, project memory and skills (and nothing else) for import. |

## What it sends, and where

Every request goes to the hub over TLS. Endpoints are in the source; search for `/api/v1/`.

| When | To | What |
|---|---|---|
| You pair this Mac | `POST https://api.thelightbulb.company/api/v1/desktop/pair` | The two public keys, the machine name, your Clerk session token. Private keys never leave the Keychain. |
| `lightbulb join` | `…/api/v1/auth/device/start`, `…/auth/device/poll` | The two public keys and the machine name. |
| Agent access is on | `wss://bv.thelightbulb.company/api/v1/gateway/ws` | Results of the commands agents ran (command output, files from approved folders, screenshots when Screen is on). |
| Session sharing is on | the paired-Mac relay socket | For each session it finds: id, status, title, folder **name** (not path), the last assistant message, and whether the git checkout changed (no paths or file contents, `local_sessions/workspace.rs`). |
| A session is shared | same socket | Its conversation events, with credentials redacted (`redact` in `local_sessions.rs`); terminal screen frames only while someone is watching. |
| A teammate may pick a session up | an upload URL the hub hands out | The session transcript and the changes in its git working tree. |
| The agent browser is in use | the agent-access socket | Chrome DevTools traffic for the agent's own profile; tab frames only while someone watches. |

Stays on your Mac: your Claude / Codex / Cursor / Grok logins and API keys, the provider
processes themselves, your private keys, files outside approved folders, and every
session you have not shared.

## How it talks to the hub

1. You sign in inside the window (Clerk, on `thelightbulb.company`). The page hands the
   app a short-lived session token.
2. The app pairs (`/desktop/pair`). The hub returns a device id, the relay URLs and the
   grants for this Mac. The app stores them so it can start without the window next time.
3. From then on the app authenticates with tokens it signs with its own device keys, so an
   idle Mac stays connected. The hub checks the signature against the public key from
   pairing.
4. The hub asks the Mac to do things. The app checks each request against the
   permissions you set at pairing, and asks you in a native dialog before it enables a new
   permission, folder or local MCP server.

## Build it

You need a Mac (the app is macOS-only; we build and test on Apple silicon), Xcode Command Line Tools, Rust stable, and the
Tauri CLI 2 (`cargo install tauri-cli --version '^2'`). Node.js 22 and npm are needed only
for the bundled browser host.

The compiled-in defaults point at our **development** hub. To build against production,
set these at build time:

```sh
export LIGHTBULB_API_BASE=https://api.thelightbulb.company
export LIGHTBULB_WS_URL=wss://bv.thelightbulb.company/api/v1/gateway/ws
export LIGHTBULB_CALLBACK_URL=https://thelightbulb.company/auth/desktop-callback
```

Save this as `apps/desktop/src-tauri/selfbuild.json`. It points the window at production,
signs ad hoc (no Apple account) and makes no updater artifacts (no signing key):

```json
{
  "build": { "devUrl": "https://thelightbulb.company/chat", "frontendDist": "https://thelightbulb.company/chat" },
  "app": { "windows": [{ "label": "main", "title": "Lightbulb", "width": 1400, "height": 900,
    "minWidth": 800, "minHeight": 600, "titleBarStyle": "Visible", "dragDropEnabled": false,
    "url": "https://thelightbulb.company/chat" }] },
  "bundle": { "createUpdaterArtifacts": false, "targets": ["app"], "macOS": { "signingIdentity": "-" } }
}
```

Then:

```sh
cd apps/desktop/src-tauri
bash scripts/vendor-sidecars.sh aarch64-apple-darwin   # optional: the agent browser host
cargo tauri build --target aarch64-apple-darwin --config selfbuild.json
```

Without the sidecars, add `"resources": []` to `bundle` in `selfbuild.json`. Everything
works except the bundled browser host.

Tests:

```sh
TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --lib
TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --test holder --test cli
```

### Things to know about your own build

- It uses the same bundle id as our signed app, so it shares the official app's Keychain
  items, data folder and `~/.local/bin/lightbulb` link. Run one or the other, not both.
  macOS asks again for Keychain access, because an ad-hoc signature is not the signature
  that made the items.
- The page in the window can offer an update. An update installs only if its signature
  matches the public key in `tauri.conf.json`, so the update is our official signed build,
  and it replaces yours.
- macOS asks again for Screen Recording, Automation and notification permission, because
  those grants follow the code signature.

## Licence

Apache-2.0 (see `LICENSE` and `NOTICE`). `packages/claude-plugin` is MIT. "Lightbulb" and
its logo are trademarks and are not licensed: distribute a fork under a different name and
icon.

## Security

See [SECURITY.md](SECURITY.md). Please report vulnerabilities privately.
