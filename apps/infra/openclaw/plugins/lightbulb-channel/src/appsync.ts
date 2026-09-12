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
