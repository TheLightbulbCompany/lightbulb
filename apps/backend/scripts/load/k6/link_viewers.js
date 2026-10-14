// T18: how many "Anyone with the link" viewers one session link holds (spec: the viewer cap,
// session_links.MAX_LINK_VIEWERS). mac_sim.py plays the Mac and runs this once its session is up.
// setup() turns the first session's link to "anyone" as its owner; then VIEWERS anonymous viewers
// arrive over RAMP_S, each as /s does: POST /public/session-links/viewing (no sign-in), subscribe
// /pub/<publicId> with the lease as its only credential, renew every 15 s, and time every frame.
//   mac_sim --macs 1 --fps 4 --frame-bytes 8192 -- k6 run -e SEND_S=180 -e VIEWERS=50 scripts/load/k6/link_viewers.js
// Every viewer comes from this one machine: the dev /public bucket (10 rps, 20 burst) is the
// stage's, not a per-IP limit, so 429s here are what real viewers spread over the world would get too.
import http from 'k6/http';
import encoding from 'k6/encoding';
import { Trend, Rate, Counter } from 'k6/metrics';
import { sleep } from 'k6';
import * as L from './lib.js';

const SEND_S = Number(__ENV.SEND_S);
const VIEWERS = Number(__ENV.VIEWERS || 1);
const RAMP_S = Number(__ENV.RAMP_S || 60);
if (!(SEND_S > RAMP_S)) throw new Error('SEND_S (seconds of watching) must be longer than RAMP_S');

const macs = JSON.parse(open(__ENV.MAC_SESSIONS));
const session = macs.sessions[0];
const FPS = Number(macs.fps);
const owner = L.fixtures.users.find((u) => u.userId === session.ownerUserId);

const frameLatency = new Trend('frame_latency_ms', true);
const frameDelivered = new Rate('frame_delivered');
const seated = new Counter('viewers_seated');
const full = new Counter('viewers_full'); // 409 LOCAL_SESSION_VIEWERS_FULL: the cap (or the link's 10-minute budget)
const throttled = new Counter('viewers_throttled'); // 429 from the /public bucket
const neverLive = new Counter('viewers_never_live');

export const options = {
  scenarios: {
    watch: {
      executor: 'ramping-vus', exec: 'watch', startVUs: 0, gracefulRampDown: '0s',
      stages: [{ target: VIEWERS, duration: RAMP_S + 's' }, { target: VIEWERS, duration: (SEND_S - RAMP_S + 15) + 's' }],
    },
  },
  thresholds: {
    'http_req_duration{name:POST /public/session-links/viewing}': ['p(95)<500'],
    frame_latency_ms: ['p(95)<1000'],
    frame_delivered: ['rate>=0.99'],
  },
};

export function setup() {
  if (!owner) throw new Error('the session owner is not in fixtures.json');
  const res = L.api(owner, 'PUT', `/messaging/sessions/${session.sessionId}/link`, { audience: 'anyone' }, 'PUT /messaging/sessions/:id/link');
  if (res.status !== 200) throw new Error(`could not make the link public: ${res.status} ${res.body}`);
  return { token: res.json('url').split('#')[1] };
}

function viewing(token, lease, fresh) {
  const res = http.post(`${L.API}/public/session-links/viewing`, JSON.stringify({ token, ...(lease ? { lease } : {}), ...(fresh ? { fresh: true } : {}) }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'POST /public/session-links/viewing' } });
  if (res.status === 429) throttled.add(1);
  if (res.status === 409 && String(res.body).includes('VIEWERS_FULL')) full.add(1);
  return res.status === 200 ? res.json() : null;
}

let done = false; // one seat per VU: a VU that got none, or whose hold ended, waits the run out

export function watch(data) {
  const end = L.remainingMs(SEND_S + RAMP_S + 15, 2_000);
  if (done || end <= 0) { sleep(Math.max(1, end / 1000)); return; }
  done = true;
  const seat = viewing(data.token, null, false);
  if (!seat) { sleep(Math.max(1, end / 1000)); return; }
  seated.add(1);
  let lease = seat.lease;
  let from = 0;
  const got = [];
  const opened = Date.now();
  L.subscribe({ appsyncJwt: lease }, seat, [seat.channel], end, {
    subscribed: () => { viewing(data.token, lease, true); from = Date.now() + 2_000; },
    tick: () => { const next = viewing(data.token, lease, false); if (next) lease = next.lease; },
    event: (ev) => {
      const f = ev.payload;
      if (ev.type !== 'workspace.session.screen' || !f || f.kind === 'ping' || !f.data) return;
      const ts = /LT=(\d+);/.exec(encoding.b64decode(f.data, 'std', 's'));
      if (!ts) return;
      frameLatency.add(Date.now() - Number(ts[1]));
      if (f.kind === 'output') got.push(Number(ts[1]));
    },
  }, 15_000);
  const until = Date.now() - 1_000;
  if (!from) { neverLive.add(1); from = opened + 2_000; }
  if (until <= from) return;
  const received = got.filter((t) => t >= from && t < until).length;
  const expected = Math.max(received, Math.floor((FPS * (until - from)) / 1000));
  for (let i = 0; i < expected; i++) frameDelivered.add(i < received);
}

export function teardown() {
  L.api(owner, 'PUT', `/messaging/sessions/${session.sessionId}/link`, { audience: 'workspace' }, 'PUT /messaging/sessions/:id/link');
}
