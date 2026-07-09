---
title: Desktop node-host device-key WS-upgrade auth
date: 2026-07-08
status: design
---

# Desktop node-host device-key WS-upgrade auth

## Problem

The desktop app runs a persistent **node-host** (`openclaw node run` subprocess,
clientId `node-host`) so the user can message their agent (Friday) **from their
phone** and have it act on the Mac. But the node-host authenticates its AWS API
Gateway WebSocket `$connect` with the **desktop web view's ~60s Clerk session
JWT** (`proxy.rs` injects `current_jwt`, written only by the Clerk webview via
`send_auth_token`). That is architecturally wrong:

- A persistent background **device** depends on an **interactive user session**.
  When the desktop is idle/backgrounded (exactly when the user is on their
  phone), Clerk's client-side refresh throttles, `current_jwt` goes stale, and
  the node-host's next reconnect is **403'd** by the Lambda authorizer
  (`ExpiredSignatureError`). The node drops out of the container gateway's node
  registry → the agent's browser/exec/file tools report "No connected
  browser-capable nodes."
- The Mac node survives only because it holds one long-lived socket (token
  checked once at `$connect`); the node-host re-authenticates on every
  reconnect.

Rejected alternatives: a long-lived (365d) bearer service token (stealable
secret at rest); reading the EFS `paired.json` from the authorizer (needs
VPC-attaching the shared authorizer + EFS mount — fragile on the auth hot path);
a new DynamoDB devices table (unnecessary duplication).

## Design: device proof-of-possession, grounded in OpenClaw's node model

OpenClaw already authenticates nodes with **device identity presented at
`connect`** (`docs/nodes/index.md:29-30`, `docs/cli/node.md:66-75`): the node
signs the gateway's `connect.challenge` with its Ed25519 device key, verified
against `devices/paired.json`. We **hoist that same device-identity check up to
the AWS WS `$connect`**, replacing the borrowed Clerk JWT.

1. **Desktop self-mints** a short-lived (~90s) compact JWT signed with its
   CLI-slot Keychain Ed25519 key on *every* reconnect — claims `{deviceId,
   owner, iat, exp, jti}`, `alg: EdDSA`. No web view, no network, no long-lived
   secret at rest. Injected as `?token=` for the upgrade.
2. **Pairing** (`POST /desktop/pair`) writes the paired device pubkeys onto the
   **existing owner `containers` DDB row** (keyed by `owner_id`, already holds
   `gateway_token`): `paired_devices[deviceId] = {publicKey, userId}` where
   `publicKey` is the base64url-raw 32-byte key (identical to `paired.json`) and
   `userId` is the paired member. No new table — state lives on the owner row.
3. **Authorizer** gains a device branch (routed by `alg == "EdDSA"`): read the
   unverified `deviceId` + `owner` claims → `GetItem(containers, owner)` →
   `paired_devices[deviceId].publicKey` → verify the signature + `exp` → Allow
   with context `{userId: member, orgId: owner if owner != member else ""}`.
   A non-VPC `boto3` DynamoDB `GetItem` (the authorizer already reads Secrets
   Manager the same way). Clerk + Paperclip branches untouched.
4. **Revocation** = un-pair: `revoke_desktop_device` removes the device from the
   owner row's `paired_devices` (in addition to the `paired.json` removal +
   `force_close_device` of the live socket). Next `$connect` is denied.

### Why this is correct + secure

- **Proof-of-possession, not a bearer secret.** The `$connect` token is a fresh
  signature from a key that never leaves the Keychain; a sniffed token is
  useless in ~90s and there is nothing long-lived to steal.
- **Autonomous.** The device authenticates as *itself*, decoupled from the
  interactive Clerk session — connectivity survives an idle/backgrounded Mac,
  which is the whole point of phone→desktop.
- **Server-authored identity.** `memberUserId` / `owner` are written by the
  Clerk-authed pair endpoint; the desktop only proves *which device* it is, not
  *who it belongs to*.
- **Org-correct.** The authorizer reproduces the exact `(userId=member,
  orgId=owner)` a Clerk member-session yields, so container routing
  (`resolve_running_container(owner)`) and per-member session-binding
  (`get_user_cli_node(member)`, `websocket_chat.py:1848`) keep working.
- **No new store, no EFS-on-edge, no VPC change.** `paired.json` (atomic
  `os.rename` write — safe to read) stays the app-layer artifact; the owner row
  serves the edge-auth point lookup.

## Rollout — 4 independently-deployable steps (Clerk + Paperclip auth intact throughout)

1. **Pairing writes the device registry** to the owner row + `ownerId` in
   `PairResponse` (backend). No reader yet → zero behavior change. Coverage
   backfills as desktops re-pair.
2. **Authorizer device branch** (infra). Server accepts device JWTs; no client
   sends one yet.
3. **Desktop node-host self-mints** the device JWT; `proxy.rs` uses it. Cures
   the 403. Old builds keep working on Clerk (no flag-day).
4. **Mac node** migration (optional follow-up; foreground-biased, low urgency).

## Files

- Backend: `core/repositories/container_repo.py` (`upsert_paired_devices` /
  `remove_paired_device`), `core/services/desktop_pair.py` (registry write on
  pair, removal on revoke), `routers/desktop_pair.py` (`ownerId` in response).
- Infra: `apps/infra/lambda/websocket-authorizer/index.py` (device branch),
  `apps/infra/lib/stacks/api-stack.ts` (`CONTAINERS_TABLE` env +
  `containersTable.grantReadData(authorizerFn)`).
- Desktop: `apps/desktop/src-tauri/src/{pair_client.rs, lib.rs, bootstrap.rs,
  proxy.rs}` (receive `ownerId`, mint device JWT, proxy uses it).
