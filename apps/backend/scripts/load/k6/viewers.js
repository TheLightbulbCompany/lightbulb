// Scenario 3: viewers of synthetic-Mac sessions (spec §4). mac_sim.py plays the Macs and runs this
// script once its sessions are registered, with MAC_SESSIONS pointing at the sessions it wrote.
//   VIEWERS people watch each session as the web view does: they subscribe its frames channel,
//   /ws/<ws>/session-frames/<sid> (and their user channel), then POST /viewing every 15 s, and time
//   every screen frame (its text carries the Mac's Date.now()). Every viewer of a session shares
//   that one channel: a frame is one publish. CHURN_S (3d) closes and reopens each view every
//   CHURN_S seconds.
import exec from 'k6/execution';
import encoding from 'k6/encoding';
import { Trend, Rate, Counter } from 'k6/metrics';
import { sleep } from 'k6';
import * as L from './lib.js';

const SEND_S = Number(__ENV.SEND_S);
const PER_SESSION = Number(__ENV.VIEWERS || 1);
const CHURN_S = Number(__ENV.CHURN_S || 0);
// mac_sim streams before this starts and stops when it exits: no head start, a 15 s drain.
const WATCH_S = Number(__ENV.SEND_S) + 15;
if (!(SEND_S > 0)) throw new Error('SEND_S (seconds of watching) is required');

const macs = JSON.parse(open(__ENV.MAC_SESSIONS));
const sessions = macs.sessions;
const FPS = Number(macs.fps); // output frames the Mac sends per second per session
const owners = new Set(sessions.map((s) => s.ownerUserId));
const all = sessions.flatMap((s) => {
  const pool = L.fixtures.users.filter((u) => u.orgId === s.orgId && !owners.has(u.userId));
  if (pool.length < PER_SESSION) throw new Error(`session ${s.sessionId}: ${pool.length} viewers < VIEWERS=${PER_SESSION}`);
  return pool.slice(0, PER_SESSION).map((user) => ({ user, sessionId: s.sessionId }));
});
const viewers = L.mine(all);

const frameLatency = new Trend('frame_latency_ms', true);
const frameDelivered = new Rate('frame_delivered'); // 3a: >= 99%; 3a' records the drop rate
const unsubscribed = new Counter('viewers_unsubscribed'); // a view that never went live: a failure

export const options = {
  scenarios: { recv: { ...L.listen(viewers.length), duration: WATCH_S + 's' } },
  thresholds: {
    http_req_failed: ['rate<0.005'],
    'http_req_duration{name:POST /messaging/sessions/:id/viewing}': ['p(95)<500'],
    frame_latency_ms: ['p(95)<1000'],
    frame_delivered: ['rate>=0.99'],
    viewers_unsubscribed: ['count==0'],
  },
};

let me = null; // this VU's viewer, kept across churn iterations
let cfg = null;

function viewing(fresh) {
  L.api(me.user, 'POST', `/messaging/sessions/${me.sessionId}/viewing`, fresh ? { fresh: true } : {},
    'POST /messaging/sessions/:id/viewing');
}

export function listen() {
  if (!me) {
    me = viewers[exec.scenario.iterationInTest];
    if (!me) throw new Error('more VUs than viewers');
    cfg = L.liveConfig(me.user); // also creates the user stream /viewing checks
  }
  const left = L.remainingMs(WATCH_S, 2_000);
  if (left <= 0) { // the last 2 s: wait them out rather than spin constant-vus iterations
    sleep(Math.max(0, L.remainingMs(WATCH_S, 0)) / 1000);
    return;
  }
  const opened = Date.now();
  // Delivery is counted against what the Mac sent while this view was live: its output frames
  // carry their send time, so a frame belongs to the window by that time, and frames never
  // received (leading, trailing, or all of them) count as lost.
  let from = 0;
  const got = [];
  const frames = `/ws/${cfg.workspaceId}/session-frames/${me.sessionId}`; // a refused subscribe never goes live
  L.subscribe(me.user, cfg, [L.userChannel(cfg), frames], CHURN_S ? Math.min(CHURN_S * 1000, left) : left, {
    // The first POST asks the Mac for a picture: only once the frames channel it comes on is live.
    subscribed: (channel) => { if (channel === frames) { viewing(true); from = Date.now() + 2_000; } }, // 2 s for the lease to reach the relay
    tick: () => viewing(false),
    event: (ev) => {
      const f = ev.payload;
      if (ev.type !== 'workspace.session.screen' || !f || f.sessionId !== me.sessionId || f.kind === 'ping') return;
      const ts = /LT=(\d+);/.exec(encoding.b64decode(f.data, 'std', 's'));
      if (!ts) return;
      frameLatency.add(Date.now() - Number(ts[1]));
      if (f.kind === 'output') got.push(Number(ts[1]));
    },
  }, 15_000);
  const until = Date.now() - 1_000; // frames still in flight at close are not counted either way
  if (!from) {
    // Never went live: it fails the run, and every frame it was due counts as lost.
    unsubscribed.add(1);
    from = opened + 2_000;
  }
  if (until <= from) return;
  const received = got.filter((t) => t >= from && t < until).length;
  const expected = Math.max(received, Math.floor((FPS * (until - from)) / 1000));
  for (let i = 0; i < expected; i++) frameDelivered.add(i < received);
}
