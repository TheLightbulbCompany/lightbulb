# Open-sourcing the Lightbulb desktop client: preparation report

Date: 2026-10-21. Source: `origin/main` at `84ebf5447`. Preparation only: no repository was
created, nothing was pushed, no visibility was changed. Everything here is uncommitted.

> **Update 2026-10-22: the scope narrows to `lightbulb-core`.** The owner chose to split the
> session network and CLI out of the Tauri crate into a new crate, `lightbulb-core`, at
> `apps/desktop/core/`. The app (window, agent Chrome, screen, Mac node, updater, terminal panel)
> stays private. Plan: `docs/superpowers/plans/2026-10-21-lightbulb-core-crate-split.md` (§7).
> Effect on this report: the export becomes `apps/desktop/core/` + `packages/claude-plugin/` +
> `packages/shared/fixtures/local-session-frames/` (about 90 files, not 172). §1's
> `apps/desktop/` row, §6's Tauri/Xcode/sidecar build story, and the "fix before announcing"
> items #1, #2 and #5 no longer apply to the public scope. They remain internal work. The sync
> draft must rsync `apps/desktop/core/`, make a core-only `Cargo.lock`, and move the gitleaks
> allowlist path to `core/src/`. The pitch below is no longer accurate, because the desktop app
> is private. The rest of this report (history, licence, audit method) stands.

**Pitch:** "Everything that runs on your computer is open. The hub is hosted."

**Bottom line:** the client can be published as it is, with no code change. Two steps
first: (1) mirror three paths in their monorepo layout, and (2) publish a fresh-history
snapshot, never a subtree split. gitleaks finds no real secret in the tree. The history
has to stay private: it has a personal author email in 21 commits, test-key commits that
are listed in `.gitleaksignore`, and the era when a Vercel bypass token was committed.
Before you announce it, fix three things: the dev origin in the prod app's IPC
capability, the sync trigger (sync on release, not on merge), and a `security@` mailbox.

## Files in this folder

| File | What |
|---|---|
| `README.md` | This report |
| `public-manifest.txt` | All 172 files the export would publish, produced by running the draft workflow's assemble step on `origin/main` |
| `drafts/LICENSE` | Apache-2.0, verbatim text |
| `drafts/NOTICE` | Copyright, per-directory licences, trademark note, sidecar note |
| `drafts/README.md` | The public repo's README: what it is, what it sends and where, how to build, how it talks to the hub |
| `drafts/SECURITY.md` | How to report vulnerabilities, scope, safe harbour |
| `drafts/gitleaks.public.toml` | The public repo's gitleaks config (2 false positives allowlisted, nothing else) |
| `drafts/sync-desktop-client.yml` | Mirror workflow, modelled on `sync-claude-plugin.yml`. Passes `actionlint`. |

---

## 1. Scope

### Public (mirror these paths, keep the monorepo layout)

| Path | Files | Why |
|---|---|---|
| `apps/desktop/` except `SIGNING.md` | 141 | The crate `lightbulb-desktop` (app, CLI, holder, `serve`), icons, the terminal panel assets, tests, the sidecar vendoring script |
| `packages/claude-plugin/` | 18 | `include_str!`'d into the binary by `local_sessions/plugin.rs:56-63` and `local_sessions/enrollment.rs:1194`. Already public (MIT) as `lightbulb-plugin`. |
| `packages/shared/fixtures/local-session-frames/` | 11 JSON | Golden wire frames that the Rust tests `include_str!` (`transport.rs:5389,5482,6991-7000`, `screen.rs:1138`, `resume.rs:336`, `session_states.rs:466`). The backend test `test_local_session_relay.py` reads the same frames. They are the protocol contract, so publishing them is part of the point. |
| root: `LICENSE NOTICE README.md SECURITY.md .gitleaks.toml` | 5 | Stored in the monorepo at `apps/desktop/oss/` and copied to the root by the sync |

Total: 172 files, 4.1 MB (`xterm.js` is 289 KB of it). There is no separate frontend.
`apps/desktop` has no `src/`: the window loads the hosted site (`tauri.conf.json`
`app.windows[0].url`). The only local web code is the terminal panel in
`src-tauri/terminal/` (index.html, terminal.js, vendored xterm.js and the fit add-on with
their MIT licences).

### Excluded or private

| Path | Why |
|---|---|
| `apps/desktop/SIGNING.md` | Internal ops runbook: GitHub secret names, the `isol8-notarize` label, `gh secret set` commands. Nothing secret in it, but it is not for users. Replace it with the "Build it" section of the public README. |
| `.github/workflows/desktop-build.yml` | Stays private. It holds the App Store Connect key id `SZ68D5WBR2` and issuer `7c5f3df2-…` (labelled "public metadata", but no reason to publish them), the self-hosted runner topology, and the signing and notarisation steps. |
| `packages/shared/` except those fixtures | Web/mobile shared TypeScript. The crate does not use it. |
| `apps/backend`, `apps/frontend`, `apps/mobile`, `apps/infra`, everything else | The hub. Private. |

### Does it build outside the monorepo? Verified.

- `Cargo.toml` has **no path dependencies and no `[workspace]`**. All 617 packages in
  `Cargo.lock` come from crates.io (`cargo metadata --locked` resolved them all). `build.rs`
  is only `tauri_build::build()`.
- **`apps/desktop` copied alone fails**: `cargo check --locked --all-targets` stops with
  19 `couldn't read …/packages/claude-plugin/…` and `…/packages/shared/fixtures/…`
  errors. Nine of them are in the non-test lib, so the app itself does not compile.
- **The three-path mirror compiles**: `cargo test --locked --lib --no-run` exits 0, with
  `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}'` as CI uses. There is 1
  warning (an unused import in `tray.rs`). The tests were not run here; CI runs them on
  the self-hosted Mac.
- `plugin.rs:437` (test) reads `CARGO_MANIFEST_DIR/../../../packages/claude-plugin`. The
  mirrored layout satisfies this too. That is why the layout is kept and the `include_str!`
  paths are not rewritten: no code change is needed, and the plugin stays single-sourced.
- `apps/desktop/package.json` (`@lightbulb/desktop`, private) only wraps `cargo tauri`.
  It is harmless outside pnpm. `tauri-action` uses it, so keep it.
- **Sidecars** (`scripts/vendor-sidecars.sh`) are not in the tree (`src-tauri/bin/` is
  gitignored). At build time the script downloads Node 22.23.2 from nodejs.org,
  `openclaw@2026.8.1` and `chrome-devtools-mcp@1.10.1` from the public npm registry, and
  checks pinned sha256 values for three openclaw dist files (`verify-browser-pin.mjs`). It
  needs network, node and npm, and nothing private. Some comments are stale: they mention
  the deleted `openclaw-version.json` and `isol8-openclaw-host-*` package names. These
  are cosmetic.

## 2. Audit

Tools: `gitleaks 8.x` (default rules) on the tree and on the history of the three paths
(261 commits), then targeted greps for the AWS account, 12-digit ids, emails,
`user_`/`org_`/`acct_`/`cus_`/`price_`/`sk_`/`rk_`/`whsec_` ids, internal hostnames,
`isol8`, personal names, signing ids, `/Users/` paths, and customer or agent names.
The fixture terminal captures were decoded and checked for usernames, paths, emails and
URLs.

### Tree findings

| # | File:line | Finding | Verdict |
|---|---|---|---|
| 1 | `src-tauri/terminal/xterm.js:1` | gitleaks `generic-api-key` on `t.FourKeyMap=` | False positive. Already allowlisted in the monorepo. Allowlisted in `gitleaks.public.toml`. |
| 2 | `src-tauri/src/local_sessions.rs:3797` | gitleaks `private-key`: `"-----BEGIN RSA PRIVATE KEY-----\nMIIE…"` | A stub PEM in the `redact` unit test. Fine. Allowlisted by exact regex. |
| 3 | `local_sessions.rs:232, 3686-3687, 3693-3696`; `local_sessions/claude.rs:2307`; `local_sessions.rs:3788` | `sk_live_`, `rk_test_`, `whsec_` prefixes, `AKIAIOSFODNN7EXAMPLE`, `postgres://u:pw@db.internal`, `hunter2` | Redaction rules and fakes assembled with `concat!`. Fine. |
| 4 | `tauri.conf.json:49`; `SIGNING.md:4-5,17,61,88` | `Developer ID Application: Prasiddha Parthsarthy (WZX4U3C22Y)` | The owner's name and Apple Team ID. `codesign -dv` already shows both on every shipped binary. Fine. Optional: move the identity out of `tauri.conf.json` into CI (`APPLE_SIGNING_IDENTITY`) so a self-builder does not hit a missing identity. |
| 5 | `tauri.conf.json:63` | Updater minisign **public** key `8BBE5BE1CF201161` | Public by design. Fine. |
| 6 | `session_pickup.rs:169-170`; `session_continue.rs:618,650` | S3 bucket names `lightbulb-{dev,prod}-openclaw-replicas` (the upload-URL allowlist) | Internal resource names, not secrets. Safe only if the bucket is private and reached through presigned URLs. Acceptable. Optional: have the hub send the allowed host in the pickup ticket instead. |
| 7 | `tauri.conf.json:9,10,24,61`; `capabilities/default.json:10-11`; `lib.rs:49,275,1067,1556-1559`; tests | `dev.`, `api-dev.`, `bv-dev.thelightbulb.company`; prod equivalents | Public API hosts. Fine. The dev defaults are a build problem (§6), not a leak. |
| 8 | `tests/pair_client_test.rs:12,30`; `node_client.rs:1210-1224`; `lib.rs:1552-1553` | Retired `ws(-dev).isol8.co` hosts and an `isol8.co` comment | Test strings and comments only, never dialled. Fine. When `isol8.co` lapses, replace them with `example.invalid` so nobody reads them as live. |
| 9 | `local_sessions.rs:3997-4002` | Display name "Isol8 Admin" in a test | Fine. |
| 10 | `packages/claude-plugin/lightbulb/hooks/band.test.ts:491-512` | "Prasiddha" as a test sender | Already public in `lightbulb-plugin`. Fine. |
| 11 | `tests/fixtures/codex_inline_status.json` | Terminal capture: `/Users/aaaaaaaaaaaaaaaaaaaa/Lightbulb/lb-accept14b-e3de9e`, "Shared with The Lightbulb Company" | The username is already scrubbed and the folder is an acceptance-test name. Fine. `claude_classic_exit.json` contains only Claude Code UI and skill names. |
| 12 | 23 comments (`owner ruling`, `#3912`, `docs/superpowers/specs/…`, `routers/*.py`, `core/services/*.py`) | Internal decision trail and backend module names | They describe architecture, not secrets. Outside readers get dead links, which is acceptable. Do not scrub them in the sync: a rewritten mirror stops matching its `Source:` commit. |
| 13 | `vendor-sidecars.sh:32,87`; `SIGNING.md:9,39` | `isol8` names | Cosmetic residue. |

**Zero hits** for: AWS account `877352799272` (and any real 12-digit id), Clerk
`user_`/`org_` ids, Stripe `acct_`/`cus_`/`price_`/live keys, real customer names, real
emails (all are `example.invalid`, `lightbulb.invalid`, `db.internal` or `git@github.com`),
and internal SSM/Secrets Manager paths. The plugin tree and the 11 shared fixtures are
clean under gitleaks.

### History findings (why history must not be published)

| Finding | Where |
|---|---|
| gitleaks `private-key` | `9816e7ab6` `local_sessions.rs:1957`: the same stub PEM as #2, at an older position |
| Fake Stripe/GCP keys and JWTs in redaction fixtures | `.gitleaksignore`: `17cf8c5cc…` (`claude.rs:2182,2184`; `local_sessions.rs:3325,3326,3329`), `97e077a7d…` (`local_sessions.rs:3401,3409`), `4e1666916…` (`local_sessions.rs:3508`). Fake, but scanners and readers will flag them. |
| Vercel protection-bypass token | `lib.rs:1550-1555` says it "shipped in the binary (and git history) and has been rotated out". It predates the `apps/desktop` import (`7beb79d25`, #279). Rotated, but publishing it would invite questions. |
| Author identity | 21 of 260 commits carry the owner's personal Gmail address as author email. The others use a `users.noreply.github.com` address. |
| Commit messages | 261 messages that name internal incidents, customers' flows and PR numbers |

## 3. History: fresh snapshot, not a subtree split

**Recommendation: snapshot copy (rsync of the export into the public clone), one commit
per sync, with a `Source: <sha>` trailer.** This is exactly the `sync-claude-plugin.yml`
pattern.

| | Snapshot (recommended) | `git subtree split` / `filter-repo` |
|---|---|---|
| Private history leaked | None: the public repo starts at the first sync | All 261 commits: messages, author emails, fake-key commits, the bypass-token era |
| Three source paths | Natural (three rsyncs) | `subtree split` takes one prefix. Three paths need `filter-repo --path … --path …`, a full-history rewrite that you then have to audit commit by commit. |
| Rerunnable and idempotent | Yes. The diff against the public clone is the commit. | A split must be regenerated deterministically on every run, and one bad rewrite is public forever. |
| Provenance | `Source:` trailer, verifiable against the private repo | Native, but only for the commits you were willing to expose |
| Cost | Public `git blame` shows sync commits, not authors | — |

First publish: create the repository empty and private, run the workflow once, check the
tree by hand against `public-manifest.txt` and with gitleaks, and only then make it
public. Do not use `gh repo create --source .` or push any branch of the monorepo to it.

## 4. License and housekeeping

**Recommendation: Apache-2.0 for the client. The plugin stays MIT.**

- Section 3 gives an express patent grant and patent-retaliation clause. That matters
  more for a desktop agent with novel session-sharing mechanics than for a plugin of
  hooks and skills.
- Section 6 says outright that no trademark rights are granted. That is the line we
  want: forks are welcome, but a fork shipped under the Lightbulb name and icon is not.
  Section 4's NOTICE requirement keeps attribution with redistributions.
- It is compatible with every dependency (below). MIT inside an Apache-2.0 repo is
  normal; `NOTICE` says which directories are MIT.
- MIT is acceptable if the owner wants one licence everywhere. The cost is that the
  patent grant and the trademark sentence are lost, and the trademark sentence then has
  to live only in `NOTICE`/README.
- The copyright holder is "Isol8 Inc., doing business as Lightbulb AI", the only legal
  name CLAUDE.md permits. The plugin's LICENSE already uses it.

Drafts: `drafts/LICENSE`, `drafts/NOTICE`, `drafts/README.md`, `drafts/SECURITY.md`.
Housekeeping still open:

- **Create `security@thelightbulb.company`.** CLAUDE.md lists only
  `hello@ support@ tickets@ admin@`. Either make an alias or change `SECURITY.md` to
  `support@`. Also turn on GitHub private vulnerability reporting on the public repo.
- Add a `CONTRIBUTING.md` line: "This is a mirror; we read issues and PRs and land
  accepted changes upstream, and the next sync carries them." Without that line,
  people's PRs will vanish on the next `rsync --delete`. Needs an owner decision on
  whether PRs are accepted at all.
- Optional: a public CI job (`cargo test --lib` on `macos-latest`) in the public repo, so
  outsiders see that the mirror builds.

## 5. Sync workflow

`drafts/sync-desktop-client.yml` (passes `actionlint`; the assemble and scan steps were
run locally on `origin/main`):

- Trigger: push to `main` on `apps/desktop/**`, `packages/claude-plugin/**` or
  `packages/shared/fixtures/local-session-frames/**`, plus `workflow_dispatch` from main.
- It assembles the export: the three rsyncs, `SIGNING.md`, `oss/`, `bin/` and `target/`
  excluded, `apps/desktop/oss/*` copied to the root.
- **Gate before push:** a deny-grep for internal identifiers (account id, `isol8-admin`,
  `/lightbulb/{env}/`, queue names, `PLATFORM_ADMIN_USER_IDS`, `.awsapps.com`), then
  gitleaks with the public config. Gitleaks runs from inside the export because the path
  allowlist is relative. Run with an absolute path, it reported the `xterm.js` false
  positive. A hit fails the job and nothing is pushed. Checked: the scan passes on the
  real export and catches a planted `ghp_` token.
- Push: deploy key `DESKTOP_SYNC_DEPLOY_KEY` (write access on `lightbulb-desktop` only),
  pinned GitHub host key, key kept in `$RUNNER_TEMP`, the guard that never publishes an
  older tree, and an empty-repo first sync (all as the plugin sync does). There is no
  version stamping: the app's version comes from `desktop-build`.
- **Recommended change:** trigger on a successful `desktop-build` dispatch instead of on
  merge. The swap is written in the file header. A merge-triggered mirror publishes a
  security fix's diff hours or days before the signed build that carries it reaches
  users. It also lets the public tree drift from what any binary contains. Tagging the
  public repo `desktop-v0.4.N` at that point would give users a source↔binary pair.

## 6. Building against our hub (outsiders)

Needs: a Mac. The crate is macOS-only in practice: `security-framework` and
`objc2-user-notifications` are unconditional or macOS-gated, Keychain and `launchctl` are
used throughout, and CI builds only `aarch64-apple-darwin`. Also Xcode CLT, Rust stable,
`tauri-cli` 2.x (the lock has `tauri 2.12.1`, `tauri-build 2.7.1`,
`tauri-plugin-updater 2.13.1`), and Node 22 + npm only for the sidecars.

Compile-time variables (`option_env!`, read at build, not at runtime):

| Variable | Default when unset | Prod value |
|---|---|---|
| `LIGHTBULB_API_BASE` (`lib.rs:47`) | `https://api-dev.thelightbulb.company` | `https://api.thelightbulb.company` |
| `LIGHTBULB_WS_URL` (`lib.rs:275,1067`) | `wss://bv-dev.thelightbulb.company/api/v1/gateway/ws` | `wss://bv.thelightbulb.company/api/v1/gateway/ws` |
| `LIGHTBULB_CALLBACK_URL` (`lib.rs:1556`) | `https://dev.thelightbulb.company/auth/desktop-callback` | `https://thelightbulb.company/auth/desktop-callback` |

The config values that CI patches with `jq` (window URL, `devUrl`, `frontendDist`,
updater endpoint, version) need a `--config` overlay. The public README gives one, which
also sets `createUpdaterArtifacts:false` (no `TAURI_SIGNING_PRIVATE_KEY`) and
`signingIdentity:"-"` (ad hoc, no Apple account). Signing and notarisation are **not**
required for a local build.

What breaks or surprises (unverified items are marked):

1. **Defaults are dev.** A plain `cargo tauri build` makes an app that loads
   `dev.thelightbulb.company` and pairs with the dev API. That is our internal environment,
   with the dev Clerk instance. Outsiders will do this by accident. **Fix first (small):**
   make the defaults prod, and have our own dev builds set the dev values (dev desktop
   builds are already local-only per CLAUDE.md).
2. **Same bundle id as the official app.** Both use the same Keychain service
   (`KEYCHAIN_SERVICE` const, `lib.rs:44`), app-support dir, launcher `OPENCLAW_HOME` (both
   hard-coded to `company.thelightbulb.desktop`), single-instance socket, `serve.lock` and
   `~/.local/bin/lightbulb`. Keychain ACLs are bound to the signature that made the item,
   so an ad-hoc build prompts or fails on the official app's keys. Changing `identifier`
   only half-separates them. **Fix later:** derive the service, directories and launcher
   path from the configured identifier.
3. **Updates replace a self-build with ours.** The updater is driven by the page
   (`apply_update_manifest`, `lib.rs:1389`). It verifies the minisign signature against
   our baked-in key, so the hub can offer our signed build and it installs over theirs.
   That is safe, but it surprises users. The README says so.
4. TCC grants (Screen Recording, Automation, notifications) follow the code signature,
   so they are asked again. *Unverified:* whether `UNUserNotificationCenter`
   authorisation works for an ad-hoc-signed bundle.
5. *Unverified:* `signingIdentity: "-"` and the overlay were checked against Tauri 2
   documentation, not built end to end here (only `cargo check`/`test --no-run` was run).
   Do a full `cargo tauri build` with the README's exact steps on a clean Mac before you
   publish the README.
6. The sidecars need network access to nodejs.org and npm, and npm resolves the
   openclaw dependency tree at build time (66 direct deps; versions float apart from the
   pinned sha256 values of three files). So bundled-browser builds are not bit-for-bit
   reproducible. Optional: commit a `package-lock.json` per sidecar.
7. Some tests need a real pty (`--test holder --test cli`) or an installed openclaw
   bundle (`tests/*.mjs`, `*.cjs`).
8. The hub does not attest the client. A self-build pairs like ours (`/desktop/pair`
   takes a Clerk token plus public keys). This is what makes "build it yourself and use
   it" true. It also means the hub must not trust anything a client claims beyond its key
   (see §7).

## 7. Risks

### What an attacker learns from the source

- **The whole client↔hub protocol:** pairing (`/api/v1/desktop/pair`,
  `DELETE /desktop/devices/{id}`), the device-code join (`/auth/device/start|poll`), the
  device-token shape (EdDSA, ~90 s, self-minted by the Keychain key, `device_token.rs`),
  the relay frame set (`local.device.heartbeat[.ack]`, `local.session.{register,events,
  screen,input,watch,command,presence,ended,receipt}`), the OpenClaw-derived node protocol
  (already public upstream), `/messaging/native-session-decisions`,
  `/public/session-copies/redeem`, the MCP endpoint, and the S3 host allowlist.
- **The local attack surface:** unix sockets (holders, the Claude mod socket, the app's
  command socket) authenticated by peer PID and process ancestry, the fixed loopback CDP
  port of the session browser, the loopback terminal-panel server with its one-time key,
  and the loopback WSS proxy. Also the list of 33 IPC commands the hosted page can call,
  and the redaction regexes (which tells an attacker what is *not* redacted).

**Why that is acceptable:** none of it is secret today. A Tauri binary carries every
route string and IPC command name in clear text, and the node protocol is upstream
OpenClaw. The real controls are server-side auth, the device key bound at pairing, peer
credentials on local sockets, native confirmations and signature checks, and none of
these depend on the code being hidden. Same-user local malware already wins without the
source. The trust story only works if the security does not depend on obscurity, and
reviewers gain more from the source than attackers do.

### Fix or check before announcing (ordered)

1. **The prod app grants IPC to the dev origin.** `capabilities/default.json:10` lists
   `https://dev.thelightbulb.company/*` beside prod. `desktop-build.yml` patches
   `tauri.conf.json` but not the capability, so the shipped prod app lets any script on
   the dev site (less hardened, preview deploys) call all 33 commands. Patch the
   capability in the prod build to the prod origin only. One `jq` line.
2. **XSS on the hub reaches the Mac.** Remote-origin IPC includes `terminal_start`,
   `session_pickup`, `shell_integration_set`, `export_claude_context`, `send_auth_token`,
   `apply_update_manifest`. The dangerous configuration changes already need a native
   dialog (`local_mcp_set` `lib.rs:849`, `file_access_add_path` `lib.rs:1266`,
   `desktop_permissions_set` `desktop_permissions.rs:179`). Review the others knowing
   the list will be public. Two examples: `terminal_start` with a page-chosen prompt in
   an approved folder; `shell_integration_set` editing `~/.zshrc`. Each should either
   need a native confirm or be harmless when a hostile page calls it.
3. **Device-code phishing.** `/auth/device/start` is unauthenticated by design. An
   attacker can start a join with *their own* keys and talk a victim into approving the
   code, and the attacker's machine is then paired into the victim's workspace. Check
   that the verify page shows the machine name and a clear "a computer will get access
   to your workspace" confirm, that codes expire fast, and that start is rate-limited.
   This is a backend check; the client code makes the flow obvious.
4. **The sync trigger.** Publish on release, not on merge (see §5).
5. **The updater's `dangerousInsecureTransportProtocol: true`** (`tauri.conf.json:64`).
   `lib.rs:1377-1387` justifies it: the manifest is served on loopback and the bundle is
   minisign-verified. Reviewers will flag it. Check two things: that a page-supplied
   manifest cannot downgrade (Tauri's comparator installs only a higher version), and
   that a non-HTTPS bundle `url` is refused. Add a code comment pointing to the check.
6. Optional hygiene: dev defaults → prod (§6.1), identifier-derived paths (§6.2), the
   signing identity moved into CI (§2 #4), placeholder hosts (§2 #8).

### Licensing of dependencies and sidecars

No `cargo-deny` is installed here, so this summary comes from `cargo metadata --locked`
(617 packages, all targets). Run `cargo deny check licenses` in CI to keep it true.

| Licence (as declared) | Crates | Note |
|---|---|---|
| MIT / Apache-2.0 / dual and tri variants | ~560 | Permissive |
| Unicode-3.0 | 18 (+2 combined) | ICU data. Permissive, attribution. |
| BSD-2/3-Clause, ISC, Zlib, 0BSD, CC0, Unlicense, BSL-1.0 (some in OR-sets) | ~30 | Permissive |
| **MPL-2.0** | 5: `cssparser`, `cssparser-macros`, `dtoa-short`, `selectors` (Tauri/wry HTML parsing), `option-ext` (`dirs`) | File-level weak copyleft. Fine unmodified. Offer source for the MPL files, which crates.io already does. |
| **CDLA-Permissive-2.0** | 2: `webpki-roots`, `webpki-root-certs` | Mozilla CA data. Permissive. |
| `MIT OR Apache-2.0 OR LGPL-2.1-or-later` | 2 | Pick MIT |
| none declared | 1: `lightbulb-desktop` itself | Add `license = "Apache-2.0"` and `repository` to `Cargo.toml` before publishing |

Nothing GPL or AGPL is linked into the binary.

Bundled sidecars and assets (in the shipped `.app`, not in the source):

| Component | Licence | Obligation |
|---|---|---|
| Node.js 22.23.2 binary | MIT + bundled third-party (OpenSSL Apache-2.0, ICU, …) | Include Node's LICENSE in the bundle |
| `openclaw@2026.8.1` + ~66 deps | MIT (top level) | Per-package notices |
| `@img/sharp-libvips-darwin-*` (via openclaw → sharp) | **LGPL-3.0-or-later** | We already redistribute it today. LGPL needs the licence text, an offer of the libvips source, and that users can replace the library (it is a separate dylib, so they can). Publishing our source does not change this, but a public repo invites the question. Generate a third-party notices file at vendor time. |
| `chrome-devtools-mcp@1.10.1` | Apache-2.0 | Include licence and NOTICE |
| `xterm.js`, `addon-fit` (vendored in source) | MIT | Licence files already beside them |

**Recommended:** a `scripts/third-party-notices.sh` step in `vendor-sidecars.sh` that
collects every `node_modules/*/LICENSE*` into `bin/THIRD_PARTY_NOTICES.txt`, plus
`cargo about` (or `cargo deny`) output for the crates, shipped in `Contents/Resources`.
This obligation exists today whether or not the source is public.

## Owner decisions needed

1. Apache-2.0 (recommended) or MIT.
2. The public repo name (`TheLightbulbCompany/lightbulb-desktop` is assumed) and whether
   it accepts PRs.
3. `security@` alias, or `support@`.
4. Sync on release (recommended) or on merge.
5. Approval to land the pre-announcement fixes in §7 (1, 2 and 5 are client-side; 3 is a
   backend check).
