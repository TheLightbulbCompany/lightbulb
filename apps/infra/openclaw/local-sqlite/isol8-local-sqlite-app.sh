#!/bin/sh
# isol8-local-sqlite-app.sh — the litestream -exec child: doctor then gateway.
#
# exec makes the gateway the actual child process, so litestream's SIGTERM
# forwarding reaches it directly. litestream performs its final replica sync
# only when the child exits cleanly after a forwarded signal — the gateway's
# 0 exit on SIGTERM is load-bearing for shutdown durability.
# doctor --fix gets a 300s budget, deliberately generous.
#
# A shorter cap was tried and reverted. The reasoning that motivated it was
# real -- on a CPU-starved container doctor burns its whole budget and is then
# killed having delivered none of its repairs -- but capping is the wrong
# lever, because the failure mode it creates is worse than the one it avoids.
#
# Measured across 9 real fleet boots, doctor takes 52-84s. A 120s cap has only
# ~1.4x headroom over the worst of those, and doctor slows down as a tenant
# adds agents. A tenant that would have finished at 150s would instead be
# killed at 120s -- having done most of the work and committed none of it.
# Truncating mid-repair is strictly worse than either finishing or being
# killed at a point where nothing was going to complete anyway.
#
# doctor is load-bearing: it creates missing session-store dirs (a boot has
# flagged one CRITICAL), refreshes the plugin registry, and auto-enables the
# configured model and browser. If it needs to work, it needs room to work.
#
# The starved-container problem is a CPU problem and belongs to sizing, not to
# this timeout. Failure here stays non-fatal.
timeout --kill-after=30 300 node /app/openclaw.mjs doctor --fix --yes --non-interactive < /dev/null \
  || echo "[o6-app] openclaw doctor --fix did not finish cleanly; continuing to gateway"
exec node /app/openclaw.mjs gateway --port 18789 --bind lan
