name: morning-briefing
# Daily communication briefing, assembled before the user's start time (cron).
# Deterministic + gateway-free. Top contacts waiting longest, highest-urgency
# items, today's calendar context — written to a notifications queue for the
# agent loop to narrate (LLM) and deliver (Slack). Neither the LLM narrative
# nor the Slack post can run inside an embedded pipeline (both 401), so this
# pipeline only PRODUCES the deterministic briefing payload.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - There is no `fast-io` and no prefix-`list`. The unified stream + tracked
#    outbound are SINGLE-KEY INDEX ARRAYS that message-inbound / message-outbound
#    maintain (thread/stream, thread/outbound-tracker). This briefing is a read-only
#    consumer of those plus the nightly preference model (thread/preferences) and
#    read-only config (triage-weights, relationship-tiers). It only WRITES
#    thread/notifications + thread/audit.
#  - Calendar context comes from the real `gog` CLI (gateway-free), not openclaw.invoke.
#  - The triage scorer needs the user's timezone for its "today" date-word match —
#    container clock is UTC, so we pass an explicit TODAY/TODAY_WORDS in the user's TZ.
#  - Scripts always exit 0 and carry status in JSON; the pipeline branches via `condition:`.

args:
  user_tz:
    required: false
    default: America/New_York   # falls back to USER.md timezone
  briefing_style:
    required: false
    default: with_context       # names_only | with_context | detailed
  briefing_channel:
    required: false
    default: "#thread-inbox"

steps:
  # 1. Load the unified message stream (single-key index array, maintained by
  #    message-inbound). Read-only here.
  - id: stream
    pipeline: state.get thread/stream

  # 2. Load tracked outbound (single-key index array, maintained by
  #    message-outbound under thread/outbound-tracker). Read-only here — feeds
  #    the follow-up tracker.
  - id: outbound
    pipeline: state.get thread/outbound-tracker

  # 3. Read-only config the user/agent maintains: triage weights, tier table,
  #    per-contact channel preferences, per-contact custom follow-up thresholds.
  - id: weights
    pipeline: state.get thread/config/triage-weights

  - id: tiers
    pipeline: state.get thread/config/relationship-tiers

  # Per-contact channel-preference model (produced nightly under thread/preferences).
  - id: prefs
    pipeline: state.get thread/preferences

  - id: thresholds
    pipeline: state.get thread/config/followup-thresholds

  # 4. Compute "today" in the USER's timezone (container clock is UTC). The
  #    triage scorer matches today-date words case-insensitively, so emit both
  #    the ISO date and the long-form weekday/month/day.
  - id: clock
    command: >
      node -e 'const tz=process.env.USER_TZ||"America/New_York";
        const now=new Date();
        const iso=new Intl.DateTimeFormat("en-CA",{timeZone:tz,year:"numeric",month:"2-digit",day:"2-digit"}).format(now);
        const words=new Intl.DateTimeFormat("en-US",{timeZone:tz,weekday:"long",month:"long",day:"numeric"}).format(now);
        process.stdout.write(JSON.stringify({date:iso, date_words:words, tz}));'
    env:
      USER_TZ: ${user_tz}

  # 5. Today's calendar events (time-sensitivity context) via the real gog CLI.
  #    Gateway-free. Tolerant: if gog/calendar isn't connected, emit an empty
  #    list + a connect notice rather than failing the run.
  - id: calendar
    command: >
      node -e 'const {execSync}=require("child_process");
        const date=(JSON.parse(process.env.CLOCK||"{}").date)||"";
        let out=""; let connected=true;
        try { out=execSync("gog calendar events list --date "+JSON.stringify(date)+" --all-calendars --json",
          {encoding:"utf8",timeout:45000}); }
        catch(e){ connected=false; out=""; }
        let events=[]; try { const p=JSON.parse(out); events=Array.isArray(p)?p:(p.events||p.items||[]); } catch(_) { events=[]; }
        const today=events.map(e=>({title:e.summary||e.title||"",
          start:(e.start&&(e.start.dateTime||e.start.date))||e.start||"",
          attendees:(e.attendees||[]).map(a=>a.email||a).filter(Boolean)}));
        process.stdout.write(JSON.stringify({connected, count:today.length, events:today}));'
    env:
      CLOCK: $clock.stdout

  # 6. Triage-score the whole stream (deterministic scorer). Inject TODAY so the
  #    scorer's date-word match runs in the user's TZ, and stamp each message
  #    with calendar_match_today when a today-meeting attendee matches its sender.
  - id: triage-input
    command: >
      node -e 'const stream=JSON.parse(process.env.STREAM||"null")||[];
        const cal=JSON.parse(process.env.CAL||"{}"); const clk=JSON.parse(process.env.CLOCK||"{}");
        const attendees=new Set();
        for (const e of (cal.events||[])) for (const a of (e.attendees||[])) attendees.add(String(a).toLowerCase());
        const messages=stream.map(m=>({...m,
          calendar_match_today: !!(m.sender && attendees.has(String(m.sender).toLowerCase()))}));
        const out={messages,
          weights: JSON.parse(process.env.WEIGHTS||"null")||undefined,
          relationship_tiers: JSON.parse(process.env.TIERS||"null")||undefined,
          today_override: clk.date, today_words_override: clk.date_words};
        process.stdout.write(JSON.stringify(out));'
    env:
      STREAM: $stream.stdout
      CAL: $calendar.stdout
      CLOCK: $clock.stdout
      WEIGHTS: $weights.stdout
      TIERS: $tiers.stdout

  - id: triage
    command: node thread-triage-scorer.js
    stdin: $triage-input.stdout

  # 7. Follow-up tracker over tracked outbound vs the inbound stream — finds the
  #    contacts waiting longest. Stream items double as the inbound corpus.
  - id: followup-input
    command: >
      node -e 'const outbound=JSON.parse(process.env.OUTBOUND||"null")||[];
        const stream=JSON.parse(process.env.STREAM||"null")||[];
        const inbound=stream.filter(m=>(m.direction||"inbound")==="inbound");
        process.stdout.write(JSON.stringify({
          outbound_messages: outbound,
          inbound_messages: inbound,
          contact_preferences: JSON.parse(process.env.PREFS||"null")||{},
          custom_thresholds: JSON.parse(process.env.THRESHOLDS||"null")||{}}));'
    env:
      OUTBOUND: $outbound.stdout
      STREAM: $stream.stdout
      PREFS: $prefs.stdout
      THRESHOLDS: $thresholds.stdout

  - id: followup
    command: node thread-followup-tracker.js
    stdin: $followup-input.stdout

  # 8. Top 3 contacts waiting longest (deterministic sort + slice). Emits an
  #    explicit count so the next step can gate on it (never gate on .length).
  - id: waiting-longest
    command: >
      node -e 'const f=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const top3=(f.overdue||[]).slice().sort((a,b)=>b.days_waiting-a.days_waiting).slice(0,3)
          .map(x=>({contact:x.contact, channel:x.channel, days_waiting:x.days_waiting,
            subject:x.subject||"", suggestion:x.suggestion||""}));
        process.stdout.write(JSON.stringify({top3, count:top3.length}));'
    stdin: $followup.stdout

  # 9. Assemble the deterministic briefing payload. Explicit counts everywhere so
  #    downstream gates use $x.json.count, not .length.
  - id: briefing-payload
    command: >
      node -e 'const t=JSON.parse(process.env.TRIAGE||"{}");
        const w=JSON.parse(process.env.WAITING||"{}");
        const cal=JSON.parse(process.env.CAL||"{}"); const clk=JSON.parse(process.env.CLOCK||"{}");
        const f=JSON.parse(process.env.FOLLOWUP||"{}");
        const topUrgent=(t.top_3||[]).slice(0,2);
        const payload={
          date: clk.date, tz: clk.tz, style: process.env.STYLE||"with_context",
          waiting_longest: w.top3||[], waiting_count: (w.top3||[]).length,
          top_urgent: topUrgent, top_urgent_count: topUrgent.length,
          time_sensitive_count: t.time_sensitive_count||0,
          todays_meetings: cal.events||[], meeting_count: cal.count||0,
          calendar_connected: cal.connected!==false,
          overdue_followups: f.overdue_count||0,
          stream_size: t.total||0};
        const action_count=(payload.waiting_count)+(payload.top_urgent_count)+(payload.meeting_count);
        process.stdout.write(JSON.stringify({...payload, action_count}));'
    env:
      TRIAGE: $triage.stdout
      WAITING: $waiting-longest.stdout
      CAL: $calendar.stdout
      CLOCK: $clock.stdout
      FOLLOWUP: $followup.stdout
      STYLE: ${briefing_style}

  # 10. Queue the briefing as a notification intent for the agent loop. The LLM
  #     narrative + Slack delivery are the agent loop's job (both 401 here): this
  #     hands it a fully-assembled, deterministic payload to narrate and post.
  #     Append to the single-key thread/notifications index array.
  - id: notify-load
    pipeline: state.get thread/notifications

  - id: notify-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const b=JSON.parse(process.env.BRIEFING||"{}");
        n.push({kind:"morning_briefing", channel:process.env.CHANNEL||"#thread-inbox",
          needs_llm:true, style:b.style,
          payload:b, created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      BRIEFING: $briefing-payload.stdout
      CHANNEL: ${briefing_channel}

  - id: notify
    pipeline: state.set thread/notifications
    stdin: $notify-next.stdout

  # 11. If calendar isn't connected, queue a one-time "connect Google Calendar"
  #     notice (click-to-connect pattern) so the briefing degrades gracefully.
  - id: connect-notice
    condition: "!$calendar.json.connected"
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"connect_notice", channel:process.env.CHANNEL||"#thread-inbox",
          service:"google_calendar",
          text:"Calendar context is unavailable for your morning briefing. Connect Google Calendar (via gog) in your settings to include todays meetings.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-next.stdout
      CHANNEL: ${briefing_channel}

  - id: connect-notice-write
    condition: "!$calendar.json.connected"
    pipeline: state.set thread/notifications
    stdin: $connect-notice.stdout

  # 12. Audit log (single-key index array; 12-month retention is the agent's job).
  - id: audit-load
    pipeline: state.get thread/audit

  - id: audit-next
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const b=JSON.parse(process.env.BRIEFING||"{}");
        a.push({action:"morning_briefing", date:b.date,
          stream_size:b.stream_size, overdue_followups:b.overdue_followups,
          time_sensitive:b.time_sensitive_count, action_count:b.action_count,
          calendar_connected:b.calendar_connected,
          authorized_by:"autonomous", timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(a));'
    env:
      AUDIT: $audit-load.stdout
      BRIEFING: $briefing-payload.stdout

  - id: audit
    pipeline: state.set thread/audit
    stdin: $audit-next.stdout
