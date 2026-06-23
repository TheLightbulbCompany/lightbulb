#!/usr/bin/env node
/**
 * ora-booking-normalizer.js
 *
 * Pure compute. The scheduling-request pipeline runs provider-specific
 * book-{gog,ms365,caldav,calctl} steps in parallel (only one fires at a
 * time, determined by the user's primary calendar). This script picks
 * whichever one produced a result and emits a normalized shape that
 * downstream steps (verify, rollback, send-confirmation) consume without
 * caring which provider actually ran.
 *
 * Zero LLM. Zero external dependencies.
 *
 * Input (stdin JSON):
 *   {
 *     gog: <book-gog response or null>,
 *     ms365: <book-ms365 response or null>,
 *     caldav: <book-caldav response or null>,
 *     calctl: <book-calctl response or null>
 *   }
 *
 * Output:
 *   {ok: true, event_id, provider, video_link, raw}
 *   {ok: false, error}
 */

function pick(results) {
  const gog = results.gog;
  if (gog && (gog.event_id || gog.id)) {
    return {
      ok: true,
      event_id: gog.event_id || gog.id,
      provider: 'gog',
      video_link: gog.hangout_link || gog.hangoutLink || (gog.conferenceData && gog.conferenceData.entryPoints && gog.conferenceData.entryPoints[0] && gog.conferenceData.entryPoints[0].uri) || null,
      raw: gog,
    };
  }

  const ms365 = results.ms365;
  if (ms365 && ms365.id) {
    return {
      ok: true,
      event_id: ms365.id,
      provider: 'ms365',
      video_link: (ms365.onlineMeeting && ms365.onlineMeeting.joinUrl) || null,
      raw: ms365,
    };
  }

  const caldav = results.caldav;
  if (caldav && caldav.event_id) {
    return {
      ok: true,
      event_id: caldav.event_id,
      provider: 'caldav',
      video_link: null,
      raw: caldav,
    };
  }

  const calctl = results.calctl;
  if (calctl && calctl.event_id) {
    return {
      ok: true,
      event_id: calctl.event_id,
      provider: 'calctl',
      video_link: null,
      raw: calctl,
    };
  }

  return { ok: false, error: 'no booking succeeded — no provider returned an event_id' };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stdout.write(JSON.stringify({ ok: false, error: 'no input' }));
    process.exit(1);
  }
  const result = pick(input);
  process.stdout.write(JSON.stringify(result));
  // "No provider returned an event_id" is a normal branch, not a crash — the
  // decision is carried in ok; exit 0 so the workflow gates downstream on .json.ok.
  process.exit(0);
}

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { data += chunk; });
    process.stdin.on('end', () => {
      try { resolve(JSON.parse(data)); }
      catch { resolve(null); }
    });
    if (process.stdin.isTTY) resolve(null);
  });
}

module.exports = { pick };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message + '\n');
    process.exit(1);
  });
}
