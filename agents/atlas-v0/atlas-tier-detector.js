#!/usr/bin/env node
// atlas-tier-detector.js
//
// Reads user's isol8 LLM tier per L1 read-both pattern: fast-io preferred, env-var fallback.
// Logs which source resolved. Hard-fails build with link to isol8 settings page if neither
// resolves (per L5).

const { execSync } = require('child_process');

// Per-tier defaults (L3) — cheapest tier-appropriate model
const TIER_DEFAULTS = Object.freeze({
  byo_openai: {
    provider: 'openai',
    cheap_model: 'gpt-5-mini',
    premium_model: 'gpt-5',
    markup_multiplier: 1.0
  },
  byo_claude: {
    provider: 'anthropic',
    cheap_model: 'claude-haiku-4-5',
    premium_model: 'claude-sonnet-4-6', // opus only when reasoning depth genuinely requires it
    markup_multiplier: 1.0
  },
  hosted_claude: {
    provider: 'anthropic', // routed via isol8 proxy
    cheap_model: 'claude-haiku-4-5',
    premium_model: 'claude-sonnet-4-6',
    markup_multiplier: 1.5 // L4 line-item markup
  }
});

// L5 — hard-fail error message links to specific isol8 settings page where tier provisioning lives.
// URL set per L5 — verify against current isol8 frontend before deploying. Update if route changes.
const ISOL8_TIER_SETTINGS_URL = 'https://app.isol8.co/chat#settings/billing'; // TODO: verify against frontend route at deploy time

function readFastIoTier() {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action read --args-json '{"key":"isol8/user/tier"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    const parsed = JSON.parse(result);
    if (parsed && parsed.tier && TIER_DEFAULTS[parsed.tier]) {
      return { tier: parsed.tier, source: 'fast-io' };
    }
  } catch (e) { /* fall through to env-var fallback */ }
  return null;
}

function readEnvTier() {
  const t = process.env.ISOL8_USER_TIER;
  if (t && TIER_DEFAULTS[t]) {
    return { tier: t, source: 'env-var' };
  }
  return null;
}

function main() {
  // L1 read-both pattern: prefer fast-io, fall back to env var, log which source resolved
  const fromFastIo = readFastIoTier();
  const fromEnv = readEnvTier();
  const resolved = fromFastIo || fromEnv;

  if (!resolved) {
    // L5 hard-fail with settings link
    process.stdout.write(JSON.stringify({
      error: 'tier_not_resolved',
      message: `isol8 hasn't provisioned your LLM tier — visit ${ISOL8_TIER_SETTINGS_URL} to configure.`,
      checked_sources: ['fast-io: isol8/user/tier', 'env: ISOL8_USER_TIER'],
      settings_url: ISOL8_TIER_SETTINGS_URL,
      pass: false
    }));
    process.exit(1);
  }

  const defaults = TIER_DEFAULTS[resolved.tier];
  const out = {
    pass: true,
    tier: resolved.tier,
    source: resolved.source,
    provider: defaults.provider,
    cheap_model: defaults.cheap_model,
    premium_model: defaults.premium_model,
    markup_multiplier: defaults.markup_multiplier,
    resolved_at: new Date().toISOString(),
    note: `Tier resolved from ${resolved.source} on ${new Date().toISOString().slice(0, 10)}.`
  };

  process.stdout.write(JSON.stringify(out));
}

if (require.main === module) main();

module.exports = { TIER_DEFAULTS, readFastIoTier, readEnvTier };
