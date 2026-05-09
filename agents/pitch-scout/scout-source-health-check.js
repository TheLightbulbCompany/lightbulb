#!/usr/bin/env node
/**
 * scout-source-health-check.js
 * Requirement 47: Flag if any signal source produces zero leads in 14 days.
 *
 * Deterministic date comparison. Zero LLM.
 */

const SILENCE_THRESHOLD_DAYS = 14;

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const signalHistory = input.signal_history || [];
  const configuredSources = input.configured_sources || [];
  const now = new Date();

  // Group signals by source
  const lastSignalBySource = {};
  for (const signal of signalHistory) {
    const source = signal.source || signal.signal_source;
    const date = new Date(signal.timestamp || signal.date);
    if (!lastSignalBySource[source] || date > lastSignalBySource[source]) {
      lastSignalBySource[source] = date;
    }
  }

  const alerts = [];
  const healthy = [];

  for (const source of configuredSources) {
    const lastSignal = lastSignalBySource[source];

    if (!lastSignal) {
      alerts.push({
        source,
        status: 'never_produced',
        days_silent: null,
        diagnosis: 'This source has never produced a signal. Check API key, query configuration, and connectivity.',
        recommendation: 'Verify API credentials and test the source manually.'
      });
      continue;
    }

    const daysSilent = Math.floor((now - lastSignal) / (1000 * 60 * 60 * 24));

    if (daysSilent >= SILENCE_THRESHOLD_DAYS) {
      alerts.push({
        source,
        status: 'silent',
        days_silent: daysSilent,
        last_signal: lastSignal.toISOString(),
        diagnosis: getDiagnosis(source, daysSilent),
        recommendation: getRecommendation(source)
      });
    } else {
      healthy.push({
        source,
        status: 'active',
        days_since_last: daysSilent,
        last_signal: lastSignal.toISOString()
      });
    }
  }

  const result = {
    has_alerts: alerts.length > 0,
    alerts,
    healthy,
    total_sources: configuredSources.length,
    silent_sources: alerts.length,
    active_sources: healthy.length,
    threshold_days: SILENCE_THRESHOLD_DAYS,
    timestamp: now.toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(alerts.length > 0 ? 1 : 0);
}

function getDiagnosis(source, daysSilent) {
  const diagnoses = {
    apollo_funding: `No funding signals in ${daysSilent} days. Possible causes: Apollo API key expired, query keywords too narrow, or ICP doesn't match funded companies.`,
    bombora: `No intent signals in ${daysSilent} days. Possible causes: API key expired, topic keywords don't match Bombora's taxonomy, or ICP companies aren't researching this category.`,
    '6sense': `No account-level signals in ${daysSilent} days. Possible causes: API access expired or ICP accounts aren't showing buying behavior.`,
    perplexity: `No news/PR signals in ${daysSilent} days. Possible causes: search queries too narrow, or ICP vertical has low news volume.`,
    builtwith: `No technographic changes in ${daysSilent} days. This may be normal — tech stack changes are infrequent.`,
    leadfeeder: `No website visitors identified in ${daysSilent} days. Possible causes: tracking script not installed, low website traffic, or API key expired.`,
    clearbit_reveal: `No website visitors in ${daysSilent} days. Same causes as Leadfeeder.`
  };
  return diagnoses[source] || `Source ${source} silent for ${daysSilent} days. Check API configuration and query parameters.`;
}

function getRecommendation(source) {
  return `1) Verify API key is valid and not expired. 2) Test the source manually with a broad query. 3) Review ICP criteria — they may be too narrow for this source. 4) Check if the source requires a paid subscription renewal.`;
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
