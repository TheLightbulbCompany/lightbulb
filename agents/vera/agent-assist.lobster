name: agent-assist
# Background assist for a human agent who just picked up an escalated ticket.
# Surfaces the escalation package, an optional fresh external lookup, a
# deterministic conversation brief, and QUEUES an LLM-draft request + a Slack
# delivery for the agent loop to fulfil. Intent preserved from the original
# (Reqs 40/41/42); rebuilt to run gateway-free on the embedded runner.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via native state.get/set on the per-owner EFS mount
#    (LOBSTER_STATE_DIR). The escalation package is read from the key the
#    escalation pipeline writes; the assist-draft + Slack delivery are appended
#    to single-key index arrays (state.* has no prefix-list) by node steps,
#    then re-written.
#  - NO LLM and NO Slack in-pipeline (both 401 here). The response DRAFT needs a
#    model and the assist needs Slack delivery, so both are written to
#    state-queues (vera/assist-requests, vera/notifications) for the agent loop.
#  - KB retrieval (local-rag-qdrant) is FICTIONAL — its query is handed to the
#    agent loop in the assist-request. An OPTIONAL fresh external lookup uses the
#    REAL Perplexity API via curl (gateway-free; PERPLEXITY_API_KEY from env).
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  conversation_id:
    required: true
  customer_email:
    required: true
  agent_message:
    default: ""
  external_lookup:
    default: ""        # optional: current-info query (carrier status, recent policy) for Perplexity

steps:
  # 1. Load the escalation package the escalation pipeline deposited for this
  #    conversation (native state, keyed by conversation_id). Read-only consumer.
  - id: load-escalation
    pipeline: state.get vera/escalations/${conversation_id}

  # 2. Gate: only assist when an escalation package actually exists. Emit an
  #    explicit boolean + count so we never gate on .length (silently false).
  - id: gate
    command: >
      node -e 'let p=null; try{p=JSON.parse(process.env.PKG||"null")}catch(e){p=null}
        const found = !!(p && (p.ticket_id || p.conversation || p.issue));
        process.stdout.write(JSON.stringify({found, has_package: found ? 1 : 0,
          ticket_id: (p&&p.ticket_id)||"", priority: (p&&p.priority)||"normal"}));'
    env:
      PKG: $load-escalation.stdout

  # 3. Decide whether to run a fresh external lookup (Perplexity). Only when the
  #    caller passed external_lookup text AND a package exists.
  - id: lookup-decide
    condition: $gate.json.found
    command: >
      node -e 'const q=(process.env.Q||"").trim();
        process.stdout.write(JSON.stringify({do_lookup: q.length>0 ? 1 : 0, query:q}));'
    env:
      Q: ${external_lookup}

  # 4. Fresh external lookup via the REAL Perplexity API (gateway-free curl).
  #    Deterministic API; key from process env. Exits 0 even on failure; the
  #    normalizer carries status so downstream branches via condition.
  - id: external-lookup
    # Match lookup-decide's own gate ($gate.json.found) so we never read its json when it
    # was skipped — only then evaluate do_lookup, which exists only when lookup-decide ran.
    condition: $gate.json.found && $lookup-decide.json.do_lookup > 0
    command: >
      node -e 'const {execSync}=require("child_process");
        const q=process.env.Q||""; let out="";
        try {
          out=execSync("curl -sS https://api.perplexity.ai/chat/completions "
            +"-H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" "
            +"-d "+JSON.stringify(JSON.stringify({model:"sonar",
                messages:[{role:"user",content:q}]})),
            {encoding:"utf8",timeout:45000});
        } catch(e){ out=""; }
        let content="", citations=[];
        try { const j=JSON.parse(out||"{}");
          content=(((j.choices||[])[0]||{}).message||{}).content||"";
          citations=Array.isArray(j.citations)?j.citations:[]; } catch(e){}
        process.stdout.write(JSON.stringify({ok: content.length>0,
          content, citations, top_urls:citations.slice(0,3)}));'
    env:
      Q: $lookup-decide.json.query

  # 5. Deterministic conversation brief from the transcript already inside the
  #    escalation package (zero LLM, zero gateway). Prefer the package's own
  #    summary if the escalation pipeline already wrote one.
  - id: brief
    condition: $gate.json.found
    command: >
      node -e 'let p={}; try{p=JSON.parse(process.env.PKG||"{}")}catch(e){p={}}
        const conv=(p.conversation||{});
        const tx=Array.isArray(conv.transcript)?conv.transcript:[];
        const lastCust=[...tx].reverse().find(m=>m.role==="customer")||{};
        const brief = conv.summary
          || ("Exchanges: "+(conv.exchange_count||tx.length||0)
              +". Last customer message: "
              +String(lastCust.text||lastCust.content||"").slice(0,400));
        process.stdout.write(JSON.stringify({brief,
          exchange_count: conv.exchange_count||tx.length||0,
          sentiment: (p.sentiment&&p.sentiment.current)||"unknown"}));'
    env:
      PKG: $load-escalation.stdout

  # 6. Append a DRAFT request to vera/assist-requests for the agent loop. The
  #    loop runs the (gateway) LLM draft grounded in the KB (local-rag-qdrant is
  #    fictional here, so the loop owns retrieval) + this external content.
  - id: assist-load
    condition: $gate.json.found
    pipeline: state.get vera/assist-requests

  - id: assist-next
    condition: $gate.json.found
    command: >
      node -e 'let arr=null; try{arr=JSON.parse(process.env.ARR||"null")}catch(e){arr=null}
        if(!Array.isArray(arr)) arr=[];
        let pkg={}; try{pkg=JSON.parse(process.env.PKG||"{}")}catch(e){pkg={}}
        let look=null; try{look=JSON.parse(process.env.LOOK||"null")}catch(e){look=null}
        let brief={}; try{brief=JSON.parse(process.env.BRIEF||"{}")}catch(e){brief={}}
        arr.push({
          kind:"agent_assist_draft",
          status:"pending",
          conversation_id: process.env.CONV_ID,
          customer_email: process.env.CUST_EMAIL,
          ticket_id: pkg.ticket_id||"",
          priority: pkg.priority||"normal",
          issue_summary: (pkg.issue&&pkg.issue.classification)||"unknown",
          kb_query: (pkg.issue&&pkg.issue.subcategory) || brief.brief || "",
          escalation_package: pkg,
          conversation_brief: brief.brief||"",
          sentiment: brief.sentiment||"unknown",
          agent_message: process.env.AGENT_MSG||"",
          external_lookup: look && look.ok ? {content:look.content, citations:look.citations} : null,
          requested_at: new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(arr));'
    env:
      ARR: $assist-load.stdout
      PKG: $load-escalation.stdout
      LOOK: $external-lookup.stdout
      BRIEF: $brief.stdout
      CONV_ID: ${conversation_id}
      CUST_EMAIL: ${customer_email}
      AGENT_MSG: ${agent_message}

  - id: assist-save
    condition: $gate.json.found
    pipeline: state.set vera/assist-requests
    stdin: $assist-next.stdout

  # 7. Queue the Slack delivery (Slack is 401 in-pipeline). The agent loop posts
  #    this to the #vera-escalations thread for the human — never to the customer.
  - id: notify-load
    condition: $gate.json.found
    pipeline: state.get vera/notifications

  - id: notify-next
    condition: $gate.json.found
    command: >
      node -e 'let arr=null; try{arr=JSON.parse(process.env.ARR||"null")}catch(e){arr=null}
        if(!Array.isArray(arr)) arr=[];
        let pkg={}; try{pkg=JSON.parse(process.env.PKG||"{}")}catch(e){pkg={}}
        let brief={}; try{brief=JSON.parse(process.env.BRIEF||"{}")}catch(e){brief={}}
        arr.push({
          kind:"agent_assist",
          channel:"#vera-escalations",
          thread_key: process.env.CONV_ID,
          deliver_to:"agent",
          ticket_id: pkg.ticket_id||"",
          priority: pkg.priority||"normal",
          conversation_id: process.env.CONV_ID,
          customer_email: process.env.CUST_EMAIL,
          conversation_brief: brief.brief||"",
          sentiment: brief.sentiment||"unknown",
          draft_pending:true,
          created_at: new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(arr));'
    env:
      ARR: $notify-load.stdout
      PKG: $load-escalation.stdout
      BRIEF: $brief.stdout
      CONV_ID: ${conversation_id}
      CUST_EMAIL: ${customer_email}

  - id: notify-save
    condition: $gate.json.found
    pipeline: state.set vera/notifications
    stdin: $notify-next.stdout

  # 8. Missing-escalation notice — if no package exists, queue a connect/repair
  #    notice for the agent loop instead of silently producing nothing.
  - id: no-package-load
    condition: "!$gate.json.found"
    pipeline: state.get vera/notifications

  - id: no-package-next
    condition: "!$gate.json.found"
    command: >
      node -e 'let arr=null; try{arr=JSON.parse(process.env.ARR||"null")}catch(e){arr=null}
        if(!Array.isArray(arr)) arr=[];
        arr.push({
          kind:"agent_assist_unavailable",
          channel:"#vera-admin",
          conversation_id: process.env.CONV_ID,
          customer_email: process.env.CUST_EMAIL,
          reason:"No escalation package found at vera/escalations/"+process.env.CONV_ID
            +". The escalation pipeline must run (and persist the package) before assist can help.",
          created_at: new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(arr));'
    env:
      ARR: $no-package-load.stdout
      CONV_ID: ${conversation_id}
      CUST_EMAIL: ${customer_email}

  - id: no-package-save
    condition: "!$gate.json.found"
    pipeline: state.set vera/notifications
    stdin: $no-package-next.stdout
