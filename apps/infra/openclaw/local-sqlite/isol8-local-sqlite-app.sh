#!/bin/sh
# isol8-local-sqlite-app.sh — the litestream -exec child: doctor then gateway.
#
# exec makes the gateway the actual child process, so litestream's SIGTERM
# forwarding reaches it directly. litestream performs its final replica sync
# only when the child exits cleanly after a forwarded signal — the gateway's
# 0 exit on SIGTERM is load-bearing for shutdown durability.
# doctor --fix is kept: it creates missing session-store dirs, refreshes the
# plugin registry and auto-enables the configured model/browser. But its BUDGET
# is capped at 120s, down from 300s.
#
# The 300s budget was never a completion estimate -- a healthy container
# finishes doctor in ~50s. It only ever binds on a container that is already
# CPU-starved, and there it does not finish at 300s either: it is killed, having
# delivered none of the repairs above, after monopolising five minutes of a
# single vCPU and dirtying every restored database for litestream to drain --
# which is the same window the gateway then has to win its SQLite locks in.
# 120s keeps every repair on boxes that can finish, and returns three minutes of
# CPU on the boxes that cannot. Failure here has always been non-fatal (below).
timeout --kill-after=15 120 node /app/openclaw.mjs doctor --fix --yes --non-interactive < /dev/null \
  || echo "[o6-app] openclaw doctor --fix did not finish cleanly; continuing to gateway"
exec node /app/openclaw.mjs gateway --port 18789 --bind lan
