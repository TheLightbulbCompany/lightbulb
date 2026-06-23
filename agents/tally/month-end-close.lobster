name: month-end-close
# Requirements 40-44: Month-end close as a review-and-confirm package.
# Triggered on the last business day of the month, or manually by the finance person.
# Fully deterministic (0 LLM). Tier-2 writes are gated behind a human approval step.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - There is NO fast-io and NO openclaw.invoke on the embedded runner (every such
#    call returns 401). Config/state lives in native state.get/set under
#    `$LOBSTER_STATE_DIR`, namespaced `tally/*`. The finance person's connected
#    integrations + accounting policies are READ-ONLY consumer state the agent seeds.
#  - The accounting platform is QuickBooks (curl + QBO_* env) OR Xero. The Xero skill
#    is unreachable from an embedded pipeline, so Xero is routed to a notifications
#    intent with a "connect / run from the agent loop" notice. QuickBooks is a real
#    REST API reached by curl; its fetch step exits 0 and carries status in JSON.
#  - Slack delivery, Google Sheets/Drive writes, and prior-period archival cannot run
#    here (Slack 401; `gog` is gmail/calendar/drive only, not a generic sheet/drive
#    writer). The assembled close package + every side effect that needs a human or a
#    tool is written to the `tally/notifications` queue for the agent loop to deliver.
#  - The Tier-2 ledger writes (posting journal entries) are NEVER performed by this
#    pipeline. We assemble drafts, take a real `approval: required` gate, and on
#    approval enqueue an APPROVED post-journal-entries intent for the agent loop.
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.

args:
  period_end:
    required: true        # YYYY-MM-DD (close date)
  period_start:
    required: true        # YYYY-MM-DD (period open)

steps:
  # ---------------------------------------------------------------------------
  # CONFIG / STATE LOAD (read-only consumer state the finance person seeds)
  # ---------------------------------------------------------------------------

  # 1. Which accounting platform is connected (xero | quickbooks | <none>).
  - id: connected
    pipeline: state.get tally/connected-accounting

  # 2. Accounting policies (accruals, prepaids, depreciation schedules).
  - id: policies
    pipeline: state.get tally/accounting-policies

  # 3. Prior-period statements (for validation comparison).
  - id: prior
    pipeline: state.get tally/prior-period-statements

  # 4. Current-period financial data (statements + close inputs the daily/weekly
  #    pipelines accumulate: accounts, receivables, pending counts, anomalies).
  - id: current
    pipeline: state.get tally/current-period

  # 5. Normalize platform selection into an explicit flag (no .length gating).
  - id: platform
    command: >
      node -e 'const c=JSON.parse(process.env.CONNECTED||"null")||{};
        const p=(c.platform||"").toLowerCase();
        process.stdout.write(JSON.stringify({
          platform:p,
          is_qbo: p==="quickbooks",
          is_xero: p==="xero",
          connected: p==="quickbooks"||p==="xero"?1:0
        }));'
    env:
      CONNECTED: $connected.stdout

  # ---------------------------------------------------------------------------
  # RECONCILIATION + DUPLICATE CHECK (Req 19, 20) — QuickBooks path
  # ---------------------------------------------------------------------------

  # 6. Pull bank accounts + period transactions from QuickBooks. exits 0 always;
  #    carries {ok, ledger_entries[], bank_entries[]} so the reconciler can run.
  - id: qbo-fetch
    condition: $platform.json.is_qbo
    command: >
      node -e 'const {execSync}=require("child_process");
        const ps=process.env.LOBSTER_ARG_PERIOD_START, pe=process.env.LOBSTER_ARG_PERIOD_END;
        const realm=process.env.QBO_REALM_ID||"", tok=process.env.QBO_ACCESS_TOKEN||"";
        function q(sql){ const u="https://quickbooks.api.intuit.com/v3/company/"+realm
            +"/query?query="+encodeURIComponent(sql)+"&minorversion=65";
          return execSync("curl -sS "+JSON.stringify(u)
            +" -H "+JSON.stringify("Authorization: Bearer "+tok)
            +" -H "+JSON.stringify("Accept: application/json"),
            {encoding:"utf8",timeout:45000}); }
        let bank=[], ledger=[], ok=true, err="";
        try {
          const ar=JSON.parse(q("SELECT * FROM Account WHERE AccountType = '"'"'Bank'"'"'"));
          bank=((ar.QueryResponse||{}).Account||[]).map(a=>({
            id:a.Id, vendor:a.Name, description:a.Name,
            amount:Number(a.CurrentBalance)||0, date:pe }));
          const pr=JSON.parse(q("SELECT * FROM Purchase WHERE TxnDate >= '"'"'"+ps+"'"'"'"
            +" AND TxnDate <= '"'"'"+pe+"'"'"'"));
          ledger=((pr.QueryResponse||{}).Purchase||[]).map(t=>({
            id:t.Id, date:t.TxnDate, amount:Number(t.TotalAmt)||0,
            vendor:((t.EntityRef||{}).name)||"", source:"quickbooks",
            invoice_number:t.DocNumber||"" }));
        } catch(e){ ok=false; err=String(e&&e.message||e); }
        process.stdout.write(JSON.stringify({
          ok, error:err, ledger_entries:ledger, bank_entries:bank,
          all_entries:ledger }));'

  # 7. Run the deterministic reconciler over the QBO fetch (exits 0; emits summary).
  - id: recon
    condition: $platform.json.is_qbo && $qbo-fetch.json.ok
    command: node tally-reconciliation.js
    stdin: $qbo-fetch.stdout

  # ---------------------------------------------------------------------------
  # CLOSE PACKAGE ASSEMBLY (Req 41-44) — platform-agnostic, deterministic
  # ---------------------------------------------------------------------------

  # 8. Close checklist (Req 41). Feed the current-period close inputs + policies.
  - id: checklist-input
    command: >
      node -e 'const cur=JSON.parse(process.env.CURRENT||"null")||{};
        const pol=JSON.parse(process.env.POLICIES||"null")||{};
        const rec=JSON.parse(process.env.RECON||"null");
        process.stdout.write(JSON.stringify({
          mode:"checklist",
          accounts: cur.accounts||[],
          pending_categorizations: cur.pending_categorizations||0,
          receivables: cur.receivables||[],
          accounting_policies: pol,
          unresolved_anomalies: cur.unresolved_anomalies||0,
          reconciliation_summary: rec? rec.summary : null }));'
    env:
      CURRENT: $current.stdout
      POLICIES: $policies.stdout
      RECON: $recon.stdout

  - id: checklist
    command: node tally-close-engine.js
    stdin: $checklist-input.stdout

  # 9. Draft journal entries (Req 42) — accruals / prepaids / depreciation.
  - id: journal-input
    command: >
      node -e 'const pol=JSON.parse(process.env.POLICIES||"null")||{};
        process.stdout.write(JSON.stringify({
          mode:"journal_entries", policies:pol,
          period:{ end_date: process.env.LOBSTER_ARG_PERIOD_END } }));'
    env:
      POLICIES: $policies.stdout

  - id: journal-entries
    command: node tally-close-engine.js
    stdin: $journal-input.stdout

  # 10. Validate statements (Req 44) — current vs prior vs projection.
  - id: validate-input
    command: >
      node -e 'const cur=JSON.parse(process.env.CURRENT||"null")||{};
        const prior=JSON.parse(process.env.PRIOR||"null")||{};
        process.stdout.write(JSON.stringify({
          mode:"validate",
          current: cur.statements||cur.current_statements||cur||{},
          prior: prior.statements||prior||{},
          projections: cur.projections||{} }));'
    env:
      CURRENT: $current.stdout
      PRIOR: $prior.stdout

  - id: validate-statements
    command: node tally-close-engine.js
    stdin: $validate-input.stdout

  # 11. Emit explicit counts so downstream gates never rely on .length.
  - id: counts
    command: >
      node -e 'const ch=JSON.parse(process.env.CHECKLIST||"[]");
        const je=JSON.parse(process.env.JOURNAL||"[]");
        const fl=JSON.parse(process.env.FLAGS||"[]");
        const ready = Array.isArray(ch) && ch.every(i=>i.status==="ready")
          && Array.isArray(fl) && fl.length===0;
        process.stdout.write(JSON.stringify({
          checklist_count: Array.isArray(ch)?ch.length:0,
          journal_count: Array.isArray(je)?je.length:0,
          flag_count: Array.isArray(fl)?fl.length:0,
          ready_to_close: ready }));'
    env:
      CHECKLIST: $checklist.stdout
      JOURNAL: $journal-entries.stdout
      FLAGS: $validate-statements.stdout

  # 12. Assemble the full close package (single object for delivery + archival).
  - id: package
    command: >
      node -e 'const ch=JSON.parse(process.env.CHECKLIST||"[]");
        const je=JSON.parse(process.env.JOURNAL||"[]");
        const fl=JSON.parse(process.env.FLAGS||"[]");
        const ct=JSON.parse(process.env.COUNTS||"{}");
        const rec=JSON.parse(process.env.RECON||"null");
        process.stdout.write(JSON.stringify({
          period_start: process.env.LOBSTER_ARG_PERIOD_START,
          period_end: process.env.LOBSTER_ARG_PERIOD_END,
          checklist: ch, journal_entries: je, validation_flags: fl,
          reconciliation: rec? rec.summary : null,
          duplicates: rec? rec.duplicates : [],
          ready_to_close: ct.ready_to_close,
          assembled_at: new Date().toISOString() }));'
    env:
      CHECKLIST: $checklist.stdout
      JOURNAL: $journal-entries.stdout
      FLAGS: $validate-statements.stdout
      COUNTS: $counts.stdout
      RECON: $recon.stdout

  # ---------------------------------------------------------------------------
  # DELIVERY (Slack / Sheets / Drive unreachable here) -> notifications queue
  # ---------------------------------------------------------------------------

  # 13. Notify: Xero is connected but unreachable from the embedded pipeline.
  - id: notify-load-a
    condition: $platform.json.is_xero
    pipeline: state.get tally/notifications

  - id: notify-xero-next
    condition: $platform.json.is_xero
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({ kind:"connect_notice", channel:"#tally-approvals",
          platform:"xero",
          text:"Month-end close ran, but the Xero reconciliation + reports must be pulled from the agent loop (the Xero skill is not reachable inside the deterministic pipeline). Run the Xero close steps interactively, or connect QuickBooks for fully automated reconciliation.",
          period_end: process.env.LOBSTER_ARG_PERIOD_END,
          created_at:new Date().toISOString() });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load-a.stdout

  - id: notify-xero
    condition: $platform.json.is_xero
    pipeline: state.set tally/notifications
    stdin: $notify-xero-next.stdout

  # 14. Notify: no accounting platform connected at all (click-to-connect notice).
  - id: notify-load-b
    condition: $platform.json.connected == 0
    pipeline: state.get tally/notifications

  - id: notify-none-next
    condition: $platform.json.connected == 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({ kind:"connect_notice", channel:"#tally-approvals",
          platform:"none",
          text:"Month-end close assembled the policy-driven checklist + draft journal entries, but no accounting software is connected — reconciliation and draft statements were skipped. To finish the close, connect Xero or QuickBooks in your settings.",
          period_end: process.env.LOBSTER_ARG_PERIOD_END,
          created_at:new Date().toISOString() });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load-b.stdout

  - id: notify-none
    condition: $platform.json.connected == 0
    pipeline: state.set tally/notifications
    stdin: $notify-none-next.stdout

  # 15. Notify: close package ready for the finance person to review (Slack-bound).
  #     The agent loop delivers this to #tally-approvals + writes the Sheet/Drive copy.
  - id: notify-load-c
    pipeline: state.get tally/notifications

  - id: notify-package-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const ct=JSON.parse(process.env.COUNTS||"{}");
        const pkg=JSON.parse(process.env.PKG||"{}");
        n.push({ kind:"close_package", channel:"#tally-approvals",
          deliver:["slack","sheets","drive"],
          sheet:{ spreadsheet:"Tally Monthly Close", tab: process.env.LOBSTER_ARG_PERIOD_END },
          drive:{ file:"Tally Audit Backup — "+process.env.LOBSTER_ARG_PERIOD_END,
                  folder:"Tally Audit Archive" },
          period_start: process.env.LOBSTER_ARG_PERIOD_START,
          period_end: process.env.LOBSTER_ARG_PERIOD_END,
          checklist_count: ct.checklist_count||0,
          journal_count: ct.journal_count||0,
          flag_count: ct.flag_count||0,
          ready_to_close: !!ct.ready_to_close,
          package: pkg,
          created_at:new Date().toISOString() });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load-c.stdout
      COUNTS: $counts.stdout
      PKG: $package.stdout

  - id: notify-package
    pipeline: state.set tally/notifications
    stdin: $notify-package-next.stdout

  # 16. Archive this period as prior-period for next month's validation comparison.
  #     (Replaces the fictional fast-io write of tally-state/prior-period-statements.)
  - id: prior-next
    command: >
      node -e 'const cur=JSON.parse(process.env.CURRENT||"null")||{};
        const stmts = cur.statements||cur.current_statements||cur||{};
        process.stdout.write(JSON.stringify({
          statements: stmts,
          period_end: process.env.LOBSTER_ARG_PERIOD_END,
          saved_at:new Date().toISOString() }));'
    env:
      CURRENT: $current.stdout

  - id: save-prior
    pipeline: state.set tally/prior-period-statements
    stdin: $prior-next.stdout

  # 17. Audit log (Req 50, 7-year retention) — append to a single-key index array.
  - id: audit-load
    pipeline: state.get tally/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const ct=JSON.parse(process.env.COUNTS||"{}");
        const rec=JSON.parse(process.env.RECON||"null");
        log.push({ action:"month_end_close",
          period: process.env.LOBSTER_ARG_PERIOD_START+" to "+process.env.LOBSTER_ARG_PERIOD_END,
          platform: (JSON.parse(process.env.PLATFORM||"{}").platform)||"none",
          checklist_items: ct.checklist_count||0,
          journal_entries_drafted: ct.journal_count||0,
          validation_flags: ct.flag_count||0,
          reconciliation: rec? rec.summary : null,
          duplicates_found: rec? (rec.summary||{}).duplicates_found||0 : 0,
          ready_to_close: !!ct.ready_to_close,
          authorized_by:"autonomous",
          timestamp:new Date().toISOString() });
        process.stdout.write(JSON.stringify(log));'
    env:
      LOG: $audit-load.stdout
      COUNTS: $counts.stdout
      RECON: $recon.stdout
      PLATFORM: $platform.stdout

  - id: audit-log
    pipeline: state.set tally/audit-log
    stdin: $audit-next.stdout

  # ---------------------------------------------------------------------------
  # TIER-2 GATE: human approval BEFORE any ledger write (AGENTS.md mandate)
  # ---------------------------------------------------------------------------

  # 18. Real approval gate. Only prompt when there are journal entries to post.
  #     The run halts with a resumeToken; the finance person approves to proceed.
  - id: approve-journal
    condition: $counts.json.journal_count > 0
    approval:
      required: true
      prompt: "Month-end close — approve drafted journal entries for posting?"

  # 19. On approval, enqueue an APPROVED post-journal-entries intent for the agent
  #     loop to execute against the connected ledger (Xero skill / QuickBooks write
  #     API). The pipeline NEVER posts to the ledger itself.
  - id: post-load
    condition: $counts.json.journal_count > 0 && $approve-journal.approved
    pipeline: state.get tally/notifications

  - id: post-next
    condition: $counts.json.journal_count > 0 && $approve-journal.approved
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const je=JSON.parse(process.env.JOURNAL||"[]");
        n.push({ kind:"post_journal_entries", approved:true,
          platform: (JSON.parse(process.env.PLATFORM||"{}").platform)||"none",
          period_end: process.env.LOBSTER_ARG_PERIOD_END,
          journal_entries: je,
          approved_at:new Date().toISOString() });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $post-load.stdout
      JOURNAL: $journal-entries.stdout
      PLATFORM: $platform.stdout

  - id: post-journal
    condition: $counts.json.journal_count > 0 && $approve-journal.approved
    pipeline: state.set tally/notifications
    stdin: $post-next.stdout
