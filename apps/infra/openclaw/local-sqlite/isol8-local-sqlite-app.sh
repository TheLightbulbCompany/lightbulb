#!/bin/sh
# isol8-local-sqlite-app.sh — the litestream -exec child: doctor then gateway.
#
# exec makes the gateway the actual child process, so litestream's SIGTERM
# forwarding reaches it directly. litestream performs its final replica sync
# only when the child exits cleanly after a forwarded signal — the gateway's
# 0 exit on SIGTERM is load-bearing for shutdown durability.
timeout --kill-after=30 300 node /app/openclaw.mjs doctor --fix --yes --non-interactive < /dev/null \
  || echo "[o6-app] openclaw doctor --fix did not finish cleanly; continuing to gateway"
exec node /app/openclaw.mjs gateway --port 18789 --bind lan
