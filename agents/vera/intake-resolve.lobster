name: intake-resolve
# Core ticket-handling pipeline. Triggered per incoming customer message.
#
# Rebuilt for the embedded OpenClaw runner (gateway-free, deterministic). See the
# lobster-authoring skill. Key mechanics copied from the validated reference
# (pitch-scout/deposit-lead.lobster):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via native state.get/set on the per-agent EFS mount
#    (LOBSTER_STATE_DIR). fast-io is FICTIONAL. Conversation/sentiment trails and the
#    escalation/notification/CSAT queues are single-key index arrays, edited in a node
#    step then re-written.
#  - Helpdesk customer lookup is real `curl` (Zendesk/Intercom) keyed off read-only
#    consumer config the user set (vera-config/connected-helpdesk).
#  - LLM judgment (ambiguous classify, KB-grounded confidence, response generation) and
#    Slack delivery CANNOT run in an embedded pipeline (401). The pipeline stays
#    deterministic and writes those as intents onto vera/notifications for the agent
#    loop to handle.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.
#  - Refund (money movement) sits behind a real step-level `approval: required` gate.
#
# DROPPED fictional integrations (work routed to the vera/notifications queue or a
# connect-X notice): local-rag-qdrant (RAG/KB), llm-task, message/slack send,
# lobster --action run, taskr, telcall-twilio, agent-browser, capability-evolver.

args:
  customer_message:
    required: true
  customer_email:
    required: true
  channel:
    default: "chat"
  conversation_id:
    default: ""

steps:
  # 1. Deterministic classification (Req 9, 27, 50). ~70% resolved here with zero LLM;
  #    ambiguous → needs_llm flag, deferred to the agent loop (no llm-task in-pipeline).
  - id: classify
    command: node vera-intake-classifier.js
    stdin: '{"message": "${customer_message}"}'

  # 2. Load this conversation's sentiment trail (single-key array; null on first turn).
  - id: sentiment-history
    pipeline: state.get vera/conversations/${conversation_id}/sentiment-history

  # 3. Deterministic sentiment detection (Req 11, 33-35). Runs every message.
  - id: sentiment
    command: node vera-sentiment-detector.js
    stdin: '{"message": "${customer_message}", "sentiment_history": $sentiment-history.json}'

  # 4. Immediate-escalation decision (Req 26, 27, 35). needs_human / urgent_escalation /
  #    distress sentiment → skip autonomous resolution entirely. If the deterministic
  #    classifier punted (needs_llm), we do NOT auto-resolve — we conservatively defer to
  #    the agent loop (no in-pipeline LLM), which is the safe default for support.
  - id: triage
    command: >
      node -e 'const c=JSON.parse(process.env.CLASSIFY||"{}");
        const s=JSON.parse(process.env.SENTIMENT||"{}");
        const category=c.classified?c.category:"needs_llm";
        const escalate = category==="needs_human" || category==="urgent_escalation" || s.action==="priority_escalate";
        const trigger = escalate ? (category==="urgent_escalation"?"distress":(s.action==="priority_escalate"?"sentiment":"request")) : null;
        const deferLlm = category==="needs_llm";
        process.stdout.write(JSON.stringify({
          needs_escalation: escalate,
          escalation_count: escalate?1:0,
          category, subcategory: c.subcategory||null,
          trigger, defer_llm: deferLlm,
          compliance_flag: !!c.compliance_flag}));'
    env:
      CLASSIFY: $classify.stdout
      SENTIMENT: $sentiment.stdout

  # 5. Append this turn to the sentiment trail (persist regardless of branch).
  - id: sentiment-trail-next
    command: >
      node -e 'const h=JSON.parse(process.env.HIST||"null")||[];
        const s=JSON.parse(process.env.SENTIMENT||"{}");
        h.push({sentiment:s.sentiment, score:s.score, action:s.action, at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(h.slice(-50)));'
    env:
      HIST: $sentiment-history.stdout
      SENTIMENT: $sentiment.stdout

  - id: sentiment-trail
    pipeline: state.set vera/conversations/${conversation_id}/sentiment-history
    stdin: $sentiment-trail-next.stdout

  # ---- AUTONOMOUS-RESOLUTION BRANCH (only when NOT escalating) ----

  # 6. Read the helpdesk the user connected (read-only consumer config). null = standalone.
  - id: helpdesk-config
    condition: $triage.json.needs_escalation == false
    pipeline: state.get vera-config/connected-helpdesk

  # 7. Customer-context lookup — never make them repeat (Req 10). One real curl per
  #    connected provider; standalone reads a single-key customer index from state.
  - id: load-customer-zendesk
    condition: $helpdesk-config.json.platform == "zendesk" && $triage.json.needs_escalation == false
    command: >
      curl -s -u "$ZENDESK_EMAIL/token:$ZENDESK_API_TOKEN"
      "https://$ZENDESK_SUBDOMAIN.zendesk.com/api/v2/search.json?query=type:user+email:${customer_email}"
    on_error: continue

  - id: load-customer-intercom
    condition: $helpdesk-config.json.platform == "intercom" && $triage.json.needs_escalation == false
    command: >
      curl -s -H "Authorization: Bearer $INTERCOM_ACCESS_TOKEN" -H "Accept: application/json"
      "https://api.intercom.io/contacts?email=${customer_email}"
    on_error: continue

  - id: load-customer-standalone
    condition: $helpdesk-config.json.platform == null && $triage.json.needs_escalation == false
    pipeline: state.get vera/customers/${customer_email}

  # 8. Normalize whichever provider responded into a single customer object.
  - id: load-customer
    condition: $triage.json.needs_escalation == false
    command: >
      node -e 'const zd=JSON.parse(process.env.ZD||"null");
        const ic=JSON.parse(process.env.IC||"null");
        const sa=JSON.parse(process.env.SA||"null");
        let c={};
        if (zd && zd.results) { const u=(zd.results||[])[0]||{};
          c={name:u.name||null,email:u.email||null,account_id:String(u.id||""),phone:u.phone||null,latest_order_id:u.latest_order_id||null,source:"zendesk"}; }
        else if (ic && ic.data) { const u=(ic.data||[])[0]||{};
          c={name:u.name||null,email:u.email||null,account_id:u.id||null,phone:u.phone||null,latest_order_id:null,source:"intercom"}; }
        else { c=sa||{}; c.source=c.source||"standalone"; }
        process.stdout.write(JSON.stringify(c));'
    env:
      ZD: $load-customer-zendesk.stdout
      IC: $load-customer-intercom.stdout
      SA: $load-customer-standalone.stdout

  # 9. Conversation history (exchange count, resolved flag) — single key per conversation.
  - id: conversation
    condition: $triage.json.needs_escalation == false
    pipeline: state.get vera/conversations/${conversation_id}

  # 10. Exchange-count check (Req 28): >=2 unresolved exchanges → offer a human.
  - id: exchange-check
    condition: $triage.json.needs_escalation == false
    command: >
      node -e 'const conv=JSON.parse(process.env.CONV||"null")||{};
        const exchanges=(conv.exchanges||0)+1;
        const offer = exchanges>=2 && !conv.resolved;
        process.stdout.write(JSON.stringify({exchanges, offer_escalation:offer, already_resolved:!!conv.resolved}));'
    env:
      CONV: $conversation.stdout

  # 11. Read the authorized-action scope the user configured (Req 17, 18, 49).
  - id: scope-config
    condition: $triage.json.needs_escalation == false
    pipeline: state.get vera-config/authorized-actions

  # 12. Resolve which (if any) autonomous action this ticket maps to, in-scope.
  - id: scope-check
    condition: $triage.json.needs_escalation == false
    command: >
      node -e 'const scope=JSON.parse(process.env.SCOPE||"null")||{};
        const sub=process.env.SUBCATEGORY||"";
        let action=null, within=true;
        if (sub==="refund") { action="refund"; within=scope.refunds_enabled!==false; }
        else if (sub==="returns") { action="return"; within=scope.returns_enabled!==false; }
        process.stdout.write(JSON.stringify({action, within_scope:within, subcategory:sub,
          max_refund_amount: (scope.max_refund_amount!==undefined?scope.max_refund_amount:null)}));'
    env:
      SCOPE: $scope-config.stdout
      SUBCATEGORY: $triage.json.subcategory

  # 13. RAG + response generation CANNOT run here (KB vector store + LLM are gateway-only,
  #     401). Enqueue a single "resolve" intent for the agent loop: it does the RAG query,
  #     runs the <85%-confidence escalate-vs-answer judgment, generates the reply, and
  #     delivers it on the customer's channel. This is the deterministic handoff boundary.
  - id: resolve-queue-load
    condition: $triage.json.needs_escalation == false
    pipeline: state.get vera/notifications

  - id: resolve-queue-next
    condition: $triage.json.needs_escalation == false
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const cust=JSON.parse(process.env.CUST||"{}");
        const exch=JSON.parse(process.env.EXCH||"{}");
        const sc=JSON.parse(process.env.SCOPE||"{}");
        q.push({kind:"resolve_ticket",
          conversation_id:process.env.LOBSTER_ARG_CONVERSATION_ID||"",
          channel:process.env.LOBSTER_ARG_CHANNEL||"chat",
          customer_email:process.env.LOBSTER_ARG_CUSTOMER_EMAIL||"",
          customer_message:process.env.LOBSTER_ARG_CUSTOMER_MESSAGE||"",
          customer:cust,
          subcategory:process.env.SUBCATEGORY||null,
          action:sc.action||null,
          action_within_scope: sc.within_scope!==false,
          offer_escalation: !!exch.offer_escalation,
          exchanges: exch.exchanges||1,
          defer_llm: process.env.DEFER_LLM==="true",
          requires:["rag_query","confidence_check","response_generation","channel_delivery"],
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: $resolve-queue-load.stdout
      CUST: $load-customer.stdout
      EXCH: $exchange-check.stdout
      SCOPE: $scope-check.stdout
      SUBCATEGORY: $triage.json.subcategory
      DEFER_LLM: $triage.json.defer_llm
    stdin: ""

  - id: resolve-queue
    condition: $triage.json.needs_escalation == false
    pipeline: state.set vera/notifications
    stdin: $resolve-queue-next.stdout

  # 14. Refund is real money movement. Gate it behind an explicit human approval BEFORE the
  #     Stripe side effect (no fictional await-approval tool). Only reached for in-scope
  #     refund actions on a non-escalated ticket.
  - id: refund-approval
    condition: $scope-check.json.action == "refund" && $scope-check.json.within_scope == true
    approval:
      required: true
      prompt: "Approve Stripe refund for ${customer_email} (conversation ${conversation_id})?"

  - id: execute-refund
    condition: $scope-check.json.action == "refund" && $scope-check.json.within_scope == true && $refund-approval.approved
    command: >
      curl -s -X POST "https://api.stripe.com/v1/refunds" -u "$STRIPE_API_KEY:"
      -d "charge=$ORDER_CHARGE_ID" -d "amount=$REFUND_AMOUNT"
    on_error: continue

  # 15. Persist conversation state (exchange count, last touch). resolved stays whatever it
  #     was — only the agent loop's customer-confirmed close flips it true (Req: never close
  #     a ticket the customer hasn't confirmed).
  - id: conversation-next
    condition: $triage.json.needs_escalation == false
    command: >
      node -e 'const conv=JSON.parse(process.env.CONV||"null")||{};
        const exch=JSON.parse(process.env.EXCH||"{}");
        conv.exchanges=exch.exchanges||((conv.exchanges||0)+1);
        conv.resolved=!!conv.resolved;
        conv.last_response_at=new Date().toISOString();
        conv.channel=process.env.LOBSTER_ARG_CHANNEL||conv.channel||"chat";
        process.stdout.write(JSON.stringify(conv));'
    env:
      CONV: $conversation.stdout
      EXCH: $exchange-check.stdout
    stdin: ""

  - id: conversation-save
    condition: $triage.json.needs_escalation == false
    pipeline: state.set vera/conversations/${conversation_id}
    stdin: $conversation-next.stdout

  # ---- ESCALATION BRANCH (needs_human / urgent / distress / ambiguous-defer) ----
  # In-pipeline escalation = enqueue a full-context escalation intent for the agent loop.
  # We CANNOT call lobster --action run (fictional) or post to Slack (401), so the agent
  # loop picks this up and runs the escalation handoff + #vera-escalations delivery.

  - id: escalation-queue-load
    condition: $triage.json.needs_escalation
    pipeline: state.get vera/notifications

  - id: escalation-queue-next
    condition: $triage.json.needs_escalation
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const cust=JSON.parse(process.env.CUST||"null");
        const tri=JSON.parse(process.env.TRIAGE||"{}");
        q.push({kind:"escalate_ticket",
          urgent: tri.trigger==="distress" || tri.compliance_flag===true,
          conversation_id:process.env.LOBSTER_ARG_CONVERSATION_ID||"",
          channel:process.env.LOBSTER_ARG_CHANNEL||"chat",
          customer_email:process.env.LOBSTER_ARG_CUSTOMER_EMAIL||"",
          customer_message:process.env.LOBSTER_ARG_CUSTOMER_MESSAGE||"",
          customer: cust,
          category: tri.category,
          escalation_trigger: tri.trigger,
          compliance_flag: tri.compliance_flag===true,
          defer_llm: tri.defer_llm===true,
          requires:["context_handoff","slack_escalation_delivery"],
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: $escalation-queue-load.stdout
      CUST: $load-customer.stdout
      TRIAGE: $triage.stdout
    stdin: ""

  - id: escalation-queue
    condition: $triage.json.needs_escalation
    pipeline: state.set vera/notifications
    stdin: $escalation-queue-next.stdout

  # ---- AUDIT (always; Req 48) ----
  # Append a deterministic audit record to a single-key audit index array. (fast-io
  # audit/{ts}/... keys are fictional and have no prefix-list; one index array is the
  # durable, enumerable equivalent.)
  - id: audit-load
    pipeline: state.get vera/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const tri=JSON.parse(process.env.TRIAGE||"{}");
        const s=JSON.parse(process.env.SENTIMENT||"{}");
        log.push({action:"ticket_handled",
          conversation_id:process.env.LOBSTER_ARG_CONVERSATION_ID||"",
          customer_email:process.env.LOBSTER_ARG_CUSTOMER_EMAIL||"",
          channel:process.env.LOBSTER_ARG_CHANNEL||"chat",
          classification:tri.category, subcategory:tri.subcategory,
          sentiment:s.sentiment, sentiment_action:s.action,
          escalated:tri.needs_escalation===true, escalation_trigger:tri.trigger,
          compliance_flag:tri.compliance_flag===true,
          authorized_by:"autonomous", at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log.slice(-1000)));'
    env:
      LOG: $audit-load.stdout
      TRIAGE: $triage.stdout
      SENTIMENT: $sentiment.stdout
    stdin: ""

  - id: audit
    pipeline: state.set vera/audit-log
    stdin: $audit-next.stdout
