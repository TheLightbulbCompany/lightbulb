#!/usr/bin/env bash
# Vendor sidecars for the Lightbulb desktop browser node.
# Produces: src-tauri/bin/ containing
#   - node-<triple>                               (per-arch Mach-O binary)
#   - openclaw-host-<triple>/                     (per-arch npm install)
#   - lightbulb-browser-service-<triple>          (per-arch launcher shim)
#   - chrome-devtools-mcp/                        (pinned npm install, any arch)
#   - lightbulb                                   (the network engine, arm64)
#
# All of them ship as bundle RESOURCES (Contents/Resources/bin/), never
# externalBin: a script in Contents/MacOS is signed via xattrs, which the
# updater's tarball drops, breaking the app's seal (see lib.rs
# resolve_node_host_binary).
#
# Per-arch npm installs are required because openclaw ships many
# arch-specific native addons via npm optionalDependencies (node-pty,
# clipboard, sharp, canvas, koffi, sqlite-vec). Sharing one
# node_modules across architectures misresolves these.
#
# NOTE: this does NOT mirror OpenClaw's own macOS Swift app, despite what
# this comment used to claim. That app IS the node -- it speaks the gateway
# protocol natively and spawns `openclaw node worker`, a hidden private JSONL
# worker over stdio, only to execute Node-side commands. We instead run the
# full headless `openclaw node run` and let it be the node. Ours is the
# supported public shape; theirs is an internal contract. The consequence is
# that we own a persistent node-host state dir (identity, node config, SQLite)
# that their app never materializes -- so upstream's answer to a state
# migration ("tell the user to run openclaw doctor --fix") is not available
# to us. We have no CLI on the user's PATH. See subprocess.rs.
set -euo pipefail

# Pin tracks the deployed container image's upstream line -- that is
# openclaw-version.json's `dev.tag`/`prod.tag` (both 2026.8.1-isol8.1-*),
# NOT its `tag` field, which is only the legacy public bootstrap fallback.
# Keeping desktop <-> container in sync avoids the protocol-version
# (PR #660/#662) and dist-chunk drift bugs.
#
# We vendor from PUBLIC npm, so this is vanilla openclaw -- none of the
# fork's carried patches. That is fine for a node host (the patches are
# gateway/agent-side) but it is a real divergence: desktop and container
# run the same VERSION, not the same BUILD.
#
# NODE_VERSION is bound by the vendored openclaw's package.json engines
# field, which is NOT advisory -- assertSupportedRuntime() errors and
# exits at startup. 2026.5.28 wanted >=22.19.0; 2026.8.1 wants
# >=22.22.3 <23. npm install does not enforce it (no engine-strict), so
# a mismatch fails at runtime inside the packaged app, not at build.
NODE_VERSION="22.23.2"
OPENCLAW_VERSION="2026.8.1"
# The session browser's MCP server (native_mcp.rs browser_command), run by the
# bundled node above. Pinned: `npx …@latest` re-resolved it from npm on every
# session start. It has no dependencies and no native addons (pure JS, so one
# arch-independent copy and nothing for the pre-sign step to sign); engines
# wants node ^22.12.0, which NODE_VERSION satisfies.
CHROME_DEVTOOLS_MCP_VERSION="1.10.1"
# The network engine (TheLightbulbCompany/cli): the one `lightbulb` MCP server, bundled as
# Contents/Resources/bin/lightbulb (engine.rs installs it when none or an older one is installed).
ENGINE_TAG="v0.9.8"
ENGINE_SHA256="aebc3c88361e80ff7cd49f50dcbf00f981cafa13769bcb8276947f6910def863"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAURI_DIR="$(dirname "$SCRIPT_DIR")"
BIN_DIR="$TAURI_DIR/bin"
TMP_DIR="$TAURI_DIR/.sidecar-tmp"

rm -rf "$TMP_DIR"
mkdir -p "$BIN_DIR" "$TMP_DIR"

vendor_arch() {
    local TRIPLE="$1"     # aarch64-apple-darwin | x86_64-apple-darwin
    local NODE_ARCH="$2"  # arm64 | x64
    local NPM_CPU="$3"    # arm64 | x64

    echo "==> Vendoring $TRIPLE (node-$NODE_ARCH, npm --cpu=$NPM_CPU)"

    # Node.js for this arch.
    local NODE_TARBALL="node-v${NODE_VERSION}-darwin-${NODE_ARCH}.tar.xz"
    local NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}"
    curl -fsSL -o "$TMP_DIR/$NODE_TARBALL" "$NODE_URL"
    tar -xf "$TMP_DIR/$NODE_TARBALL" -C "$TMP_DIR"
    cp "$TMP_DIR/node-v${NODE_VERSION}-darwin-${NODE_ARCH}/bin/node" \
       "$BIN_DIR/node-${TRIPLE}"
    chmod +x "$BIN_DIR/node-${TRIPLE}"

    # Arch-specific openclaw install. --cpu/--os/--libc force npm to
    # resolve optional deps for the target arch, regardless of the
    # host the vendor script runs on.
    local HOST_DIR="$BIN_DIR/openclaw-host-${TRIPLE}"
    mkdir -p "$HOST_DIR"
    cat > "$HOST_DIR/package.json" <<EOF
{
  "name": "isol8-openclaw-host-${TRIPLE}",
  "private": true,
  "version": "0.0.0",
  "dependencies": {
    "openclaw": "${OPENCLAW_VERSION}"
  }
}
EOF
    (
        cd "$HOST_DIR"
        # Use the arch-matching node for the install pass. npm uses
        # its own runtime's arch for platform-dep resolution unless
        # overridden, so set explicitly.
        PATH="$BIN_DIR:$PATH" \
        npm install \
            --production \
            --no-audit \
            --no-fund \
            --cpu="${NPM_CPU}" \
            --os=darwin
    )
    node "$SCRIPT_DIR/verify-browser-pin.mjs" "$HOST_DIR/node_modules/openclaw"

    local BEFORE_KB
    BEFORE_KB="$(du -sk "$HOST_DIR" | cut -f1)"
    host_prune_list "$HOST_DIR" "$NPM_CPU" | while IFS= read -r p; do rm -rf "$p"; done
    # Run the real host (node run + each command family Hermes uses), then make sure
    # nothing it did brought a pruned path back.
    # The smoke runs the vendored node, so it needs a host of the same arch.
    if [ "$NPM_CPU" = "$(uname -m | sed 's/x86_64/x64/')" ]; then
        "$BIN_DIR/node-${TRIPLE}" "$SCRIPT_DIR/smoke-node-host.mjs" "$BIN_DIR/node-${TRIPLE}" "$HOST_DIR"
    else
        echo "==> skipping the openclaw-host-${TRIPLE} smoke on a $(uname -m) host"
    fi
    if [ -n "$(host_prune_list "$HOST_DIR" "$NPM_CPU")" ]; then
        echo "pruned openclaw-host paths are present:" >&2
        host_prune_list "$HOST_DIR" "$NPM_CPU" | head -20 >&2
        exit 1
    fi
    echo "==> openclaw-host-${TRIPLE}: ${BEFORE_KB} KB -> $(du -sk "$HOST_DIR" | cut -f1) KB"
}

# Paths in the openclaw install that the node host never loads. Printed one per line,
# existing ones only; the vendor step deletes them and then fails if any are still here.
# The PR that added this list carries the proof of non-use for each entry; re-prove
# before widening it:
#  - claude-agent-sdk-darwin-*: the bundled `claude` binary. Only the Agent SDK's query()
#    spawns it, and only extensions/anthropic/agent-sdk.runtime.js imports the SDK, for
#    agent turns (cli-backend, executionMode "agent"). A node host runs no agent turns.
#  - cua-driver-darwin-*: loaded only by cua-computer's loadCuaDriverSdk() for computer
#    actions. The node host declares no computer.* command and the backend grants none.
#  - openclaw/docs: read only by docs-path (agent prompts, `openclaw docs`), which returns
#    null when the dir is absent.
#  - tree-sitter-bash native build (prebuilds, src, bindings, binding.gyp): openclaw loads
#    only tree-sitter-bash.wasm through web-tree-sitter.
#  - other platforms' native addons: fs-safe and pi-tui pick theirs by process.platform/arch.
#  - source maps and type declarations: nothing enables source maps; Node never loads a
#    .d.ts. TypeScript's own lib .d.ts files are kept (its compiler reads them).
host_prune_list() {
    local NM="$1/node_modules" CPU="$2" p
    {
        printf '%s\n' "$NM"/@anthropic-ai/claude-agent-sdk-darwin-* \
            "$NM"/@trycua/cua-driver-darwin-* \
            "$NM/openclaw/docs" \
            "$NM"/tree-sitter-bash/{prebuilds,src,bindings,binding.gyp} \
            "$NM/@earendil-works/pi-tui/native/win32"
        for p in "$NM"/openclaw/dist/native/* "$NM"/@openclaw/fs-safe/dist/native/* \
                 "$NM"/@earendil-works/pi-tui/native/darwin/prebuilds/*; do
            [ "${p##*/}" = "darwin-${CPU}" ] || printf '%s\n' "$p"
        done
        find "$NM" -path "$NM/typescript" -prune -o -type f \
            \( -name '*.d.ts' -o -name '*.d.mts' -o -name '*.d.cts' \
               -o \( -name '*.map' ! -name '*.wasm.map' \) \) -print
    } | while IFS= read -r p; do if [ -e "$p" ]; then printf '%s\n' "$p"; fi; done
}

# Write a per-triple launcher shim. It is a resource like node +
# openclaw-host, so it always sits beside them in the same `bin/`
# (Contents/Resources/bin/ packaged, target/<profile>/bin/ in dev).
write_launcher() {
    local TRIPLE="$1"
    local LAUNCHER="$BIN_DIR/lightbulb-browser-service-${TRIPLE}"
    cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/usr/bin/env bash
set -euo pipefail
ASSETS="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
# Target Tauri's loopback WSS proxy. Tauri sets LIGHTBULB_PROXY_PORT,
# OPENCLAW_STATE_DIR, and OPENCLAW_GATEWAY_TOKEN before spawning.
: "\${LIGHTBULB_PROXY_PORT:?LIGHTBULB_PROXY_PORT must be set by Tauri supervisor}"
# Isolate the bundled openclaw's config dir from the user's
# personal ~/.openclaw/. Without OPENCLAW_HOME, both the CLI install
# and our bundled copy read the same ~/.openclaw/openclaw.json — so
# user-installed plugins (e.g. a Discord bot referencing
# @buape/carbon) make our bundled node_modules fail to resolve
# imports it never had. Putting our state under macOS' standard
# per-app Application Support dir gives full isolation; users keep
# their CLI openclaw setup intact.
export OPENCLAW_HOME="\${HOME}/Library/Application Support/company.thelightbulb.desktop/openclaw"
mkdir -p "\$OPENCLAW_HOME"
exec "\$ASSETS/node-${TRIPLE}" --disable-warning=ExperimentalWarning \\
    "\$ASSETS/openclaw-host-${TRIPLE}/node_modules/openclaw/openclaw.mjs" \\
    node run --host 127.0.0.1 --port "\$LIGHTBULB_PROXY_PORT" "\$@"
LAUNCHER_EOF
    chmod +x "$LAUNCHER"
}

# Arch-independent: installed once into bin/chrome-devtools-mcp/.
# --ignore-scripts: nothing from the registry runs at build time.
vendor_chrome_devtools_mcp() {
    local DIR="$BIN_DIR/chrome-devtools-mcp"
    echo "==> Vendoring chrome-devtools-mcp@${CHROME_DEVTOOLS_MCP_VERSION}"
    rm -rf "$DIR"
    mkdir -p "$DIR"
    cat > "$DIR/package.json" <<EOF
{
  "name": "lightbulb-chrome-devtools-mcp",
  "private": true,
  "version": "0.0.0",
  "dependencies": {
    "chrome-devtools-mcp": "${CHROME_DEVTOOLS_MCP_VERSION}"
  }
}
EOF
    (cd "$DIR" && npm install --omit=dev --ignore-scripts --no-audit --no-fund)
    local ENTRY="$DIR/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js"
    test -f "$ENTRY" || { echo "chrome-devtools-mcp entry missing: $ENTRY" >&2; exit 1; }
    local GOT
    GOT="$(node -p "require('$DIR/node_modules/chrome-devtools-mcp/package.json').version")"
    [ "$GOT" = "$CHROME_DEVTOOLS_MCP_VERSION" ] || { echo "chrome-devtools-mcp is $GOT, pinned $CHROME_DEVTOOLS_MCP_VERSION" >&2; exit 1; }
    # Loaded with node --env-file (native_mcp.rs BROWSER_ENV): no npm update check, no usage report.
    printf 'CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS=1\nCHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=1\n' > "$DIR/mcp.env"
}

# The engine release is arm64 only; its tools.json must match the backend's copy.
vendor_engine() {
    echo "==> Vendoring the lightbulb engine ${ENGINE_TAG}"
    local TARBALL="$TMP_DIR/lightbulb-macos-arm64.tar.gz"
    curl -fsSL -o "$TARBALL" "https://github.com/TheLightbulbCompany/cli/releases/download/${ENGINE_TAG}/lightbulb-macos-arm64.tar.gz"
    echo "${ENGINE_SHA256}  ${TARBALL}" | shasum -a 256 -c -
    tar -xzf "$TARBALL" -C "$BIN_DIR" lightbulb
    chmod +x "$BIN_DIR/lightbulb"
    # The arm64 binary runs only on an arm64 Mac: an Intel host vendors it unchecked.
    if [ "$(uname -m)" = "arm64" ]; then
        "$BIN_DIR/lightbulb" mcp --tools-json | cmp - "$TAURI_DIR/../../backend/core/services/network_tools.json"
    fi
}

# CI passes a single target triple; local dev (no args) vendors both
# so `cargo tauri dev` works on either arch without re-running.
TARGET="${1:-}"
case "$TARGET" in
    aarch64-apple-darwin)
        vendor_arch "aarch64-apple-darwin" "arm64" "arm64"
        write_launcher "aarch64-apple-darwin"
        ;;
    x86_64-apple-darwin)
        vendor_arch "x86_64-apple-darwin" "x64" "x64"
        write_launcher "x86_64-apple-darwin"
        ;;
    "")
        vendor_arch "aarch64-apple-darwin" "arm64" "arm64"
        vendor_arch "x86_64-apple-darwin" "x64" "x64"
        write_launcher "aarch64-apple-darwin"
        write_launcher "x86_64-apple-darwin"
        ;;
    *)
        echo "usage: $0 [aarch64-apple-darwin | x86_64-apple-darwin]" >&2
        exit 1
        ;;
esac
vendor_chrome_devtools_mcp
vendor_engine

rm -rf "$TMP_DIR"
echo "==> Sidecars vendored at $BIN_DIR"
ls -1 "$BIN_DIR"
