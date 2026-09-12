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
