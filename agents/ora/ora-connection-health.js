#!/usr/bin/env node
/**
 * ora-connection-health.js
 * Requirement 46: Alert if calendar integration loses access.
 *
 * Deterministic API health checks. Zero LLM.
 */

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const connections = input.connections || [];
  const results = [];

  for (const conn of connections) {
    const result = {
      platform: conn.platform,
      connection_type: conn.type, // caldav, ms365, calctl
      status: 'unknown',
      checked_at: new Date().toISOString()
    };

    // Check API response
    if (conn.api_test) {
      if (conn.api_test.success) {
        result.status = conn.api_test.latency_ms > 5000 ? 'degraded' : 'active';
        if (conn.api_test.latency_ms > 5000) {
          result.issue = `API responding slowly (${conn.api_test.latency_ms}ms). Calendar operations may be delayed.`;
        }
      } else {
        result.status = 'disconnected';
        result.issue = conn.api_test.error || 'API returned an error.';
        result.remediation = getRemediation(conn.type, conn.api_test.error_code);
      }
    } else {
      result.status = 'untested';
      result.issue = 'No API test result available.';
    }

    // Check webhook subscription
    if (conn.webhook_status) {
      result.webhook_active = conn.webhook_status.active;
      result.webhook_expires = conn.webhook_status.expires_at;

      // Google push notifications expire — check if renewal needed
      if (conn.webhook_status.expires_at) {
        const expiresIn = new Date(conn.webhook_status.expires_at) - Date.now();
        const hoursUntilExpiry = expiresIn / (1000 * 60 * 60);
        if (hoursUntilExpiry < 6) {
          result.webhook_warning = `Webhook subscription expires in ${Math.round(hoursUntilExpiry)} hours. Renew to maintain real-time sync.`;
          result.needs_webhook_renewal = true;
        }
      }

      if (!conn.webhook_status.active) {
        result.webhook_warning = 'Webhook is inactive. Ora is falling back to polling, which risks stale data and race conditions.';
      }
    }

    // Check OAuth token
    if (conn.token_status) {
      result.token_valid = conn.token_status.valid;
      if (!conn.token_status.valid) {
        result.status = 'auth_expired';
        result.issue = 'OAuth token has expired. Re-authenticate to restore calendar access.';
        result.remediation = `Re-authenticate your ${conn.platform} calendar connection.`;
      }
    }

    results.push(result);
  }

  const disconnected = results.filter(r => r.status === 'disconnected' || r.status === 'auth_expired');
  const degraded = results.filter(r => r.status === 'degraded');
  const active = results.filter(r => r.status === 'active');

  const overallStatus = disconnected.length > 0 ? 'red' :
                        degraded.length > 0 ? 'yellow' : 'green';

  const result = {
    overall_status: overallStatus,
    emoji: { green: '🟢', yellow: '🟡', red: '🔴' }[overallStatus],
    alert: overallStatus === 'red',
    connections: results,
    active_count: active.length,
    degraded_count: degraded.length,
    disconnected_count: disconnected.length,
    webhook_renewals_needed: results.filter(r => r.needs_webhook_renewal).length,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  // A red/disconnected health status is a normal finding, not a crash — the
  // decision is carried in overall_status/alert; exit 0 so the workflow gates on JSON.
  process.exit(0);
}

function getRemediation(type, errorCode) {
  if (errorCode === 401 || errorCode === 403) {
    return 'OAuth credentials expired or revoked. Re-authenticate through the calendar connection settings.';
  }
  if (errorCode === 404) {
    return 'Calendar endpoint not found. The calendar may have been deleted or the API URL may have changed.';
  }
  if (type === 'caldav') return 'Check CalDAV server URL, credentials, and vdirsyncer configuration.';
  if (type === 'ms365') return 'Check Azure AD app registration, Graph API permissions, and client secret expiry.';
  if (type === 'calctl') return 'Verify icalBuddy and AppleScript access on macOS.';
  return 'Check connection settings and re-authenticate if needed.';
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
