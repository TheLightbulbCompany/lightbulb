# Lightbulb bridge

One ECS task (TCP 8080, private subnets, ingress from the backend SG only) that runs one
embedded [Lightbulb Connect](https://github.com/TheLightbulbCompany/lightbulb-connect)
instance per connected workspace: that workspace's single `kind: cloud` member.

- **Boot.** `GET {BACKEND_INTERNAL_URL}/api/v1/internal/bridge/workspaces` lists the
  workspaces. For each one the bridge reads its key from Secrets Manager
  (`lightbulb/{env}/network/bridge/{owner}`, a base64 Ed25519 seed) into a fresh home's
  `identity.key`, restores `s3://$OWNER_STATE_BUCKET/bridge-state/<owner>/state.db` when it
  exists, and starts the instance.
- **Snapshots.** Right after a new network starts (its genesis), 1 s after a burst of
  state-writing events, and once more on SIGTERM. An object that is not a SQLite database is
  refused: the workspace stays down with one log line.
- **Lease.** Only the holder of the containers-table row `__bridge__#lease` runs instances. A
  separate task renews it every 10 s (30 s life), whatever boot is doing; boot starts up to 8
  workspaces at once, and every backend, Secrets Manager and S3 call has a 10 s limit. A
  renewal that does not take stops every instance at once (20 s at most). Every upload
  happens only before the last good put + 20 s, none at all once another holder has the row,
  and none when the bytes are unchanged. A deploy is a short outage: the old task stops its
  instances, uploads their snapshots and releases the lease on SIGTERM, then the new one
  boots.
- **HTTP.** `GET /health` is open; every other route needs `x-lightbulb-bridge-internal`.
- **Logs.** Ids, sizes, states and timings only: never a body, key, proof or token.

## Memory ceiling

One idle instance (no peers, solo network) measured 16.2 MiB of RSS above the bare process
(11.1 MiB before, 27.3 MiB after; macOS arm64, debug build, `idle_rss` in
`tests/instances.rs`). Ten idle instances measured 20.0 MiB above it, so the first instance
carries most of that (shared runtime and TLS setup) and each further one about 0.4 MiB.

**workspaces per 1 GiB = floor(800 MiB / 16.2 MiB) = 49**

This is the conservative figure. A connected instance's real cost grows with its peers,
roster and mail; re-measure on the Fargate task (linux/arm64, release) once workspaces are
connected.

## Tests

```sh
cargo test && cargo fmt --check && cargo clippy --all-targets -- -D warnings
BRIDGE_RSS_N=10 cargo test --test instances idle_rss -- --ignored --nocapture
```
