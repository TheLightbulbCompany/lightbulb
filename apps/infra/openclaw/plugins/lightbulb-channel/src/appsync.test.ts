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

describe("ecsCredentialsProvider on AgentCore (MMDS)", () => {
  // A microVM has no ECS credential endpoint and no static keys. Without this source the
  // plugin throws and an agent's reply frames never reach AppSync — the turn runs and the UI
  // never sees it. Proven necessary in dev 2026-09-16.
  const mmdsFetch = (overrides: Record<string, any> = {}) =>
    vi.fn(async (url: string, init?: any) => {
      if (url.endsWith("/latest/api/token")) {
        expect(init.method).toBe("PUT");
        expect(init.headers["X-aws-ec2-metadata-token-ttl-seconds"]).toBe("21600");
        return overrides.token ?? { status: 200, text: async () => "TOKEN123" };
      }
      if (url.endsWith("/security-credentials/")) {
        expect(init.headers["X-aws-ec2-metadata-token"]).toBe("TOKEN123");
        return overrides.role ?? { status: 200, text: async () => "lightbulb-dev-agentcore-gw" };
      }
      if (url.endsWith("/security-credentials/lightbulb-dev-agentcore-gw")) {
        expect(init.headers["X-aws-ec2-metadata-token"]).toBe("TOKEN123");
        return overrides.creds ?? {
          status: 200,
          json: async () => ({ AccessKeyId: "AKIA_MMDS", SecretAccessKey: "secret", Token: "sess" }),
        };
      }
      throw new Error(`unexpected url ${url}`);
    });

  it("resolves the execution role through the token-gated metadata service", async () => {
    const fetch = mmdsFetch();
    const provider = ecsCredentialsProvider(() => ({}), fetch as any);
    expect(await provider()).toMatchObject({ accessKeyId: "AKIA_MMDS", secretAccessKey: "secret", sessionToken: "sess" });
  });

  it("prefers the ECS endpoint and never touches MMDS when one is present", async () => {
    const fetch = vi.fn(async () => ({ status: 200, json: async () => ({ AccessKeyId: "ECS", SecretAccessKey: "s" }) }));
    const provider = ecsCredentialsProvider(() => ({ AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: "/v2/c" }), fetch as any);
    expect((await provider()).accessKeyId).toBe("ECS");
    expect(fetch.mock.calls.every(([u]: any[]) => !String(u).includes("169.254.169.254"))).toBe(true);
  });

  it("prefers static env keys over MMDS", async () => {
    const fetch = mmdsFetch();
    const provider = ecsCredentialsProvider(() => ({ AWS_ACCESS_KEY_ID: "STATIC", AWS_SECRET_ACCESS_KEY: "s" }), fetch as any);
    expect((await provider()).accessKeyId).toBe("STATIC");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("still throws the original error when MMDS is unreachable — we are simply not on a microVM", async () => {
    const fetch = vi.fn(async () => { throw new Error("connect ECONNREFUSED"); });
    const provider = ecsCredentialsProvider(() => ({}), fetch as any);
    await expect(provider()).rejects.toThrow("no AWS credential source");
  });

  it("throws rather than returning half-credentials when MMDS answers without a token", async () => {
    const fetch = mmdsFetch({ token: { status: 401, text: async () => "" } });
    const provider = ecsCredentialsProvider(() => ({}), fetch as any);
    await expect(provider()).rejects.toThrow("no AWS credential source");
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

  it("evicts a drained channel and still publishes the next batch on it independently", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    const pub = createFramePublisher({ fetch: fetch as any, region: "us-east-1", credentials: async () => CREDS });
    // Nothing queued for this channel: no publish, and the flush returns without waiting.
    await expect(pub.flush(CHANNEL)).resolves.toBeUndefined();
    expect(fetch).not.toHaveBeenCalled();

    pub.push(ENDPOINT, CHANNEL, frame("run_active"));
    await pub.flush(CHANNEL);
    // The entry is gone now; this push rebuilds one, and its batch must not carry the first.
    pub.push(ENDPOINT, CHANNEL, frame("done"));
    await pub.flush(CHANNEL);
    expect(fetch).toHaveBeenCalledTimes(2);
    const batches = fetch.mock.calls.map(([, init]: any[]) =>
      JSON.parse(init.body).events.map((event: string) => JSON.parse(event).payload.frame.type),
    );
    expect(batches).toEqual([["run_active"], ["done"]]);
  });

  it("waits for an in-flight publish even though the entry is evicted as it settles", async () => {
    vi.useFakeTimers();
    try {
      let landed = false;
      const fetch = vi.fn(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
        landed = true;
        return new Response("{}", { status: 200 });
      });
      const pub = createFramePublisher({ fetch: fetch as any, region: "us-east-1", credentials: async () => CREDS });
      pub.push(ENDPOINT, CHANNEL, frame("run_ended"));
      // The assertion runs at flush-resolution time: an eviction that let flush() skip the
      // await would resolve it before the post landed.
      const flushed = pub.flush(CHANNEL).then(() => expect(landed).toBe(true));
      await vi.runAllTimersAsync();
      await flushed;
      await expect(pub.flush(CHANNEL)).resolves.toBeUndefined();
      expect(fetch).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops an unserializable frame without poisoning the channel", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    const logged: string[] = [];
    const pub = createFramePublisher({ fetch: fetch as any, region: "us-east-1", credentials: async () => CREDS, log: (message) => logged.push(message) });
    pub.push(ENDPOINT, CHANNEL, { ...frame("chunk"), cycle: 1n } as unknown as Parameters<typeof pub.push>[2]);
    await expect(pub.flush(CHANNEL)).resolves.toBeUndefined();
    expect(fetch).not.toHaveBeenCalled();
    expect(logged).toEqual(["lightbulb: dropped 1 unserializable frame(s)"]);

    pub.push(ENDPOINT, CHANNEL, frame("done"));
    await expect(pub.flush(CHANNEL)).resolves.toBeUndefined();
    expect(fetch).toHaveBeenCalledOnce();
    expect(JSON.parse(fetch.mock.calls[0][1].body).events.map((e: string) => JSON.parse(e).payload.frame.type)).toEqual(["done"]);
  });
});
