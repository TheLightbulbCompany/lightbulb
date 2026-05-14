#!/usr/bin/env bash
# Vendor sidecars for the Isol8 desktop browser node.
# Produces: src-tauri/bin/ containing
#   - node-<triple>                               (per-arch Mach-O binary)
#   - openclaw-host-<triple>/                     (per-arch npm install)
#   - isol8-browser-service-universal-apple-darwin (single dispatch shim)
#
# Tauri's --target universal-apple-darwin bundler looks for exactly one
# externalBin file named `<base>-universal-apple-darwin`. Bash shims
# can't be lipo'd into a fat Mach-O, so we ship ONE shim that detects
# $(uname -m) at runtime and execs the matching node + openclaw-host.
#
# Per-arch npm installs are required because openclaw ships many
# arch-specific native addons via npm optionalDependencies (node-pty,
# clipboard, sharp, canvas, koffi, sqlite-vec). Sharing one
# node_modules across architectures misresolves these.
#
# Mirrors OpenClaw's own macOS Swift integration: a native host
# spawns the CLI (`openclaw node run`) and hits it over loopback.
set -euo pipefail

# openclaw@2026.4.x requires Node >=22.14 (see its package.json
# engines). Node 22 is the current LTS line.
NODE_VERSION="22.14.0"
OPENCLAW_VERSION="2026.4.5"

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
}

# Write a per-triple launcher shim that finds node + openclaw-host at
# runtime. Asset locations differ per build mode:
#
#   - dev (`cargo tauri dev`): shim + siblings co-located in target/debug/
#   - packaged (`tauri build`): shim lands in Contents/MacOS/, resources
#     in Contents/Resources/bin/ (Tauri preserves the relative `bin/`
#     directory from the resources glob in tauri.conf.json:
#     `bin/node-*`, `bin/openclaw-host-*/**/*`).
#
# Check the packaged Resources/bin path first (most installs), then a
# flat Resources/ fallback (older bundle layouts), finally co-located.
write_launcher() {
    local TRIPLE="$1"
    local LAUNCHER="$BIN_DIR/isol8-browser-service-${TRIPLE}"
    cat > "$LAUNCHER" <<LAUNCHER_EOF
#!/usr/bin/env bash
set -euo pipefail
HERE="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
if [ -f "\$HERE/../Resources/bin/node-${TRIPLE}" ]; then
    ASSETS="\$HERE/../Resources/bin"
elif [ -f "\$HERE/../Resources/node-${TRIPLE}" ]; then
    ASSETS="\$HERE/../Resources"
else
    ASSETS="\$HERE"
fi
# Isolate the bundled openclaw's config dir from the user's
# personal ~/.openclaw/. Without OPENCLAW_HOME, both the CLI install
# and our bundled copy read the same ~/.openclaw/openclaw.json — so
# user-installed plugins (e.g. a Discord bot referencing
# @buape/carbon) make our bundled node_modules fail to resolve
# imports it never had. Putting our state under macOS' standard
# per-app Application Support dir gives full isolation; users keep
# their CLI openclaw setup intact.
export OPENCLAW_HOME="\${HOME}/Library/Application Support/co.isol8.desktop/openclaw"
mkdir -p "\$OPENCLAW_HOME"
exec "\$ASSETS/node-${TRIPLE}" \\
    "\$ASSETS/openclaw-host-${TRIPLE}/node_modules/openclaw/openclaw.mjs" \\
    node run --host 127.0.0.1 --port 18789 "\$@"
LAUNCHER_EOF
    chmod +x "$LAUNCHER"
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

rm -rf "$TMP_DIR"
echo "==> Sidecars vendored at $BIN_DIR"
ls -1 "$BIN_DIR"
