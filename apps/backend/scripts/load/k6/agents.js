// Scenario 2: agent-conversation load (spec §3). See README.md for the per-sub-test commands.
//   Every fixture org gets AGENT_ROOMS load rooms holding its creator and the six starter agents
//   on `mentions`, so only an @mention wakes anyone (a user_request: no Jev, no ambient wake).
//   Iteration i messages org i%O, then rooms, then agents in turn. Each message asks for a unique
//   token that names the room and agent it was sent to: a reply carrying another room's or
//   agent's token is cross-talk and aborts the run.
import exec from 'k6/execution';
import { check } from 'k6';
import { Trend, Counter } from 'k6/metrics';
import * as L from './lib.js';

const SEND_S = Number(__ENV.SEND_S);
const ROOMS_PER_ORG = Number(__ENV.AGENT_ROOMS || 1);
if (!(SEND_S > 0)) throw new Error('SEND_S (seconds of sending) is required');

const byId = Object.fromEntries(L.fixtures.users.map((u) => [u.userId, u]));
const orgs = L.fixtures.orgs.map((o) => ({
  ...o,
  creator: byId[o.creatorUserId] || L.fixtures.users.find((u) => u.orgId === o.orgId),
}));
const receivers = L.mine(orgs.map((_, i) => i)); // one subscriber per org

const replySeen = new Trend('reply_seen_ms', true);
const crossTalk = new Counter('crosstalk');
const repliesOk = new Counter('replies_ok');
const repliesWithoutToken = new Counter('replies_without_token');
const turnsSent = new Counter('turns_sent');
const TOKEN = /LT(\d+)x(\d+)x(\d+)x[a-z0-9]+/g;

export const options = {
  scenarios: {
    send: { ...L.arrival(30, 30), exec: 'send', startTime: '30s' },
    recv: L.listen(receivers.length),
  },
  thresholds: {
    http_req_failed: ['rate<0.005'],
    crosstalk: [{ threshold: 'count==0', abortOnFail: true }],
    reply_seen_ms: [`p(95)<${Number(__ENV.REPLY_P95_MS || 20000)}`], // 90000 for cold sub-tests
  },
};

function roster(user) {
  const rooms = L.api(user, 'GET', '/messaging/rooms').json('rooms');
  const general = rooms.find((r) => (r.name || '').toLowerCase() === 'general');
  if (!general) throw new Error(`${user.orgId} has no #general: run fixtures.py setup`);
  return L.api(user, 'GET', `/messaging/rooms/${general.roomId}`, undefined, 'GET /messaging/rooms/:id')
    .json('members').filter((m) => m.kind === 'agent');
}

export function setup() {
  const runId = __ENV.RUN_ID || String(Date.now());
  const rooms = []; // global index -> {org, roomId, ...}; the index rides in every token
  const out = orgs.map((org, o) => {
    const agents = roster(org.creator).map((a) => ({ id: a.id, handle: a.mentionHandle, name: a.displayName }));
    const members = agents.map((a) => ({ kind: 'agent', id: a.id, role: 'member', listenMode: 'mentions' }));
    const workspaceId = L.liveConfig(org.creator).workspaceId;
    const mineRooms = [];
    for (let r = 0; r < ROOMS_PER_ORG; r++) {
      const room = L.createRoom(org.creator, `load-agents-${runId}-${o}-${r}`, `load-agents-${runId}-${o}-${r}`, members);
      mineRooms.push(rooms.length);
      rooms.push({ org: o, roomId: room.roomId, deliveryKey: room.deliveryKey, workspaceId: room.workspaceId || workspaceId });
    }
    return { rooms: mineRooms, agents };
  });
  return { orgs: out, rooms };
}

export function send(data) {
  const i = exec.scenario.iterationInTest;
  const o = i % orgs.length;
  const j = Math.floor(i / orgs.length);
  const org = data.orgs[o];
  const roomIdx = org.rooms[j % org.rooms.length];
  const agentIdx = Math.floor(j / org.rooms.length) % org.agents.length;
  const agent = org.agents[agentIdx];
  const token = `LT${Date.now()}x${roomIdx}x${agentIdx}x${L.uuid4().slice(0, 6)}`;
  const res = L.api(orgs[o].creator, 'POST', `/messaging/rooms/${data.rooms[roomIdx].roomId}/messages`, {
    clientMessageId: L.uuid4(),
    text: `@${agent.handle} Reply with exactly the token ${token} and nothing else. Do not use tools.`,
    mentions: [{ kind: 'agent', id: agent.id }],
    replyToMessageId: null,
    threadRootMessageId: null,
  }, 'POST /messaging/rooms/:id/messages');
  if (check(res, { 'turn accepted': (r) => r.status === 200 || r.status === 201 })) turnsSent.add(1);
}

export function listen(data) {
  if (!L.onlyFirstIteration(L.LISTEN_S)) return;
  const o = receivers[exec.scenario.iterationInTest];
  if (o === undefined) return;
  const org = data.orgs[o];
  const user = orgs[o].creator;
  const channels = org.rooms.map((r) => L.roomChannel(data.rooms[r]));
  L.subscribe(user, L.liveConfig(user), channels, L.remainingMs(L.LISTEN_S, 5_000), {
    event: (ev) => {
      const m = ev.payload;
      if (ev.type !== 'room.message.created' || !m || !m.author || m.author.kind !== 'agent') return;
      const found = [...(m.text || '').matchAll(TOKEN)];
      if (!found.length) { repliesWithoutToken.add(1); return; }
      for (const [, ms, roomIdx, agentIdx] of found) {
        const room = data.rooms[Number(roomIdx)];
        const agent = room && data.orgs[room.org].agents[Number(agentIdx)];
        if (!room || room.roomId !== m.roomId || !agent || agent.id !== m.author.id) {
          crossTalk.add(1);
          exec.test.abort(`cross-talk: token for room ${roomIdx} agent ${agentIdx} answered in ${m.roomId} by ${m.author.id}`);
        }
        repliesOk.add(1);
        replySeen.add(Date.now() - Number(ms));
      }
    },
  });
}
