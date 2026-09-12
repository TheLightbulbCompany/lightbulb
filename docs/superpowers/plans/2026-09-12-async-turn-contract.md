# Async Turn Contract Implementation Plan (sub-project A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A room turn is acknowledged in milliseconds, runs to completion inside the container, streams its frames straight to AppSync, and reports its lifecycle and usage to a DynamoDB run ledger — with no per-run state in the backend process and no request on the turn path that outlives a few seconds.

**Architecture:** One AppSync Events namespace `ws` with workspace-scoped channel paths and handler-enforced tenancy replaces `rooms`/`users`. The SQS consumer mints a deterministic `runId`, writes a ledger row, POSTs the ingress body carrying the run contract and deletes the message on ack. The channel plugin acks 202 after admission, runs the turn in the background, publishes frames with the task role's SigV4 credentials, and reports `run.started` / `run.heartbeat` / `run.ended` (with usage read from its own session store) over the hook-token route. A one-minute sweeper closes runs that never started or stopped heartbeating. Abort and steer are HTTP calls to the plugin. The backend's WebSocket relay for room frames is deleted. No compatibility paths: one cutover per environment followed by a fleet roll.

**Tech Stack:** TypeScript plugin on OpenClaw 2026.9.3 plugin-sdk (vitest), FastAPI + boto3 (pytest/moto), AWS CDK (jest), APPSYNC_JS handlers, Next.js/Expo shared client (vitest/jest).

**Spec:** `docs/superpowers/specs/2026-09-12-async-turn-contract-design.md` (amended 2026-09-12 to match this plan; it is the authority).

## Global Constraints

- Channel paths: `/ws/{workspaceId}/rooms/{roomId}/{deliveryKey}` and `/ws/{workspaceId}/users/{userKey}`; every segment after `ws` is a lowercase UUID. No Clerk id ever appears in a path.
- `workspaceId` is minted lazily as the messaging-table row `OWNER#{ownerId}` / `WORKSPACE` and stamped as `workspaceId` on every `ROOM#…/META` and `STREAM#…/META` row.
- `runId = uuid5(RUN_NS, f"{agentId}:{messageId}")`, deterministic per dispatch event.
- Run ledger row: `OWNER#{ownerId}` / `RUN#{runId}`; status `dispatched | started | ended`; outcome ∈ `completed | error | aborted | ignored | gate_error | not_admitted`; `expiresAt` 7 days; sweeper thresholds 120 s (dispatched) and 90 s (heartbeat); heartbeat every 30 s.
- Ingress body carries one **required** object `run: { runId, workspaceId, frameChannel, frameEndpoint }`. A body without it is a 400. There is no legacy path in the plugin and no `turn_contract_v2` flag in the backend.
- Plugin ingress answers: `202 {eventId, runId, status:"accepted"}`, `200 {eventId, runId, status:"replayed"}`, `409 {error:"not_admitted"}`. Consumer deletes the SQS message on 200/202/409, retries on anything else. Queue visibility 60 s, no extension, `maxReceiveCount` 5.
- Callbacks: `run.started {sessionKey}`, `run.heartbeat`, `run.ended {outcome, errorKind, usage}`; `usage` is required unless the outcome is `ignored` or `gate_error`; unknown `runId` → 404; replay → 200 no-op.
- Frames: `run_active`, `thinking`, `chunk`, `tool_start`, `tool_progress`, `tool_end`, `tool_error`, `command_output`, `agent_activity`, `done`, `error`, `run_ended`, every frame stamped with `runId` and `agentId` (camelCase on the wire — the backend serializer renames `agent_id` to `agentId`, so the plugin emits `agentId` directly); coalesced ≤ 1 publish / 100 ms, ≤ 5 events per publish; on 429/5xx drop `chunk`/`thinking`, retry lifecycle frames.
- Container IAM: `appsync:EventPublish` on `arn:aws:appsync:{region}:{account}:apis/*/channelNamespace/ws` in `OwnerBedrockBasePolicy` and `OwnerBedrockBoundary`.
- Never log message text, handles or emails; metrics never carry owner/user/room/member ids as dimensions. The single-dimension rollup allowlist is `status|outcome|status_class|count_bucket|source`.
- Every commit message ends with the attribution lines in the conversation's system reminder. Infra changes run `cd apps/infra && npx cdk synth` and commit a regenerated `.github/workflows/deploy.yml` if it changed. `main` carries unrelated red tests: run only the files you touch.

---

### Task 1: Identity spike — which `ctx.identity` field carries the IAM caller ARN

**Files:**
- Modify: `docs/superpowers/specs/2026-09-12-async-turn-contract-design.md` (record the result under §3)
- Scratch (not committed): `/tmp/spike-handler.js`, `/tmp/spike_publish.py`

**Interfaces:**
- Produces: the field name Task 6's handler reads (expected `ctx.identity.userArn`, shaped `arn:aws:sts::{account}:assumed-role/{roleName}/{session}`).

Dev only. The scratch namespace is created and deleted with the CLI in one sitting; nothing references it and it is gone within the hour.

- [ ] **Step 1: Write the scratch handler**

```js
// /tmp/spike-handler.js
import { util } from '@aws-appsync/utils';
export function onPublish(ctx) {
  util.error(JSON.stringify({ identity: ctx.identity, path: ctx.info.channel.path }));
}
```

- [ ] **Step 2: Create the namespace on dev**

```bash
export AWS_PROFILE=isol8-admin
API_ID=$(aws appsync list-apis --query "apis[?name=='lightbulb-dev-messaging-events'].apiId" --output text)
aws appsync create-channel-namespace --api-id "$API_ID" --name spike \
  --code-handlers file:///tmp/spike-handler.js \
  --publish-auth-modes authType=AWS_IAM --subscribe-auth-modes authType=AWS_IAM
```

- [ ] **Step 3: Publish once with the SSO role and read the echoed identity**

```python
# /tmp/spike_publish.py — run: cd apps/backend && AWS_PROFILE=isol8-admin uv run python /tmp/spike_publish.py
import json, subprocess, urllib.error, urllib.request
import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

http_dns = subprocess.check_output(["aws", "appsync", "list-apis", "--query", "apis[?name=='lightbulb-dev-messaging-events'].dns.HTTP", "--output", "text"]).decode().strip()
body = json.dumps({"channel": "/spike/test", "events": ["{}"]}).encode()
req = AWSRequest(method="POST", url=f"https://{http_dns}/event", data=body, headers={"content-type": "application/json"})
SigV4Auth(botocore.session.get_session().get_credentials(), "appsync", "us-east-1").add_auth(req)
prepared = req.prepare()
try:
    with urllib.request.urlopen(urllib.request.Request(prepared.url, data=prepared.body, headers=dict(prepared.headers), method="POST")) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
```

Expected: the response's `failed[0].message` (or the HTTP error body) contains the JSON with `identity.userArn` = `arn:aws:sts::877352799272:assumed-role/AWSReservedSSO_…/…`. (If boto3 cannot refresh SSO in the venv, run `aws sso login --profile isol8-admin` first.)

- [ ] **Step 4: Tear down and record**

```bash
aws appsync delete-channel-namespace --api-id "$API_ID" --name spike
```

Append to spec §3 after the "Open verification" paragraph: `Verified 2026-09-XX on dev: the IAM caller ARN is ctx.identity.userArn, shaped arn:aws:sts::{account}:assumed-role/{roleName}/{session}.` (If a different field carried it, write that name and use it in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-09-12-async-turn-contract-design.md
git commit -m "docs(specs): record AppSync IAM identity field from dev spike"
```

---

### Task 2: Plugin — require the run contract and split admission from the turn

**Files:**
- Modify: `apps/infra/openclaw/plugins/lightbulb-channel/src/channel.ts` (`LightbulbRoomIngress` :319, `parseIngress` :454, `dispatchRoomEvent` :710-890)
- Delete: `apps/infra/openclaw/plugins/lightbulb-channel/src/fixtures/legacy-ingress-a45e6de.ts.fixture`
- Test: `apps/infra/openclaw/plugins/lightbulb-channel/src/index.test.ts`

**Interfaces:**
- Produces: `LightbulbRoomIngress.run: RunContract` (required); `admitRoomEvent(deps, event): Promise<AdmittedRoomEvent>`; `runRoomTurn(deps, event, admitted, invocation?): Promise<AmbientGateOutcome>`; `AmbientGateOutcome` exported. `dispatchRoomEvent` is deleted.

- [ ] **Step 1: Write the failing parse tests and retire the legacy parity test**

Delete the test `keeps the actual pre-adapter strict parser compatible with new backend canonical JSON` (:62-72) and the fixture it reads — the old ingress shape is gone. Add near the other constants and give `ingress()` a `run` by default:

```ts
const WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const DELIVERY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const RUN = {
  runId: RUN_ID,
  workspaceId: WORKSPACE,
  frameChannel: `/ws/${WORKSPACE}/rooms/${ROOM}/${DELIVERY}`,
  frameEndpoint: "https://abc123.appsync-api.us-east-1.amazonaws.com/event",
};
// inside ingress(overrides): add `run: RUN,` before `...overrides`

it("requires a whole run contract consistent with the room", () => {
  const parsed = parseIngress(ingress());
  expect(parsed.ok && parsed.value.run).toEqual(RUN);
  const { run: _run, ...withoutRun } = ingress();
  expect(parseIngress(withoutRun).ok).toBe(false);
  expect(parseIngress(ingress({ run: { ...RUN, frameChannel: `/ws/${WORKSPACE}/rooms/${WORKSPACE}/${DELIVERY}` } })).ok).toBe(false);
  expect(parseIngress(ingress({ run: { ...RUN, frameEndpoint: "http://insecure/event" } })).ok).toBe(false);
  expect(parseIngress(ingress({ run: { runId: RUN_ID } })).ok).toBe(false);
  expect(parseIngress(ingress({ run: { ...RUN, extra: 1 } })).ok).toBe(false);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- -t "run contract"`
Expected: FAIL (`body.run: unknown field`).

- [ ] **Step 3: Extend the type and parser**

In `src/channel.ts`, add to `LightbulbRoomIngress` after `recentHistory`:

```ts
  /** The async turn contract (spec §4). */
  run: RunContract;
};

export type RunContract = {
  runId: string;
  workspaceId: string;
  frameChannel: string;
  frameEndpoint: string;
};
```

In `parseIngress`, add `"run"` to the top-level allowlist and parse it after `room` is built:

```ts
    const r = obj(value, "body", [
      "version", "eventId", "targetAgentId", "eventKind", "room", "sender", "message",
      "mentions", "participants", "recentHistory", "run",
    ]);
    // …existing room/sender parsing…
    const runRaw = obj(r.run, "body.run", ["runId", "workspaceId", "frameChannel", "frameEndpoint"]);
    const runId = uuid(runRaw.runId, "body.run.runId");
    const workspaceId = uuid(runRaw.workspaceId, "body.run.workspaceId");
    const frameChannel = str(runRaw.frameChannel, "body.run.frameChannel");
    const framePrefix = `/ws/${workspaceId}/rooms/${room.id}/`;
    if (!frameChannel.startsWith(framePrefix) || !UUID_RE.test(frameChannel.slice(framePrefix.length))) {
      throw new Invalid("body.run.frameChannel: must be /ws/{workspaceId}/rooms/{room.id}/{deliveryKey}");
    }
    const frameEndpoint = str(runRaw.frameEndpoint, "body.run.frameEndpoint");
    if (!frameEndpoint.startsWith("https://")) throw new Invalid("body.run.frameEndpoint: expected https URL");
    const run: RunContract = { runId, workspaceId, frameChannel, frameEndpoint };
```

and include `run` in the returned value.

- [ ] **Step 4: Split `dispatchRoomEvent`**

Replace it with two exported functions and export the outcome type. `admitRoomEvent` is the current body up to and including the `ingress.ingress.admission !== "dispatch"` check; `runRoomTurn` is the rest, unchanged.

```ts
export type AmbientGateOutcome = "dispatched" | "ignored" | "gate_error";

export type AdmittedRoomEvent = {
  route: ReturnType<typeof resolveChannelInboundRouteEnvelope>["route"];
  buildEnvelope: ReturnType<typeof resolveChannelInboundRouteEnvelope>["buildEnvelope"];
  ingress: Awaited<ReturnType<typeof resolveStableChannelMessageIngress>>;
  wasMentioned: boolean;
  commandAuthorized: boolean;
};

/** Route + structural admission. Throws IngressRejected(409|500); never runs a model. */
export async function admitRoomEvent(deps: InboundDeps, event: LightbulbRoomIngress): Promise<AdmittedRoomEvent> {
  const { sdk, cfg, accountId } = deps;
  const { room, sender, eventKind } = event;
  const { route, buildEnvelope } = sdk.resolveChannelInboundRouteEnvelope({ /* unchanged from :714-719 */ });
  // …unchanged route/session checks (500) from :720-726…
  const wasMentioned = event.mentions.some((m) => m.kind === "agent" && m.id === accountId);
  const commandAuthorized = eventKind === "user_request" && sender.kind === "human";
  const ingress = await sdk.resolveStableChannelMessageIngress({ /* unchanged from :731-748 */ });
  if (ingress.ingress.admission !== "dispatch") {
    throw new IngressRejected(409, "lightbulb: channel ingress declined the event", { admission: ingress.ingress.admission });
  }
  return { route, buildEnvelope, ingress, wasMentioned, commandAuthorized };
}

/** Ambient gate + context + native dispatch. Resolves when the whole turn is over. */
export async function runRoomTurn(
  deps: InboundDeps,
  event: LightbulbRoomIngress,
  admitted: AdmittedRoomEvent,
  invocation?: string,
): Promise<AmbientGateOutcome> {
  const { route, buildEnvelope, ingress, wasMentioned, commandAuthorized } = admitted;
  // …the remainder of the old body, unchanged, from `if (eventKind === "room_event")` (:755) to `return "dispatched"` (:889)…
}
```

In `index.test.ts`, every `dispatchRoomEvent(deps, event, invocation)` call becomes `runRoomTurn(deps, event, await admitRoomEvent(deps, event), invocation)`; add that one-line helper `dispatch(deps, event, invocation?)` at the top of the test file and import `admitRoomEvent`/`runRoomTurn` instead of `dispatchRoomEvent`.

- [ ] **Step 5: Run the whole plugin suite**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A apps/infra/openclaw/plugins/lightbulb-channel/src
git commit -m "feat(plugin): require the run contract on ingress; split admission from the turn"
```

---

### Task 3: Plugin — SigV4 frame publisher with coalescing

**Files:**
- Create: `apps/infra/openclaw/plugins/lightbulb-channel/src/appsync.ts`
- Test: `apps/infra/openclaw/plugins/lightbulb-channel/src/appsync.test.ts`

**Interfaces:**
- Produces: `signAppSyncPublish({url, body, region, credentials, now?}) → headers`; `ecsCredentialsProvider(env, fetch) → () => Promise<AwsCredentials>`; `createFramePublisher({fetch, region, credentials, log?}) → { push(endpoint, channel, event), flush(channel) }`; `FRAME_FLUSH_MS = 100`, `FRAME_BATCH = 5`; type `LiveFrameEvent`.

- [ ] **Step 1: Write the failing tests**

```ts
// src/appsync.test.ts
import { describe, expect, it, vi } from "vitest";
import { FRAME_BATCH, FRAME_FLUSH_MS, createFramePublisher, ecsCredentialsProvider, signAppSyncPublish } from "./appsync.js";

const CREDS = { accessKeyId: "AKIDEXAMPLE", secretAccessKey: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", sessionToken: "SESSIONTOKENEXAMPLE" };
const ENDPOINT = "https://abc123.appsync-api.us-east-1.amazonaws.com/event";
const CHANNEL = "/ws/11111111-1111-4111-8111-111111111111/rooms/22222222-2222-4222-8222-222222222222/33333333-3333-4333-8333-333333333333";
const frame = (type: string) => ({ schemaVersion: 1, type: "room.run.frame", payload: { frame: { type } } });

describe("signAppSyncPublish", () => {
  it("matches botocore's SigV4 for a fixed request", () => {
    // Golden computed with botocore SigV4Auth (canonical_request / string_to_sign /
    // signature) for exactly these inputs with request.context.timestamp 20260912T031500Z.
    const body = JSON.stringify({ channel: CHANNEL, events: ["{}"] });
    const headers = signAppSyncPublish({ url: ENDPOINT, body, region: "us-east-1", credentials: CREDS, now: new Date("2026-09-12T03:15:00Z") });
    expect(headers["x-amz-date"]).toBe("20260912T031500Z");
    expect(headers["x-amz-security-token"]).toBe("SESSIONTOKENEXAMPLE");
    expect(headers.authorization).toBe(
      "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260912/us-east-1/appsync/aws4_request, " +
        "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token, " +
        "Signature=147f9479b4b9273fcdae4aa7091a37e70c5b7756a75b20cba33060f0ca62d80c",
    );
  });
});

describe("ecsCredentialsProvider", () => {
  it("reads the task-role endpoint once and caches until near expiry", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ AccessKeyId: "A", SecretAccessKey: "S", Token: "T", Expiration: new Date(Date.now() + 3_600_000).toISOString() }), { status: 200 }));
    const provider = ecsCredentialsProvider(() => ({ AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: "/v2/credentials/abc" }), fetch as any);
    expect(await provider()).toMatchObject({ accessKeyId: "A", sessionToken: "T" });
    await provider();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe("http://169.254.170.2/v2/credentials/abc");
  });
});

describe("createFramePublisher", () => {
  it("coalesces up to five events per publish and flushes on the timer", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    const pub = createFramePublisher({ fetch: fetch as any, region: "us-east-1", credentials: async () => CREDS });
    for (let i = 0; i < FRAME_BATCH + 1; i++) pub.push(ENDPOINT, CHANNEL, frame("chunk"));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetch.mock.calls[0][1].body).events).toHaveLength(FRAME_BATCH);
    await vi.advanceTimersByTimeAsync(FRAME_FLUSH_MS);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetch.mock.calls[1][1].body).channel).toBe(CHANNEL);
    vi.useRealTimers();
  });

  it("drops streaming frames on 429 and retries only lifecycle frames", async () => {
    vi.useFakeTimers();
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response("", { status: 429 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    const pub = createFramePublisher({ fetch: fetch as any, region: "us-east-1", credentials: async () => CREDS });
    pub.push(ENDPOINT, CHANNEL, frame("chunk"));
    pub.push(ENDPOINT, CHANNEL, frame("done"));
    const flushed = pub.flush(CHANNEL);
    await vi.runAllTimersAsync();
    await flushed;
    expect(fetch).toHaveBeenCalledTimes(2);
    const retried = JSON.parse(fetch.mock.calls[1][1].body).events.map((e: string) => JSON.parse(e).payload.frame.type);
    expect(retried).toEqual(["done"]);
    vi.useRealTimers();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- appsync`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `src/appsync.ts`**

```ts
/**
 * Direct AppSync Events publishing from the container: SigV4 with the ECS task-role
 * credentials, no AWS SDK (the plugin is dependency-free and baked by a plain COPY).
 */
import { createHash, createHmac } from "node:crypto";
import type { FetchLike, RawEnv } from "./channel.js";

export type AwsCredentials = { accessKeyId: string; secretAccessKey: string; sessionToken?: string; expiration?: number };
export const FRAME_FLUSH_MS = 100;
export const FRAME_BATCH = 5;
const PUBLISH_TIMEOUT_MS = 5_000;
const CREDENTIAL_REFRESH_MARGIN_MS = 5 * 60_000;
const RETRY_LIMIT = 3;

const hmac = (key: Buffer | string, data: string) => createHmac("sha256", key).update(data, "utf8").digest();
const sha256Hex = (data: string) => createHash("sha256").update(data, "utf8").digest("hex");

export function signAppSyncPublish(params: {
  url: string;
  body: string;
  region: string;
  credentials: AwsCredentials;
  now?: Date;
}): Record<string, string> {
  const { hostname, pathname } = new URL(params.url);
  const amzDate = (params.now ?? new Date()).toISOString().replace(/[:-]|\.\d{3}/g, "");
  const dateStamp = amzDate.slice(0, 8);
  const headers: Record<string, string> = {
    "content-type": "application/json",
    host: hostname,
    "x-amz-date": amzDate,
    ...(params.credentials.sessionToken ? { "x-amz-security-token": params.credentials.sessionToken } : {}),
  };
  const names = Object.keys(headers).sort();
  const canonicalRequest = [
    "POST",
    pathname,
    "",
    names.map((name) => `${name}:${headers[name].trim()}\n`).join(""),
    names.join(";"),
    sha256Hex(params.body),
  ].join("\n");
  const scope = `${dateStamp}/${params.region}/appsync/aws4_request`;
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, scope, sha256Hex(canonicalRequest)].join("\n");
  const kSigning = hmac(hmac(hmac(hmac(`AWS4${params.credentials.secretAccessKey}`, dateStamp), params.region), "appsync"), "aws4_request");
  const signature = createHmac("sha256", kSigning).update(stringToSign, "utf8").digest("hex");
  return {
    ...headers,
    authorization: `AWS4-HMAC-SHA256 Credential=${params.credentials.accessKeyId}/${scope}, SignedHeaders=${names.join(";")}, Signature=${signature}`,
  };
}

/** ECS task-role credentials (Fargate sets AWS_CONTAINER_CREDENTIALS_RELATIVE_URI); static env keys for local runs. */
export function ecsCredentialsProvider(env: () => RawEnv, fetchImpl: FetchLike): () => Promise<AwsCredentials> {
  let cached: AwsCredentials | undefined;
  return async () => {
    if (cached && (cached.expiration ?? Number.POSITIVE_INFINITY) - Date.now() > CREDENTIAL_REFRESH_MARGIN_MS) return cached;
    const e = env();
    const url = e.AWS_CONTAINER_CREDENTIALS_FULL_URI
      ?? (e.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI ? `http://169.254.170.2${e.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI}` : undefined);
    if (!url) {
      if (e.AWS_ACCESS_KEY_ID && e.AWS_SECRET_ACCESS_KEY) {
        cached = { accessKeyId: e.AWS_ACCESS_KEY_ID, secretAccessKey: e.AWS_SECRET_ACCESS_KEY, sessionToken: e.AWS_SESSION_TOKEN };
        return cached;
      }
      throw new Error("lightbulb: no AWS credential source for AppSync publishing");
    }
    const res = await fetchImpl(url, {
      method: "GET",
      headers: e.AWS_CONTAINER_AUTHORIZATION_TOKEN ? { Authorization: e.AWS_CONTAINER_AUTHORIZATION_TOKEN } : {},
      signal: AbortSignal.timeout(2_000),
    });
    if (res.status !== 200) throw new Error(`lightbulb: credential endpoint returned ${res.status}`);
    const body = (await res.json()) as { AccessKeyId: string; SecretAccessKey: string; Token?: string; Expiration?: string };
    cached = {
      accessKeyId: body.AccessKeyId,
      secretAccessKey: body.SecretAccessKey,
      sessionToken: body.Token,
      expiration: body.Expiration ? Date.parse(body.Expiration) : undefined,
    };
    return cached;
  };
}

export type LiveFrameEvent = { payload: { frame: { type: string } } } & Record<string, unknown>;
export type FramePublisher = ReturnType<typeof createFramePublisher>;

const isStreaming = (event: LiveFrameEvent) => event.payload.frame.type === "chunk" || event.payload.frame.type === "thinking";

export function createFramePublisher(deps: {
  fetch: FetchLike;
  region: string;
  credentials: () => Promise<AwsCredentials>;
  log?: (message: string) => void;
}) {
  type Queue = { endpoint: string; events: LiveFrameEvent[]; timer?: ReturnType<typeof setTimeout>; chain: Promise<void> };
  const queues = new Map<string, Queue>();

  async function post(endpoint: string, channel: string, events: LiveFrameEvent[], attempt = 0): Promise<void> {
    const body = JSON.stringify({ channel, events: events.map((event) => JSON.stringify(event)) });
    let status: number;
    try {
      const headers = signAppSyncPublish({ url: endpoint, body, region: deps.region, credentials: await deps.credentials() });
      status = (await deps.fetch(endpoint, { method: "POST", headers, body, signal: AbortSignal.timeout(PUBLISH_TIMEOUT_MS) })).status;
    } catch (error) {
      status = 599;
      deps.log?.(`lightbulb: frame publish failed (${error instanceof Error ? error.name : "error"})`);
    }
    if (status === 200) return;
    // A run never fails because of streaming: text catch-up rides the final full-text
    // chunk, so streaming frames are the first thing to drop; lifecycle frames retry.
    const lifecycle = events.filter((event) => !isStreaming(event));
    if (lifecycle.length === 0 || attempt + 1 >= RETRY_LIMIT) {
      deps.log?.(`lightbulb: dropped ${events.length} frame(s) after ${status}`);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
    return post(endpoint, channel, lifecycle, attempt + 1);
  }

  function flushNow(channel: string): void {
    const queue = queues.get(channel);
    if (!queue || queue.events.length === 0) return;
    if (queue.timer) clearTimeout(queue.timer);
    queue.timer = undefined;
    const batch = queue.events.splice(0, FRAME_BATCH);
    queue.chain = queue.chain.then(() => post(queue.endpoint, channel, batch));
    if (queue.events.length > 0) flushNow(channel);
  }

  return {
    push(endpoint: string, channel: string, event: LiveFrameEvent): void {
      let queue = queues.get(channel);
      if (!queue) {
        queue = { endpoint, events: [], chain: Promise.resolve() };
        queues.set(channel, queue);
      }
      queue.endpoint = endpoint;
      queue.events.push(event);
      if (queue.events.length >= FRAME_BATCH) flushNow(channel);
      else if (!queue.timer) queue.timer = setTimeout(() => flushNow(channel), FRAME_FLUSH_MS);
    },
    /** Publish everything queued for the channel and wait for the in-flight posts. */
    async flush(channel: string): Promise<void> {
      flushNow(channel);
      const queue = queues.get(channel);
      if (queue) await queue.chain;
    },
  };
}
```

Node's `fetch` drops a caller-supplied `host` header and sends the URL host, which is the value the signature covers.

- [ ] **Step 4: Run the tests**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- appsync`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/openclaw/plugins/lightbulb-channel/src/appsync.ts apps/infra/openclaw/plugins/lightbulb-channel/src/appsync.test.ts
git commit -m "feat(plugin): SigV4 AppSync frame publisher with coalescing and 429 policy"
```

---

### Task 4: Plugin — run supervisor, callbacks with usage, immediate ack, abort/steer routes

**Files:**
- Create: `apps/infra/openclaw/plugins/lightbulb-channel/src/runs.ts`
- Modify: `apps/infra/openclaw/plugins/lightbulb-channel/src/channel.ts` (constants :38-42, `buildOutboundBody` :190-205, `createOutbound` :238, `InboundDeps` :607-613, `createInboundHandler` :892-962, `GatewayDeps`/`startGatewayAccount` :964-1035)
- Modify: `apps/infra/openclaw/plugins/lightbulb-channel/src/index.ts`
- Test: `apps/infra/openclaw/plugins/lightbulb-channel/src/runs.test.ts`, `src/index.test.ts`

**Interfaces:**
- Consumes: Task 2's `admitRoomEvent`/`runRoomTurn`/`AmbientGateOutcome`/`RunContract`; Task 3's `FramePublisher`/`LiveFrameEvent`.
- Produces: `createRunSupervisor(deps) → { start, abort, steer, runIdForSession }`; `createRunCallbacks({fetch, env, log?})`; `createRunControlHandler({sdk, runs})`; `RUNS_ROUTE_PREFIX = "/plugins/lightbulb-channel/runs/"` (in `channel.ts`, next to `ROUTE_PREFIX`); `HEARTBEAT_MS = 30_000`; `RunUsage`; outbound reply body gains `runId` when a run is active for the room session.

- [ ] **Step 1: Write the failing supervisor tests**

```ts
// src/runs.test.ts
import { describe, expect, it, vi } from "vitest";
import { HEARTBEAT_MS, createRunSupervisor, transformAgentEvent } from "./runs.js";

const RUN = {
  runId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  roomId: "22222222-2222-4222-8222-222222222222",
  agentId: "friday",
  sessionKey: "agent:friday:lightbulb:channel:22222222-2222-4222-8222-222222222222",
  frameChannel: "/ws/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/rooms/22222222-2222-4222-8222-222222222222/cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  frameEndpoint: "https://abc123.appsync-api.us-east-1.amazonaws.com/event",
  threadRootMessageId: null,
  eventKind: "user_request" as const,
};
const ENTRY = { sessionId: "session-1", model: "claude-sonnet-4-6", inputTokens: 1200, outputTokens: 300, cacheRead: 5000, cacheWrite: 100, estimatedCostUsd: 0.0123 };

function harness() {
  let listener: ((evt: any) => void) | undefined;
  const sdk = {
    onAgentEvent: vi.fn((fn: (evt: any) => void) => { listener = fn; return () => { listener = undefined; }; }),
    resolveActiveEmbeddedRunSessionId: vi.fn(() => "session-1"),
    abortEmbeddedAgentRun: vi.fn(() => true),
    queueAgentHarnessMessage: vi.fn(() => true),
    getSessionEntry: vi.fn(() => ENTRY),
  };
  const callbacks = { send: vi.fn(async () => {}) };
  const frames = { push: vi.fn(), flush: vi.fn(async () => {}) };
  const runs = createRunSupervisor({ sdk, callbacks, frames, uuid: () => "e1", now: () => new Date("2026-09-12T03:15:00Z") });
  const emit = (evt: any) => listener?.({ runId: "sdk-run", seq: 1, ts: 0, sessionKey: RUN.sessionKey, ...evt });
  const types = () => frames.push.mock.calls.map((c: any[]) => c[2].payload.frame.type);
  const sent = () => callbacks.send.mock.calls.map((c: any[]) => c[0]);
  const settle = () => new Promise((r) => setImmediate(r));
  return { sdk, callbacks, frames, runs, emit, types, sent, settle };
}

describe("run supervisor", () => {
  it("reports started on the first lifecycle event, streams frames, ends completed with usage and a full-text resync", async () => {
    const h = harness();
    let finish!: (v: "dispatched") => void;
    h.runs.start(RUN, () => new Promise((resolve) => { finish = resolve; }));
    h.emit({ stream: "lifecycle", data: { phase: "start" } });
    h.emit({ stream: "assistant", data: { text: "Hel", delta: "Hel" } });
    h.emit({ stream: "assistant", data: { text: "Hello", delta: "lo" } });
    h.emit({ stream: "tool", data: { phase: "start", name: "exec", toolCallId: "t1" } });
    finish("dispatched");
    await h.settle();
    expect(h.sent()[0]).toMatchObject({ runId: RUN.runId, type: "run.started", sessionKey: RUN.sessionKey });
    expect(h.types()).toEqual(["run_active", "chunk", "chunk", "tool_start", "chunk", "done", "run_ended", "run_active"]);
    const resync = h.frames.push.mock.calls[4][2].payload.frame;
    expect(resync).toMatchObject({ content: "Hello", runId: RUN.runId, agentId: "friday" });
    expect(resync.append).toBeUndefined();
    expect(h.sdk.getSessionEntry).toHaveBeenCalledWith({ agentId: "friday", sessionKey: RUN.sessionKey });
    expect(h.sent().at(-1)).toEqual({
      runId: RUN.runId, type: "run.ended", at: "2026-09-12T03:15:00.000Z", outcome: "completed",
      usage: { model: "claude-sonnet-4-6", inputTokens: 1200, outputTokens: 300, cacheReadTokens: 5000, cacheWriteTokens: 100, estimatedCostUsd: 0.0123 },
    });
    expect(h.frames.flush).toHaveBeenCalledWith(RUN.frameChannel);
  });

  it("ends with error after a lifecycle error and never re-runs", async () => {
    const h = harness();
    h.runs.start(RUN, async () => { h.emit({ stream: "lifecycle", data: { phase: "start" } }); h.emit({ stream: "lifecycle", data: { phase: "error", error: "boom", stopReason: "timeout" } }); return "dispatched"; });
    await h.settle();
    expect(h.types()).toEqual(["run_active", "error", "run_ended", "run_active"]);
    expect(h.sent().at(-1)).toMatchObject({ type: "run.ended", outcome: "error", errorKind: "timeout", usage: expect.any(Object) });
  });

  it("reports ignored without frames or usage when the ambient gate declines", async () => {
    const h = harness();
    h.runs.start(RUN, async () => "ignored");
    await h.settle();
    expect(h.types()).toEqual([]);
    expect(h.sent()).toEqual([{ runId: RUN.runId, type: "run.ended", at: "2026-09-12T03:15:00.000Z", outcome: "ignored" }]);
  });

  it("heartbeats every 30 s while running and stops at the end", async () => {
    vi.useFakeTimers();
    const h = harness();
    let finish!: (v: "dispatched") => void;
    h.runs.start(RUN, () => new Promise((resolve) => { finish = resolve; }));
    h.emit({ stream: "lifecycle", data: { phase: "start" } });
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 2);
    expect(h.sent().filter((e: any) => e.type === "run.heartbeat")).toHaveLength(2);
    finish("dispatched");
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 2);
    expect(h.sent().filter((e: any) => e.type === "run.heartbeat")).toHaveLength(2);
    vi.useRealTimers();
  });

  it("aborts and steers through the SDK by session, and answers not_found otherwise", () => {
    const h = harness();
    expect(h.runs.abort(RUN.runId)).toBe("not_found");
    h.runs.start(RUN, () => new Promise(() => {}));
    expect(h.runs.abort(RUN.runId)).toBe("aborting");
    expect(h.sdk.resolveActiveEmbeddedRunSessionId).toHaveBeenCalledWith(RUN.sessionKey);
    expect(h.sdk.abortEmbeddedAgentRun).toHaveBeenCalledWith("session-1");
    expect(h.runs.steer(RUN.runId, "focus on tests")).toBe("accepted");
    expect(h.sdk.queueAgentHarnessMessage).toHaveBeenCalledWith("session-1", "focus on tests");
    expect(h.runs.runIdForSession(RUN.sessionKey)).toBe(RUN.runId);
  });
});

describe("transformAgentEvent", () => {
  it("mirrors the backend mapping", () => {
    expect(transformAgentEvent("assistant", { text: "ab", delta: "b" })).toEqual({ type: "chunk", content: "b", append: true });
    expect(transformAgentEvent("assistant", { text: "ab", replace: true })).toEqual({ type: "chunk", content: "ab" });
    expect(transformAgentEvent("thinking", { text: "" })).toBeNull();
    expect(transformAgentEvent("tool", { phase: "update", toolCallId: "t", partialResult: "x" })).toEqual({ type: "tool_progress", toolCallId: "t", partialResult: "x" });
    expect(transformAgentEvent("tool", { phase: "result", name: "exec", toolCallId: "t", isError: true, result: "r" })).toEqual({ type: "tool_error", tool: "exec", toolCallId: "t", result: "r" });
    expect(transformAgentEvent("command_output", { output: "o" })).toBeNull();
    expect(transformAgentEvent("compaction", { phase: "start" })).toEqual({ type: "agent_activity", activity: "compaction", title: "start", detail: { phase: "start" } });
    expect(transformAgentEvent("plan", { title: "Plan" })).toEqual({ type: "agent_activity", activity: "plan", title: "Plan", detail: { title: "Plan" } });
    expect(transformAgentEvent("approval", { phase: "requested" })).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- runs`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `src/runs.ts`**

```ts
/**
 * The async turn contract on the container side (spec §4/§5): one supervisor per
 * gateway process owns every in-flight room run, mirrors the run's agent events to
 * AppSync as the frame vocabulary clients already consume, and reports lifecycle and
 * usage to the backend over the hook-token route.
 */
import type { FramePublisher, LiveFrameEvent } from "./appsync.js";
import { IngressRejected, resolveEnv, type AmbientGateOutcome, type FetchLike, type RawEnv } from "./channel.js";

export const HEARTBEAT_MS = 30_000;
const CALLBACK_ATTEMPTS = 3;
const CALLBACK_TIMEOUT_MS = 10_000;

export type RunOutcome = "completed" | "error" | "aborted" | "ignored" | "gate_error";
export type RunUsage = { model: string; inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheWriteTokens: number; estimatedCostUsd: number | null };
export type RunEvent = { runId: string; type: "run.started" | "run.heartbeat" | "run.ended"; at: string; sessionKey?: string; outcome?: RunOutcome; errorKind?: string; usage?: RunUsage };
export type RunCallbacks = { send(event: RunEvent): Promise<void> };
export type AgentEvent = { runId: string; stream: string; data: Record<string, unknown>; sessionKey?: string };
/** The session-store row the gateway patches at run end (sessions.list reads the same fields). */
export type SessionUsageEntry = { model?: string; inputTokens?: number; outputTokens?: number; cacheRead?: number; cacheWrite?: number; estimatedCostUsd?: number };
export type RunSdk = {
  onAgentEvent: (listener: (evt: AgentEvent) => void) => () => void;
  resolveActiveEmbeddedRunSessionId: (sessionKey: string) => string | undefined;
  abortEmbeddedAgentRun: (sessionId: string) => boolean;
  queueAgentHarnessMessage: (sessionId: string, text: string) => boolean;
  getSessionEntry: (params: { agentId: string; sessionKey: string }) => SessionUsageEntry | undefined;
};
export type RunStart = {
  runId: string;
  roomId: string;
  agentId: string;
  sessionKey: string;
  frameChannel: string;
  frameEndpoint: string;
  threadRootMessageId: string | null;
  eventKind: "user_request" | "room_event";
};
type Frame = Record<string, unknown> & { type: string };
type ActiveRun = RunStart & {
  started: boolean;
  heartbeat?: ReturnType<typeof setInterval>;
  text: { done: string; current: string };
  terminal?: { phase: "end" | "error"; aborted: boolean; error?: string; stopReason?: string };
};

/** Port of the backend's GatewayConnection._transform_agent_event (the frames clients render). */
export function transformAgentEvent(stream: string, data: Record<string, unknown>): Frame | null {
  const text = typeof data.text === "string" ? data.text : "";
  const delta = typeof data.delta === "string" ? data.delta : "";
  const textFrame = (type: string): Frame | null => {
    if (data.replace !== true && delta) return { type, content: delta, append: true };
    return text ? { type, content: text } : null;
  };
  if (stream === "assistant") return textFrame("chunk");
  if (stream === "reasoning" || stream === "thinking") return textFrame("thinking");
  if (stream === "tool") {
    const phase = data.phase;
    const name = typeof data.name === "string" ? data.name : "";
    const toolCallId = typeof data.toolCallId === "string" ? data.toolCallId : "";
    if (phase === "update") {
      if (!toolCallId) return null;
      return { type: "tool_progress", toolCallId, ...(name ? { tool: name } : {}), ...("partialResult" in data ? { partialResult: data.partialResult } : {}) };
    }
    if (!name) return null;
    if (phase === "start") return { type: "tool_start", tool: name, ...(toolCallId ? { toolCallId } : {}), ...("args" in data ? { args: data.args } : {}) };
    if (phase === "result") {
      return {
        type: data.isError ? "tool_error" : "tool_end",
        tool: name,
        ...(toolCallId ? { toolCallId } : {}),
        ...("result" in data ? { result: data.result } : {}),
        ...(data.meta ? { meta: data.meta } : {}),
      };
    }
    return null;
  }
  if (stream === "command_output") {
    const toolCallId = typeof data.toolCallId === "string" ? data.toolCallId : "";
    return toolCallId ? { type: "command_output", toolCallId, ...("output" in data ? { output: data.output } : {}) } : null;
  }
  if (stream === "run_status" || stream === "compaction") {
    const phase = typeof data.phase === "string" ? data.phase : "";
    return phase ? { type: "agent_activity", activity: stream, title: phase, detail: data } : null;
  }
  if (stream === "plan" || stream === "patch" || stream === "item" || stream === "acp") {
    return { type: "agent_activity", activity: stream, title: typeof data.title === "string" ? data.title : "", detail: data };
  }
  return null; // approval and lifecycle are consumed by the supervisor itself
}

export function createRunCallbacks(deps: { fetch: FetchLike; env: () => RawEnv; log?: (message: string) => void }): RunCallbacks {
  return {
    async send(event) {
      const resolved = resolveEnv(deps.env());
      const url = `${resolved.base}/api/v1/internal/messaging/runs/${encodeURIComponent(resolved.ownerId)}/events`;
      for (let attempt = 0; attempt < CALLBACK_ATTEMPTS; attempt++) {
        try {
          const res = await deps.fetch(url, {
            method: "POST",
            headers: { "content-type": "application/json", Authorization: `Bearer ${resolved.token}` },
            body: JSON.stringify(event),
            signal: AbortSignal.timeout(CALLBACK_TIMEOUT_MS),
          });
          if (res.status < 500) return; // 200 applied/replayed and 404 unknown are both final
        } catch {
          // network failure: retry below
        }
        await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
      }
      deps.log?.(`lightbulb: run callback ${event.type} undelivered for ${event.runId}`);
    },
  };
}

const asCount = (value: unknown) => (typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.round(value) : 0);

export function createRunSupervisor(deps: {
  sdk: RunSdk;
  callbacks: RunCallbacks;
  frames: FramePublisher;
  uuid: () => string;
  now: () => Date;
  log?: (message: string) => void;
}) {
  const runs = new Map<string, ActiveRun>();
  const bySession = new Map<string, string>();
  let unsubscribe: (() => void) | undefined;

  const iso = () => deps.now().toISOString();
  function frame(run: ActiveRun, body: Frame): void {
    const payload: Record<string, unknown> = {
      roomId: run.roomId,
      agentId: run.agentId,
      runId: run.runId,
      frame: { ...body, runId: run.runId, agentId: run.agentId },
    };
    if (run.threadRootMessageId) payload.threadRootMessageId = run.threadRootMessageId;
    const event = { schemaVersion: 1, eventId: deps.uuid(), type: "room.run.frame", occurredAt: iso(), roomId: run.roomId, payload } as LiveFrameEvent;
    deps.frames.push(run.frameEndpoint, run.frameChannel, event);
  }
  function markStarted(run: ActiveRun): void {
    if (run.started) return;
    run.started = true;
    void deps.callbacks.send({ runId: run.runId, type: "run.started", at: iso(), sessionKey: run.sessionKey });
    frame(run, { type: "run_active", active: true });
    run.heartbeat = setInterval(() => void deps.callbacks.send({ runId: run.runId, type: "run.heartbeat", at: iso() }), HEARTBEAT_MS);
  }
  function trackText(run: ActiveRun, data: Record<string, unknown>): void {
    // Upstream clears `text` at every message start, so a text that no longer extends
    // the current message is a new message: bank the old one for the final resync.
    const text = typeof data.text === "string" ? data.text : "";
    const delta = typeof data.delta === "string" ? data.delta : "";
    if (run.text.current && text && !text.startsWith(run.text.current)) {
      run.text.done += run.text.current;
      run.text.current = "";
    }
    run.text.current = data.replace !== true && delta ? (text || run.text.current + delta) : text || run.text.current;
  }
  function usageFor(run: ActiveRun): RunUsage {
    const entry = deps.sdk.getSessionEntry({ agentId: run.agentId, sessionKey: run.sessionKey }) ?? {};
    const cost = entry.estimatedCostUsd;
    return {
      model: typeof entry.model === "string" && entry.model ? entry.model : "unknown",
      inputTokens: asCount(entry.inputTokens),
      outputTokens: asCount(entry.outputTokens),
      cacheReadTokens: asCount(entry.cacheRead),
      cacheWriteTokens: asCount(entry.cacheWrite),
      estimatedCostUsd: typeof cost === "number" && Number.isFinite(cost) && cost > 0 ? cost : null,
    };
  }
  function onEvent(evt: AgentEvent): void {
    const runId = evt.sessionKey ? bySession.get(evt.sessionKey) : undefined;
    const run = runId ? runs.get(runId) : undefined;
    if (!run) return;
    if (evt.stream === "lifecycle") {
      const phase = evt.data.phase;
      if (phase === "start") markStarted(run);
      else if (phase === "end" || phase === "error") {
        run.terminal = {
          phase,
          aborted: evt.data.aborted === true,
          error: typeof evt.data.error === "string" ? evt.data.error : undefined,
          stopReason: typeof evt.data.stopReason === "string" ? evt.data.stopReason : undefined,
        };
      }
      return;
    }
    markStarted(run); // any event for the session proves the run is live
    const transformed = transformAgentEvent(evt.stream, evt.data);
    if (!transformed) return;
    if (transformed.type === "chunk") trackText(run, evt.data);
    frame(run, transformed);
  }
  async function finish(run: ActiveRun, gate?: "ignored" | "gate_error", failure?: unknown): Promise<void> {
    if (run.heartbeat) clearInterval(run.heartbeat);
    let outcome: RunOutcome;
    let errorKind: string | undefined;
    let message = "Agent run failed";
    if (gate) outcome = gate;
    else if (run.terminal?.aborted) {
      outcome = "aborted";
      message = "Agent run was cancelled";
    } else if (failure !== undefined || run.terminal?.phase === "error") {
      outcome = "error";
      errorKind = run.terminal?.stopReason ?? (failure instanceof IngressRejected ? "dispatch_failed" : "unknown");
      message = run.terminal?.error ?? (failure instanceof Error ? failure.message : message);
    } else outcome = "completed";
    if (run.started) {
      if (outcome === "completed") {
        const text = run.text.done + run.text.current;
        if (text) frame(run, { type: "chunk", content: text });
        frame(run, { type: "done" });
      } else if (outcome === "error" || outcome === "aborted") {
        frame(run, { type: "error", message, ...(errorKind ? { code: errorKind } : {}) });
      }
      frame(run, { type: "run_ended", outcome: outcome === "completed" ? "done" : outcome });
      frame(run, { type: "run_active", active: false });
      await deps.frames.flush(run.frameChannel);
    }
    runs.delete(run.runId);
    if (bySession.get(run.sessionKey) === run.runId) bySession.delete(run.sessionKey);
    await deps.callbacks.send({
      runId: run.runId,
      type: "run.ended",
      at: iso(),
      outcome,
      ...(errorKind ? { errorKind } : {}),
      ...(gate ? {} : { usage: usageFor(run) }),
    });
  }
  function sessionIdFor(runId: string): string | undefined {
    const run = runs.get(runId);
    return run ? deps.sdk.resolveActiveEmbeddedRunSessionId(run.sessionKey) : undefined;
  }

  return {
    /** The ack has been written; run the turn to completion in the background. */
    start(spec: RunStart, turn: () => Promise<AmbientGateOutcome>): void {
      unsubscribe ??= deps.sdk.onAgentEvent(onEvent);
      const run: ActiveRun = { ...spec, started: false, text: { done: "", current: "" } };
      runs.set(spec.runId, run);
      bySession.set(spec.sessionKey, spec.runId);
      void turn().then(
        (gate) => finish(run, gate === "dispatched" ? undefined : gate),
        (failure) => finish(run, undefined, failure),
      );
    },
    abort(runId: string): "aborting" | "not_found" {
      const sessionId = sessionIdFor(runId);
      return sessionId && deps.sdk.abortEmbeddedAgentRun(sessionId) ? "aborting" : "not_found";
    },
    // ponytail: queueAgentHarnessMessage is deprecated upstream; if it stops queuing,
    // dispatch the steer text as a second inbound turn on the same session instead.
    steer(runId: string, text: string): "accepted" | "not_found" {
      const sessionId = sessionIdFor(runId);
      return sessionId && deps.sdk.queueAgentHarnessMessage(sessionId, text) ? "accepted" : "not_found";
    },
    runIdForSession(sessionKey: string): string | undefined {
      return bySession.get(sessionKey);
    },
  };
}
export type RunSupervisor = ReturnType<typeof createRunSupervisor>;
```

- [ ] **Step 4: Run the supervisor tests**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- runs`
Expected: PASS.

- [ ] **Step 5: Write the failing handler tests**

Append to `src/index.test.ts`. `responseRecorder()` returns the response (with `.statusCode` and `.body`); `request()` returns a bare request; `inboundSdk({ readJsonWebhookBodyOrReject })` decides the parsed body; `handlerDeps` builds `InboundDeps` — give its `deps` object a `runs: fakeRuns()` field.

```ts
import { createRunControlHandler, RUNS_ROUTE_PREFIX } from "./channel.js";

const OTHER_MESSAGE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const OTHER_RUN = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";

function fakeRuns() {
  return { start: vi.fn(), abort: vi.fn(() => "aborting" as const), steer: vi.fn(() => "accepted" as const), runIdForSession: vi.fn(() => RUN_ID) };
}
function withBody(body: unknown, overrides: Partial<LightbulbSdk> = {}) {
  return inboundSdk({ readJsonWebhookBodyOrReject: vi.fn(async () => ({ ok: true, value: body })) as any, ...overrides });
}

it("acks 202 accepted before the turn runs, 200 replayed on redelivery, 409 when not admitted", async () => {
  const dispatch = vi.fn(() => new Promise<never>(() => {})); // a turn that never ends
  const runs = fakeRuns();
  const handler = createInboundHandler({ sdk: withBody(ingress()), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs });
  const first = responseRecorder();
  await handler(request(), first);
  expect(first.statusCode).toBe(202);
  expect(JSON.parse(first.body)).toEqual({ eventId: MESSAGE, runId: RUN_ID, status: "accepted" });
  expect(runs.start).toHaveBeenCalledTimes(1);
  expect(runs.start.mock.calls[0][0]).toMatchObject({ runId: RUN_ID, roomId: ROOM, agentId: FRIDAY, frameChannel: RUN.frameChannel, frameEndpoint: RUN.frameEndpoint, eventKind: "user_request" });
  expect(dispatch).not.toHaveBeenCalled(); // the turn is the supervisor's to start
  const second = responseRecorder();
  await handler(request(), second);
  expect(second.statusCode).toBe(200);
  expect(JSON.parse(second.body)).toEqual({ eventId: MESSAGE, runId: RUN_ID, status: "replayed" });
  expect(runs.start).toHaveBeenCalledTimes(1);
  const declined = responseRecorder();
  const declining = withBody(ingress({ eventId: OTHER_MESSAGE, message: canonicalMessage({ messageId: OTHER_MESSAGE }), run: { ...RUN, runId: OTHER_RUN } }), {
    resolveStableChannelMessageIngress: vi.fn(async () => ({ ...dispatchedIngress(), ingress: { admission: "drop" } })) as any,
  });
  await createInboundHandler({ sdk: declining, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs })(request(), declined);
  expect(declined.statusCode).toBe(409);
  expect(runs.start).toHaveBeenCalledTimes(1);
});

it("rejects a body without the run contract", async () => {
  const { run: _run, ...legacy } = ingress();
  const rec = responseRecorder();
  await createInboundHandler({ sdk: withBody(legacy), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV, runs: fakeRuns() })(request(), rec);
  expect(rec.statusCode).toBe(400);
});

it("routes abort and steer to the supervisor", async () => {
  const runs = fakeRuns();
  const at = (action: string, body: unknown = {}) => Object.assign(request(), { url: `${RUNS_ROUTE_PREFIX}${RUN_ID}/${action}`, sdkBody: body });
  const handler = createRunControlHandler({ sdk: inboundSdk({ readJsonWebhookBodyOrReject: vi.fn(async ({ req }: any) => ({ ok: true, value: req.sdkBody })) as any }), runs });
  const abort = responseRecorder();
  await handler(at("abort"), abort);
  expect(abort.statusCode).toBe(202);
  expect(runs.abort).toHaveBeenCalledWith(RUN_ID);
  const steer = responseRecorder();
  await handler(at("steer", { text: "shorter" }), steer);
  expect(steer.statusCode).toBe(202);
  expect(runs.steer).toHaveBeenCalledWith(RUN_ID, "shorter");
  const bad = responseRecorder();
  await handler(at("steer", {}), bad);
  expect(bad.statusCode).toBe(400);
  runs.abort.mockReturnValueOnce("not_found" as any);
  const gone = responseRecorder();
  await handler(at("abort"), gone);
  expect(gone.statusCode).toBe(404);
  const unknown = responseRecorder();
  await handler(Object.assign(request(), { url: "/plugins/lightbulb-channel/runs/nope" }), unknown);
  expect(unknown.statusCode).toBe(404);
});

it("stamps the active run id on the outbound reply body", () => {
  const runs = fakeRuns();
  const body = buildOutboundBody({ to: `lightbulb:${ROOM}`, accountId: FRIDAY, text: "hi", deliveryQueueId: "q", deliveryPartIndex: 0 } as any, runs);
  expect(body.runId).toBe(RUN_ID);
  expect(runs.runIdForSession).toHaveBeenCalledWith(sessionKeyFor(FRIDAY, ROOM));
});
```

(`canonicalMessage(overrides)` and `dispatchedIngress()` already exist in the file; if `canonicalMessage` does not accept an override for `messageId`, add one — `eventId` must equal `message.messageId`.)

- [ ] **Step 6: Run to verify failure**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- index`
Expected: FAIL (`runs` is not a known dep; `createRunControlHandler` undefined; a body without `run` is still accepted).

- [ ] **Step 7: Wire the handler, the control route and the outbound run id**

In `src/channel.ts`:

```ts
export const RUNS_ROUTE_PREFIX = "/plugins/lightbulb-channel/runs/";   // next to ROUTE_PREFIX
import type { RunSupervisor } from "./runs.js";                          // type-only: runs.ts imports values from this file

export type InboundDeps = {
  sdk: LightbulbSdk;
  cfg: OpenClawConfig;
  accountId: string;
  channelRuntime: PluginRuntime["channel"];
  env: () => RawEnv;
  runs: RunSupervisor;
};
```

`buildOutboundBody(ctx, runs?: Pick<RunSupervisor, "runIdForSession">)`:

```ts
  const runId = runs?.runIdForSession(sessionKeyFor(agentId, roomId));
  return {
    eventId: resolveOutboundEventId(ctx),
    roomId,
    agentId,
    text: ctx.text,
    replyToMessageId: ctx.replyToId ?? null,
    threadRootMessageId: ctx.threadId == null ? null : String(ctx.threadId),
    ...(runId ? { runId } : {}),
  };
```

`OutboundBody` gains `runId?: string`; `createOutbound(deps: { fetch; env; runs?: Pick<RunSupervisor, "runIdForSession"> })` passes `deps.runs` into `buildOutboundBody`.

Replace the `try { const outcome = await effectOnce.runOnce(...)` block in `createInboundHandler` with:

```ts
    try {
      const outcome = await effectOnce.runOnce<AdmittedRoomEvent | "not_admitted">({
        eventId: event.eventId,
        effect: "dispatch",
        run: async () => {
          try {
            return await admitRoomEvent(deps, event);
          } catch (error) {
            if (error instanceof IngressRejected && error.status === 409) return "not_admitted";
            throw error;
          }
        },
      });
      const { run } = event;
      if (outcome.kind === "replayed") return json(res, 200, { eventId: event.eventId, runId: run.runId, status: "replayed" });
      if (outcome.value === "not_admitted") {
        return json(res, 409, { eventId: event.eventId, error: "not_admitted", message: "lightbulb: channel declined the event" });
      }
      // The claim is committed and the caller is answered before the turn starts: a
      // death in between is a run that never started, which the backend sweeper reports
      // as an error rather than re-running (spec §2).
      json(res, 202, { eventId: event.eventId, runId: run.runId, status: "accepted" });
      const admitted = outcome.value;
      deps.runs.start(
        {
          runId: run.runId,
          roomId: event.room.id,
          agentId: accountId,
          sessionKey: sessionKeyFor(accountId, event.room.id),
          frameChannel: run.frameChannel,
          frameEndpoint: run.frameEndpoint,
          threadRootMessageId: event.message.threadRootMessageId ?? null,
          eventKind: event.eventKind,
        },
        () => runRoomTurn(deps, event, admitted, invocation),
      );
      return true;
    } catch (error) { /* unchanged */ }
```

Add the control handler after `createInboundHandler`:

```ts
const RUN_CONTROL_RE = /^\/plugins\/lightbulb-channel\/runs\/([0-9a-f-]{36})\/(abort|steer)$/;

export function createRunControlHandler(deps: { sdk: Pick<LightbulbSdk, "applyBasicWebhookRequestGuards" | "readJsonWebhookBodyOrReject">; runs: RunSupervisor }) {
  return async (req: IncomingMessage, res: ServerResponse): Promise<boolean> => {
    if (!deps.sdk.applyBasicWebhookRequestGuards({ req, res, allowMethods: ["POST"], requireJsonContentType: true })) return true;
    const match = RUN_CONTROL_RE.exec((req.url ?? "").split("?")[0]);
    if (!match) return json(res, 404, { error: "not_found" });
    const [, runId, action] = match;
    const body = await deps.sdk.readJsonWebhookBodyOrReject({ req, res, maxBytes: MAX_TEXT_BYTES + 1_024, timeoutMs: 10_000, invalidJsonMessage: "invalid JSON body" });
    if (!body.ok) return true;
    if (action === "abort") {
      const state = deps.runs.abort(runId);
      return json(res, state === "aborting" ? 202 : 404, { runId, state });
    }
    const text = (body.value as Record<string, unknown> | null)?.text;
    if (typeof text !== "string" || !text.trim() || Buffer.byteLength(text, "utf8") > MAX_TEXT_BYTES) {
      return json(res, 400, { error: "invalid_steer", message: "text is required" });
    }
    const state = deps.runs.steer(runId, text);
    return json(res, state === "accepted" ? 202 : 404, { runId, state });
  };
}
```

`GatewayDeps` gains `runs: RunSupervisor` and `runControlHandler: ReturnType<typeof createRunControlHandler>`; in `startGatewayAccount` pass `runs: deps.runs` into `createInboundHandler` and register the control route once per process (it is shared by every account and harmless when idle):

```ts
let controlsRegistered = false;
// …inside startGatewayAccount, right after the inbound route registration:
    if (!controlsRegistered) {
      deps.registerPluginHttpRoute({
        path: RUNS_ROUTE_PREFIX,
        auth: "gateway",
        match: "prefix",
        pluginId: PLUGIN_ID,
        source: "lightbulb-channel-runs",
        replaceExisting: true,
        throwOnFailure: true,
        handler: deps.runControlHandler,
      });
      controlsRegistered = true;
    }
```

In `src/index.ts`:

```ts
import { randomUUID } from "node:crypto";
import { abortEmbeddedAgentRun, onAgentEvent, queueAgentHarnessMessage, resolveActiveEmbeddedRunSessionId } from "openclaw/plugin-sdk/agent-harness-runtime";
import { getSessionEntry } from "openclaw/plugin-sdk/session-store-runtime";
import { createFramePublisher, ecsCredentialsProvider } from "./appsync.js";
import { createRunCallbacks, createRunSupervisor } from "./runs.js";
// add createRunControlHandler to the ./channel.js import list

const fetchLike = (url: string, init: RequestInit) => fetch(url, init);
const runs = createRunSupervisor({
  sdk: {
    onAgentEvent,
    resolveActiveEmbeddedRunSessionId,
    abortEmbeddedAgentRun,
    queueAgentHarnessMessage,
    getSessionEntry: ({ agentId, sessionKey }) => getSessionEntry({ agentId, sessionKey, env: process.env }) as any,
  },
  callbacks: createRunCallbacks({ fetch: fetchLike, env: () => process.env, log: (m) => console.warn(m) }),
  frames: createFramePublisher({
    fetch: fetchLike,
    region: process.env.AWS_REGION ?? "us-east-1",
    credentials: ecsCredentialsProvider(() => process.env, fetchLike),
    log: (m) => console.warn(m),
  }),
  uuid: () => randomUUID(),
  now: () => new Date(),
});
const outbound = createOutbound({ fetch: fetchLike, env: () => process.env, runs });
// …
    gateway: {
      startAccount: async (ctx) =>
        await startGatewayAccount(ctx, {
          sdk: { /* unchanged */ },
          env: () => process.env,
          registerPluginHttpRoute,
          waitUntilAbort,
          channelReadyPatch,
          channelStoppedPatch,
          runs,
          runControlHandler: createRunControlHandler({ sdk: { applyBasicWebhookRequestGuards, readJsonWebhookBodyOrReject }, runs }),
        }),
    },
```

Every existing test that builds `InboundDeps` or `GatewayDeps` literally gets `runs: fakeRuns()` (and `runControlHandler: vi.fn()` for gateway deps).

- [ ] **Step 8: Run the whole plugin suite and the build**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test && npm run build && git status --short -- dist`
Expected: PASS; `dist/` shows the rebuilt files — commit them (CI checks `dist/` is clean after its own build).

- [ ] **Step 9: Commit**

```bash
git add -A apps/infra/openclaw/plugins/lightbulb-channel
git commit -m "feat(plugin): async turn contract — immediate ack, background turn, callbacks with usage, direct frames, abort/steer"
```

---

### Task 5: Plugin PR and image build

**Files:**
- none in this task beyond the PR itself

**Interfaces:**
- Produces: the ECR tag `lightbulb/openclaw-extended:2026.9.3-isol8.2-<shortsha>` the cutover pins (Task 14).

- [ ] **Step 1: Open the plugin PR and merge it**

Push `arch/async-turn-contract` (Tasks 1–4), open the PR, merge after the required check. `build-openclaw-image.yml` runs the plugin proof tests (`npm test`, `npm run build`, `dist` clean check, `config validate`) and pushes the image. Nothing runs it until the pins move.

- [ ] **Step 2: Read the tag and digests**

```bash
gh run list --repo TheLightbulbCompany/lightbulb --workflow build-openclaw-image.yml --limit 1 --json databaseId,conclusion
gh run view <id> --repo TheLightbulbCompany/lightbulb --log | grep -o 'openclaw-extended:2026\.9\.3-isol8\.2-[0-9a-f]\{7\}' | sort -u
aws ecr describe-images --repository-name lightbulb/openclaw-extended --image-ids imageTag=<tag> --profile isol8-admin --query 'imageDetails[0].imageDigest'
```

Record the tag and digest for Task 14.

---

### Task 6: Infra — `ws` namespace, tenancy handler, IAM, sweep GSI, queue redrive

**Files:**
- Modify: `apps/infra/lib/stacks/api-stack.ts` (handler code :131-259, table :537-554, queue :565-583, namespaces :638-664, projector grants :706-707, oldest-age alarm :889-905)
- Modify: `apps/infra/lib/stacks/service-stack.ts:353-360`
- Modify: `apps/infra/lib/stacks/container-stack.ts` (base policy :320-352, boundary :358-432)
- Modify: `apps/infra/lib/stacks/observability-stack.ts:1028-1041`
- Test: `apps/infra/test/api-stack.test.ts` (:252-258, :285-305, :484-624), `apps/infra/test/service-stack.test.ts:1058-1082`, `apps/infra/test/container-stack.test.ts:722-746`

**Interfaces:**
- Produces: namespace `ws`; GSI `by-run-deadline` (`sweep` S, `deadlineAt` N, KEYS_ONLY); queue `visibilityTimeout` 60 s, `maxReceiveCount` 5; `appsync:EventPublish` on the `ws` namespace for backend, projector and owner roles; P20 queue-wait alarm at 30 s.

- [ ] **Step 1: Update the tests first**

`api-stack.test.ts` :295 `VisibilityTimeout: 60`, :298 `maxReceiveCount: 5`. In the messaging describe block replace the `for (const name of ["rooms", "users"])` loop with one `name = "ws"` assertion plus `API_TEMPLATE.resourceCountIs("AWS::AppSync::ChannelNamespace", 1)`; keep the code-content expectations and add:

```ts
    expect(code).toContain("'ws'");
    expect(code).toContain("userArn");
    expect(code).toContain("owner-bedrock-");
    expect(code).toContain("workspaceId");
```

Projector: `expect(JSON.stringify(publishResources)).toContain("channelNamespace/ws")` and `.not.toContain("channelNamespace/rooms")`. Add a GSI assertion next to the `by-member` one (:252):

```ts
        Match.objectLike({
          IndexName: "by-run-deadline",
          KeySchema: [
            { AttributeName: "sweep", KeyType: "HASH" },
            { AttributeName: "deadlineAt", KeyType: "RANGE" },
          ],
          Projection: { ProjectionType: "KEYS_ONLY" },
        }),
```

`service-stack.test.ts:1071-1074` → `Resource: "arn:aws:appsync:us-east-1:877352799272:apis/events-dev/channelNamespace/ws"`.

`container-stack.test.ts`: add one test after the boundary test:

```ts
  test("owner base policy and boundary both allow publishing to the ws namespace", () => {
    const policies = Object.values(template.findResources("AWS::IAM::ManagedPolicy")) as any[];
    for (const [name, sid] of [
      ["lightbulb-dev-owner-bedrock-base", "AppSyncWsPublish"],
      ["lightbulb-dev-owner-bedrock-boundary", "BoundaryAppSyncWsPublish"],
    ]) {
      const policy = policies.find((p) => p.Properties.ManagedPolicyName === name);
      const statement = (policy.Properties.PolicyDocument.Statement as any[]).find((s) => s.Sid === sid);
      expect(statement.Action).toBe("appsync:EventPublish");
      expect(JSON.stringify(statement.Resource)).toContain("apis/*/channelNamespace/ws");
    }
  });
```

- [ ] **Step 2: Run the infra tests to verify they fail**

Run: `cd apps/infra && pnpm test -- api-stack service-stack container-stack`
Expected: FAIL on the new assertions.

- [ ] **Step 3: Replace the two handlers with one**

Delete `roomsHandlerCode` and `usersHandlerCode`; add:

```ts
// One namespace, workspace-scoped paths, tenancy decided here (spec §3). Segments are
// UUID-only; every decision strong-reads the base table. IAM admits publishers to the
// namespace; this handler narrows a per-owner container task role to its own rows.
const wsHandlerCode = (tableName: string, env: string) => `
import { util } from '@aws-appsync/utils';
const TABLE = '${tableName}';
const OWNER_ROLE_PREFIX = 'lightbulb-${env}-owner-bedrock-';
// String pattern via util.matches: APPSYNC_JS has no regex literals.
const UUID = '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$';
// /ws/{workspaceId}/rooms/{roomId}/{deliveryKey} or /ws/{workspaceId}/users/{userKey}
function parsePath(ctx) {
  const p = ctx.info.channel.path.split('/');
  if (p.length < 5 || p[0] !== '' || p[1] !== 'ws' || !util.matches(UUID, p[2])) util.unauthorized();
  if (p[3] === 'rooms' && p.length === 6 && util.matches(UUID, p[4]) && util.matches(UUID, p[5])) {
    return { kind: 'rooms', workspaceId: p[2], roomId: p[4], deliveryKey: p[5] };
  }
  if (p[3] === 'users' && p.length === 5 && util.matches(UUID, p[4])) {
    return { kind: 'users', workspaceId: p[2], userKey: p[4] };
  }
  util.unauthorized();
}
function metaKey(path) {
  return path.kind === 'rooms' ? { pk: 'ROOM#' + path.roomId, sk: 'META' } : { pk: 'STREAM#' + path.userKey, sk: 'META' };
}
function denyOnReadFailure(ctx) {
  if (ctx.error || !ctx.result) util.unauthorized();
}
function metaMatchesPath(meta, path) {
  if (!meta || meta.workspaceId !== path.workspaceId) return false;
  return path.kind === 'rooms' ? meta.deliveryKey === path.deliveryKey : meta.active === true;
}
// arn:aws:sts::{account}:assumed-role/{roleName}/{session}; null when the caller is not
// an owner task role (backend service role, projector) — IAM already admitted it.
function callerOwnerId(ctx) {
  const arn = ctx.identity && ctx.identity.userArn;
  if (typeof arn !== 'string') util.unauthorized();
  const parts = arn.split('/');
  if (parts.length !== 3 || parts[0].indexOf(':assumed-role') === -1) util.unauthorized();
  if (parts[1].indexOf(OWNER_ROLE_PREFIX) !== 0) return null;
  return parts[1].substring(OWNER_ROLE_PREFIX.length);
}
export const onSubscribe = {
  request(ctx) {
    const path = parsePath(ctx);
    const sub = ctx.identity && ctx.identity.sub;
    if (typeof sub !== 'string' || sub.length === 0) util.unauthorized();
    const keys = [util.dynamodb.toMapValues(metaKey(path))];
    if (path.kind === 'rooms') keys.push(util.dynamodb.toMapValues({ pk: 'ROOM#' + path.roomId, sk: 'MEMBER#human#' + sub }));
    return { operation: 'BatchGetItem', tables: { [TABLE]: { keys, consistentRead: true } } };
  },
  response(ctx) {
    denyOnReadFailure(ctx);
    const path = parsePath(ctx);
    const rows = ctx.result.data && ctx.result.data[TABLE];
    const pending = ctx.result.unprocessedKeys && ctx.result.unprocessedKeys[TABLE];
    if (!Array.isArray(rows) || (Array.isArray(pending) && pending.length > 0)) util.unauthorized();
    const meta = rows[0];
    if (!metaMatchesPath(meta, path)) util.unauthorized();
    if (path.kind === 'rooms' ? !rows[1] : meta.userId !== ctx.identity.sub) util.unauthorized();
  },
};
export const onPublish = {
  request(ctx) {
    return { operation: 'GetItem', key: util.dynamodb.toMapValues(metaKey(parsePath(ctx))), consistentRead: true };
  },
  response(ctx) {
    denyOnReadFailure(ctx);
    const path = parsePath(ctx);
    if (!metaMatchesPath(ctx.result, path)) util.unauthorized();
    const owner = callerOwnerId(ctx);
    if (owner !== null && ctx.result.ownerId !== owner) util.unauthorized();
    return ctx.events;
  },
};
`;
```

(If Task 1 recorded a field other than `userArn`, use that name.)

- [ ] **Step 4: Rewire the namespace, GSI, queue, grants and alarms**

```ts
    messagingTable.addGlobalSecondaryIndex({
      indexName: "by-run-deadline",
      // Sparse: only open run-ledger rows carry `sweep`; the sweeper queries
      // sweep = "open" AND deadlineAt < now. ponytail: one constant partition —
      // fine while open runs stay in the low thousands.
      partitionKey: { name: "sweep", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "deadlineAt", type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.KEYS_ONLY,
    });
```

Queue: `visibilityTimeout: cdk.Duration.seconds(60)`, `deadLetterQueue: { queue: workDlq, maxReceiveCount: 5 }`, with the comment replaced by: `// The plugin acks within milliseconds (async turn contract); a pointer not acked in 60 s is redelivered, and five failures are a poison pointer.` Oldest-age alarm: `threshold: 5 * 60`, description `"PAGE: durable OpenClaw work has waited longer than five minutes."`, and drop the "turns of 30 minutes are routine" comment.

Namespace:

```ts
    const wsNamespace = eventApi.addChannelNamespace("WsNamespace", {
      channelNamespaceName: "ws",
      code: appsync.Code.fromInline(wsHandlerCode(messagingTable.tableName, env)),
      publishHandlerConfig: { dataSource: messagingDataSource },
      subscribeHandlerConfig: { dataSource: messagingDataSource },
      authorizationConfig: {
        publishAuthModeTypes: [appsync.AppSyncAuthorizationType.IAM],
        subscribeAuthModeTypes: [appsync.AppSyncAuthorizationType.OIDC],
      },
    });
    // …
    wsNamespace.grantPublish(projectorFn);
```

`service-stack.ts:356-359` → `resources: [`${props.appsyncEventsApiArn}/channelNamespace/ws`]`.

`container-stack.ts` base policy and boundary each gain (sid `BoundaryAppSyncWsPublish` in the boundary):

```ts
        // Async turn contract: the container publishes its run frames straight to
        // AppSync Events. Namespace-wide grant; per-owner tenancy is enforced by the
        // ws namespace handler (api-stack), which only IAM callers reach.
        new iam.PolicyStatement({
          sid: "AppSyncWsPublish",
          actions: ["appsync:EventPublish"],
          resources: [`arn:aws:appsync:${this.region}:${this.account}:apis/*/channelNamespace/ws`],
        }),
```

`observability-stack.ts:1033` → `threshold: 30_000`, description `"PAGE: p99 wait before the worker picks work up exceeds 30s - the worker is falling behind the queue."`.

- [ ] **Step 5: Run tests and synth**

Run: `cd apps/infra && pnpm test -- api-stack service-stack container-stack observability-stack && npx cdk synth > /dev/null && git status --short .github/workflows/deploy.yml`
Expected: PASS; commit `deploy.yml` if it changed.

- [ ] **Step 6: Commit**

```bash
git add apps/infra .github/workflows/deploy.yml
git commit -m "feat(infra): ws namespace with workspace tenancy, container publish grant, run-deadline GSI, 60s/5 redrive"
```

---

### Task 7: Backend — workspace id, publisher and projector paths, backfill

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py` (docstring :1-25, key builders :88-149, `create_room` :428-527, `ensure_user_stream` :1199-1261)
- Modify: `apps/backend/core/services/live_events.py:95-122`
- Modify: `apps/backend/core/services/messaging.py:470-475` (`LiveConfig`)
- Modify: `apps/backend/routers/messaging.py:212-229`
- Modify: `apps/infra/lambda/messaging-projector/index.py` (:391-424 `_notify_members`, :488-500 `_project_message_update`, :502-524 `_project_message`, :540-560 `_project_live`)
- Create: `apps/backend/scripts/backfill_workspace_ids.py` (one-shot; deleted in Task 14)
- Test: `apps/backend/tests/unit/repositories/test_messaging_repo.py`, `apps/backend/tests/unit/services/test_live_events.py` (create if absent), `apps/infra/lambda/messaging-projector/test_index.py`

**Interfaces:**
- Produces: `messaging_repo.ensure_workspace_id(owner_id) -> str`; `workspaceId` on room META and stream META; `LiveConfig.workspace_id` (required); publisher/projector paths `/ws/{workspaceId}/…`.

- [ ] **Step 1: Failing repo tests**

Append to `tests/unit/repositories/test_messaging_repo.py` (the `table` fixture and a `_room()`-style creator around :60-77 already exist; mirror that `create_room` call):

```python
@pytest.mark.asyncio
async def test_ensure_workspace_id_mints_once_and_is_stamped_on_rooms_and_streams(table):
    first = await repo.ensure_workspace_id(OWNER)
    assert first == await repo.ensure_workspace_id(OWNER)
    assert first != await repo.ensure_workspace_id("owner_other")
    room_id, meta = await _room()  # the file's existing helper around create_room
    assert meta["workspaceId"] == first
    assert (await repo.get_room(room_id))["workspaceId"] == first
    key = await repo.ensure_user_stream(OWNER, USER_A)
    assert (await repo.get_stream(key))["workspaceId"] == first
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_messaging_repo.py -q -k workspace`
Expected: FAIL (`ensure_workspace_id` missing).

- [ ] **Step 3: Implement**

`messaging_repo.py` — add to the docstring registry `OWNER#<owner>   WORKSPACE   opaque AppSync channel-path segment` and `OWNER#<owner>   RUN#<runId>   async turn ledger (Task 9)`, then:

```python
def workspace_sk() -> str:
    return "WORKSPACE"


async def ensure_workspace_id(owner_id: str) -> str:
    """The owner's opaque channel-path segment, minted once (spec §3)."""
    row = await _get(owner_pk(owner_id), workspace_sk())
    if row is not None:
        return row["workspaceId"]
    candidate = str(uuid.uuid4())
    try:
        await run_in_thread(
            _table().put_item,
            Item={"pk": owner_pk(owner_id), "sk": workspace_sk(), "workspaceId": candidate, "ownerId": owner_id, "createdAt": utc_now_iso()},
            ConditionExpression="attribute_not_exists(pk)",
        )
        return candidate
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    row = await _get(owner_pk(owner_id), workspace_sk())
    if row is None:
        raise MessagingRepoError("workspace id allocation raced and no row exists")
    return row["workspaceId"]
```

In `create_room`, before building `meta`: `workspace_id = await ensure_workspace_id(owner_id)` and add `"workspaceId": workspace_id` to `meta`. In `ensure_user_stream`, before building `reverse`: `workspace_id = await ensure_workspace_id(owner_id)` and add `"workspaceId": workspace_id` to `reverse`.

`live_events.py`:

```python
        key = _uuid(str(meta.get("deliveryKey", "")), "room delivery key")
        workspace = _uuid(str(meta.get("workspaceId", "")), "room workspace id")
        await self._publish(f"/ws/{workspace}/rooms/{room_id}/{key}", validated)
# …
        workspace = _uuid(str(reverse.get("workspaceId", "")), "stream workspace id")
        await self._publish(f"/ws/{workspace}/users/{key}", validated)
```

`messaging.py` `LiveConfig`: add `workspace_id: uuid.UUID`. Router `live_config`: `workspace_id=await messaging_repo.ensure_workspace_id(owner_id)`.

Projector: in `_project_message` and `_project_message_update` add `workspace_id = _require_uuid(meta.get("workspaceId"), "workspaceId")` and publish to `f"/ws/{workspace_id}/rooms/{room_id}/{delivery_key}"`; in `_notify_members` add `workspace_id = _require_uuid(reverse.get("workspaceId"), "workspaceId")` and publish to `f"/ws/{workspace_id}/users/{key}"`; in `_project_live` the same from `mapping`.

- [ ] **Step 4: Update the publisher and projector tests**

`test_index.py`: every FakeDdb META row gains `"workspaceId": WORKSPACE_ID` (`WORKSPACE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"`), and the path assertions become `f"/ws/{WORKSPACE_ID}/rooms/{ROOM_ID}/{DELIVERY_KEY}"` / `f"/ws/{WORKSPACE_ID}/users/{CHANNEL_KEY}"`. Add one test: a META row without `workspaceId` is a partial failure (nothing published). For the backend publisher, in `tests/unit/services/test_live_events.py`:

```python
from unittest.mock import AsyncMock

import pytest

from core.services import live_events
from core.services.messaging import make_live_event

ROOM = "22222222-2222-4222-8222-222222222222"
KEY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_publish_room_uses_the_workspace_scoped_path(monkeypatch):
    seen = []
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock(side_effect=lambda channel, event: seen.append(channel)))
    monkeypatch.setattr(live_events.messaging_repo, "get_room", AsyncMock(return_value={"deliveryKey": KEY, "workspaceId": WS}))
    assert await publisher.publish_room(ROOM, make_live_event("room.typing.changed", {"roomId": ROOM}, room_id=ROOM))
    assert seen == [f"/ws/{WS}/rooms/{ROOM}/{KEY}"]


@pytest.mark.asyncio
async def test_publish_room_refuses_a_room_without_a_workspace_id(monkeypatch):
    publisher = live_events.LiveEventPublisher()
    monkeypatch.setattr(publisher, "_publish", AsyncMock())
    monkeypatch.setattr(live_events.messaging_repo, "get_room", AsyncMock(return_value={"deliveryKey": KEY}))
    with pytest.raises(live_events.LivePublishError):
        await publisher.publish_room(ROOM, make_live_event("room.typing.changed", {"roomId": ROOM}, room_id=ROOM))
```

- [ ] **Step 5: Write the backfill script**

```python
# apps/backend/scripts/backfill_workspace_ids.py
"""ONE-SHOT: stamp workspaceId onto every ROOM#/META and STREAM#/META row that lacks it.

Run once per environment before that environment's cutover deploy, then DELETE this
file (CLAUDE.md: one-shot backfills do not accumulate; git history is the record).

    cd apps/backend && AWS_PROFILE=isol8-admin MESSAGING_TABLE=lightbulb-dev-messaging \
        uv run python scripts/backfill_workspace_ids.py --dry-run
"""

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from botocore.exceptions import ClientError  # noqa: E402

from core.dynamodb import run_in_thread  # noqa: E402
from core.repositories import messaging_repo as repo  # noqa: E402


async def main(dry_run: bool) -> int:
    table = repo._table()
    kwargs = {
        "FilterExpression": "sk = :meta AND attribute_not_exists(workspaceId) AND (begins_with(pk, :room) OR begins_with(pk, :stream))",
        "ExpressionAttributeValues": {":meta": "META", ":room": "ROOM#", ":stream": "STREAM#"},
        "ProjectionExpression": "pk, sk, ownerId",
    }
    stamped = 0
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        for row in page.get("Items", []):
            workspace_id = await repo.ensure_workspace_id(row["ownerId"])
            if not dry_run:
                try:
                    await run_in_thread(
                        table.update_item,
                        Key={"pk": row["pk"], "sk": row["sk"]},
                        UpdateExpression="SET workspaceId = :ws",
                        ConditionExpression="attribute_not_exists(workspaceId)",
                        ExpressionAttributeValues={":ws": workspace_id},
                    )
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
            stamped += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    print(f"{'would stamp' if dry_run else 'stamped'} {stamped} rows")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args().dry_run)))
```

- [ ] **Step 6: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_messaging_repo.py tests/unit/services/test_live_events.py tests/unit/routers/test_messaging*.py -q && cd ../infra/lambda/messaging-projector && python3 -m pytest test_index.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/backend apps/infra/lambda/messaging-projector
git commit -m "feat(messaging): lazy workspace id stamped on rooms and streams; ws channel paths in publisher and projector"
```

---

### Task 8: Clients — workspace-scoped channel paths

**Files:**
- Modify: `packages/shared/src/messaging/types.ts:172-178` (`LiveConfig`)
- Modify: `packages/shared/src/live/appsyncEvents.ts:113-114`
- Modify: `apps/frontend/src/hooks/useLiveEvents.tsx:82-83`
- Modify: `apps/mobile/src/providers/live-events.tsx:131-134`
- Test: `packages/shared/src/live/appsyncEvents.test.ts:66-67,231`, `packages/shared/src/live/useLiveEvents.test.tsx:55-62`, `apps/frontend/src/hooks/__tests__/useLiveEvents.test.tsx:100-125`

**Interfaces:**
- Consumes: `LiveConfig.workspaceId` from Task 7.

- [ ] **Step 1: Update the tests**

```ts
// packages/shared/src/live/appsyncEvents.test.ts
const WS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const USER = `/ws/${WS}/users/11111111-1111-4111-8111-111111111111`;
const ROOM = `/ws/${WS}/rooms/22222222-2222-4222-8222-222222222222/33333333-3333-4333-8333-333333333333`;
// :231
const nextRoom = `/ws/${WS}/rooms/44444444-4444-4444-8444-444444444444/55555555-5555-4555-8555-555555555555`;
```

`useLiveEvents.test.tsx` (shared): give `config` a `workspaceId` and build `userChannel`/`roomA`/`roomB` the same way. Frontend `useLiveEvents.test.tsx:118-123`: expected `roomChannel` values become `` `/ws/${config.workspaceId}/rooms/room-a/key-a` `` (the live-config fixture in that file gains `workspaceId`).

- [ ] **Step 2: Run to verify failure**

Run: `cd packages/shared && pnpm test -- appsyncEvents useLiveEvents && cd ../../apps/frontend && pnpm test -- useLiveEvents`
Expected: FAIL (type error on `workspaceId`; classification by `startsWith("/users/")`).

- [ ] **Step 3: Implement**

```ts
// types.ts
export interface LiveConfig {
  schemaVersion: 1;
  httpEndpoint: string;
  realtimeEndpoint: string;
  region: string;
  userChannelKey: string;
  workspaceId: string;
}
// appsyncEvents.ts:113-114
    const users = unique.filter((channel) => channel.includes("/users/"));
    const rooms = unique.filter((channel) => channel.includes("/rooms/"));
// useRooms.ts is NOT touched: the messaging-latency pass (another session) deletes the
// `liveChannels.includes(subscriptionPath)` gate at :319-323 outright; history reconciles
// off activeRoomId. Do not reintroduce a liveChannels dependency there.
// apps/frontend/src/hooks/useLiveEvents.tsx:82-83
  const userChannel = config ? `/ws/${config.workspaceId}/users/${config.userChannelKey}` : null;
  const roomChannel = config && activeRoom ? `/ws/${config.workspaceId}/rooms/${activeRoom.roomId}/${activeRoom.deliveryKey}` : null;
// apps/mobile/src/providers/live-events.tsx:131-134 — the same two lines
```

- [ ] **Step 4: Run the suites**

Run: `cd packages/shared && pnpm test && cd ../../apps/frontend && pnpm test -- useLiveEvents useRooms MessagesIntegration && pnpm lint && cd ../mobile && pnpm test -- conversation thread`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/shared apps/frontend apps/mobile
git commit -m "feat(live): workspace-scoped AppSync channel paths"
```

---

### Task 9: Backend — run ledger repository

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_messaging_repo.py` (fixture :12-50 gains the GSI)

**Interfaces:**
- Produces: `RUN_NS`, `run_id_for(agent_id, message_id) -> str`, `run_sk(run_id)`, `create_run(owner_id, run_id, *, room_id, agent_id, workspace_id, trigger_message_id, event_kind) -> bool`, `get_run(owner_id, run_id) -> dict | None`, `start_run(owner_id, run_id, *, session_key) -> bool`, `heartbeat_run(owner_id, run_id) -> bool`, `end_run(owner_id, run_id, *, outcome, error_kind=None, usage=None) -> dict | None`, `list_overdue_runs(now_epoch, limit=100) -> list[dict]` (`[{"ownerId", "runId"}]`), constants `RUN_DISPATCH_TIMEOUT_S = 120`, `RUN_HEARTBEAT_TIMEOUT_S = 90`, `RUN_OUTCOMES`, `GSI_BY_RUN_DEADLINE`.

- [ ] **Step 1: Failing tests**

Add the GSI to the moto fixture's `GlobalSecondaryIndexes` and `AttributeDefinitions` for `sweep` (S) and `deadlineAt` (N):

```python
                {
                    "IndexName": "by-run-deadline",
                    "KeySchema": [
                        {"AttributeName": "sweep", "KeyType": "HASH"},
                        {"AttributeName": "deadlineAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
```

Tests (`import time` at the top):

```python
RUN_ROOM = "22222222-2222-4222-8222-222222222222"
USAGE = {"model": "claude-sonnet-4-6", "inputTokens": 1200, "outputTokens": 300, "cacheReadTokens": 5000, "cacheWriteTokens": 100, "estimatedCostUsd": 0.0123}


async def _dispatched(owner="owner_1", agent="a1", message="m1"):
    run_id = repo.run_id_for(agent, message)
    created = await repo.create_run(owner, run_id, room_id=RUN_ROOM, agent_id=agent, workspace_id="ws", trigger_message_id=message, event_kind="user_request")
    return run_id, created


@pytest.mark.asyncio
async def test_run_id_is_deterministic_and_the_row_is_created_once(table):
    run_id, created = await _dispatched()
    assert created and run_id == repo.run_id_for("a1", "m1")
    assert (await _dispatched())[1] is False
    row = await repo.get_run("owner_1", run_id)
    assert row["status"] == "dispatched" and row["sweep"] == "open" and row["deadlineAt"] > int(time.time())


@pytest.mark.asyncio
async def test_transitions_are_conditional(table):
    run_id, _ = await _dispatched()
    assert await repo.start_run("owner_1", run_id, session_key="agent:a1:lightbulb:channel:x")
    assert not await repo.start_run("owner_1", run_id, session_key="again")
    before = (await repo.get_run("owner_1", run_id))["deadlineAt"]
    assert await repo.heartbeat_run("owner_1", run_id)
    assert (await repo.get_run("owner_1", run_id))["deadlineAt"] >= before
    ended = await repo.end_run("owner_1", run_id, outcome="completed", usage=USAGE)
    assert ended["status"] == "ended" and ended["usage"]["inputTokens"] == 1200 and "sweep" not in ended and "deadlineAt" not in ended
    assert await repo.end_run("owner_1", run_id, outcome="error", error_kind="late") is None
    assert not await repo.heartbeat_run("owner_1", run_id)
    assert not await repo.start_run("owner_1", "unknown", session_key="k")


@pytest.mark.asyncio
async def test_overdue_query_sees_only_open_runs_past_their_deadline(table):
    run_id, _ = await _dispatched()
    now = int(time.time())
    assert await repo.list_overdue_runs(now) == []
    assert await repo.list_overdue_runs(now + repo.RUN_DISPATCH_TIMEOUT_S + 1) == [{"ownerId": "owner_1", "runId": run_id}]
    await repo.end_run("owner_1", run_id, outcome="completed", usage=USAGE)
    assert await repo.list_overdue_runs(now + 10_000) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_messaging_repo.py -q -k "run_id or transitions or overdue"`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
RUN_NS = uuid.UUID("2b5f1c7e-9a3d-4e6f-8b1c-0d2e4f6a8c1e")
RUN_DISPATCH_TIMEOUT_S = 120
RUN_HEARTBEAT_TIMEOUT_S = 90
RUN_OUTCOMES = frozenset({"completed", "error", "aborted", "ignored", "gate_error", "not_admitted"})
GSI_BY_RUN_DEADLINE = "by-run-deadline"


def run_sk(run_id: str) -> str:
    return f"RUN#{run_id}"


def run_id_for(agent_id: str, message_id: str) -> str:
    """Deterministic per dispatch event so an SQS redelivery lands on the same row."""
    return str(uuid.uuid5(RUN_NS, f"{agent_id}:{message_id}"))


async def create_run(
    owner_id: str,
    run_id: str,
    *,
    room_id: str,
    agent_id: str,
    workspace_id: str,
    trigger_message_id: str,
    event_kind: str,
) -> bool:
    """Put the ledger row as ``dispatched``. False when it already exists (redelivery)."""
    now = utc_now_iso()
    item = {
        "pk": owner_pk(owner_id),
        "sk": run_sk(run_id),
        "runId": run_id,
        "ownerId": owner_id,
        "roomId": room_id,
        "agentId": agent_id,
        "workspaceId": workspace_id,
        "triggerMessageId": trigger_message_id,
        "eventKind": event_kind,
        "status": "dispatched",
        "dispatchedAt": now,
        "updatedAt": now,
        "sweep": "open",
        "deadlineAt": int(time.time()) + RUN_DISPATCH_TIMEOUT_S,
        "expiresAt": _expires_at(),
    }
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def get_run(owner_id: str, run_id: str) -> dict | None:
    return await _get(owner_pk(owner_id), run_sk(run_id))


async def _run_update(owner_id: str, run_id: str, **kwargs: Any) -> dict | None:
    try:
        response = await run_in_thread(
            _table().update_item,
            Key={"pk": owner_pk(owner_id), "sk": run_sk(run_id)},
            ReturnValues="ALL_NEW",
            **kwargs,
        )
        return _plain(response["Attributes"])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


async def start_run(owner_id: str, run_id: str, *, session_key: str) -> bool:
    """dispatched -> started."""
    now = utc_now_iso()
    row = await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :started, sessionKey = :sk, startedAt = :now, lastHeartbeatAt = :now, deadlineAt = :deadline, updatedAt = :now",
        ConditionExpression="attribute_exists(sk) AND #s = :dispatched",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":started": "started",
            ":dispatched": "dispatched",
            ":sk": session_key,
            ":now": now,
            ":deadline": int(time.time()) + RUN_HEARTBEAT_TIMEOUT_S,
        },
    )
    return row is not None


async def heartbeat_run(owner_id: str, run_id: str) -> bool:
    """Extend the deadline while not ended (a heartbeat before ``run.started`` also starts the run)."""
    now = utc_now_iso()
    row = await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :started, lastHeartbeatAt = :now, deadlineAt = :deadline, updatedAt = :now, startedAt = if_not_exists(startedAt, :now)",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":started": "started",
            ":ended": "ended",
            ":now": now,
            ":deadline": int(time.time()) + RUN_HEARTBEAT_TIMEOUT_S,
        },
    )
    return row is not None


async def end_run(
    owner_id: str,
    run_id: str,
    *,
    outcome: str,
    error_kind: str | None = None,
    usage: dict | None = None,
) -> dict | None:
    """Close once. Returns the row when this call closed it, None on replay/unknown."""
    if outcome not in RUN_OUTCOMES:
        raise MessagingRepoError(f"unknown run outcome {outcome!r}")
    now = utc_now_iso()
    return await _run_update(
        owner_id,
        run_id,
        UpdateExpression="SET #s = :ended, outcome = :outcome, errorKind = :ek, #u = :usage, endedAt = :now, updatedAt = :now REMOVE sweep, deadlineAt",
        ConditionExpression="attribute_exists(sk) AND #s <> :ended",
        ExpressionAttributeNames={"#s": "status", "#u": "usage"},
        ExpressionAttributeValues={":ended": "ended", ":outcome": outcome, ":ek": error_kind, ":usage": _ddb_safe(usage), ":now": now},
    )


async def list_overdue_runs(now_epoch: int, limit: int = 100) -> list[dict]:
    """Open ledger rows whose deadline passed, via the sparse ``by-run-deadline`` GSI."""
    response = await run_in_thread(
        _table().query,
        IndexName=GSI_BY_RUN_DEADLINE,
        KeyConditionExpression="sweep = :open AND deadlineAt < :now",
        ExpressionAttributeValues={":open": "open", ":now": int(now_epoch)},
        Limit=limit,
    )
    return [
        {"ownerId": item["pk"][len("OWNER#"):], "runId": item["sk"][len("RUN#"):]}
        for item in response.get("Items", [])
    ]
```

(`_ddb_safe` at :181 already converts floats to `Decimal`; `estimatedCostUsd` rides through it.) Update the module docstring: `Only WORK#, LIVE# and RUN# rows carry expiresAt (7 days)`; `the GSIs are queried by list_rooms_for_member (by-member) and list_overdue_runs (by-run-deadline) and nowhere else`.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_messaging_repo.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/messaging_repo.py apps/backend/tests/unit/repositories/test_messaging_repo.py
git commit -m "feat(messaging): run ledger rows with conditional transitions, usage and a deadline index"
```

---

### Task 10: Backend — run event callback route

**Files:**
- Modify: `apps/backend/core/services/messaging.py` (schemas near :384-416; new `handle_run_event`; `AgentMessageRequest` :336; `create_agent_message` :1716; delete `_receipt_run_id` :1664-1689)
- Modify: `apps/backend/routers/messaging.py` (new route after :556)
- Test: `apps/backend/tests/unit/routers/test_messaging_run_events.py` (new), `apps/backend/tests/unit/services/test_run_events.py` (new), `apps/backend/tests/unit/services/test_messaging_telemetry.py:400-430`

**Interfaces:**
- Consumes: Task 9 ledger functions; `core.services.usage_service.record_usage`; `core.services.push_service.send_push_to_user`.
- Produces: `POST /api/v1/internal/messaging/runs/{owner_id}/events` (hook token) → `RunEventResponse {run_id, status: applied|replayed}`, 404 `RUN_NOT_FOUND` for an unknown run; `RunEventRequest`, `RunUsage`; `service.handle_run_event(owner_id, event)`; metrics `messaging.run.callback {type, outcome}`, `messaging.run.ended {outcome, event_kind}`, `messaging.run.start_latency` (ms), `messaging.run.duration` (ms); `AgentMessageRequest.run_id` (optional — the agent's `message` tool sends outside runs).

- [ ] **Step 1: Failing tests**

```python
# tests/unit/routers/test_messaging_run_events.py
"""Hook-token gate + wiring for the run event callback (mirrors test_agent_mail.py)."""
from unittest.mock import AsyncMock, patch

from core.services.messaging import RunEventResponse

OWNER, TOKEN = "user_a", "hook-token-for-a"
RUN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
H = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"runId": RUN, "type": "run.heartbeat", "at": "2026-09-12T03:15:00Z"}
USAGE = {"model": "m", "inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 3, "cacheWriteTokens": 4, "estimatedCostUsd": 0.01}


def _container():
    return patch("routers.messaging.container_repo.get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}))


def test_missing_bearer_is_401(client):
    assert client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY).status_code == 401


def test_wrong_token_is_401(client):
    with _container():
        r = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY, headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 401


def test_valid_token_reaches_the_service(client):
    with _container(), patch("routers.messaging.service.handle_run_event", AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied"))) as handle:
        r = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json=BODY, headers=H)
    assert r.status_code == 200 and r.json() == {"runId": RUN, "status": "applied"}
    assert handle.call_args.args[0] == OWNER and handle.call_args.args[1].type == "run.heartbeat"


def test_ended_requires_outcome_and_usage_for_a_started_run(client):
    with _container():
        no_outcome = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json={**BODY, "type": "run.ended"}, headers=H)
        no_usage = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json={**BODY, "type": "run.ended", "outcome": "completed"}, headers=H)
    assert no_outcome.status_code == 422 and no_usage.status_code == 422
    with _container(), patch("routers.messaging.service.handle_run_event", AsyncMock(return_value=RunEventResponse(run_id=RUN, status="applied"))):
        ignored = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json={**BODY, "type": "run.ended", "outcome": "ignored"}, headers=H)
        completed = client.post(f"/api/v1/internal/messaging/runs/{OWNER}/events", json={**BODY, "type": "run.ended", "outcome": "completed", "usage": USAGE}, headers=H)
    assert ignored.status_code == 200 and completed.status_code == 200
```

```python
# tests/unit/services/test_run_events.py
from unittest.mock import AsyncMock, patch

import pytest

from core.services import messaging
from core.services.messaging import ApiProblem, RunEventRequest, RunUsage

RUN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ROOM = "22222222-2222-4222-8222-222222222222"
USAGE = RunUsage(model="claude-sonnet-4-6", input_tokens=1200, output_tokens=300, cache_read_tokens=5000, cache_write_tokens=100, estimated_cost_usd=0.0123)
ROW = {"runId": RUN, "roomId": ROOM, "agentId": "a1", "triggerMessageId": "m1", "eventKind": "user_request", "dispatchedAt": "2026-09-12T03:15:00+00:00", "startedAt": "2026-09-12T03:15:01+00:00", "endedAt": "2026-09-12T03:15:09+00:00", "outcome": "completed", "status": "ended"}


def _event(**kw):
    return RunEventRequest(run_id=RUN, at="2026-09-12T03:15:00Z", **kw)


@pytest.fixture
def metrics(monkeypatch):
    seen = []
    monkeypatch.setattr(messaging, "put_metric", lambda name, **kw: seen.append((name, kw.get("dimensions"))))
    return seen


@pytest.mark.asyncio
async def test_started_and_heartbeat_map_to_conditional_transitions(metrics):
    with patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=True)) as start:
        assert (await messaging.handle_run_event("o", _event(type="run.started", session_key="k"))).status == "applied"
    start.assert_awaited_once_with("o", RUN, session_key="k")
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=False)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=ROW)),
    ):
        assert (await messaging.handle_run_event("o", _event(type="run.heartbeat"))).status == "replayed"
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=False)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=None)),
    ):
        with pytest.raises(ApiProblem) as exc:
            await messaging.handle_run_event("o", _event(type="run.heartbeat"))
    assert exc.value.error.code == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_ended_records_usage_and_pushes_only_on_error(metrics):
    record = AsyncMock()
    push = AsyncMock()
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=ROW)) as end,
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value={"author": {"kind": "human", "id": "user_bob"}})),
        patch.object(messaging, "record_usage", record),
        patch.object(messaging, "send_push_to_user", push),
        patch.object(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Friday"}])),
    ):
        result = await messaging.handle_run_event("o", _event(type="run.ended", outcome="completed", usage=USAGE))
        assert result.status == "applied"
        assert end.await_args.kwargs["usage"]["inputTokens"] == 1200
        record.assert_awaited_once_with(
            owner_id="o", user_id="user_bob", model="claude-sonnet-4-6", input_tokens=1200, output_tokens=300,
            cache_read=5000, cache_write=100, raw_cost_microcents=12300, room_turn=True, event_kind="user_request",
        )
        push.assert_not_awaited()
        await messaging.handle_run_event("o", _event(type="run.ended", outcome="error", error_kind="timeout", usage=USAGE))
        push.assert_awaited_once()
        assert push.call_args.args[0] == "user_bob" and "Friday" in push.call_args.args[1]
    assert ("messaging.run.ended", {"outcome": "completed", "event_kind": "user_request"}) in metrics
    assert any(name == "messaging.run.duration" for name, _ in metrics)


@pytest.mark.asyncio
async def test_zero_usage_is_not_recorded(metrics):
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=ROW)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None)),
        patch.object(messaging, "record_usage", AsyncMock()) as record,
    ):
        empty = RunUsage(model="unknown", input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, estimated_cost_usd=None)
        await messaging.handle_run_event("o", _event(type="run.ended", outcome="completed", usage=empty))
    record.assert_not_awaited()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_messaging_run_events.py tests/unit/services/test_run_events.py -q`
Expected: FAIL (route 404; `RunEventRequest` missing).

- [ ] **Step 3: Implement**

`messaging.py` schemas (next to `RunStatusResponse`; `model_validator` from pydantic):

```python
class RunUsage(ContractModel):
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = None


class RunEventRequest(ContractModel):
    run_id: uuid.UUID
    type: Literal["run.started", "run.heartbeat", "run.ended"]
    at: datetime
    session_key: str | None = None
    outcome: Literal["completed", "error", "aborted", "ignored", "gate_error"] | None = None
    error_kind: str | None = None
    usage: RunUsage | None = None

    @model_validator(mode="after")
    def _ended_is_complete(self) -> "RunEventRequest":
        if self.type == "run.ended":
            if self.outcome is None:
                raise ValueError("run.ended requires outcome")
            if self.outcome not in {"ignored", "gate_error"} and self.usage is None:
                raise ValueError("run.ended requires usage for a run that started")
        return self


class RunEventResponse(ContractModel):
    run_id: uuid.UUID
    status: Literal["applied", "replayed"]
```

`AgentMessageRequest` gains `run_id: uuid.UUID | None = None` (optional only because the agent's `message` tool posts to rooms from crons and heartbeats with no run). In `create_agent_message` replace the `_receipt_run_id` call with a ledger check, delete `_receipt_run_id`, and delete its two tests in `tests/unit/services/test_messaging_telemetry.py` (:408 and :424; the stamped/unset metric stays):

```python
    run_id = str(request.run_id) if request.run_id else None
    if run_id is not None:
        # A present run id is a claim about the ledger, not a hint: it must be this
        # owner's run for this room and agent. A run that already closed still accepts
        # its late reply (durable retries, sweeper closures) — delivery is at-least-once.
        run = await messaging_repo.get_run(owner_id, run_id)
        if run is None or run.get("roomId") != room_id or run.get("agentId") != agent_id:
            raise ApiProblem("RUN_NOT_FOUND", "runId does not name this owner's run for this room and agent.")
```

Add to `tests/unit/services/test_run_events.py`:

```python
@pytest.mark.asyncio
async def test_agent_message_with_a_run_id_must_match_the_ledger_row(metrics):
    from core.services.messaging import AgentMessageRequest, create_agent_message

    request = AgentMessageRequest(event_id="f0f0f0f0-f0f0-4f0f-8f0f-f0f0f0f0f0f0", room_id=ROOM, agent_id=AGENT_UUID, text="hi", reply_to_message_id=None, thread_root_message_id=None, run_id=RUN)
    with (
        patch.object(messaging.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "o", "kind": "channel"})),
        patch.object(messaging.messaging_repo, "get_member", AsyncMock(return_value={"id": AGENT_UUID})),
        patch.object(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT_UUID, "name": "Friday"}])),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value={"roomId": "33333333-3333-4333-8333-333333333333", "agentId": AGENT_UUID, "status": "ended"})),
    ):
        with pytest.raises(ApiProblem) as exc:
            await create_agent_message("o", request)
    assert exc.value.error.code == "RUN_NOT_FOUND"
```

(`AGENT_UUID` is a UUID constant; `agent_id` on the request is typed `uuid.UUID`.)

Service:

```python
from core.services.push_service import send_push_to_user
from core.services.usage_service import record_usage


def _ms_between(start: Any, end: Any) -> float | None:
    try:
        return max(0.0, (datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


async def handle_run_event(owner_id: str, event: RunEventRequest) -> RunEventResponse:
    """Apply one at-least-once callback from the container's run supervisor (spec §4/§5)."""
    run_id = str(event.run_id)
    if event.type == "run.started":
        applied = await messaging_repo.start_run(owner_id, run_id, session_key=event.session_key or "")
    elif event.type == "run.heartbeat":
        applied = await messaging_repo.heartbeat_run(owner_id, run_id)
    else:
        usage = event.usage.model_dump(mode="json", by_alias=True) if event.usage else None
        row = await messaging_repo.end_run(owner_id, run_id, outcome=event.outcome, error_kind=event.error_kind, usage=usage)
        applied = row is not None
        if row is not None:
            event_kind = row.get("eventKind") or "unknown"
            put_metric("messaging.run.ended", dimensions={"outcome": event.outcome, "event_kind": event_kind})
            for name, start, end in (("messaging.run.start_latency", "dispatchedAt", "startedAt"), ("messaging.run.duration", "startedAt", "endedAt")):
                value = _ms_between(row.get(start), row.get(end)) if row.get(start) and row.get(end) else None
                if value is not None:
                    put_metric(name, value=value, unit="Milliseconds", dimensions={"event_kind": event_kind})
            await _after_run_ended(owner_id, row, event.usage)
    if not applied and await messaging_repo.get_run(owner_id, run_id) is None:
        put_metric("messaging.run.callback", dimensions={"type": event.type, "outcome": "unknown"})
        raise ApiProblem("RUN_NOT_FOUND", "Run not found.")
    put_metric("messaging.run.callback", dimensions={"type": event.type, "outcome": "applied" if applied else "replayed"})
    return RunEventResponse(run_id=event.run_id, status="applied" if applied else "replayed")


async def _after_run_ended(owner_id: str, row: dict, usage: RunUsage | None) -> None:
    """Usage counters (analytics only; Metronome is billed by the meter Lambda) and the
    failure push. Never raises into the callback."""
    if row.get("outcome") not in {"completed", "error", "aborted"}:
        return
    trigger = await messaging_repo.get_message_by_id(row["roomId"], row["triggerMessageId"])
    author = (trigger or {}).get("author") or {}
    member_user_id = author.get("id") if author.get("kind") == "human" else None
    if usage is not None:
        cost = usage.estimated_cost_usd
        raw_cost_microcents = round(cost * 1_000_000) if cost is not None and cost > 0 else None
        # Same drop rule as the personal-chat path: no token volume and no billable cost.
        if usage.input_tokens > 0 or usage.output_tokens > 0 or raw_cost_microcents:
            try:
                await record_usage(
                    owner_id=owner_id,
                    user_id=member_user_id or owner_id,
                    model=usage.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read=usage.cache_read_tokens,
                    cache_write=usage.cache_write_tokens,
                    raw_cost_microcents=raw_cost_microcents,
                    room_turn=True,
                    event_kind=row.get("eventKind"),
                )
            except Exception:
                logger.exception("room run usage record failed")
    # A completed reply is pushed by the projector on room.message.created; only a
    # failure has no message to carry it.
    if row["outcome"] == "error" and member_user_id and member_user_id.startswith("user_"):
        try:
            roster = {agent["id"]: agent for agent in await load_agent_roster(owner_id)}
            name = (roster.get(row["agentId"]) or {}).get("name") or "Agent"
            await send_push_to_user(member_user_id, f"{name} run failed", "Agent run failed", {"agentId": row["agentId"], "roomId": row["roomId"], "runId": row["runId"]})
        except Exception:
            logger.exception("run failure push failed")
```

Router:

```python
@router.post("/internal/messaging/runs/{owner_id}/events", response_model=RunEventResponse)
async def post_run_event(owner_id: str, body: RunEventRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.handle_run_event(owner_id, body)
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_messaging_run_events.py tests/unit/services/test_run_events.py tests/unit/services/test_messaging_telemetry.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend
git commit -m "feat(messaging): run event callback route with ledger transitions, usage counters and failure push"
```

---

### Task 11: Backend — ledger sweeper

**Files:**
- Create: `apps/backend/core/services/run_ledger.py`
- Modify: `apps/backend/main.py:398-479`
- Test: `apps/backend/tests/unit/services/test_run_ledger.py`

**Interfaces:**
- Consumes: Task 9 `list_overdue_runs`, `get_run`, `end_run`; `live_events.get_live_event_publisher().publish_room`; `messaging.make_live_event`.
- Produces: `sweep_runs_once() -> int`, `run_ledger_sweeper(stop: asyncio.Event)`; metric `messaging.run.swept {error}`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/services/test_run_ledger.py
from unittest.mock import AsyncMock, patch

import pytest

from core.services import run_ledger

ROOM = "22222222-2222-4222-8222-222222222222"


def _row(status, deadline):
    return {"runId": "r1", "ownerId": "o", "roomId": ROOM, "agentId": "a1", "status": status, "deadlineAt": deadline, "eventKind": "user_request"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error_kind", [("dispatched", "agent_did_not_start"), ("started", "agent_stopped_responding")])
async def test_overdue_runs_close_as_error_with_an_error_and_run_ended_frame(monkeypatch, status, error_kind):
    monkeypatch.setattr(run_ledger, "put_metric", lambda *a, **k: None)
    monkeypatch.setattr(run_ledger.time, "time", lambda: 1_000)
    publisher = AsyncMock()
    with (
        patch.object(run_ledger.messaging_repo, "list_overdue_runs", AsyncMock(return_value=[{"ownerId": "o", "runId": "r1"}])),
        patch.object(run_ledger.messaging_repo, "get_run", AsyncMock(return_value=_row(status, 900))),
        patch.object(run_ledger.messaging_repo, "end_run", AsyncMock(return_value={**_row("ended", 900), "outcome": "error", "errorKind": error_kind})) as end,
        patch.object(run_ledger, "get_live_event_publisher", lambda: publisher),
    ):
        assert await run_ledger.sweep_runs_once() == 1
    end.assert_awaited_once_with("o", "r1", outcome="error", error_kind=error_kind)
    frames = [call.args[1]["payload"]["frame"] for call in publisher.publish_room.await_args_list]
    assert [f["type"] for f in frames] == ["error", "run_ended"]
    assert frames[0]["code"] == error_kind and frames[1]["outcome"] == "error" and frames[1]["runId"] == "r1" and frames[1]["agentId"] == "a1"


@pytest.mark.asyncio
async def test_rows_that_closed_or_extended_meanwhile_are_skipped(monkeypatch):
    monkeypatch.setattr(run_ledger, "put_metric", lambda *a, **k: None)
    monkeypatch.setattr(run_ledger.time, "time", lambda: 1_000)
    with (
        patch.object(run_ledger.messaging_repo, "list_overdue_runs", AsyncMock(return_value=[{"ownerId": "o", "runId": "r1"}, {"ownerId": "o", "runId": "r2"}])),
        patch.object(run_ledger.messaging_repo, "get_run", AsyncMock(side_effect=[_row("ended", 900), _row("started", 2_000)])),
        patch.object(run_ledger.messaging_repo, "end_run", AsyncMock()) as end,
    ):
        assert await run_ledger.sweep_runs_once() == 0
    end.assert_not_awaited()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_run_ledger.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""The one clock in the turn contract: close runs that never started or stopped
heartbeating (spec §5). Reports; never re-runs."""

from __future__ import annotations

import asyncio
import logging
import time

from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services.live_events import get_live_event_publisher
from core.services.messaging import make_live_event

logger = logging.getLogger(__name__)

_SWEEP_SECONDS = 60
_MESSAGES = {
    "agent_did_not_start": "The agent did not start this run.",
    "agent_stopped_responding": "The agent stopped responding.",
}


async def sweep_runs_once() -> int:
    now = int(time.time())
    closed = 0
    for ref in await messaging_repo.list_overdue_runs(now):
        row = await messaging_repo.get_run(ref["ownerId"], ref["runId"])
        if row is None or row.get("status") == "ended" or int(row.get("deadlineAt", 0)) >= now:
            continue  # closed or extended between the index read and this consistent read
        error_kind = "agent_did_not_start" if row["status"] == "dispatched" else "agent_stopped_responding"
        ended = await messaging_repo.end_run(row["ownerId"], row["runId"], outcome="error", error_kind=error_kind)
        if ended is None:
            continue
        closed += 1
        put_metric("messaging.run.ended", dimensions={"outcome": "error", "event_kind": ended.get("eventKind") or "unknown"})
        put_metric("messaging.run.swept", dimensions={"error": error_kind})
        try:
            await _publish_terminal_frames(ended, error_kind)
        except Exception:
            logger.warning("sweeper terminal frames failed (%s)", error_kind, exc_info=True)
    return closed


async def _publish_terminal_frames(row: dict, error_kind: str) -> None:
    publisher = get_live_event_publisher()
    base = {"roomId": row["roomId"], "agentId": row["agentId"], "runId": row["runId"]}
    stamp = {"runId": row["runId"], "agentId": row["agentId"]}
    for frame in (
        {"type": "error", "message": _MESSAGES[error_kind], "code": error_kind, **stamp},
        {"type": "run_ended", "outcome": "error", **stamp},
    ):
        await publisher.publish_room(row["roomId"], make_live_event("room.run.frame", {**base, "frame": frame}, room_id=row["roomId"]))


async def run_ledger_sweeper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await sweep_runs_once()
        except Exception:
            put_metric("messaging.run.swept", dimensions={"error": "sweep_error"})
            logger.warning("run ledger sweep failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
```

`main.py` — copy the five `trial_idle_sweeper` wiring points: import `run_ledger_sweeper`, create `run_sweeper_stop = asyncio.Event()` and `run_sweeper_task = asyncio.create_task(run_ledger_sweeper(run_sweeper_stop))`, register `"run_ledger_sweeper": run_sweeper_task` in `BACKGROUND_TASKS`, `run_sweeper_stop.set()` at shutdown, and include the task in both the graceful gather list and the timeout-cancel tuple.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_run_ledger.py -q && uv run python -c "import main"`
Expected: PASS; import succeeds.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/run_ledger.py apps/backend/main.py apps/backend/tests/unit/services/test_run_ledger.py
git commit -m "feat(messaging): one-minute run ledger sweeper closes dead runs with an error frame"
```

---

### Task 12: Backend — consumer on the async contract

**Files:**
- Modify: `apps/backend/core/services/openclaw_work.py` (constants :35-41, `_ingress_status_class` :109-117, `_process_message` :262-320, delete `_extend_visibility` :322-334, `_dispatch_message` :395-513)
- Test: `apps/backend/tests/unit/services/test_openclaw_work_retry_visibility.py`, `apps/backend/tests/unit/services/test_openclaw_work_metrics.py`

**Interfaces:**
- Consumes: Task 9 `run_id_for`, `create_run`, `end_run`; `settings.APPSYNC_EVENTS_HTTP_ENDPOINT`; room META `workspaceId`/`deliveryKey`.
- Produces: ingress body `run` object; metrics `messaging.run.dispatched {outcome: new|redelivered, event_kind}`, `messaging.ingress.ack {status: accepted|replayed|unknown, event_kind}`; `_ingress_status_class` treats 200/202/409 as contractual; `_VISIBILITY_SECONDS = 60`.

- [ ] **Step 1: Rewrite the consumer tests**

In both test files reduce `FakePool` to `ensure_connection` only, drop every `subscribe_room_session`/`mark_run_active`/`clear_active_run` reference and every `cleared` assertion, and give `meta` `"workspaceId": WS, "deliveryKey": KEY` (two UUID constants). Stub the ledger with recorders and let `fake_post` record the body:

```python
    ledger = {"created": [], "ended": []}

    async def fake_create_run(owner_id, run_id, **fields):
        ledger["created"].append((owner_id, run_id, fields))
        return True

    async def fake_end_run(owner_id, run_id, **fields):
        ledger["ended"].append((owner_id, run_id, fields))
        return {"runId": run_id}

    monkeypatch.setattr(work.messaging_repo, "create_run", fake_create_run)
    monkeypatch.setattr(work.messaging_repo, "end_run", fake_end_run)
```

Delete `test_consumed_outcome_clears_synthetic_run` and the "clears the active run" half of `test_plugin_ingress_timeout_is_a_short_retry_and_clears_the_active_run` (keep the short-retry assertion). Add:

```python
@pytest.mark.asyncio
async def test_dispatch_writes_the_ledger_row_and_the_run_contract(monkeypatch):
    metrics, ledger, posted = _capture_metrics(monkeypatch), {}, {}
    await _dispatch(monkeypatch, answer=_Answer(202, {"status": "accepted"}), event_kind="user_request", metrics=metrics, ledger=ledger, posted=posted)
    expected = work.messaging_repo.run_id_for("a1", "m1")
    assert ledger["created"][0][1] == expected
    assert posted["body"]["run"] == {"runId": expected, "workspaceId": WS, "frameChannel": f"/ws/{WS}/rooms/{_ROOM}/{KEY}", "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT}
    assert ledger["ended"] == []
    assert {"status": "accepted", "event_kind": "user_request"} in _dimension_sets(metrics, "messaging.ingress.ack")


@pytest.mark.asyncio
async def test_replayed_and_not_admitted_are_consumed_without_retry(monkeypatch):
    metrics, ledger = _capture_metrics(monkeypatch), {}
    await _dispatch(monkeypatch, answer=_Answer(200, {"status": "replayed"}), event_kind="user_request", metrics=metrics, ledger=ledger, posted={})
    assert ledger["ended"] == []
    await _dispatch(monkeypatch, answer=_Answer(409, {"error": "not_admitted"}), event_kind="room_event", metrics=metrics, ledger=ledger, posted={})
    assert ledger["ended"][0][2] == {"outcome": "not_admitted"}


@pytest.mark.asyncio
async def test_a_400_from_an_unrolled_container_is_a_retry(monkeypatch):
    with pytest.raises(WorkRetry):
        await _dispatch(monkeypatch, answer=_Answer(400, {"error": "invalid_ingress"}), event_kind="user_request", metrics=_capture_metrics(monkeypatch), ledger={}, posted={})


def test_no_visibility_extension_task_exists():
    assert not hasattr(OpenClawWorkWorker, "_extend_visibility")
    assert work._VISIBILITY_SECONDS == 60
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_openclaw_work_retry_visibility.py tests/unit/services/test_openclaw_work_metrics.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Constants: `_VISIBILITY_SECONDS = 60`; delete `_VISIBILITY_REFRESH_SECONDS`; keep `_RETRY_VISIBILITY_SECONDS = 10`.

```python
def _ingress_status_class(status_code: int) -> str:
    """200/202/409 are the plugin's contractual answers (replayed/accepted/not admitted)."""
    if status_code in (200, 202, 409):
        return str(status_code)
    if status_code >= 500:
        return "5xx"
    return "4xx" if status_code >= 400 else "other"
```

`_process_message`: delete the `extender = …` line and the `extender.cancel()` / `await extender` block in `finally`; delete `_extend_visibility`.

`_dispatch_message` from `token = …` onward:

```python
        pool = get_gateway_pool()
        # Still the door for exec approvals and questions (sub-project B removes it);
        # room run frames and usage no longer transit this connection.
        await pool.ensure_connection(owner_id, ip, token)
        members = with_human_handles(await messaging_repo.list_members(meta["roomId"]))
        body = await build_ingress_body(meta=meta, target_agent_id=member["id"], event_kind=event_kind, message=message, members=members)
        # The earlier read chose the work; this one is deliberately adjacent to
        # the side effect so queued work cannot outlive membership revocation.
        if await messaging_repo.get_member(meta["roomId"], "agent", member["id"]) is None:
            return
        run_id = messaging_repo.run_id_for(member["id"], message["messageId"])
        body["run"] = {
            "runId": run_id,
            "workspaceId": meta["workspaceId"],
            "frameChannel": f"/ws/{meta['workspaceId']}/rooms/{meta['roomId']}/{meta['deliveryKey']}",
            "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
        }
        created = await messaging_repo.create_run(
            owner_id, run_id, room_id=meta["roomId"], agent_id=member["id"], workspace_id=meta["workspaceId"],
            trigger_message_id=message["messageId"], event_kind=event_kind,
        )
        put_metric("messaging.run.dispatched", dimensions={"outcome": "new" if created else "redelivered", "event_kind": event_kind})
        from core.services import local_session_grants
        from core.services.messaging import ApiProblem

        invocation = None
        try:
            invocation = local_session_grants.mint(owner_id, meta["roomId"], member["id"], message["messageId"])
        except (RuntimeError, ApiProblem):
            logger.warning("Local session invocation unavailable")
        url = f"http://{ip}:18789/plugins/lightbulb-channel/inbound/{member['id']}"
        started = time.perf_counter()
        try:
            response = await self._post(url, body, token, invocation)
            put_metric("messaging.ingress.post", dimensions={"status_class": _ingress_status_class(response.status_code), "event_kind": event_kind})
            put_metric("messaging.ingress.latency", value=(time.perf_counter() - started) * 1000, unit="Milliseconds", dimensions={"event_kind": event_kind})
            if response.status_code == 409:
                # Permanent synchronous refusal (loop guard, non-member): the ledger
                # closes here because the plugin will never call back for it.
                await messaging_repo.end_run(owner_id, run_id, outcome="not_admitted")
                put_metric("messaging.ambient_gate", dimensions={"outcome": "not_admitted", "event_kind": event_kind})
                return
            if response.status_code not in (200, 202):
                raise WorkRetry(f"plugin refused ingress ({response.status_code})")
            try:
                status = response.json().get("status")
            except (AttributeError, TypeError, ValueError):
                status = "unknown"
            if status not in {"accepted", "replayed"}:
                status = "unknown"
            put_metric("messaging.ingress.ack", dimensions={"status": status, "event_kind": event_kind})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            put_metric("messaging.ingress.post", dimensions={"status_class": "timeout" if isinstance(exc, httpx.TimeoutException) else "transport", "event_kind": event_kind})
            # Effect-once on the plugin makes a retry safe; the ledger row is reused.
            raise WorkRetry(f"plugin ingress {type(exc).__name__}") from exc
```

Keep `lightbulb_session_key` and every other function unchanged.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_openclaw_work_retry_visibility.py tests/unit/services/test_openclaw_work_metrics.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/openclaw_work.py apps/backend/tests/unit/services
git commit -m "feat(messaging): consumer mints ledger rows and dispatches the run contract; no visibility extension"
```

---

### Task 13: Backend — abort, steer and run status from the ledger over HTTP; delete the relay

**Files:**
- Modify: `apps/backend/routers/messaging.py:439-537`
- Modify: `apps/backend/core/gateway/connection_pool.py`
- Modify/Delete tests under `apps/backend/tests/unit/gateway/` and `apps/backend/tests/gateway/`
- Test: `apps/backend/tests/unit/routers/test_messaging_run_status.py` (rewrite), `apps/backend/tests/unit/routers/test_messaging_run_controls.py` (new)

**Interfaces:**
- Consumes: Task 9 `get_run`; plugin routes from Task 4.
- Produces: `_resolved_run(auth, run_id) -> tuple[str, dict]`; `_post_run_control(url, token, payload) -> int`; the relay surface is gone.

- [ ] **Step 1: Rewrite the run-status test and add the controls test**

`test_messaging_run_status.py`: replace `fake_pool.resolve_run` with `patch("routers.messaging.messaging_repo.get_run", new_callable=AsyncMock, return_value={...})` returning `{"roomId": ROOM_ID, "agentId": AGENT_ID, "status": "started"}` → `running`; `"ended"` → `terminal`; `None` → `terminal`. Keep the membership patches. New file:

```python
# tests/unit/routers/test_messaging_run_controls.py
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.messaging import AbortRunResponse, ApiProblem, SteerRunRequest, SteerRunResponse

AGENT_ID = "11111111-1111-4111-8111-111111111111"
ROOM_ID = "22222222-2222-4222-8222-222222222222"
ROW = {"runId": "run-1", "roomId": ROOM_ID, "agentId": AGENT_ID, "status": "started"}


def _auth():
    auth = MagicMock()
    auth.org_id, auth.user_id = "org_1", "user_1"
    return auth


@contextlib.contextmanager
def _common(row=ROW, control_status=202):
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw"}, "10.0.0.9"))
    pool = MagicMock()
    pool.gate_chat = AsyncMock(return_value={"blocked": False})
    with (
        patch("routers.messaging.messaging_repo.get_run", new_callable=AsyncMock, return_value=row),
        patch("routers.messaging.service.require_room_member", new_callable=AsyncMock),
        patch("routers.messaging.messaging_repo.get_member", new_callable=AsyncMock, return_value={"id": AGENT_ID}),
        patch("routers.messaging.get_ecs_manager", return_value=manager),
        patch("routers.messaging.get_gateway_pool", return_value=pool),
        patch("routers.messaging.container_repo.touch_last_interactive", new_callable=AsyncMock),
        patch("routers.messaging._post_run_control", new_callable=AsyncMock, return_value=control_status) as post,
    ):
        yield post, pool


@pytest.mark.asyncio
async def test_abort_posts_to_the_plugin_route():
    from routers.messaging import abort_run
    with _common() as (post, _):
        assert await abort_run("run-1", auth=_auth()) == AbortRunResponse(run_id="run-1", state="aborting")
    post.assert_awaited_once_with("http://10.0.0.9:18789/plugins/lightbulb-channel/runs/run-1/abort", "gw", {})


@pytest.mark.asyncio
async def test_abort_on_an_ended_run_is_already_terminal_without_a_call():
    from routers.messaging import abort_run
    with _common(row={**ROW, "status": "ended"}) as (post, _):
        assert (await abort_run("run-1", auth=_auth())).state == "already-terminal"
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_steer_gates_then_posts_text():
    from routers.messaging import steer_run
    with _common() as (post, pool):
        assert await steer_run("run-1", SteerRunRequest(text="focus"), auth=_auth()) == SteerRunResponse(run_id="run-1")
    post.assert_awaited_once_with("http://10.0.0.9:18789/plugins/lightbulb-channel/runs/run-1/steer", "gw", {"text": "focus"})
    assert pool.gate_chat.await_args.kwargs["session_key"] == f"agent:{AGENT_ID}:lightbulb:channel:{ROOM_ID}"


@pytest.mark.asyncio
async def test_steer_404_from_the_plugin_is_run_not_active():
    from routers.messaging import steer_run
    with _common(control_status=404):
        with pytest.raises(ApiProblem) as exc:
            await steer_run("run-1", SteerRunRequest(text="focus"), auth=_auth())
    assert exc.value.error.code == "RUN_NOT_ACTIVE"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_messaging_run_status.py tests/unit/routers/test_messaging_run_controls.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the routes**

Replace `_resolved_active_run`, `get_run_status`, `abort_run`, `steer_run`:

```python
import httpx  # top of module


async def _resolved_run(auth: AuthContext, run_id: str) -> tuple[str, dict]:
    owner_id = resolve_owner_id(auth)
    row = await messaging_repo.get_run(owner_id, run_id)
    if row is None:
        raise ApiProblem("RUN_NOT_FOUND", "Run not found.")
    await service.require_room_member(owner_id, auth.user_id, row["roomId"])
    if await messaging_repo.get_member(row["roomId"], "agent", row["agentId"]) is None:
        raise ApiProblem("RUN_NOT_ACTIVE", "The run's agent is no longer in this room.")
    return owner_id, row


async def _post_run_control(url: str, token: str, payload: dict) -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        response = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
    return response.status_code


async def _run_control(owner_id: str, run_id: str, action: str, payload: dict) -> int | None:
    """202 accepted, 404 not active, None when no container is running."""
    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    if not container or not ip:
        return None
    return await _post_run_control(
        f"http://{ip}:18789/plugins/lightbulb-channel/runs/{run_id}/{action}", container["gateway_token"], payload
    )


@router.get("/messaging/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(run_id: str, auth: AuthContext = Depends(get_current_user)):
    """Read-only reconciliation for a run the client already knows about (ledger-backed)."""
    try:
        _, row = await _resolved_run(auth, run_id)
    except ApiProblem as exc:
        if exc.error.code != "RUN_NOT_FOUND":
            raise
        return RunStatusResponse(run_id=run_id, state="terminal")
    return RunStatusResponse(run_id=run_id, state="terminal" if row.get("status") == "ended" else "running")


@router.post("/messaging/runs/{run_id}/abort", response_model=AbortRunResponse)
async def abort_run(run_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id, row = await _resolved_run(auth, run_id)
    if row.get("status") == "ended":
        return AbortRunResponse(run_id=run_id, state="already-terminal")
    try:
        status = await _run_control(owner_id, run_id, "abort", {})
    except httpx.HTTPError as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not abort the run.", retryable=True, retry_after=3) from exc
    return AbortRunResponse(run_id=run_id, state="aborting" if status == 202 else "already-terminal")


@router.post("/messaging/runs/{run_id}/steer", response_model=SteerRunResponse)
async def steer_run(run_id: str, request: SteerRunRequest, auth: AuthContext = Depends(get_current_user)):
    owner_id, row = await _resolved_run(auth, run_id)
    if row.get("status") == "ended":
        raise ApiProblem("RUN_NOT_ACTIVE", "Run is already terminal.")
    session_key = f"agent:{row['agentId']}:lightbulb:channel:{row['roomId']}"
    gate = await get_gateway_pool().gate_chat(user_id=auth.user_id, owner_id=owner_id, session_key=session_key)
    if gate.get("blocked"):
        raise ApiProblem("RUN_NOT_ACTIVE", gate.get("message", "Steering is blocked."))
    await container_repo.touch_last_interactive(owner_id)
    try:
        status = await _run_control(owner_id, run_id, "steer", {"text": request.text})
    except httpx.HTTPError as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "Could not steer the run.", retryable=True, retry_after=3) from exc
    if status == 202:
        return SteerRunResponse(run_id=run_id)
    if status == 404:
        raise ApiProblem("RUN_NOT_ACTIVE", "Run is already terminal.")
    raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is unavailable.", retryable=True, retry_after=3)
```

Remove the `uuid` import if nothing else in the router uses it.

- [ ] **Step 4: Delete the relay**

Run: `cd apps/backend && git grep -n "subscribe_room_session\|_room_session_subs\|_send_room_session_subscribe\|_publish_room_frame\|_room_run_context\|mark_run_active\|def clear_active_run\|def resolve_run\|def run_id_for_session\|_ROOM_SAFE_FRAME_TYPES\|_transform_agent_event\|_rewrite_run_full_text\|_run_text\b\|_run_state\b" -- core routers`
Expected after the route change: hits only inside `connection_pool.py`.

In `connection_pool.py` remove: `_ROOM_SAFE_FRAME_TYPES`, `_RUN_TEXT_TYPES`, `_RunText`/`_run_text`/`_RUN_TEXT_CAP` with `_run_text_state`/`_rewrite_run_full_text`, `_transform_agent_event`/`_build_text_frame`, `_publish_room_frame`, `_room_run_context`, `_room_session_subs` and `subscribe_room_session`/`_send_room_session_subscribe` (connection + pool) with the reconnect re-subscribe loop, connection-level `mark_run_active` and pool-level `mark_run_active`/`clear_active_run`/`resolve_run`/`run_id_for_session`, and in `_handle_message`: the `room`-gated tail of the `agent` branch (keep approval and lifecycle handling and `_touch_run`), every `_publish_room_frame(...)` call in the `chat` branch (keep metrics, push and `_clear_active_run` for personal chat), the `chat.side_result` branch and the `sessions.changed` branch. Keep `_active_runs`, `_clear_active_run`, `_touch_run`, `_terminal_run_ids`, `has_active_runs`, `_fetch_and_record_usage`, `_record_usage_from_session` and `parse_lightbulb_session_key` (still the room test for metric dimensions).

Tests: delete `tests/unit/gateway/test_connection_pool_run_active.py`, `tests/unit/core/test_chat_event_transform.py`, `tests/unit/gateway/test_run_text_accumulation.py`, `tests/gateway/test_event_session_id.py` and `tests/gateway/test_side_result_forward.py` (each tests a deleted subject — check the docstring first and keep any test whose subject survived). In `test_e2e_latency_ceiling.py`, `test_e2e_latency_emit.py`, `test_lifecycle_billing.py` and `test_push_dispatch.py` remove the `patch.object(conn, "_publish_room_frame", …)` lines and every `mark_run_active(...)` call, replacing the latter with the direct `conn._active_runs[key] = {...}` entry `test_push_dispatch.py:180-205` already documents, and delete assertions about room-frame forwarding.

- [ ] **Step 5: Run the suites**

Run: `cd apps/backend && uv run pytest tests/unit/gateway tests/gateway tests/unit/routers/test_messaging_run_status.py tests/unit/routers/test_messaging_run_controls.py tests/unit/services/test_openclaw_work_retry_visibility.py tests/unit/services/test_openclaw_work_metrics.py tests/unit/services/test_messaging_telemetry.py -q`
Expected: PASS (pre-existing unrelated reds on `main` excepted — report them, do not fix).

- [ ] **Step 6: Update CLAUDE.md**

In the "Messaging" section replace the channel lines in the diagram and the "Live delivery" paragraph's two channels with `/ws/<workspaceId>/rooms/<roomId>/<deliveryKey>` and `/ws/<workspaceId>/users/<opaqueUserKey>`; replace the paragraph "**Backend↔container transport is unchanged.** …" with:

```markdown
**Room turns are asynchronous (spec `2026-09-12-async-turn-contract-design.md`).** The
consumer POSTs the ingress body with a `run` contract and gets `202 accepted` in
milliseconds; the container runs the turn, publishes the run frames itself to
`/ws/<workspaceId>/rooms/…` with its task role, and reports `run.started` /
`run.heartbeat` / `run.ended {outcome, usage}` to
`POST /api/v1/internal/messaging/runs/{ownerId}/events` (hook token). The ledger row
`OWNER#<owner>/RUN#<runId>` is the only run state; a one-minute sweeper closes runs that
never started (120 s) or stopped heartbeating (90 s) with an `error` + `run_ended` pair.
Abort and steer are HTTP calls to `/plugins/lightbulb-channel/runs/{runId}/{abort|steer}`.
`core/gateway/connection_pool.py` no longer subscribes to room sessions or relays frames;
it still carries exec approvals, questions and personal-chat usage until sub-project B.
```

and in the Gateway table row for `connection_pool.py` delete the sentence starting "It fans nothing out to browsers" through "after the last tool call."

- [ ] **Step 7: Commit**

```bash
git add apps/backend CLAUDE.md
git commit -m "feat(messaging): ledger-backed run status, abort and steer over HTTP; delete the room frame relay"
```

---

### Task 14: Cutover — pins, deploy, fleet roll, DLQ redrive, cleanup

**Files:**
- Modify: `openclaw-version.json` (`dev.tag`, `prod.tag`, `notes`)
- Delete (after both environments ran it): `apps/backend/scripts/backfill_workspace_ids.py`
- Delete (when shipped): `docs/superpowers/plans/2026-09-12-async-turn-contract.md`

**Interfaces:**
- Consumes: the image tag and digest from Task 5.

- [ ] **Step 1: Pin both environments in the cutover PR**

Set `dev.tag` and `prod.tag` to the Task 5 tag with its `artifactDigest`/`runtimeManifestDigest`. Prepend to `notes`: `CUTOVER (2026-09-XX): dev.tag and prod.tag -> <tag>; async turn contract (#plugin-PR, #cutover-PR). NOT backward compatible: the plugin requires the run contract and the backend sends it, so deploy and fleet roll happen in the same sitting per environment; pointers dead-lettered during the roll are redriven with scripts/openclaw_work_redrive.py.` The cutover PR carries Tasks 6–13 and this pin (one branch merged as one PR, or a short stack merged back to back before any dispatch).

- [ ] **Step 2: Dev**

1. `cd apps/backend && AWS_PROFILE=isol8-admin MESSAGING_TABLE=lightbulb-dev-messaging uv run python scripts/backfill_workspace_ids.py --dry-run`, then without `--dry-run`.
2. Merge the cutover PR; watch the dev deploy (`gh run watch`).
3. Roll the dev fleet from a signed-in `dev.flickmeon.ai` admin tab:

```js
await fetch('https://api-dev.flickmeon.ai/api/v1/container/updates', { method: 'POST', headers: { Authorization: 'Bearer ' + await Clerk.session.getToken(), 'Content-Type': 'application/json' }, body: JSON.stringify({ owner_id: 'all', update_type: 'task_definition_refresh' }) }).then(r => r.json())
```

4. Verify (spec §8):
   - One turn end to end: frames arrive on `/ws/{ws}/rooms/…` (the room streams text); `aws dynamodb get-item` on `OWNER#{owner}` / `RUN#{runId}` shows `started` then `ended/completed` with `usage`; `messaging.run.ended {outcome: completed}` increments; `messaging.ingress.latency` p99 < 1 s.
   - Kill mid-turn: `aws ecs update-service --desired-count 0` on the owner's service during a long turn, then back to 1. Within 90 s: one `error` + `run_ended` pair in the room, ledger `ended/error(agent_stopped_responding)`, no duplicate reply after restart, any redelivered pointer answered `replayed`.
   - Backend roll mid-turn: `aws ecs update-service --force-new-deployment` on the backend service during a turn. Frames keep flowing, the callbacks land on the new instance, the reply persists once.
   - Rotate a delivery key mid-turn: remove and re-add a human member. The tail of the stream is lost; the reply and the ledger close normally.
5. `aws sqs get-queue-attributes` on the dev DLQ; if anything dead-lettered during the roll, `uv run python scripts/openclaw_work_redrive.py` (dev).

- [ ] **Step 3: Prod (owner runs the dispatch and the roll)**

1. Backfill against `lightbulb-prod-messaging` (dry run, then real).
2. `gh workflow run deploy.yml --ref main` when `gh run list --workflow deploy.yml --json status --jq '[.[]|select(.status!="completed")]|length'` reads 0; watch it.
3. Roll the prod fleet immediately (same call against `api.flickmeon.ai` from a `flickmeon.ai` admin tab), then confirm every running task is on the new task definition:

```bash
aws ecs list-services --cluster lightbulb-prod-openclaw --profile isol8-admin --query 'serviceArns' --output text | tr '\t' '\n' | xargs -I{} aws ecs describe-services --cluster lightbulb-prod-openclaw --services {} --profile isol8-admin --query 'services[0].[serviceName,taskDefinition]' --output text
```

4. Redrive the prod DLQ if the roll dead-lettered anything.

- [ ] **Step 4: Cleanup**

```bash
git rm apps/backend/scripts/backfill_workspace_ids.py
git commit -m "chore(messaging): remove the one-shot workspace id backfill (ran in dev and prod)"
```

Delete this plan in the same PR or the next one; the spec stays while sub-projects B–D are in flight. Note in the `notifications` memory that room-run pushes now come from the projector (replies) and the run callback (failures).
