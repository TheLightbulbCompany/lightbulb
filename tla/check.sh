#!/usr/bin/env sh
# Model-check every tla/*.cfg with TLC.  Usage: tla/check.sh [Module.cfg ...]
# A config named Module_Variant.cfg checks Module.tla.  Needs Java 17+.
set -eu
cd "$(dirname "$0")"
VERSION=1.7.4
SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
JAR="${TLA2TOOLS:-${TMPDIR:-/tmp}/tla2tools-$VERSION.jar}"
[ -f "$JAR" ] || curl -sSfL -o "$JAR" "https://github.com/tlaplus/tlaplus/releases/download/v$VERSION/tla2tools.jar"
if command -v sha256sum >/dev/null 2>&1; then echo "$SHA256  $JAR" | sha256sum -c - >/dev/null
else echo "$SHA256  $JAR" | shasum -a 256 -c - >/dev/null; fi
[ $# -gt 0 ] || set -- *.cfg
status=0
for cfg in "$@"; do
  module="${cfg%.cfg}"; module="${module%%_*}"
  echo "== TLC $cfg -> $module.tla"
  java -XX:+UseParallelGC -cp "$JAR" tlc2.TLC -workers auto -cleanup -config "$cfg" "$module.tla" || status=1
done
exit $status
