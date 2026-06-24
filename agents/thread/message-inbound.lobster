name: message-inbound
# Process one incoming message from any channel into Thread's unified stream.
# Triggered per inbound message (Gmail Pub/Sub push, Slack event, etc.) via /hooks/agent.
# Fully deterministic — 0 LLM, 0 gateway calls. Intent: Requirements 1-4, 7-11, 19, 26.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node) under `command:`.
#  - Durable state via native state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    `fast-io` is FICTIONAL — every thread-* key is now a single state.* key, namespaced
#    `thread/...`. The unified stream + per-contact history + audit log are single-key INDEX
#    ARRAYS (state.* has no prefix-list): state.get -> a node step appends -> state.set.
#  - Config the user sets (relationship tiers, triage weights, safe senders) is read-only
#    consumer state under `thread/config/*` and `thread/state/*`.
#  - LLM judgment + Slack/Teams delivery CANNOT run in an embedded pipeline (openclaw.invoke,
#    slack, summarize, llm-task all 401). So: injection alerts + preview compression + any
#    notification are written to a `thread/notifications` queue for the agent loop to deliver.
#  - email-security / capability-evolver / taskr are skill-layer tools, not pipeline-callable;
#    their work is deferred to the agent loop (see notes at the relevant steps).
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  channel:
    required: true
  sender:
    required: true
  content:
    required: true
  message_id:
    required: true
  timestamp:
    required: false        # ISO8601 arrival time; defaults to run time if absent

steps:
  # ---------------------------------------------------------------------------
  # Step 1: Sanitize message content (Req 8) — strip HTML/zero-width/base64/etc.
  #   Runs on EVERY message from EVERY channel before the AI layer sees it.
  #   email-security (the extra email-only sanitization layer) is a skill, not
  #   pipeline-callable — its deep scan is deferred to the agent loop, which reads
  #   the `email_pending_security` flag emitted by the assemble step below.
  - id: sanitize
    command: node thread-message-sanitizer.js
    stdin: '{"content": "${content}", "channel": "${channel}"}'

  # ---------------------------------------------------------------------------
  # Step 2: Load the injection-detector safe-sender list (consumer config).
  - id: safe-senders
    pipeline: state.get thread/state/safe-senders

  # Step 3: Assemble the injection-detector input (sanitized text + sender + safe list).
  - id: injection-input
    command: >
      node -e 'const s=JSON.parse(process.env.SAN||"{}");
        const safe=JSON.parse(process.env.SAFE||"null");
        process.stdout.write(JSON.stringify({
          text: s.sanitized_text||"",
          sender_email: process.env.LOBSTER_ARG_SENDER||"",
          safe_senders: Array.isArray(safe)?safe:(safe&&safe.list)||[]
        }));'
    env:
      SAN: $sanitize.stdout
      SAFE: $safe-senders.stdout

  # Step 4: Injection detection (Req 9). Script carries `flagged` in JSON; we branch
  #   on it — never on the script's exit code. (See .js fix: the script currently
  #   process.exit(1)s when flagged, which under on_error:stop would abort the run.)
  - id: injection-check
    command: node thread-injection-detector.js
    stdin: $injection-input.stdout
    on_error: continue

  # ---------------------------------------------------------------------------
  # Step 5: Resolve contact identity from the per-owner contact index.
  #   Replaces fast-io read of `thread-contacts/by-address/<sender>`.
  - id: contacts
    pipeline: state.get thread/contacts/by-address

  - id: resolve-contact
    command: >
      node -e 'const idx=JSON.parse(process.env.IDX||"null")||{};
        const sender=(process.env.LOBSTER_ARG_SENDER||"").toLowerCase();
        const c=idx[sender]||{};
        process.stdout.write(JSON.stringify({
          id: c.id||"",
          name: c.name||"",
          relationship_tier: c.relationship_tier||"known_contact",
          has_pending_outbound: !!c.has_pending_outbound,
          found: !!c.id
        }));'
    env:
      IDX: $contacts.stdout

  # Step 6: Load relationship-tier definitions + triage weights (consumer config;
  #   both feed the triage scorer). Absent → scorer falls back to its baked defaults.
  - id: load-tiers
    pipeline: state.get thread/config/relationship-tiers

  - id: triage-weights
    pipeline: state.get thread/config/triage-weights

  # ---------------------------------------------------------------------------
  # Step 7: Calendar time-sensitivity context (Req 26) via the REAL gog CLI.
  #   Best-effort: if gog isn't connected the step still emits has_meeting_today:false
  #   so triage degrades gracefully (the script exits 0 regardless).
  - id: calendar-context
    command: >
      node -e 'const {execSync}=require("child_process");
        const sender=process.env.LOBSTER_ARG_SENDER||"";
        const today=new Date().toISOString().slice(0,10);
        let has=false;
        try {
          const out=execSync("gog calendar events list --date "+JSON.stringify(today)+" --json",
            {encoding:"utf8",timeout:30000});
          const ev=JSON.parse(out); const items=Array.isArray(ev)?ev:(ev.events||ev.items||[]);
          has=items.some(e=>JSON.stringify(e).toLowerCase().includes(sender.toLowerCase()));
        } catch(e){ has=false; }
        process.stdout.write(JSON.stringify({has_meeting_today:has, checked_date:today}));'
    on_error: continue

  # ---------------------------------------------------------------------------
  # Step 8: Triage score (Req 3, 4, 26). Wrap this single message as messages[1]
  #   for the scorer, with the loaded weights/tiers folded in.
  - id: triage-input
    command: >
      node -e 'const san=JSON.parse(process.env.SAN||"{}");
        const rc=JSON.parse(process.env.RC||"{}");
        const cal=JSON.parse(process.env.CAL||"{}");
        const tiers=JSON.parse(process.env.TIERS||"null");
        const weights=JSON.parse(process.env.WEIGHTS||"null");
        const ts=process.env.LOBSTER_ARG_TIMESTAMP||new Date().toISOString();
        const out={messages:[{
          id: process.env.LOBSTER_ARG_MESSAGE_ID||"",
          sender: process.env.LOBSTER_ARG_SENDER||"",
          channel: process.env.LOBSTER_ARG_CHANNEL||"",
          timestamp: ts,
          preview: san.sanitized_text||"",
          sender_tier: rc.relationship_tier||"known_contact",
          is_reply_to_user: !!rc.has_pending_outbound,
          calendar_match_today: !!cal.has_meeting_today
        }]};
        if (tiers) out.relationship_tiers=tiers;
        if (weights) out.weights=weights;
        process.stdout.write(JSON.stringify(out));'
    env:
      SAN: $sanitize.stdout
      RC: $resolve-contact.stdout
      CAL: $calendar-context.stdout
      TIERS: $load-tiers.stdout
      WEIGHTS: $triage-weights.stdout

  - id: triage
    command: node thread-triage-scorer.js
    stdin: $triage-input.stdout

  # Step 8b: Flatten the scorer's ranked[0] into a single record (no bracket-index
  #   refs allowed downstream; we expose scalar fields the stream/audit steps consume).
  - id: triage-flat
    command: >
      node -e 'const t=JSON.parse(process.env.T||"{}");
        const top=(t.ranked&&t.ranked[0])||{};
        process.stdout.write(JSON.stringify({
          final_score: top.final_score||0,
          composite_score: top.composite_score||0,
          time_sensitivity_boost: top.time_sensitivity_boost||0,
          breakdown: top.breakdown||{}
        }));'
    env:
      T: $triage.stdout

  # ---------------------------------------------------------------------------
  # Step 9: Store the message in the unified stream (Req 1, 2).
  #   The stream is a single-key index array — load, append (bounded), re-write.
  #   Preview compression (summarize) is a skill, not pipeline-callable; we store
  #   the sanitized text and set needs_preview_compression for the agent loop.
  - id: stream-load
    pipeline: state.get thread/stream

  - id: stream-next
    command: >
      node -e 'const stream=JSON.parse(process.env.STREAM||"null")||[];
        const san=JSON.parse(process.env.SAN||"{}");
        const rc=JSON.parse(process.env.RC||"{}");
        const tf=JSON.parse(process.env.TF||"{}");
        const inj=JSON.parse(process.env.INJ||"{}");
        const ts=process.env.LOBSTER_ARG_TIMESTAMP||new Date().toISOString();
        const channel=process.env.LOBSTER_ARG_CHANNEL||"";
        const text=san.sanitized_text||"";
        stream.push({
          id: process.env.LOBSTER_ARG_MESSAGE_ID||"",
          sender: process.env.LOBSTER_ARG_SENDER||"",
          contact_id: rc.id||"",
          contact_name: rc.name||"",
          channel,
          channel_label: channel+" · "+(rc.name||process.env.LOBSTER_ARG_SENDER||""),
          preview: text.length>200? text.slice(0,200)+"…" : text,
          full_text: text,
          needs_preview_compression: text.length>200,
          triage_score: tf.final_score||0,
          triage_breakdown: tf.breakdown||{},
          relationship_tier: rc.relationship_tier||"known_contact",
          injection_flagged: !!inj.flagged,
          sanitization_notes: san.elements_stripped||[],
          timestamp: ts,
          status: "unread"
        });
        const MAX=2000;
        const trimmed=stream.length>MAX? stream.slice(stream.length-MAX) : stream;
        process.stdout.write(JSON.stringify(trimmed));'
    env:
      STREAM: $stream-load.stdout
      SAN: $sanitize.stdout
      RC: $resolve-contact.stdout
      TF: $triage-flat.stdout
      INJ: $injection-check.stdout

  - id: stream-write
    pipeline: state.set thread/stream
    stdin: $stream-next.stdout

  # ---------------------------------------------------------------------------
  # Step 10: Update the per-contact relationship thread (Req 19), keyed by contact id.
  #   The key is DYNAMIC (`thread/contacts/history/<id>`), which the native
  #   pipeline state.get/set verbs can't template — so this single step reads and
  #   writes the backing state file directly at $LOBSTER_STATE_DIR/<key>.json
  #   (documented path; key lowercased, [^a-z0-9._-]→_). Skipped when the contact
  #   is unresolved (no id to key on) — the agent loop backfills on resolve.
  - id: history-write
    condition: $resolve-contact.json.found
    command: >
      node -e 'const fs=require("fs"), path=require("path");
        const dir=process.env.LOBSTER_STATE_DIR||(process.env.HOME+"/.lobster/state");
        const id=(process.env.CID||"").toLowerCase().replace(/[^a-z0-9._-]/g,"_");
        const key="thread/contacts/history/"+id;
        const safe=key.toLowerCase().replace(/[^a-z0-9._\/-]/g,"_");
        const file=path.join(dir, safe+".json");
        let h=[]; try { h=JSON.parse(fs.readFileSync(file,"utf8"))||[]; } catch(e){ h=[]; }
        if(!Array.isArray(h)) h=[];
        const san=JSON.parse(process.env.SAN||"{}");
        const ts=process.env.LOBSTER_ARG_TIMESTAMP||new Date().toISOString();
        const text=san.sanitized_text||"";
        h.push({timestamp:ts, channel:process.env.LOBSTER_ARG_CHANNEL||"",
          direction:"inbound", preview: text.length>200? text.slice(0,200)+"…":text,
          message_id:process.env.LOBSTER_ARG_MESSAGE_ID||""});
        const MAX=500; if(h.length>MAX) h=h.slice(h.length-MAX);
        try { fs.mkdirSync(path.dirname(file),{recursive:true});
          fs.writeFileSync(file, JSON.stringify(h)); } catch(e){}
        process.stdout.write(JSON.stringify({written:true, key, count:h.length}));'
    env:
      CID: $resolve-contact.json.id
      SAN: $sanitize.stdout
    on_error: continue

  # ---------------------------------------------------------------------------
  # Step 11: Audit log (Req 11) — append to a single-key audit index array.
  #   Replaces the per-key `thread-audit/<date>/inbound/<id>` fast-io writes.
  - id: audit-load
    pipeline: state.get thread/audit

  - id: audit-next
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const san=JSON.parse(process.env.SAN||"{}");
        const tf=JSON.parse(process.env.TF||"{}");
        const inj=JSON.parse(process.env.INJ||"{}");
        const ts=process.env.LOBSTER_ARG_TIMESTAMP||new Date().toISOString();
        a.push({
          action: "message_received",
          date: ts.slice(0,10),
          sender: process.env.LOBSTER_ARG_SENDER||"",
          channel: process.env.LOBSTER_ARG_CHANNEL||"",
          message_id: process.env.LOBSTER_ARG_MESSAGE_ID||"",
          triage_score: tf.final_score||0,
          injection_flagged: !!inj.flagged,
          sanitized: !!san.had_suspicious_content,
          authorized_by: "autonomous",
          timestamp: ts
        });
        const MAX=5000;
        process.stdout.write(JSON.stringify(a.length>MAX? a.slice(a.length-MAX):a));'
    env:
      AUDIT: $audit-load.stdout
      SAN: $sanitize.stdout
      TF: $triage-flat.stdout
      INJ: $injection-check.stdout

  - id: audit-write
    pipeline: state.set thread/audit
    stdin: $audit-next.stdout

  # ---------------------------------------------------------------------------
  # Step 12: Queue a notification for the agent loop to DELIVER (Slack is 401 here).
  #   Covers (a) injection alerts (Req — surface flagged messages to the user) and
  #   (b) deferred work: email deep-security scan, preview compression. The agent
  #   loop drains thread/notifications and posts to the configured Slack surface.
  - id: notify-load
    pipeline: state.get thread/notifications

  - id: notify-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const inj=JSON.parse(process.env.INJ||"{}");
        const san=JSON.parse(process.env.SAN||"{}");
        const rc=JSON.parse(process.env.RC||"{}");
        const channel=process.env.LOBSTER_ARG_CHANNEL||"";
        const sender=process.env.LOBSTER_ARG_SENDER||"";
        const ts=process.env.LOBSTER_ARG_TIMESTAMP||new Date().toISOString();
        const mid=process.env.LOBSTER_ARG_MESSAGE_ID||"";
        if (inj.flagged) {
          n.push({kind:"injection_alert", urgent:true, message_id:mid,
            sender, channel, severity:inj.max_severity||"medium",
            detail:inj.message||"Possible prompt-injection content detected.",
            created_at:ts});
        }
        if (channel==="email") {
          n.push({kind:"email_security_scan", message_id:mid, sender,
            note:"Run email-security deep scan in the agent loop before surfacing.",
            created_at:ts});
        }
        if ((san.sanitized_text||"").length>200) {
          n.push({kind:"compress_preview", message_id:mid, sender, channel,
            note:"Summarize for stream preview (>200 chars).", created_at:ts});
        }
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      INJ: $injection-check.stdout
      SAN: $sanitize.stdout
      RC: $resolve-contact.stdout

  - id: notify-write
    pipeline: state.set thread/notifications
    stdin: $notify-next.stdout
