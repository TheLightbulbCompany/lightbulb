// Shared k6 pieces for the load harness (see scripts/load/README.md).
// Dev only: every request goes to the hard-coded dev hosts, and the script aborts otherwise.
import http from 'k6/http';
import ws from 'k6/ws';
import crypto from 'k6/crypto';
import encoding from 'k6/encoding';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { Counter } from 'k6/metrics';

export const API = 'https://api-dev.thelightbulb.company/api/v1';
const FAPI = 'https://up-moth-55.clerk.accounts.dev';
const REFRESH_MS = 40_000; // Clerk session JWTs live 60 s

const raw = JSON.parse(open(__ENV.FIXTURES || './fixtures.json'));
if (raw.api !== API || raw.fapi !== FAPI) {
  throw new Error(`refusing: fixtures point at ${raw.api} / ${raw.fapi}, dev only`);
}
export const fixtures = raw;

// 429s are the stage throttle answering: counted on their own, not as failures (spec §2 Errors row).
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 399 }, 429));
export const http429 = new Counter('http_429');
export const appsyncErrors = new Counter('appsync_errors');
export const dupes = new Counter('dupes'); // AppSync redeliveries, dropped before any metric sees them
const SEEN_EVENT_CAP = 1_000; // as AppSyncEventsClient.deliver

// ---- DLT partitioning: every task runs the whole script; each keeps its own slice.
const TASK_INDEX = Number(__ENV.TASK_INDEX || 0);
const TASK_COUNT = Number(__ENV.TASK_COUNT || 1);
export const mine = (items) => items.filter((_, i) => i % TASK_COUNT === TASK_INDEX);
/** This task's share of a global iteration sequence: task t of n takes t, t+n, t+2n, ... */
export const taskIteration = (i) => i * TASK_COUNT + TASK_INDEX;

// ---- the spec's scenario shapes (§1 "k6 shape").
// Defaults stay <= fixture users (spec); PRE_VUS/MAX_VUS override, e.g. 2c's 18-mention burst from one user.
const vus = (name, n) => (__ENV[name] ? Number(__ENV[name]) : Math.max(1, Math.min(n, fixtures.users.length)));
export const arrival = (pre, max) => ({
  executor: 'ramping-arrival-rate', startRate: 0, timeUnit: __ENV.TIME_UNIT || '1s',
  stages: JSON.parse(__ENV.STAGES), preAllocatedVUs: vus('PRE_VUS', pre), maxVUs: vus('MAX_VUS', max),
});
// Receivers start at 0, senders at 30 s for SEND_S: hold the 30 s head start plus a 60 s drain.
export const LISTEN_S = Number(__ENV.SEND_S) + 90;
export const listen = (n) => ({ executor: 'constant-vus', exec: 'listen', vus: n, duration: LISTEN_S + 's' });

/** ms left in the current scenario, minus a margin for end-of-run reads. */
export function remainingMs(durationS, marginMs) {
  return exec.scenario.startTime + durationS * 1000 - Date.now() - marginMs;
}

/** A constant-vus VU runs its listen function again if it returns early: hold one subscription per VU. */
export function onlyFirstIteration(durationS) {
  if (exec.vu.iterationInScenario === 0) return true;
  const left = remainingMs(durationS, 0);
  if (left > 0) sleep(left / 1000);
  return false;
}

export function uuid4() {
  const b = new Uint8Array(crypto.randomBytes(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/** A UUID-shaped id that is the same in every task: `clientRoomId` makes room creation idempotent. */
export function stableUuid(seed) {
  const h = crypto.sha256(seed, 'hex');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-4${h.slice(13, 16)}-8${h.slice(17, 20)}-${h.slice(20, 32)}`;
}

// ---- Clerk tokens, cached per user inside this VU. The client JWT rotates: carry it forward.
const tokens = {};
function mint(user, kind) {
  const t = tokens[user.userId] || (tokens[user.userId] = { clientJwt: user.clientJwt });
  const fresh = t[kind];
  if (fresh && Date.now() - fresh.at < REFRESH_MS) return fresh.jwt;
  const path = kind === 'appsync' ? '/tokens/lightbulb-appsync' : '/tokens';
  const res = http.post(`${FAPI}/v1/client/sessions/${user.sid}${path}?_is_native=1`, {}, {
    headers: { Authorization: t.clientJwt }, tags: { name: `clerk ${kind} token` },
  });
  if (res.status !== 200) throw new Error(`clerk ${kind} token for ${user.userId}: ${res.status}`);
  t.clientJwt = res.headers.Authorization || t.clientJwt;
  t[kind] = { jwt: res.json('jwt'), at: Date.now() };
  return t[kind].jwt;
}
export const apiToken = (user) => mint(user, 'api');
export const appsyncToken = (user) => mint(user, 'appsync');

/** One API call as `user`. `name` groups URLs with ids into one metric series. */
export function api(user, method, path, body, name) {
  const res = http.request(method, API + path, body === undefined ? null : JSON.stringify(body), {
    headers: { Authorization: `Bearer ${apiToken(user)}`, 'Content-Type': 'application/json' },
    tags: { name: name || path },
  });
  if (res.status === 429) http429.add(1);
  return res;
}

/** An end-of-run read that must not be skipped: 3 tries (1 s, 2 s backoff); null if all fail. */
export function readWithRetry(user, path, name) {
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt) sleep(attempt);
    const res = api(user, 'GET', path, undefined, name);
    if (res.status === 200) return res;
  }
  return null;
}

/** All messages after `afterSeq`, paged like a client reconciling a gap; null if a page cannot be read. */
export function messagesAfter(user, roomId, afterSeq) {
  const out = [];
  for (let cursor = afterSeq; ;) {
    const res = readWithRetry(user, `/messaging/rooms/${roomId}/messages?afterSeq=${cursor}&limit=100`,
      'GET /messaging/rooms/:id/messages');
    if (!res) return null;
    const page = res.json('messages') || [];
    if (!page.length) return out;
    for (const m of page) { out.push(m); cursor = Math.max(cursor, m.messageSeq); }
  }
}

/** Counted when an authoritative end-of-run read failed: the run cannot pass with data unverified. */
export const completenessReadFailed = new Counter('completeness_read_failed');

// ---- AppSync Events: a minimal port of packages/shared/src/live/appsyncEvents.ts (subscribe only).
/**
 * Hold one AppSync connection for `holdMs`, subscribed to `channels`.
 * `on.subscribed(channel, atMs)`, `on.event(liveEvent, channel)`, `on.tick()` every `tickMs`.
 */
export function subscribe(user, cfg, channels, holdMs, on, tickMs) {
  const header = JSON.stringify({ Authorization: appsyncToken(user), host: cfg.httpEndpoint.replace(/^https?:\/\//, '').split('/')[0] });
  const protocols = `header-${encoding.b64encode(header, 'rawurl')}, aws-appsync-event-ws`;
  const byId = {};
  const seen = new Set(); // eventIds, bounded: a redelivered reply or frame must not offset a lost one
  ws.connect(cfg.realtimeEndpoint, { headers: { 'Sec-WebSocket-Protocol': protocols } }, (socket) => {
    socket.on('open', () => socket.send(JSON.stringify({ type: 'connection_init' })));
    socket.on('message', (data) => {
      let msg;
      try { msg = JSON.parse(data); } catch (_) { return; }
      if (msg.type === 'connection_ack') {
        for (const channel of channels) {
          const id = uuid4();
          byId[id] = channel;
          socket.send(JSON.stringify({ type: 'subscribe', id, channel, authorization: { Authorization: appsyncToken(user) } }));
        }
      } else if (msg.type === 'subscribe_success') {
        if (byId[msg.id] && on.subscribed) on.subscribed(byId[msg.id], Date.now());
      } else if (msg.type === 'data') {
        const items = typeof msg.event === 'string' ? [msg.event] : Array.isArray(msg.event) ? msg.event : [];
        for (const item of items) {
          let ev;
          try { ev = typeof item === 'string' ? JSON.parse(item) : item; } catch (_) { continue; }
          if (!ev) continue;
          if (ev.eventId) {
            if (seen.has(ev.eventId)) { dupes.add(1); continue; }
            seen.add(ev.eventId);
            if (seen.size > SEEN_EVENT_CAP) seen.delete(seen.values().next().value);
          }
          if (on.event) on.event(ev, byId[msg.id]);
        }
      } else if (msg.type === 'subscribe_error' || msg.type === 'connection_error' || msg.type === 'error') {
        appsyncErrors.add(1, { type: msg.type });
      }
    });
    socket.on('error', () => appsyncErrors.add(1, { type: 'socket' }));
    // Keep this user's 60 s JWTs fresh for the HTTP calls made from inside the socket loop.
    socket.setInterval(() => { apiToken(user); appsyncToken(user); }, REFRESH_MS);
    if (on.tick) socket.setInterval(on.tick, tickMs);
    socket.setTimeout(() => socket.close(), Math.max(1000, holdMs));
  });
}

export function liveConfig(user) {
  const res = api(user, 'GET', '/messaging/live-config');
  if (res.status !== 200) throw new Error(`live-config ${res.status}`);
  return res.json();
}

export const roomChannel = (room) => `/ws/${room.workspaceId}/rooms/${room.roomId}/${room.deliveryKey}`;
export const userChannel = (cfg) => `/ws/${cfg.workspaceId}/users/${cfg.userChannelKey}`;

/** Create (or, same clientRoomId, get back) a human-only or agent room as `creator`. */
export function createRoom(creator, seed, name, members) {
  const res = api(creator, 'POST', '/messaging/rooms', {
    clientRoomId: stableUuid(seed), name, kind: 'channel', members,
  }, 'POST /messaging/rooms');
  if (res.status !== 201 && res.status !== 200) throw new Error(`create room ${name}: ${res.status} ${res.body}`);
  return res.json();
}
