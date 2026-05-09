#!/usr/bin/env node
/**
 * pitch-interrupt-checker.js
 * Requirement 29: Pause sequences on reply, material change, CRM status change, or 5-touch max.
 *
 * Checks for interrupt conditions against active sequences.
 * Deterministic. Zero LLM.
 */

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const dueTouches = input.due_touches || [];
  const recentSignals = input.recent_signals || [];
  const crmChanges = input.crm_changes || [];
  const replies = input.replies || [];

  const interrupts = [];
  const cleared = [];

  for (const touch of dueTouches) {
    const domain = touch.domain;
    let interrupted = false;
    let interruptReason = null;

    // Check 1: Prospect replied
    const reply = replies.find(r => r.prospect_domain === domain);
    if (reply) {
      interrupts.push({
        domain,
        touch_number: touch.touch_number,
        interrupt_type: 'prospect_reply',
        reason: 'Prospect replied. Sequence paused. Route to reply-handler pipeline.',
        reply_preview: (reply.body || '').substring(0, 200),
        reply_date: reply.date
      });
      interrupted = true;
    }

    // Check 2: Material company change
    if (!interrupted) {
      const companyChange = recentSignals.find(s =>
        s.company_domain === domain &&
        ['acquisition', 'layoff', 'executive_departure', 'product_launch', 'funding'].includes(s.signal_type)
      );
      if (companyChange) {
        interrupts.push({
          domain,
          touch_number: touch.touch_number,
          interrupt_type: 'material_change',
          reason: `Material company change detected: ${companyChange.signal_type}. Context for remaining touches has changed.`,
          signal: companyChange
        });
        interrupted = true;
      }
    }

    // Check 3: CRM status change
    if (!interrupted) {
      const statusChange = crmChanges.find(c => c.prospect_domain === domain);
      if (statusChange) {
        interrupts.push({
          domain,
          touch_number: touch.touch_number,
          interrupt_type: 'crm_status_change',
          reason: `CRM status changed to: ${statusChange.new_status}. ${getStatusChangeReason(statusChange.new_status)}`,
          old_status: statusChange.old_status,
          new_status: statusChange.new_status
        });
        interrupted = true;
      }
    }

    // Check 4: About to hit 5-touch max
    if (!interrupted && touch.touch_number === 5) {
      interrupts.push({
        domain,
        touch_number: touch.touch_number,
        interrupt_type: 'final_touch',
        reason: 'This is touch 5 of 5. After this touch, the sequence closes permanently. Surface to rep for final review.'
      });
      // Don't mark as interrupted — let it proceed but flag it
    }

    if (!interrupted) {
      cleared.push(touch);
    }
  }

  const result = {
    cleared_touches: cleared,
    interrupts,
    has_due_touches: cleared.length > 0,
    interrupted_count: interrupts.length,
    cleared_count: cleared.length,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
}

function getStatusChangeReason(status) {
  const reasons = {
    'current_customer': 'Continued outreach to a current customer requires explicit authorization.',
    'disqualified': 'Prospect has been disqualified. Outreach is inappropriate.',
    'competitor': 'Prospect identified as competitor. Outreach should not continue.',
    'do_not_contact': 'Contact marked as do-not-contact. Hard stop.',
    'closed_won': 'Deal closed. Sales outreach no longer appropriate.',
    'closed_lost': 'Deal closed-lost. Review before continuing outreach.'
  };
  return reasons[status] || 'Review new status before continuing.';
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
