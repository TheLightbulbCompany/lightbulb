#!/bin/sh
# Lightbulb installer. Read it first: https://thelightbulb.company/install
#
#   curl -fsSL https://thelightbulb.company/install | sh
#   curl -fsSL https://thelightbulb.company/install | sh -s -- --no-join
#
# What it does, and nothing else:
#   1. Checks this is a Mac with Apple silicon.
#   2. Downloads the latest signed Lightbulb.app from thelightbulb.company.
#      The `lightbulb` command is the app's own binary: macOS shows its
#      Allow/Decline notifications only for a signed app bundle.
#   3. Verifies the code signature, the signing team and Gatekeeper.
#   4. Installs to ~/Applications/Lightbulb.app, or replaces
#      /Applications/Lightbulb.app in place when that is where it already is.
#   5. Links ~/.local/bin/lightbulb to the app's binary.
#   6. Runs `lightbulb join` to connect this Mac (skip with --no-join).
# It never uses sudo, and running it again only upgrades.
set -eu

TEAM_ID=WZX4U3C22Y                       # Apple Developer ID team that signs Lightbulb
BUNDLE_ID=company.thelightbulb.desktop
BASE=${LIGHTBULB_URL:-https://thelightbulb.company}
BIN_DIR=${LIGHTBULB_BIN_DIR:-$HOME/.local/bin}   # override for testing only

say() { printf '%s\n' "$*"; }
die() { printf 'lightbulb install: %s\n' "$*" >&2; exit 1; }

main() {
  join=1
  for arg in "$@"; do
    case $arg in
      --no-join) join=0 ;;
      -h|--help) say "usage: curl -fsSL $BASE/install | sh [-s -- --no-join]"; exit 0 ;;
      *) die "unknown option: $arg (try --no-join)" ;;
    esac
  done

  [ "$(uname -s)" = Darwin ] || die "the Lightbulb CLI runs on macOS only; this is $(uname -s)."
  # Under Rosetta `uname -m` says x86_64; the sysctl reports the real chip.
  [ "$(uname -m)" = arm64 ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] \
    || die "Lightbulb needs a Mac with Apple silicon (M1 or later)."

  # Where the app goes: an existing /Applications copy is upgraded in place.
  if [ -n "${LIGHTBULB_APP_DIR:-}" ]; then dir=$LIGHTBULB_APP_DIR
  elif [ -d /Applications/Lightbulb.app ]; then dir=/Applications
  else dir=$HOME/Applications
  fi
  app=$dir/Lightbulb.app
  exe=$app/Contents/MacOS/lightbulb-desktop
  mkdir -p "$dir"
  [ -w "$dir" ] || die "cannot write to $dir. Drag Lightbulb.app there from the DMG, or move it to ~/Applications and run this again."

  if [ -x "$exe" ] && up_to_date "$app"; then
    say "Lightbulb $(version_of "$app") is already the latest."
  else
    install_app "$dir" "$app"
  fi

  link "$exe"

  if [ "$join" = 1 ]; then
    say "Connecting this Mac to your Lightbulb workspace..."
    # `cli join`: the binary's own entry for `lightbulb join`, independent of PATH.
    "$exe" cli join </dev/null
  else
    say "Run \`lightbulb join\` to connect this Mac."
  fi
}

version_of() { /usr/bin/defaults read "$1/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo 0; }

# The updater endpoint answers 204 when nothing newer is published.
up_to_date() {
  code=$(curl -fsS -o /dev/null -w '%{http_code}' "$BASE/api/desktop/update/darwin-aarch64/$(version_of "$1")" || echo 0)
  [ "$code" = 204 ]
}

install_app() {
  dir=$1 app=$2
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/lightbulb.XXXXXX")
  # Stage beside the target so the final step is a rename on one volume.
  stage=$(mktemp -d "$dir/.lightbulb-install.XXXXXX")
  trap 'rm -rf "$tmp" "$stage"' EXIT
  trap 'exit 130' INT TERM

  say "Downloading Lightbulb (about 270 MB)..."
  curl -fL --retry 3 --progress-bar -o "$tmp/Lightbulb.app.tar.gz" \
    "$BASE/api/download/desktop?arch=aarch64&kind=bundle"
  tar -xzf "$tmp/Lightbulb.app.tar.gz" -C "$stage"
  new=$stage/Lightbulb.app
  [ -d "$new" ] || die "the download did not contain Lightbulb.app."

  say "Verifying the signature..."
  /usr/bin/codesign --verify --deep --strict "$new" || die "the code signature is not valid."
  info=$(/usr/bin/codesign -dv "$new" 2>&1)
  printf '%s\n' "$info" | grep -qx "TeamIdentifier=$TEAM_ID" || die "the app is not signed by Lightbulb (team $TEAM_ID)."
  printf '%s\n' "$info" | grep -qx "Identifier=$BUNDLE_ID" || die "the app is not $BUNDLE_ID."
  /usr/sbin/spctl --assess --type execute "$new" || die "Gatekeeper did not accept the app."

  # Swap: the old copy moves aside, the new one takes its place, the old one is deleted.
  # A running `lightbulb serve` notices its binary changed and restarts on the new one.
  [ -e "$app" ] && mv "$app" "$stage/Lightbulb.old.app"
  mv "$new" "$app"
  say "Installed Lightbulb $(version_of "$app") to $app"
}

# ~/.local/bin/lightbulb -> the app binary. Only a missing link or one of ours is replaced.
link() {
  target=$(readlink "$BIN_DIR/lightbulb" 2>/dev/null || true)
  if [ -e "$BIN_DIR/lightbulb" ] && [ ! -L "$BIN_DIR/lightbulb" ]; then
    say "Note: $BIN_DIR/lightbulb is not a Lightbulb link; leaving it."
    return
  fi
  case $target in
    ''|*/lightbulb-desktop) mkdir -p "$BIN_DIR" && ln -sfn "$1" "$BIN_DIR/lightbulb" ;;
    *) say "Note: $BIN_DIR/lightbulb points at $target; leaving it."; return ;;
  esac
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "Add ~/.local/bin to your PATH: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
  esac
}

# Everything above only defines functions: a download cut short runs nothing.
main "$@"
