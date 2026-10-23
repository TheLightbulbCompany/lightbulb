# Lightbulb Connect relay

The relay carries a member-to-member QUIC connection when the two computers cannot
reach each other directly. It forwards end-to-end ciphertext only.

| | dev | prod |
|---|---|---|
| Host | `relay-dev.thelightbulb.company` | `relay.thelightbulb.company` |
| Stack | `lightbulb-dev-relay` | `lightbulb-prod-relay` |
| ECS service | `lightbulb-dev-relay` | `lightbulb-prod-relay` |
| Logs | `/lightbulb/dev/relay` | `/lightbulb/prod/relay` |

## Shape

`CloudFront (relay host, WebSockets) → VPC origin → existing internal ALB :443
(host-header rule) → one iroh-relay task on :8080`. Code: `apps/infra/lib/stacks/relay-stack.ts`,
image `apps/infra/relay/`: n0's iroh-relay `v1.4.1` release binary (the engine's iroh pin;
aarch64 musl, sha256-checked in the Dockerfile), run as nonroot (uid 65532).

To bump the version: change `RELAY_URL` and `RELAY_SHA256` in the Dockerfile together
(`shasum -a 256` of the release tarball). The image build needs BuildKit (Docker 23+).

- TLS ends at the ALB (wildcard cert). No NLB, no UDP, no ACME: QUIC address discovery is off.
- `access = "everyone"`. Abuse is bounded by the `[limits]` in `config.toml`
  (10 new connections/s, burst 50; 1 MiB/s per client, burst 4 MiB).
- Metrics: `:9090` on the task, reachable from inside the VPC only. Nothing routes to it.
- **First deploy is two steps** (api-stack.ts records a VpcOrigin and the distribution that
  references it breaking a first create). Step 1 ships the service, target group, ALB rule and
  the VpcOrigin; after it deploys in dev (and prod), step 2 adds the distribution, the A/AAAA
  aliases and the CloudFront alarms. If a relay stack create ever fails, it sits in
  `ROLLBACK_COMPLETE` and blocks every later deploy job: delete the stack, then re-run.
- Deploys stop the old task before the new one starts (`minHealthyPercent: 0`). Members
  reconnect on their own; expect a minute without a relay per deploy.
- Health: `GET /generate_204` → 204 (ALB target-group check).
- Alarms (warn topic): `lightbulb-{env}-relay-bytes-transferred` (CloudFront bytes down +
  up over 20 GiB in an hour; a starting guess, tune it) and `lightbulb-{env}-relay-5xx`
  (5xx rate over 5% for 15 minutes).

## relays.json

The engine fetches `relays.json` and `relays.json.minisig` from the issuer and accepts the
list only if one of its two compiled-in keys (`relay::KEYS`) signed it. The files live at
`apps/frontend/public/network-relays/{prod,dev}/`. Until the key ceremony below they ship
**unsigned** (no `.minisig`), so engines use no Lightbulb relay (direct only).

### Key ceremony (once, owner, with prod AWS credentials)

```bash
cd apps/backend
uv run python -m scripts.network_sign_relays --generate
```

This creates `lightbulb/prod/network/relays_signing_key_1` and `_2` in Secrets Manager and
prints only the two public strings. Put them into the engine's `src/relay.rs` `KEYS` (the
only thing Plan C writes into the public repo besides the issuer public keys). Key 1 signs;
key 2 is the standby. The script refuses to run if either secret already exists.

### Re-sign (every 90 days)

`expires_at` is a rollback floor, not a hard expiry: the engine refuses a list older than the
newest one it has verified, but keeps using its cached list after `expires_at` passes. Re-sign
on schedule anyway so the floor keeps moving and a stale list can never be replayed.

```bash
cd apps/backend
uv run python -m scripts.network_sign_relays --sign prod
uv run python -m scripts.network_sign_relays --sign dev
```

Each run writes `relays.json` (`expires_at` = now + 90 days) and `relays.json.minisig`; commit both
and merge. An engine refuses a list older than the newest one it has seen, so never
commit an older file over a newer one.

**Calendar:** re-sign every 90 days, at least two weeks before `expires_at` in the
committed `relays.json`. Current: `expires_at` = 1800554950 (2027-01-21 UTC); re-sign by
2027-01-07.

## Checks after a deploy

- `curl -sI https://relay-dev.thelightbulb.company/generate_204` → `204`.
- A debug engine with `relay = "https://relay-dev.thelightbulb.company"` passes Plan B's
  `messages_flow_via_relay_when_direct_is_blocked`.
- A relay connection left idle for 15 minutes is still up (the relay pings every client
  every ~15-20 s, inside CloudFront's 60 s read timeout and the ALB's 300 s idle timeout).
