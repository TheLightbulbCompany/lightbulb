name: nightly-maintenance
# Nightly housekeeping for Thread (Req 13, 14, 21, 22, 27): rebuild the per-contact
# channel-preference model from response latency, flag overdue follow-ups, and queue
# the weekly triage-calibration request for the agent loop.
#
# Fully deterministic (0 LLM in the pipeline). Runs nightly on cron.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via native state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    `fast-io` is fictional. The message stream, tracked outbound, contacts, and the
#    preferences model are SINGLE-KEY INDEX ARRAYS/objects (state.* has no prefix-list).
#  - The two analysis scripts are FLAT in the agent dir (node thread-*.js), starved of
#    stdin — this pipeline assembles their exact input shape and feeds it.
#  - Slack/llm-task/capability-evolver are unreachable here (401 / fictional). The
#    follow-up suggestions and the weekly triage-calibration request are written to
#    state queues for the agent loop to deliver/judge.
#  - Scripts exit 0 and carry status in JSON; steps branch via `condition:`. We gate on
#    explicit counts, never `.length`.
#  - Day-of-week is computed in the user's timezone (America/New_York), not container-UTC.

steps:
  # ---------------------------------------------------------------------------
  # LOAD durable state (read-before-use). All four are index arrays/objects that
  # message-inbound / message-outbound maintain; preferences is this run's own
  # producer key, read back so the model compounds across nights.
  # ---------------------------------------------------------------------------

  # Per-contact records, each carrying its own history[] (consumer of by-contact rows).
  - id: contacts
    pipeline: state.get thread/contacts

  # Unified message stream (inbound + outbound events) — the latency signal source.
  - id: stream
    pipeline: state.get thread/stream

  # Tracked outbound still awaiting a response (follow-up candidates);
  # produced by message-outbound under thread/outbound-tracker.
  - id: outbound
    pipeline: state.get thread/outbound-tracker

  # Existing preference model (compounds nightly).
  - id: prefs-existing
    pipeline: state.get thread/preferences

  # Per-contact custom follow-up thresholds the user has set (read-only config).
  - id: thresholds
    pipeline: state.get thread/config/followup-thresholds

  # ---------------------------------------------------------------------------
  # PREFERENCE LEARNING (Req 13, 14) — feed thread-preference-learner.js its exact
  # input shape {contacts, message_history, existing_preferences}.
  # ---------------------------------------------------------------------------

  # Assemble learner input. contacts/prefs default to {} (objects), stream to [] (array).
  - id: learn-input
    command: >
      node -e 'const contactsArr=JSON.parse(process.env.CONTACTS||"null")||[];
        const contacts={}; for (const c of contactsArr) { if (c && c.id) contacts[c.id]=c; }
        const history=JSON.parse(process.env.STREAM||"null")||[];
        const existing=JSON.parse(process.env.PREFS||"null")||{};
        process.stdout.write(JSON.stringify({contacts, message_history:history, existing_preferences:existing}));'
    env:
      CONTACTS: $contacts.stdout
      STREAM: $stream.stdout
      PREFS: $prefs-existing.stdout

  # Run the deterministic learner (exits 0; emits {preferences, contacts_updated, ...}).
  - id: learn-preferences
    command: node thread-preference-learner.js
    stdin: $learn-input.stdout

  # Merge the freshly-learned per-contact preferences over the existing model so a
  # quiet contact (no new pairs tonight) keeps its prior model instead of vanishing.
  - id: prefs-merged
    command: >
      node -e 'const prev=JSON.parse(process.env.PREV||"null")||{};
        const learned=(JSON.parse(process.env.LEARNED||"{}").preferences)||{};
        const merged={...prev, ...learned};
        process.stdout.write(JSON.stringify(merged));'
    env:
      PREV: $prefs-existing.stdout
      LEARNED: $learn-preferences.stdout

  # Persist the merged model back to the single producer key.
  - id: store-preferences
    pipeline: state.set thread/preferences
    stdin: $prefs-merged.stdout

  # ---------------------------------------------------------------------------
  # FOLLOW-UP CHECK (Req 21, 22) — feed thread-followup-tracker.js
  # {outbound_messages, inbound_messages, contact_preferences, custom_thresholds}.
  # outbound = tracked rows awaiting_response; inbound = stream inbound events.
  # ---------------------------------------------------------------------------

  - id: followup-input
    command: >
      node -e 'const out=(JSON.parse(process.env.OUTBOUND||"null")||[]).filter(m=>m&&m.awaiting_response!==false);
        const stream=JSON.parse(process.env.STREAM||"null")||[];
        const inbound=stream.filter(m=>m&&m.direction==="inbound");
        const prefs=JSON.parse(process.env.PREFS||"null")||{};
        const custom=JSON.parse(process.env.THRESHOLDS||"null")||{};
        process.stdout.write(JSON.stringify({outbound_messages:out, inbound_messages:inbound, contact_preferences:prefs, custom_thresholds:custom}));'
    env:
      OUTBOUND: $outbound.stdout
      STREAM: $stream.stdout
      PREFS: $prefs-merged.stdout
      THRESHOLDS: $thresholds.stdout

  # Run the deterministic tracker (exits 0; emits {overdue, overdue_count, ...}).
  - id: check-followups
    command: node thread-followup-tracker.js
    stdin: $followup-input.stdout

  # ---------------------------------------------------------------------------
  # SURFACE overdue follow-ups (Req 22). Slack is unreachable here, so enqueue a
  # notification for the agent loop to deliver. Gate on an EXPLICIT count, never
  # `.length`. The producing tracker already emits overdue_count.
  # ---------------------------------------------------------------------------

  - id: notify-load
    condition: $check-followups.json.overdue_count > 0
    pipeline: state.get thread/notifications

  - id: notify-next
    condition: $check-followups.json.overdue_count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const f=JSON.parse(process.env.FOLLOWUPS||"{}");
        n.push({kind:"followup_suggestions", deliver_to:"#thread-inbox",
          overdue_count:f.overdue_count||0,
          channel_switch_suggestions:f.channel_switch_suggestions||0,
          overdue:(f.overdue||[]).map(o=>({contact:o.contact, channel:o.channel,
            days_waiting:o.days_waiting, suggestion:o.suggestion,
            alternative_channel:o.alternative_channel||null})),
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      FOLLOWUPS: $check-followups.stdout

  - id: notify
    condition: $check-followups.json.overdue_count > 0
    pipeline: state.set thread/notifications
    stdin: $notify-next.stdout

  # ---------------------------------------------------------------------------
  # WEEKLY TRIAGE CALIBRATION (Req 27). capability-evolver + the LLM judgment are
  # not reachable in an embedded pipeline. Compute "is it Sunday in the user's TZ"
  # (NOT container-UTC) and, when so, enqueue a calibration REQUEST carrying the raw
  # signal keys for the agent loop to analyze. We never call llm-task here.
  # ---------------------------------------------------------------------------

  # Day-of-week in America/New_York. Emit an explicit flag to gate on.
  - id: dow
    command: >
      node -e 'const wd=new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",weekday:"short"}).format(new Date());
        process.stdout.write(JSON.stringify({weekday:wd, is_sunday: wd==="Sun"}));'

  # Load the raw calibration-signal index arrays the agent loop will reason over.
  - id: cal-overrides
    condition: $dow.json.is_sunday
    pipeline: state.get thread/state/triage-overrides

  - id: cal-routing
    condition: $dow.json.is_sunday
    pipeline: state.get thread/state/routing-outcomes

  - id: cal-briefing
    condition: $dow.json.is_sunday
    pipeline: state.get thread/state/briefing-actions

  - id: cal-dismissals
    condition: $dow.json.is_sunday
    pipeline: state.get thread/state/safe-senders

  # Existing calibration-request queue.
  - id: cal-queue
    condition: $dow.json.is_sunday
    pipeline: state.get thread/calibration-requests

  # Append a calibration request with the gathered signals. The agent loop dequeues
  # this, runs the model judgment, and writes back adjusted thread/config/triage-weights.
  - id: cal-next
    condition: $dow.json.is_sunday
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const parse=(s)=>JSON.parse(s||"null");
        q.push({kind:"triage_calibration",
          focus:"Which triage weight adjustments would better match the users actual priorities? Which channel routing predictions were wrong? Which briefing items did the user act on vs defer? Which injection flags were false positives?",
          signals:{
            triage_overrides:parse(process.env.OVERRIDES),
            routing_outcomes:parse(process.env.ROUTING),
            briefing_actions:parse(process.env.BRIEFING),
            safe_senders:parse(process.env.DISMISSALS)
          },
          requested_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: $cal-queue.stdout
      OVERRIDES: $cal-overrides.stdout
      ROUTING: $cal-routing.stdout
      BRIEFING: $cal-briefing.stdout
      DISMISSALS: $cal-dismissals.stdout

  - id: cal-enqueue
    condition: $dow.json.is_sunday
    pipeline: state.set thread/calibration-requests
    stdin: $cal-next.stdout

  # ---------------------------------------------------------------------------
  # AUDIT LOG (Req 11). Append a run record to the audit index array. Date stamped
  # in the user's timezone so nightly rows don't straddle UTC midnight.
  # ---------------------------------------------------------------------------

  - id: audit-load
    pipeline: state.get thread/audit

  - id: audit-next
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const learn=JSON.parse(process.env.LEARN||"{}");
        const follow=JSON.parse(process.env.FOLLOW||"{}");
        const dow=JSON.parse(process.env.DOW||"{}");
        const date=new Intl.DateTimeFormat("en-CA",{timeZone:"America/New_York"}).format(new Date());
        a.push({action:"nightly_maintenance", date,
          preferences_updated:learn.contacts_updated||0,
          contacts_with_sufficient_data:learn.contacts_with_sufficient_data||0,
          followups_overdue:follow.overdue_count||0,
          channel_switch_suggestions:follow.channel_switch_suggestions||0,
          weekly_calibration_requested:!!dow.is_sunday,
          authorized_by:"autonomous",
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(a));'
    env:
      AUDIT: $audit-load.stdout
      LEARN: $learn-preferences.stdout
      FOLLOW: $check-followups.stdout
      DOW: $dow.stdout

  - id: audit
    pipeline: state.set thread/audit
    stdin: $audit-next.stdout
