#!/usr/bin/env node
/**
 * vera-ticket-closer.js
 * Requirement 44, 55: Never close until confirmed or 48 hours pass.
 *
 * Runs hourly on cron. Checks pending_confirmation tickets.
 * Deterministic keyword matching for confirmation detection.
 * Zero LLM.
 */

const CONFIRMATION_KEYWORDS = [
  'yes', 'yeah', 'yep', 'yup', 'correct', 'right',
  'thanks', 'thank you', 'thx', 'ty',
  'that worked', 'that fixed it', 'working now', 'fixed',
  'perfect', 'great', 'awesome', 'excellent',
  'all set', 'all good', 'all sorted', 'sorted',
  'resolved', 'solved', 'done', 'good to go',
  'exactly what i needed', 'that did it', 'problem solved'
];

const NEGATIVE_KEYWORDS = [
  'no', 'nope', 'not', 'didn\'t work', 'still broken',
  'that didn\'t help', 'same issue', 'still happening',
  'wrong', 'incorrect', 'not what i asked', 'not resolved',
  'still need help', 'doesn\'t work', 'failed'
];

const AUTO_CLOSE_HOURS = 48;

function checkConfirmation(message) {
  const lower = (message || '').toLowerCase().trim();

  // Check negative first — takes priority
  for (const kw of NEGATIVE_KEYWORDS) {
    if (lower.includes(kw)) {
      return { confirmed: false, reopened: true, reason: 'negative_response', matched: kw };
    }
  }

  // Check positive
  for (const kw of CONFIRMATION_KEYWORDS) {
    if (lower.includes(kw)) {
      return { confirmed: true, reopened: false, reason: 'positive_response', matched: kw };
    }
  }

  // Ambiguous — route to agent loop for contextual interpretation
  // "ok" might mean "resolved" or "I give up." The agent loop reads
  // the full conversation and determines which.
  return {
    confirmed: false,
    reopened: false,
    reason: 'ambiguous',
    matched: null,
    needs_agent_loop: true,
    agent_loop_context: 'Customer response is ambiguous. Read the full conversation to determine if the customer sounds satisfied (close) or is giving up / still frustrated (reopen with acknowledgment).'
  };
}

function checkAutoClose(ticket) {
  const pendingSince = new Date(ticket.pending_since || ticket.resolved_at);
  const hoursPending = (Date.now() - pendingSince.getTime()) / (1000 * 60 * 60);

  if (hoursPending >= AUTO_CLOSE_HOURS) {
    return {
      auto_close: true,
      hours_pending: Math.round(hoursPending),
      reason: `No response for ${Math.round(hoursPending)} hours. Auto-closing per configured policy.`
    };
  }

  return {
    auto_close: false,
    hours_pending: Math.round(hoursPending),
    hours_remaining: Math.round(AUTO_CLOSE_HOURS - hoursPending)
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const tickets = input.tickets || [input.ticket || input];
  const results = [];

  for (const ticket of tickets) {
    const ticketResult = {
      ticket_id: ticket.ticket_id || ticket.id,
      customer_domain: ticket.customer_domain || ticket.customer_email,
      current_status: ticket.status
    };

    // If customer sent a new message, check for confirmation
    if (ticket.latest_customer_message) {
      const confirmation = checkConfirmation(ticket.latest_customer_message);
      ticketResult.confirmation = confirmation;

      if (confirmation.confirmed) {
        ticketResult.action = 'close';
        ticketResult.new_status = 'resolved';
        ticketResult.close_reason = 'customer_confirmed';
      } else if (confirmation.reopened) {
        ticketResult.action = 'reopen';
        ticketResult.new_status = 'open';
        ticketResult.reopen_reason = 'customer_indicated_unresolved';
      } else {
        ticketResult.action = confirmation.needs_agent_loop ? 'route_to_agent_loop' : 'keep_pending';
        ticketResult.new_status = 'pending_confirmation';
        ticketResult.needs_agent_loop = confirmation.needs_agent_loop || false;
        ticketResult.agent_loop_context = confirmation.agent_loop_context || null;
      }
    } else {
      // No new message — check auto-close timer
      const autoClose = checkAutoClose(ticket);
      ticketResult.auto_close_check = autoClose;

      if (autoClose.auto_close) {
        ticketResult.action = 'close';
        ticketResult.new_status = 'resolved';
        ticketResult.close_reason = 'auto_close_48h';
      } else {
        ticketResult.action = 'keep_pending';
        ticketResult.new_status = 'pending_confirmation';
      }
    }

    ticketResult.timestamp = new Date().toISOString();
    results.push(ticketResult);
  }

  const output = {
    results,
    closed: results.filter(r => r.action === 'close').length,
    reopened: results.filter(r => r.action === 'reopen').length,
    pending: results.filter(r => r.action === 'keep_pending').length,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(output));
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

module.exports = { checkConfirmation, checkAutoClose };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
