// Scenario 1: messaging load, humans only (spec §2). See README.md for the per-sub-test commands.
//   ROOMS rooms of ROOM_SIZE fixture users each; the first SENDERS_PER_ROOM of every room send,
//   the rest subscribe. STAGES drives the total send rate; SEND_S is the send phase's length.
import exec from 'k6/execution';
import { check } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';
import * as L from './lib.js';

const ROOMS = Number(__ENV.ROOMS || 1);
const SIZE = Number(__ENV.ROOM_SIZE || 2);
const SENDERS = Number(__ENV.SENDERS_PER_ROOM || 1);
const SEND_S = Number(__ENV.SEND_S);
const users = L.fixtures.users;
if (!(SEND_S > 0)) throw new Error('SEND_S (seconds of sending) is required');
if (SIZE > 32) throw new Error('ROOM_SIZE > 32 = messaging_repo.MAX_ROOM_MEMBERS');
if (SENDERS < 1 || SENDERS >= SIZE) throw new Error('need 1 <= SENDERS_PER_ROOM < ROOM_SIZE');
if (ROOMS * SIZE > users.length) throw new Error(`${ROOMS} x ${SIZE} needs more than ${users.length} fixture users`);

const layout = Array.from({ length: ROOMS }, (_, r) => users.slice(r * SIZE, (r + 1) * SIZE));
const senders = L.mine(layout.flatMap((m, room) => m.slice(0, SENDERS).map((user) => ({ user, room }))));
const receivers = L.mine(layout.flatMap((m, room) => m.slice(SENDERS).map((user) => ({ user, room }))));

const liveDelivery = new Trend('live_delivery_ms', true);
const liveCompleteness = new Rate('live_completeness'); // live only, before any reconcile: recorded
const completeAfterReconcile = new Rate('complete_after_reconcile'); // pass bar: 100%
const gapReconciles = new Counter('gap_reconciles');
const sent = new Counter('messages_sent');

export const options = {
  scenarios: {
    send: { ...L.arrival(30, 60), exec: 'send', startTime: '30s' },
    recv: L.listen(receivers.length),
  },
  thresholds: {
    http_req_failed: ['rate<0.005'],
    'http_req_duration{name:POST /messaging/rooms/:id/messages}': ['p(95)<500'],
    live_delivery_ms: ['p(95)<2000'],
    complete_after_reconcile: ['rate==1'],
  },
};

export function setup() {
  const runId = __ENV.RUN_ID || String(Date.now());
  const rooms = layout.map((members, r) => {
    const creator = members[0];
    const others = members.slice(1).map((u) => ({ kind: 'human', id: u.userId, role: 'member', listenMode: null }));
    const room = L.createRoom(creator, `load-${runId}-${r}`, `load-${runId}-${r}`, others);
    const workspaceId = room.workspaceId || L.liveConfig(creator).workspaceId;
    return { roomId: room.roomId, deliveryKey: room.deliveryKey, workspaceId, baseSeq: room.lastMessageSeq || 0 };
  });
  return { rooms };
}

export function send(data) {
  const s = senders[exec.scenario.iterationInTest % senders.length];
  const room = data.rooms[s.room];
  const res = L.api(s.user, 'POST', `/messaging/rooms/${room.roomId}/messages`, {
    clientMessageId: L.uuid4(), text: `load ts=${Date.now()} n=${L.uuid4()}`, mentions: [],
    replyToMessageId: null, threadRootMessageId: null,
  }, 'POST /messaging/rooms/:id/messages');
  if (check(res, { 'message accepted': (r) => r.status === 200 || r.status === 201 })) sent.add(1);
}

export function listen(data) {
  if (!L.onlyFirstIteration(SEND_S + 60)) return;
  const me = receivers[exec.scenario.iterationInTest];
  if (!me) return;
  const room = data.rooms[me.room];
  const firstSend = exec.scenario.startTime + 30_000; // senders start at 30 s
  let counted = false;
  let lastSeq = room.baseSeq;
  const live = new Set();
  const healed = new Set();
  const cfg = L.liveConfig(me.user);
  L.subscribe(me.user, cfg, [L.roomChannel(room)], L.remainingMs(SEND_S + 60, 15_000), {
    subscribed: (_, at) => { counted = at < firstSend; },
    event: (ev) => {
      if (ev.type !== 'room.message.created' || ev.payload.roomId !== room.roomId) return;
      const seq = ev.payload.messageSeq;
      live.add(seq);
      const ts = /^load ts=(\d+) /.exec(ev.payload.text || '');
      if (ts && counted) liveDelivery.add(Date.now() - Number(ts[1]));
      if (seq > lastSeq + 1) { // a gap: reconcile like a client does
        gapReconciles.add(1);
        for (const m of L.messagesAfter(me.user, room.roomId, lastSeq)) healed.add(m.messageSeq);
      }
      lastSeq = Math.max(lastSeq, seq);
    },
  });
  if (!counted) return; // joined after the first send: its misses say nothing
  const detail = L.api(me.user, 'GET', `/messaging/rooms/${room.roomId}`, undefined, 'GET /messaging/rooms/:id');
  const last = detail.status === 200 ? detail.json('lastMessageSeq') : lastSeq;
  for (const m of L.messagesAfter(me.user, room.roomId, room.baseSeq)) healed.add(m.messageSeq);
  for (let seq = room.baseSeq + 1; seq <= last; seq++) {
    liveCompleteness.add(live.has(seq));
    completeAfterReconcile.add(live.has(seq) || healed.has(seq));
  }
}
