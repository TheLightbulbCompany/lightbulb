#!/bin/zsh
# Start a held session with nothing attached: ./start.sh claude   or   ./start.sh codex
DIR="${0:A:h}"
cd "$DIR/work" || exit 1
SOCK=/private/tmp/lbh-manual.sock
nohup env -i HOME="$HOME" PATH="$PATH" USER="$USER" SHELL="$SHELL" TMPDIR="$TMPDIR" \
  "$DIR/holder/target/release/lb-holder-spike" "$SOCK" "$DIR/logs-$1" -- "$@" >/dev/null 2>&1 &
disown
for i in {1..20}; do [[ -S $SOCK ]] && break; sleep 0.25; done
[[ -S $SOCK ]] && echo "holder started for: $*  (socket $SOCK)" || { echo "holder did not start"; exit 1; }
