#!/usr/bin/env node
/**
 * scout-dedup-check.js
 * Requirements 39, 40, 41, 60: Deduplicate by email, domain, LinkedIn URL.
 * Check CRM, Scout queue, and Pitch active sequences.
 *
 * Deterministic matching. Zero LLM.
 */

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const lead = input.lead || {};
  const crmRecords = input.crm_records || [];
  const scoutQueue = input.scout_queue || [];
  const pitchSequences = input.pitch_sequences || [];

  const identifiers = {
    email: (lead.email || '').toLowerCase().trim(),
    domain: (lead.domain || lead.company_domain || '').toLowerCase().trim(),
    linkedin: (lead.linkedin_url || '').toLowerCase().trim()
  };

  const duplicates = [];

  // Check 1: CRM — existing customers, open deals, do-not-contact (Req 39)
  for (const record of crmRecords) {
    const match = matchesIdentifiers(record, identifiers);
    if (match.matched) {
      duplicates.push({
        source: 'crm',
        match_type: match.match_field,
        record_status: record.status || 'unknown',
        record_type: record.type || 'contact',
        action: getAction(record.status),
        domain: record.domain
      });
    }
  }

  // Check 2: Scout's own queue — already deposited (Req 40)
  for (const queued of scoutQueue) {
    const match = matchesIdentifiers(queued, identifiers);
    if (match.matched) {
      duplicates.push({
        source: 'scout_queue',
        match_type: match.match_field,
        existing_signals: queued.signals || [],
        new_signal: lead.signal_type,
        action: 'append_signal',
        domain: queued.domain
      });
    }
  }

  // Check 3: Pitch's active sequences — currently being worked (Req 41)
  for (const seq of pitchSequences) {
    const match = matchesIdentifiers(seq, identifiers);
    if (match.matched) {
      duplicates.push({
        source: 'pitch_sequence',
        match_type: match.match_field,
        sequence_status: seq.status,
        touch_count: seq.touch_count,
        action: 'append_signal_to_sequence',
        domain: seq.domain || seq.prospect_domain
      });
    }
  }

  const hasDuplicates = duplicates.length > 0;
  const hardBlock = duplicates.some(d =>
    d.action === 'block' || d.action === 'skip'
  );

  const result = {
    lead_domain: identifiers.domain,
    lead_email: identifiers.email,
    is_duplicate: hasDuplicates,
    hard_block: hardBlock,
    duplicates,
    action: hardBlock ? 'drop' :
            duplicates.some(d => d.action === 'append_signal') ? 'append_signal' :
            duplicates.some(d => d.action === 'append_signal_to_sequence') ? 'append_signal_to_sequence' :
            'proceed',
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(hardBlock ? 1 : 0);
}

function matchesIdentifiers(record, identifiers) {
  const recordEmail = (record.email || '').toLowerCase().trim();
  const recordDomain = (record.domain || record.company_domain || record.prospect_domain || '').toLowerCase().trim();
  const recordLinkedin = (record.linkedin_url || record.linkedin || '').toLowerCase().trim();

  if (identifiers.email && recordEmail && identifiers.email === recordEmail) {
    return { matched: true, match_field: 'email' };
  }
  if (identifiers.domain && recordDomain && identifiers.domain === recordDomain) {
    return { matched: true, match_field: 'domain' };
  }
  if (identifiers.linkedin && recordLinkedin && identifiers.linkedin === recordLinkedin) {
    return { matched: true, match_field: 'linkedin_url' };
  }
  return { matched: false };
}

function getAction(status) {
  const blockStatuses = ['customer', 'current_customer', 'do_not_contact', 'dnc', 'opted_out'];
  const skipStatuses = ['open_deal', 'active_deal', 'in_progress'];

  if (blockStatuses.includes((status || '').toLowerCase())) return 'block';
  if (skipStatuses.includes((status || '').toLowerCase())) return 'skip';
  return 'review';
}

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => data += chunk);
    process.stdin.on('end', () => {
      try { resolve(JSON.parse(data)); }
      catch { resolve(null); }
    });
    if (process.stdin.isTTY) resolve(null);
  });
}

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
