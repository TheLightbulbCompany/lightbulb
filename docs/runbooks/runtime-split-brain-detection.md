# Detecting a split AgentCore session (two microVMs for one workspace)

AgentCore gives N concurrent invokes on a session with no compute N microVMs (no 409). The
backend prevents it with a single-flight wake (`runtime_transport._ensure_session`, the
`__session__#<owner>` row in the containers table), and every stop goes through
`hermes_provisioning.stop_runtime_session`, which clears that row. The residue is a stop the
backend never saw (AWS recycling, a crash, a stop outside the helper). This query finds it.

**Signal:** two log streams of one runtime restoring a profile within the same minute.

CloudWatch Logs Insights, select every `/aws/bedrock-agentcore/runtimes/lb_<env>_*` log group:

```
filter @message like /restored profile/
| stats count_distinct(@logStream) as microVMs by @log, bin(1m) as minute
| filter microVMs > 1
| sort minute desc
```

A row is a split. Verified 2026-09-25 against prod 09-23 → 09-26: it returns exactly the two
known splits (founder 09-25 13:00, Santi 09-25 08:03) and nothing else. A pair straddling a
minute boundary can slip through; widen to `bin(2m)` when chasing a specific incident.

**What to do with a hit:** stop the session with `PATCH /api/v1/debug/provision` (dev) or the
`scripts/roll_hermes_runtimes.py --owner-id <owner> --respec` (prod), never with a bare
`StopRuntimeSession`. Note that one stop SIGTERMs only
ONE microVM of a split; the others answer nothing to later stops (`ResourceNotFoundException`)
and live on until their own idle / maxLifetime limit (dev, 2026-09-25). They receive no further
traffic, and the next cold start claims a fresh pool microVM, not a leftover.
