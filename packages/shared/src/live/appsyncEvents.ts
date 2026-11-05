/**
 * Dependency-free AWS AppSync Events WebSocket client (spec §8).
 *
 * Implements exactly the documented Event API protocol on the platform-native
 * `WebSocket`: OIDC auth in the `header-*` subprotocol, `connection_init` →
 * `connection_ack`, one `subscribe` per desired channel with a fresh Clerk
 * template token, `ka` watchdog from the ACK's `connectionTimeoutMs`,
 * `unsubscribe` on replacement, and a full-jitter reconnect capped at 30s on
 * any lost connection or rejected subscription. It has no publish method,
 * sends no client pings, and never logs a token or a raw server payload.
 *
 * One instance owns one reconnect loop; consumers replace the desired
 * channel set and read events — nothing else.
 */
import { parseLiveEvent, type LiveEvent } from "./types";

export const APPSYNC_EVENT_WS_PROTOCOL = "aws-appsync-event-ws";
const RECONNECT_BASE_MS = 500;
const RECONNECT_CAP_MS = 30_000;
const DEFAULT_CONNECTION_TIMEOUT_MS = 300_000;
const SEEN_EVENT_CAP = 1_000;

export type LiveErrorKind =
  | "no_token"
  | "connection_error"
  | "subscribe_error"
  /** A session frames channel the authorizer refused: dropped, not retried (see subscribe_error). */
  | "subscribe_refused"
  | "protocol_error";

export interface LiveStatus {
  /** `connection_ack` received on the current socket. */
  connected: boolean;
  /** Channels acknowledged with `subscribe_success` on the current socket. */
  liveChannels: readonly string[];
  /** Bumped on every ACK — a consumer reconciles over HTTP when it changes. */
  ackCount: number;
}

export interface AppSyncEventsClientOptions {
  /** `LiveConfig.httpEndpoint` — its host goes in the auth header. */
  httpEndpoint: string;
  /** `LiveConfig.realtimeEndpoint` — opened verbatim. */
  realtimeEndpoint: string;
  /** Clerk `getToken({ template: "lightbulb-appsync" })`. Called per connection
   *  AND per subscribe; a null token is an auth gap, not a crash. */
  getToken: () => Promise<string | null>;
  onEvent: (event: LiveEvent) => void;
  onStatus?: (status: LiveStatus) => void;
  /** Redacted: a kind and (for subscribe errors) the channel path — never the
   *  server payload or the token. */
  onError?: (kind: LiveErrorKind, channel?: string) => void;
  /** Subscription id source (lowercase UUID), injected for web/React Native parity. */
  uuid: () => string;
  /** Jitter source in [0, 1). Injectable for deterministic tests. */
  random?: () => number;
}

/** Base64URL (no padding) of a UTF-8 string, on `btoa` only. */
export function base64Url(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** The two subprotocols AppSync requires: encoded auth header + event ws. */
export function buildSubprotocols(httpEndpoint: string, token: string): string[] {
  const header = JSON.stringify({ Authorization: token, host: new URL(httpEndpoint).host });
  return [`header-${base64Url(header)}`, APPSYNC_EVENT_WS_PROTOCOL];
}

/** Full-jitter exponential backoff: random(0, min(cap, base * 2^attempt)). */
export function reconnectDelayMs(attempt: number, random: () => number): number {
  const ceiling = Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** attempt);
  return Math.floor(random() * ceiling);
}

/** The sibling frames channel for a room channel: `/rooms/` -> `/frames/`, same
 *  workspace/room/deliveryKey. AppSync caps channel paths at 5 segments, so this
 *  is NOT `<roomChannel>/frames` — see setChannels. */
export function framesChannelFor(roomChannel: string): string {
  return roomChannel.replace("/rooms/", "/frames/");
}

/** AppSync's answer to a handler's `util.unauthorized()`: an `Unauthorized…` errorType. Anything
 *  else (throttling, a service fault) is transient and takes the reconnect path. */
function refusedByAuthorizer(errors: unknown): boolean {
  return Array.isArray(errors) && errors.some((e) => /unauthori[sz]ed/i.test(String((e as { errorType?: unknown } | null)?.errorType ?? "")));
}

interface Subscription {
  id: string;
  live: boolean;
}

export class AppSyncEventsClient {
  private readonly opts: AppSyncEventsClientOptions;
  private readonly uuid: () => string;
  private readonly random: () => number;
  private socket: WebSocket | null = null;
  /** Socket generation. Every async continuation re-checks it so a stale
   *  token fetch, subscribe, or server frame can never act on a newer socket. */
  private gen = 0;
  private closed = false;
  private acked = false;
  private ackCount = 0;
  private attempt = 0;
  private connectionTimeoutMs = DEFAULT_CONNECTION_TIMEOUT_MS;
  private kaTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private desired: string[] = [];
  private readonly subs = new Map<string, Subscription>();
  private readonly channelById = new Map<string, string>();
  private readonly seen = new Set<string>();

  constructor(opts: AppSyncEventsClientOptions) {
    this.opts = opts;
    this.uuid = opts.uuid;
    this.random = opts.random ?? Math.random;
    void this.connect();
  }

  /** Replace the desired channel set: at most one user channel, at most one workspace
   *  `/sessions` channel, at most one workspace `/tasks` channel (super-agent switch on), at most one room channel, and — only alongside that same room
   *  channel — its `/frames` sibling
   *  (see framesChannelFor). Any other extra room-family channel, including a
   *  `/frames` channel that does not match the current room, is a `protocol_error`
   *  and is dropped rather than subscribed. Any number of shared sessions' `/session-frames/`
   *  channels ride along: one per open terminal view. A public session link's page holds one
   *  `/pub/<id>` channel and nothing else (its token is a link viewer's lease, not a Clerk token). */
  setChannels(channels: readonly string[]): void {
    if (this.closed) return;
    const unique = [...new Set(channels)];
    const users = unique.filter((channel) => channel.includes("/users/"));
    const rooms = unique.filter((channel) => channel.includes("/rooms/"));
    const frames = unique.filter((channel) => channel.includes("/frames/"));
    const sessions = unique.filter((channel) => channel.endsWith("/sessions"));
    const tasks = unique.filter((channel) => channel.endsWith("/tasks"));
    const sessionFrames = unique.filter((channel) => channel.includes("/session-frames/") || channel.startsWith("/pub/"));
    const room = rooms[0];
    const validFrames = frames.filter((channel) => !!room && channel === framesChannelFor(room));
    if (
      users.length > 1 ||
      sessions.length > 1 ||
      tasks.length > 1 ||
      rooms.length > 1 ||
      frames.length > validFrames.length ||
      users.length + sessions.length + tasks.length + rooms.length + frames.length + sessionFrames.length !== unique.length
    ) {
      this.opts.onError?.("protocol_error");
    }
    this.desired = [...users.slice(0, 1), ...sessions.slice(0, 1), ...tasks.slice(0, 1), ...rooms.slice(0, 1), ...validFrames.slice(0, 1), ...sessionFrames];
    for (const [channel, sub] of [...this.subs]) {
      if (!this.desired.includes(channel)) this.dropSubscription(channel, sub, true);
    }
    if (this.acked) {
      for (const channel of this.desired) {
        if (!this.subs.has(channel)) void this.subscribe(channel);
      }
    }
    this.emitStatus();
  }

  close(): void {
    this.closed = true;
    this.gen += 1;
    this.clearTimers();
    for (const [channel, sub] of [...this.subs]) this.dropSubscription(channel, sub, true);
    const socket = this.socket;
    this.socket = null;
    this.acked = false;
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      socket.close(1000, "closed");
    }
  }

  // ---- connection ----

  private async connect(): Promise<void> {
    if (this.closed) return;
    const gen = ++this.gen;
    let token: string | null = null;
    try {
      token = await this.opts.getToken();
    } catch {
      token = null;
    }
    if (this.closed || gen !== this.gen) return;
    if (!token) {
      this.opts.onError?.("no_token");
      this.scheduleReconnect();
      return;
    }
    let socket: WebSocket;
    try {
      socket = new WebSocket(this.opts.realtimeEndpoint, buildSubprotocols(this.opts.httpEndpoint, token));
    } catch {
      this.opts.onError?.("connection_error");
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (gen !== this.gen) return;
      socket.send(JSON.stringify({ type: "connection_init" }));
    };
    socket.onmessage = (event: MessageEvent) => {
      if (gen !== this.gen) return;
      this.handleMessage(event.data, gen);
    };
    socket.onerror = () => {
      // The close event that follows carries the state change.
    };
    socket.onclose = () => {
      if (gen !== this.gen) return;
      this.socket = null;
      this.acked = false;
      this.clearKa();
      for (const [channel, sub] of [...this.subs]) this.dropSubscription(channel, sub, false);
      this.emitStatus();
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.closed || this.reconnectTimer) return;
    const delay = reconnectDelayMs(this.attempt, this.random);
    this.attempt += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  private clearTimers(): void {
    this.clearKa();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearKa(): void {
    if (this.kaTimer) {
      clearTimeout(this.kaTimer);
      this.kaTimer = null;
    }
  }

  /** One watchdog, re-armed on ACK and every `ka`; expiry closes the socket
   *  and lets onclose drive the reconnect. */
  private armKa(): void {
    this.clearKa();
    const socket = this.socket;
    this.kaTimer = setTimeout(() => {
      this.kaTimer = null;
      if (socket && socket === this.socket) socket.close(4000, "keepalive timeout");
    }, this.connectionTimeoutMs);
  }

  // ---- messages ----

  private handleMessage(data: unknown, gen: number): void {
    if (typeof data !== "string") return;
    let msg: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(data);
      if (!parsed || typeof parsed !== "object") return;
      msg = parsed as Record<string, unknown>;
    } catch {
      return;
    }
    switch (msg.type) {
      case "connection_ack": {
        if (this.acked) return;
        this.acked = true;
        this.ackCount += 1;
        const timeout = msg.connectionTimeoutMs;
        this.connectionTimeoutMs =
          typeof timeout === "number" && timeout > 0 ? timeout : DEFAULT_CONNECTION_TIMEOUT_MS;
        this.armKa();
        for (const channel of this.desired) {
          if (!this.subs.has(channel)) void this.subscribe(channel);
        }
        this.emitStatus();
        return;
      }
      case "ka":
        if (this.acked) this.armKa();
        return;
      case "subscribe_success": {
        if (!this.acked) return;
        const channel = typeof msg.id === "string" ? this.channelById.get(msg.id) : undefined;
        const sub = channel ? this.subs.get(channel) : undefined;
        if (!channel || !sub || sub.id !== msg.id) return;
        sub.live = true;
        // Only a fully live cycle resets the reconnect backoff: an ACK alone
        // does not, or a persistently rejected channel would reconnect-spin.
        if (this.desired.every((c) => this.subs.get(c)?.live)) this.attempt = 0;
        this.emitStatus();
        return;
      }
      case "subscribe_error": {
        const channel = typeof msg.id === "string" ? this.channelById.get(msg.id) : undefined;
        const sub = channel ? this.subs.get(channel) : undefined;
        if (!channel || !sub || sub.id !== msg.id) return;
        this.dropSubscription(channel, sub, false);
        if (channel.includes("/session-frames/") && refusedByAuthorizer(msg.errors)) {
          // Not a member, not shared (or the authorizer could not read): this socket stops
          // asking, and its other channels stay up. The view claims it again with backoff, and
          // its own watch answer (403/404) is what ends it.
          this.desired = this.desired.filter((c) => c !== channel);
          this.opts.onError?.("subscribe_refused", channel);
          this.emitStatus();
          return;
        }
        this.opts.onError?.("subscribe_error", channel);
        this.emitStatus();
        // Nothing else would ever retry: the socket stays acked, so the ACK's
        // subscribe loop never re-fires and `ka` keeps it alive forever. Close
        // and let the jittered reconnect resubscribe everything — its fresh
        // ACK is also what makes consumers reconcile the gap over HTTP.
        this.socket?.close(4002, "subscribe rejected");
        return;
      }
      case "data": {
        if (!this.acked) return;
        const channel = typeof msg.id === "string" ? this.channelById.get(msg.id) : undefined;
        const sub = channel ? this.subs.get(channel) : undefined;
        if (!channel || !sub?.live || sub.id !== msg.id) return;
        // AppSync Events delivers one stringified event per data frame
        // (`event: "<json>"`); an array is tolerated for batched delivery.
        const items = typeof msg.event === "string" ? [msg.event] : Array.isArray(msg.event) ? msg.event : [];
        for (const item of items) this.deliver(item, gen);
        return;
      }
      case "connection_error":
      case "error": {
        this.opts.onError?.(this.acked ? "protocol_error" : "connection_error");
        if (!this.acked) this.socket?.close(4001, "connection error");
        return;
      }
      default:
        return; // unsubscribe_success and anything unknown
    }
  }

  private deliver(item: unknown, gen: number): void {
    const event = parseLiveEvent(item);
    if (!event || gen !== this.gen) return;
    if (this.seen.has(event.eventId)) return;
    this.seen.add(event.eventId);
    while (this.seen.size > SEEN_EVENT_CAP) {
      const oldest = this.seen.values().next().value;
      if (oldest === undefined) break;
      this.seen.delete(oldest);
    }
    try {
      this.opts.onEvent(event);
    } catch {
      // A subscriber bug must not break the protocol loop.
    }
  }

  // ---- subscriptions ----

  private async subscribe(channel: string): Promise<void> {
    const gen = this.gen;
    const sub: Subscription = { id: this.uuid(), live: false };
    this.subs.set(channel, sub);
    this.channelById.set(sub.id, channel);
    let token: string | null = null;
    try {
      token = await this.opts.getToken();
    } catch {
      token = null;
    }
    // Superseded while the token was in flight: socket turned over, the
    // channel was replaced, or the client closed.
    if (this.closed || gen !== this.gen || this.subs.get(channel) !== sub) return;
    if (!token) {
      this.dropSubscription(channel, sub, false);
      this.opts.onError?.("no_token", channel);
      this.socket?.close(4003, "subscription auth unavailable");
      return;
    }
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(
      JSON.stringify({
        type: "subscribe",
        id: sub.id,
        channel,
        authorization: { Authorization: token },
      }),
    );
  }

  /** Forget a subscription; optionally tell the server. Correctness never
   *  depends on the unsubscribe being acknowledged — the id is already dead
   *  locally, so a late frame on it is dropped as stale. */
  private dropSubscription(channel: string, sub: Subscription, notify: boolean): void {
    this.subs.delete(channel);
    this.channelById.delete(sub.id);
    const socket = this.socket;
    if (notify && this.acked && socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "unsubscribe", id: sub.id }));
    }
  }

  private emitStatus(): void {
    this.opts.onStatus?.({
      connected: this.acked,
      liveChannels: [...this.subs].filter(([, s]) => s.live).map(([c]) => c),
      ackCount: this.ackCount,
    });
  }
}
