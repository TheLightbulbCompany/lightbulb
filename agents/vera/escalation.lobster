name: escalation
# Full escalation flow with full-context handoff for a human agent.
# Triggered from intake-resolve when escalation conditions are met (Req 26,29,30,31,42,50,59).
#
# Rebuilt for the embedded OpenClaw runner (gateway-free, deterministic). Contract notes
# (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs (node/curl) under `command:`.
#  - There is NO fast-io / taskr — durable state is native state.get/set on the per-agent EFS
#    workspace (LOBSTER_STATE_DIR). Per-conversation records live under one key each; cross-run
#    queues (callbacks, notifications) are single-key INDEX ARRAYS, appended in a node step then
#    re-written (state.* has no prefix-list).
#  - Helpdesk customer lookup is REAL: Zendesk / Intercom via `curl` with their env creds; if no
#    helpdesk is connected, customer data comes from state (standalone mode).
#  - LLM judgment (suggested resolution, out-of-hours wording) and Slack/Twilio/customer-message
#    DELIVERY cannot run in an embedded pipeline (openclaw.invoke/slack/llm-task/message all 401).
#    The pipeline assembles a complete, deterministic escalation package and writes the
#    LLM-needed + delivery work to a `vera/notifications` queue for the agent loop to handle.
#    FICTIONAL integrations dropped: telcall-twilio (voice), summarize-as-a-tool on a JSON blob,
#    llm-task, slack, message — all routed to notifications or done deterministically in node.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.
#  - Business-hours check uses the user's configured timezone (NOT container UTC getHours()).

args:
  conversation_id:
    required: true
  customer_email:
    required: true
  escalation_trigger:
    required: true
  customer_message:
    default: ""

steps:
  # 1. Load the full conversation record (transcript + sentiment trail) from durable state.
  #    Producer: intake-resolve writes vera/conversations/${conversation_id}.
  - id: load-conversation
    pipeline: state.get vera/conversations/${conversation_id}

  # 2. Load ticket attempts (what Vera already tried) from durable state.
  #    Agent-loop-owned record; namespaced vera/* to match the agent-owned convention.
  - id: load-attempts
    pipeline: state.get vera/tickets/${conversation_id}

  # 3. Load CSAT history for this customer (single-key index array of {customer_email,score,date}).
  #    Agent-loop-owned record; namespaced vera/* to match the agent-owned convention.
  - id: load-csat
    pipeline: state.get vera/csat-index

  # 4. Load the connected-helpdesk config (click-to-connect). null platform => standalone mode.
  - id: load-helpdesk-config
    pipeline: state.get vera-config/connected-helpdesk

  # 5. Load business-hours config (staffed days/hours + timezone + out-of-hours behavior).
  - id: load-hours-config
    pipeline: state.get vera-config/business-hours

  # 6. Customer lookup — REAL helpdesk APIs via curl, gated on the connected platform.
  #    Each exits 0; on failure curl yields empty/err which the normalizer tolerates.
  - id: load-customer-zendesk
    condition: $load-helpdesk-config.json.platform == "zendesk"
    command: >
      curl -sS -u "$ZENDESK_EMAIL/token:$ZENDESK_API_TOKEN"
      "https://$ZENDESK_SUBDOMAIN.zendesk.com/api/v2/search.json?query=type:user+email:$LOBSTER_ARG_CUSTOMER_EMAIL&include=tickets,orders"
      || echo '{}'

  - id: load-customer-intercom
    condition: $load-helpdesk-config.json.platform == "intercom"
    command: >
      curl -sS -H "Authorization: Bearer $INTERCOM_ACCESS_TOKEN" -H "Accept: application/json"
      "https://api.intercom.io/contacts?email=$LOBSTER_ARG_CUSTOMER_EMAIL"
      || echo '{}'

  # Standalone customer record from durable state (no helpdesk connected).
  #    Same key intake-resolve reads (vera/customers/...) so both hit one record.
  - id: load-customer-standalone
    condition: $load-helpdesk-config.json.platform == null
    pipeline: state.get vera/customers/${customer_email}

  # 7. Normalize customer data from whichever source responded. Reads via env (safe).
  - id: load-customer
    command: >
      node -e 'const zd=JSON.parse(process.env.ZD||"null");
        const ic=JSON.parse(process.env.IC||"null");
        const sa=JSON.parse(process.env.SA||"null");
        let c={};
        if (zd && Array.isArray(zd.results)) {
          const u=(zd.results||[])[0]||{};
          c={name:u.name||null, email:u.email||null, account_id:String(u.id||""),
             tickets:u.tickets||[], orders:u.orders||[], latest_order_id:u.latest_order_id||null,
             phone:u.phone||null, created_at:u.created_at||null, ltv:u.ltv||null};
        } else if (ic && Array.isArray(ic.data)) {
          const u=(ic.data||[])[0]||{};
          c={name:u.name||null, email:u.email||null, account_id:u.id||null,
             tickets:[], orders:[], latest_order_id:null, phone:u.phone||null,
             created_at:u.created_at||null, ltv:null};
        } else { c=sa||{}; }
        process.stdout.write(JSON.stringify(c));'
    env:
      ZD: $load-customer-zendesk.stdout
      IC: $load-customer-intercom.stdout
      SA: $load-customer-standalone.stdout

  # 8. Filter CSAT history to THIS customer + summarize a transcript (deterministic, zero LLM).
  #    summarize CLI operates on url/file, not a JSON blob — so compaction is done in node.
  - id: prep-context
    command: >
      node -e 'const all=JSON.parse(process.env.CSAT||"null")||[];
        const email=process.env.LOBSTER_ARG_CUSTOMER_EMAIL||"";
        const csat=all.filter(c=>c && c.customer_email===email);
        const conv=JSON.parse(process.env.CONV||"null")||{};
        const msgs=Array.isArray(conv.transcript)?conv.transcript:(Array.isArray(conv.messages)?conv.messages:[]);
        const exchanges=msgs.filter(m=>m && m.role==="customer").length || (conv.exchanges||0);
        const tail=msgs.slice(-8).map(m=>((m.role||"?")+": "+String(m.text||m.content||"")).slice(0,240));
        const transcript_summary=tail.join("\n") || (process.env.LOBSTER_ARG_CUSTOMER_MESSAGE||"");
        process.stdout.write(JSON.stringify({csat, exchanges, transcript_summary,
          sentiment: conv.sentiment || (Array.isArray(conv.sentiment_history)?conv.sentiment_history.slice(-1)[0]:{}) || {}}));'
    env:
      CSAT: $load-csat.stdout
      CONV: $load-conversation.stdout

  # 9. Assemble the builder's input contract, then run the deterministic escalation builder.
  - id: build-input
    command: >
      node -e 'const conv=JSON.parse(process.env.CONV||"null")||{};
        const cust=JSON.parse(process.env.CUST||"null")||{};
        const att=JSON.parse(process.env.ATT||"null")||{};
        const ctx=JSON.parse(process.env.CTX||"null")||{};
        const msgs=Array.isArray(conv.transcript)?conv.transcript:(Array.isArray(conv.messages)?conv.messages:[]);
        process.stdout.write(JSON.stringify({
          conversation: msgs,
          customer: cust,
          classification: att.classification || conv.classification || {},
          attempts: att.attempts || [],
          confidenceScores: att.confidence_scores || conv.confidence_scores || [],
          sentiment: ctx.sentiment || {},
          csatHistory: ctx.csat || [],
          previousTickets: cust.tickets || att.previous_tickets || [],
          escalationTrigger: process.env.LOBSTER_ARG_ESCALATION_TRIGGER || "unknown",
          ticket_id: att.ticket_id || conv.ticket_id || null,
          channel: conv.channel || att.channel || "unknown"
        }));'
    env:
      CONV: $load-conversation.stdout
      CUST: $load-customer.stdout
      ATT: $load-attempts.stdout
      CTX: $prep-context.stdout

  - id: build-package
    command: node vera-escalation-builder.js
    stdin: $build-input.stdout

  # 9b. Persist the escalation package under a per-conversation key so agent-assist can
  #     read it later (it loads vera/escalations/${conversation_id}). This is the producer
  #     for that consumer — the notifications queue carries the package too, but agent-assist
  #     reads a single keyed package object, not the mixed-kind queue array.
  - id: persist-escalation
    pipeline: state.set vera/escalations/${conversation_id}
    stdin: $build-package.stdout

  # 10. Business-hours check — uses the CONFIGURED TIMEZONE (not container UTC getHours()).
  #     Emits is_staffed + an explicit count so downstream gates are numeric.
  - id: check-hours
    command: >
      node -e 'const cfg=JSON.parse(process.env.HOURS||"null")||{};
        const tz=cfg.timezone||"UTC";
        const now=new Date();
        let hour=0, day=0;
        try {
          const parts=new Intl.DateTimeFormat("en-US",{timeZone:tz,hour:"numeric",hour12:false,weekday:"short"}).formatToParts(now);
          hour=parseInt((parts.find(p=>p.type==="hour")||{}).value||"0",10);
          const wd=(parts.find(p=>p.type==="weekday")||{}).value||"Sun";
          day=["Sun","Mon","Tue","Wed","Thu","Fri","Sat"].indexOf(wd);
        } catch(e) { hour=now.getUTCHours(); day=now.getUTCDay(); }
        const sh=cfg.staffed_hours||{};
        const start=parseInt(sh.start,10); const end=parseInt(sh.end,10);
        const days=Array.isArray(cfg.staffed_days)?cfg.staffed_days:[];
        const staffed = days.includes(day) && !isNaN(start) && !isNaN(end) && hour>=start && hour<end;
        process.stdout.write(JSON.stringify({
          is_staffed: !!staffed,
          staffed_count: staffed?1:0,
          timezone: tz,
          out_of_hours_behavior: cfg.out_of_hours_behavior || "queue_with_callback"
        }));'
    env:
      HOURS: $load-hours-config.stdout

  # 11. Build the escalation notification (Slack delivery happens in the agent loop — 401 here).
  #     During business hours: an escalation card for #vera-escalations. Out-of-hours: a flag so
  #     the agent loop generates the transparent out-of-hours customer message (LLM) + confirmation.
  - id: notify-load
    pipeline: state.get vera/notifications

  - id: notify-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const pkg=JSON.parse(process.env.PKG||"null")||{};
        const cust=JSON.parse(process.env.CUST||"null")||{};
        const ctx=JSON.parse(process.env.CTX||"null")||{};
        const hours=JSON.parse(process.env.HOURS||"null")||{};
        n.push({
          kind:"escalation",
          staffed: !!hours.is_staffed,
          urgent: pkg.priority==="urgent",
          needs_llm_suggested_resolution: true,
          needs_out_of_hours_message: !hours.is_staffed,
          deliver_to_slack_channel: "#vera-escalations",
          customer_email: process.env.LOBSTER_ARG_CUSTOMER_EMAIL,
          customer_name: cust.name || null,
          conversation_id: process.env.LOBSTER_ARG_CONVERSATION_ID,
          escalation_trigger: process.env.LOBSTER_ARG_ESCALATION_TRIGGER,
          ticket_id: pkg.ticket_id || null,
          priority: pkg.priority || "normal",
          sentiment: (pkg.sentiment||{}).current || null,
          transcript_summary: ctx.transcript_summary || "",
          escalation_package: pkg,
          out_of_hours_behavior: hours.out_of_hours_behavior || null,
          created_at: new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      PKG: $build-package.stdout
      CUST: $load-customer.stdout
      CTX: $prep-context.stdout
      HOURS: $check-hours.stdout

  - id: notify
    pipeline: state.set vera/notifications
    stdin: $notify-next.stdout

  # 12. Out-of-hours: queue a callback for the next staffed hour (single-key index array).
  - id: callback-load
    condition: $check-hours.json.staffed_count == 0
    pipeline: state.get vera/callbacks

  - id: callback-next
    condition: $check-hours.json.staffed_count == 0
    command: >
      node -e 'const cb=JSON.parse(process.env.CALLBACKS||"null")||[];
        const pkg=JSON.parse(process.env.PKG||"null")||{};
        cb.push({
          conversation_id: process.env.LOBSTER_ARG_CONVERSATION_ID,
          customer_email: process.env.LOBSTER_ARG_CUSTOMER_EMAIL,
          ticket_id: pkg.ticket_id || null,
          escalation_package: pkg,
          queued_at: new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(cb));'
    env:
      CALLBACKS: $callback-load.stdout
      PKG: $build-package.stdout

  - id: callback
    condition: $check-hours.json.staffed_count == 0
    pipeline: state.set vera/callbacks
    stdin: $callback-next.stdout

  # 13. Urgent + phone-on-file: voice escalation is a real-world side effect (telcall-twilio is
  #     fictional here). Gate it behind a HUMAN APPROVAL before queuing the voice-call intent, so
  #     no autonomous call fires. The agent loop performs the actual call after approval.
  - id: urgency-check
    command: >
      node -e 'const pkg=JSON.parse(process.env.PKG||"null")||{};
        const cust=JSON.parse(process.env.CUST||"null")||{};
        const wants = pkg.priority==="urgent" && !!cust.phone;
        process.stdout.write(JSON.stringify({voice_eligible: wants, voice_count: wants?1:0, phone: cust.phone||null}));'
    env:
      PKG: $build-package.stdout
      CUST: $load-customer.stdout

  - id: voice-approval
    condition: $urgency-check.json.voice_count > 0
    approval:
      prompt: "Place an urgent voice escalation call to the customer for ticket ${conversation_id}?"

  - id: voice-load
    condition: $urgency-check.json.voice_count > 0 && $voice-approval.approved
    pipeline: state.get vera/notifications

  - id: voice-next
    condition: $urgency-check.json.voice_count > 0 && $voice-approval.approved
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const pkg=JSON.parse(process.env.PKG||"null")||{};
        const uc=JSON.parse(process.env.URG||"null")||{};
        const cust=JSON.parse(process.env.CUST||"null")||{};
        n.push({
          kind:"voice_escalation",
          approved:true,
          to: uc.phone,
          customer_name: cust.name||null,
          ticket_id: pkg.ticket_id||null,
          message:"Priority support escalation for "+(cust.name||"customer")+". Ticket "+(pkg.ticket_id||"")+".",
          created_at:new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $voice-load.stdout
      PKG: $build-package.stdout
      URG: $urgency-check.stdout
      CUST: $load-customer.stdout

  - id: voice-queue
    condition: $urgency-check.json.voice_count > 0 && $voice-approval.approved
    pipeline: state.set vera/notifications
    stdin: $voice-next.stdout

  # 14. Append an audit record to the durable audit log (single-key index array).
  - id: audit-load
    pipeline: state.get vera/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const pkg=JSON.parse(process.env.PKG||"null")||{};
        const hours=JSON.parse(process.env.HOURS||"null")||{};
        log.push({
          action:"escalation",
          conversation_id: process.env.LOBSTER_ARG_CONVERSATION_ID,
          customer_email: process.env.LOBSTER_ARG_CUSTOMER_EMAIL,
          trigger: process.env.LOBSTER_ARG_ESCALATION_TRIGGER,
          priority: pkg.priority || "normal",
          sentiment: (pkg.sentiment||{}).current || null,
          is_staffed: !!hours.is_staffed,
          compliance_flag: (pkg.issue||{}).compliance_flag || false,
          ticket_id: pkg.ticket_id || null,
          authorized_by:"autonomous",
          timestamp: new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      PKG: $build-package.stdout
      HOURS: $check-hours.stdout

  - id: audit
    pipeline: state.set vera/audit-log
    stdin: $audit-next.stdout
