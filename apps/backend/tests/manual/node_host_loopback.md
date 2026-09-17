# Node-host loopback + dev proof (M2 Task 30)

Proves spec §6 M2's pass criteria: the backend's node protocol (`node_broker.py` + `routers/gateway_relay.py`'s node branch) against the **real vendored upstream node-host CLI**, not the Rust client — it has its own tick watchdog, protocol renegotiation, and a 15s post-connect deadline the Rust client doesn't (report 02 R6: "a bridge tested only against the Tauri path will pass and then fail on every Mac") — then a real Hermes turn on dev.

## Part A — loopback proof (no dev deploy needed)

Runs on your Mac against **real dev DynamoDB** (SSO) with a **local** backend and a **local** bridging proxy — no ECS/AgentCore deploy. Uses the spike org (`org_3JCfoyOVF0AjDYjOnIZz5cK8XJL`, already `runtime_host: agentcore`, real `hook_token`) instead of a throwaway row. `pair_desktop_devices` only writes/removes its OWN device keys (`container_repo.py::_put_paired_device` is a scoped, per-device conditional write), so this cannot clobber another paired device on that org.

**Caution:** `main:app`'s lifespan also starts the trial-idle sweeper, run-ledger sweeper, openclaw-work SQS worker and scheduled worker against those same real dev tables — unavoidable running the app as-is; keep this session's uptime short.

### 0. One-time setup

```bash
export AWS_PROFILE=isol8-admin
aws sso login --profile isol8-admin
cd /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri
[ -d bin/openclaw-host-* ] || scripts/vendor-sidecars.sh   # ~2 min; needs internet (npm + nodejs.org)
```

No single `bin/openclaw-host` binary exists — the script produces `bin/node-<triple>`, `bin/openclaw-host-<triple>/node_modules/openclaw/openclaw.mjs` (the CLI entry point) and a launcher shim `bin/lightbulb-browser-service-<triple>` (`exec`s `node run --host 127.0.0.1 --port "$LIGHTBULB_PROXY_PORT"`, the shape used below). `<triple>` is `aarch64-apple-darwin` on Apple Silicon, `x86_64-apple-darwin` on Intel.

### 1. Start the local backend (terminal 1)

Env the relay path needs (`core/config.py` / `connection_service.py` / `machine_auth.py` / `runtime_transport.py`) — everything else defaults correctly for dev (`DYNAMODB_TABLE_PREFIX=lightbulb-dev-`, `WS_CONNECTIONS_TABLE=lightbulb-dev-ws-connections`). Neither the relay's auth (`machine_auth.verify_upgrade`) nor `/internal/node/invoke`'s (`_require_hook_token`) consults Clerk at request time, but `core/config.py`'s `Settings` still refuses to construct — so `main:app` refuses to even import — without a real `CLERK_ISSUER` (`field_validator` rejects the placeholder default): use your normal local `apps/backend/.env` (per `.env.example`), which any day-to-day local dev already has.

```bash
cd /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
export EFS_MOUNT_PATH=/tmp/node-loopback-efs   # default /mnt/efs/users doesn't exist locally
mkdir -p "$EFS_MOUNT_PATH"
uv run uvicorn main:app --port 8000
```

### 2. Confirm the org is AgentCore-hosted (terminal 2, same exports)

```bash
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1 EFS_MOUNT_PATH=/tmp/node-loopback-efs
export ORG=org_3JCfoyOVF0AjDYjOnIZz5cK8XJL
aws dynamodb get-item --table-name lightbulb-dev-containers --profile isol8-admin \
  --key "{\"owner_id\":{\"S\":\"$ORG\"}}" --projection-expression 'runtime_host,hook_token'
# if runtime_host isn't "agentcore" (it should already be, from the M1 spike):
aws dynamodb update-item --table-name lightbulb-dev-containers --profile isol8-admin \
  --key "{\"owner_id\":{\"S\":\"$ORG\"}}" --update-expression 'SET runtime_host = :h' \
  --expression-attribute-values '{":h":{"S":"agentcore"}}'
```

### 3. Mint a throwaway paired CLI device

M2-R10: pairing must happen before the proof, or every command is `COMMAND_NOT_ALLOWED`. The desktop never sends its private key to the backend — it self-mints a short-lived EdDSA JWT (`deviceId`, `owner`, `kind:"device"`, `exp-iat<=300s`) with the Ed25519 key it registered via `POST /desktop/pair`. Call `pair_desktop_devices()` directly (same function the router calls) to skip standing up a Clerk session for a throwaway key:

```bash
cd /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend
python3 - <<'PY'
import asyncio, os, time, json
os.environ.setdefault("EFS_MOUNT_PATH", "/tmp/node-loopback-efs")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import jwt
from core.containers import get_workspace
from core.repositories import container_repo
from core.config import settings
from core.services.desktop_pair import EfsWorkspaceAdapter, pair_desktop_devices
OWNER, USER = "org_3JCfoyOVF0AjDYjOnIZz5cK8XJL", "user_3JRCqaxTRuc0XkW5OAvUDUXDy8d"
def gen_pem():
    sk = Ed25519PrivateKey.generate()
    priv = sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = sk.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub
mac_priv, mac_pub = gen_pem()
cli_priv, cli_pub = gen_pem()
async def main():
    adapter = EfsWorkspaceAdapter(owner_id=OWNER, workspace=get_workspace(), container_repo=container_repo, gateway_ws_url=settings.WS_GATEWAY_URL)
    result = await pair_desktop_devices(user_id=USER, owner_id=OWNER, display_name_base="node-loopback-test",
                                         tauri_mac_pubkey=mac_pub, cli_pubkey=cli_pub, workspace=adapter)
    now = int(time.time())
    token = jwt.encode({"deviceId": result.device_id_cli, "owner": OWNER, "kind": "device", "iat": now, "exp": now + 290}, cli_priv, algorithm="EdDSA")
    json.dump({"deviceId": result.device_id_cli, "token": token}, open("/tmp/node-loopback-jwt.json", "w"))
    print("cli deviceId:", result.device_id_cli, "commands:", result.commands_cli)
asyncio.run(main())
PY
```

Verify with `cat /tmp/node-loopback-efs/$ORG/nodes/paired.json` (what `declared_commands_for` reads) — the new device should show `system.run` in `commands`. The JWT is valid ~290s; start the CLI (step 5) right after minting. Its key does NOT need to match the CLI's own internally-generated identity: the WS-upgrade JWT (checked once, at accept, against `paired_devices` in DynamoDB) and the in-band `connect` challenge signature (self-certifying) are independent checks.

### 4. Start the loopback bridging proxy

The CLI only accepts `--host`/`--port` (no path, no query string), but the relay lives at `/api/v1/gateway/ws?token=...&clientId=...`. Production bridges this with Tauri's `proxy.rs:165-171` (a local WSS proxy the CLI dials, which itself dials the real relay with the token injected); same shape, standalone, using the `websockets` lib already vendored:

```bash
JWT=$(python3 -c "import json;print(json.load(open('/tmp/node-loopback-jwt.json'))['token'])")
python3 - "$JWT" <<'PY' > /tmp/node-loopback-proxy.log 2>&1 &
import asyncio, sys, websockets
UPSTREAM = f"ws://127.0.0.1:8000/api/v1/gateway/ws?token={sys.argv[1]}&clientId=node-host"
async def pipe(src, dst):
    async for msg in src:
        await dst.send(msg)
async def handler(ws):
    async with websockets.connect(UPSTREAM, max_size=26_214_400) as up:
        print("[proxy] upstream WS open", flush=True)
        tasks = [asyncio.create_task(pipe(ws, up)), asyncio.create_task(pipe(up, ws))]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending: t.cancel()
async def main():
    async with websockets.serve(handler, "127.0.0.1", 0, max_size=26_214_400) as server:
        print(f"PROXY_PORT={server.sockets[0].getsockname()[1]}", flush=True)
        await asyncio.Future()
asyncio.run(main())
PY
sleep 1; grep PROXY_PORT /tmp/node-loopback-proxy.log   # note the port
```

### 5. Launch the real CLI node-host against the proxy

```bash
export LIGHTBULB_PROXY_PORT=<port from step 4>
TRIPLE=$([ "$(uname -m)" = "arm64" ] && echo aarch64-apple-darwin || echo x86_64-apple-darwin)
BIN=/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/bin
mkdir -p /tmp/node-loopback
OPENCLAW_STATE_DIR=/tmp/node-loopback OPENCLAW_GATEWAY_TOKEN=loopback-unused \
LIGHTBULB_PROXY_PORT="$LIGHTBULB_PROXY_PORT" "$BIN/lightbulb-browser-service-$TRIPLE" \
  > /tmp/node-loopback/cli.log 2>&1 &
```

(`OPENCLAW_GATEWAY_TOKEN` only authenticates the subprocess to Tauri's own proxy in production — our `node_broker` never reads it; any value works.) Watch `/tmp/node-loopback/cli.log` for a clean connect (no `1008`/`1012`/`4000`) and `/tmp/node-loopback-proxy.log` for `[proxy] upstream WS open`.

### 6. Drive one command through the bridge's own rail

```bash
TOKEN=$(aws dynamodb get-item --table-name lightbulb-dev-containers --profile isol8-admin \
  --key "{\"owner_id\":{\"S\":\"$ORG\"}}" --projection-expression 'hook_token' --query 'Item.hook_token.S' --output text)
curl -sX POST http://127.0.0.1:8000/api/v1/internal/node/invoke/$ORG \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"command":"system.run","paramsJSON":"{\"command\":[\"bash\",\"-lc\",\"uname -a\"]}","timeoutMs":30000}'
```

## Checklist (all must pass)

- [ ] clean connect — `/tmp/node-loopback/cli.log` shows no `1008`/`1012`/`4000`
- [ ] the curl above returns `{"ok":true,"payload":{...}}` with this Mac's `uname -a` in `payload.stdout` and `payload.exitCode: 0`
- [ ] **ten minutes, no reconnect**: `grep -c 'WS open' /tmp/node-loopback-proxy.log` stays at 1 for 10 minutes (the relay's 15s tick keeps the CLI's 30s watchdog from firing — `node_broker.TICK_INTERVAL_MS`)
- [ ] a `"timeoutMs":2000` invoke of `["bash","-lc","sleep 30"]` returns `payload.timedOut: true` on the raw route (the plugin, `.../lightbulb_node/__init__.py::execute`, is what turns this into `returncode: 124` — that translation is plugin-side, not on this route). **The `timeoutMs` that the node's own `system.run` handler enforces is a `paramsJSON` field, separate from the top-level invoke-body `timeoutMs`** (`core/services/node_broker.py::NodeSession.invoke` has zero grace beyond the top-level value, so it always wins the race and the caller gets a bare `504 NODE_TIMEOUT` — no `payload` at all, and the CLI never kills the child process — unless the node is *also* told its own bound via `paramsJSON`). Give the top-level value real headroom over it, e.g. `-d '{"command":"system.run","paramsJSON":"{\"command\":[\"bash\",\"-lc\",\"sleep 30\"],\"timeoutMs\":2000}","timeoutMs":8000}'`.
- [ ] an exec-approval prompt on the Mac (if the CLI's local policy asks) is honoured; a denial surfaces in `payload.error`/`error.message`, never a hang to `NODE_TIMEOUT` (exact denial string is upstream's own, not defined in this repo)
- [ ] `camera.capture` (undeclared by node-host) → **403 COMMAND_NOT_ALLOWED** before it reaches the socket: `curl ... -d '{"command":"camera.capture","paramsJSON":"{}"}'`
- [ ] **cross-instance forwarding** (M2-R23): third terminal, `PORT=8001 AWS_PROFILE=isol8-admin EFS_MOUNT_PATH=/tmp/node-loopback-efs uv run uvicorn main:app --port 8001` (`PORT` env, not just `--port` — `task_address.own_address()` reads `PORT`). Point the step-6 curl at `:8001`; it must still return the same payload, forwarded once to `:8000` (which holds the socket) — confirm `:8000`'s own log shows the forwarded request carrying `X-Lightbulb-Node-Forwarded`, and it never bounces to a third instance

**Failure map:** `1008` at connect → Task 26's challenge/deadline; `4000` → Task 26's ticker; `INVALID_REQUEST: paramsJSON required` → Task 25's schema; a request that bounces forever → Task 28's single-hop rule (`NODE_FORWARD_HEADER`).

**Cleanup** (removes only the throwaway device; other paired devices are untouched):

```bash
python3 - <<'PY'
import asyncio, json, os
os.environ.setdefault("EFS_MOUNT_PATH", "/tmp/node-loopback-efs")
from core.containers import get_workspace
from core.repositories import container_repo
from core.config import settings
from core.services.desktop_pair import EfsWorkspaceAdapter, revoke_desktop_device
OWNER = "org_3JCfoyOVF0AjDYjOnIZz5cK8XJL"
device_id = json.load(open("/tmp/node-loopback-jwt.json"))["deviceId"]
adapter = EfsWorkspaceAdapter(owner_id=OWNER, workspace=get_workspace(), container_repo=container_repo, gateway_ws_url=settings.WS_GATEWAY_URL)
asyncio.run(revoke_desktop_device(device_id=device_id, workspace=adapter, force_close_device=lambda d: asyncio.sleep(0, result=True)))
PY
kill %1 %2 2>/dev/null   # the proxy + CLI background jobs
```

## Part B — dev proof (after Part A passes; needs PR #2718 promoted to dev)

Uses the spike org throughout: org `org_3JCfoyOVF0AjDYjOnIZz5cK8XJL`, `#general` room `dcfac9a5-913d-53bb-bb59-04dc385becc6`, CEO agent `227fe9d5-7991-42b4-ba91-9754310f3aa2`, spike user `user_3JRCqaxTRuc0XkW5OAvUDUXDy8d` (org admin).

### 1. Sign the spike user into a browser, then the real desktop app

```bash
CLERK_SECRET=$(aws secretsmanager get-secret-value --secret-id lightbulb/dev/clerk_secret_key --profile isol8-admin --query SecretString --output text)
curl -sX POST https://api.clerk.com/v1/sign_in_tokens -H "Authorization: Bearer $CLERK_SECRET" \
  -H 'Content-Type: application/json' -d '{"user_id":"user_3JRCqaxTRuc0XkW5OAvUDUXDy8d"}'
```

Redeem the returned `{token}` in a browser (`Clerk.client.signIn.create({strategy:'ticket', ticket:'<token>'})` then `setActive`) on any `dev.flickmeon.ai` page, then navigate to `/auth/desktop-callback`. That page (already Clerk-authed) calls `POST /api/v1/auth/desktop/sign-in-token` (`routers/desktop_auth.py` — a SECOND, desktop-scoped one-time token) and redirects to `lightbulbai://auth?ticket=<token>`. The desktop app's deep-link handler redeems that ticket for its own Clerk session, then calls `POST /api/v1/desktop/pair` with its two Keychain-backed Ed25519 pubkeys — the same `pair_desktop_devices()` path Part A called directly, landing in the same `nodes/paired.json` shape on the real dev EFS. Confirm pairing: the app should stop showing a NOT_PAIRED retry loop.

### 2. Point the CEO agent's terminal backend at the node route

Nothing in M2 writes `terminal.backend: lightbulb_node` onto a profile — set it once via the `config.patch` RPC (`POST /api/v1/runtime/rpc`, Clerk-authed). Shape verified against `apps/frontend/src/hooks/useConfigPatch.ts` (`raw` is a JSON-merge-patch STRING, `baseHash` comes from a fresh `config.get`):

```bash
TOK=<Clerk bearer for the spike user>
HASH=$(curl -s https://api-dev.flickmeon.ai/api/v1/runtime/rpc -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"method":"config.get","params":{}}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["payload"]["hash"])')
curl -sX POST https://api-dev.flickmeon.ai/api/v1/runtime/rpc -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d "{\"method\":\"config.patch\",\"params\":{\"baseHash\":\"$HASH\",\"raw\":\"{\\\"agents\\\":{\\\"list\\\":[{\\\"id\\\":\\\"227fe9d5-7991-42b4-ba91-9754310f3aa2\\\",\\\"terminal\\\":{\\\"backend\\\":\\\"lightbulb_node\\\"}}]}}\"}}"
```

### 3. Drive a real turn

```bash
TOK=tok-spike2.sh CEO=227fe9d5-7991-42b4-ba91-9754310f3aa2 \
  ~/.claude/jobs/d3c73d10/tmp/hermes-room.py send "Run \`uname -a\` on my Mac and tell me what it says."
~/.claude/jobs/d3c73d10/tmp/hermes-room.py watch 240
ORG=org_3JCfoyOVF0AjDYjOnIZz5cK8XJL ~/.claude/jobs/d3c73d10/tmp/hermes-room.py runs   # the ledger row
```

### 4. Expected log lines

- `/ecs/lightbulb-dev` — `POST /api/v1/internal/node/invoke/org_3JCfoyOVF0AjDYjOnIZz5cK8XJL` access-log entries, one per shell command the turn ran, each `200`
- `/aws/bedrock-agentcore/runtimes/lb_hermes_3JCfoyOVF0AjDYjOnIZz-NL7Hk93XJY-DEFAULT` — Hermes's tool-execution trace naming the `terminal` tool (`lightbulb_node/__init__.py`'s `_invoke()` has no log statement of its own, so "one per command" is verified from the backend side; what THIS log group proves is the negative — no outbound WebSocket dial attempt from the microVM at all)

### 5. Failure signatures (M2-R11)

| HTTP | code | meaning |
|---|---|---|
| 403 | `COMMAND_NOT_ALLOWED` | a node is attached, but no attached device declared this command (wrong device, or the CEO's grant doesn't include it) |
| 409 | `NODE_NOT_ATTACHED` | no desktop node socket for this owner anywhere in the fleet — re-open/re-pair the desktop app |
| 504 | `NODE_TIMEOUT` | a node answered the WS-invoke door but the command didn't return before `timeoutMs` (or a cross-instance forward's own read timeout, `timeoutMs + 5s`, elapsed) |
