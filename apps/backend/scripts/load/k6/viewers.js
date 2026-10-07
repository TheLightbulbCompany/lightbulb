// Scenario 3: viewers of synthetic-Mac sessions (spec §4). mac_sim.py plays the Macs and runs this
// script once its sessions are registered, with MAC_SESSIONS pointing at the sessions it wrote.
//   VIEWERS people watch each session: they hold their user channel, POST /viewing every 15 s, and
//   time every screen frame (its text carries the Mac's Date.now()). CHURN_S (3d) closes and
//   reopens each view every CHURN_S seconds.
import exec from 'k6/execution';
import encoding from 'k6/encoding';
import { Trend, Rate } from 'k6/metrics';
import * as L from './lib.js';

const SEND_S = Number(__ENV.SEND_S);
const PER_SESSION = Number(__ENV.VIEWERS || 1);
const CHURN_S = Number(__ENV.CHURN_S || 0);
if (!(SEND_S > 0)) throw new Error('SEND_S (seconds of watching) is required');

const sessions = JSON.parse(open(__ENV.MAC_SESSIONS)).sessions;
const owners = new Set(sessions.map((s) => s.ownerUserId));
const all = sessions.flatMap((s) => {
  const pool = L.fixtures.users.filter((u) => u.orgId === s.orgId && !owners.has(u.userId));
  if (pool.length < PER_SESSION) throw new Error(`session ${s.sessionId}: ${pool.length} viewers < VIEWERS=${PER_SESSION}`);
  return pool.slice(0, PER_SESSION).map((user) => ({ user, sessionId: s.sessionId }));
});
const viewers = L.mine(all);

const frameLatency = new Trend('frame_latency_ms', true);
const frameDelivered = new Rate('frame_delivered'); // 3a: >= 99%; 3a' records the drop rate

export const options = {
  scenarios: { recv: L.listen(viewers.length) },
  thresholds: {
    http_req_failed: ['rate<0.005'],
    'http_req_duration{name:POST /messaging/sessions/:id/viewing}': ['p(95)<500'],
    frame_latency_ms: ['p(95)<1000'],
    frame_delivered: ['rate>=0.99'],
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
  const left = L.remainingMs(SEND_S + 60, 2_000);
  if (left <= 0) return;
  const seqs = {}; // epoch -> Set(seq)
  L.subscribe(me.user, cfg, [L.userChannel(cfg)], CHURN_S ? Math.min(CHURN_S * 1000, left) : left, {
    subscribed: () => viewing(true),
    tick: () => viewing(false),
    event: (ev) => {
      const f = ev.payload;
      if (ev.type !== 'workspace.session.screen' || !f || f.sessionId !== me.sessionId || f.kind === 'ping') return;
      (seqs[f.epoch] = seqs[f.epoch] || new Set()).add(f.seq);
      const ts = /LT=(\d+);/.exec(encoding.b64decode(f.data, 'std', 's'));
      if (ts) frameLatency.add(Date.now() - Number(ts[1]));
    },
  }, 15_000);
  // Every seq between the first and last frame this view saw, per Mac relay run.
  for (const set of Object.values(seqs)) {
    const list = [...set];
    for (let s = Math.min(...list); s <= Math.max(...list); s++) frameDelivered.add(set.has(s));
  }
}
