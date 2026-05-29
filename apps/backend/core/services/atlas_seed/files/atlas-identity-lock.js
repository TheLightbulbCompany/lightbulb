#!/usr/bin/env node
// atlas-identity-lock.js
//
// Locks new agent's name/role/scope at intake (Step 1, H3 hard rule). Writes immutable
// identity record + SHA256 to atlas-config/identity-locks/{{build_id}}. Refuses reserved
// names per ATLAS1 regex.

const crypto = require('crypto');
const fs = require('fs');

// ============================================================================
// ATLAS1 RESERVED NAMES PATTERN — frozen. Three documented properties:
// 1. Anchored (^...$): only exact matches reserved
// 2. Optional version suffix (-v\d+)?: covers atlas, atlas-v0, atlas-v1, atlas-v2, ...
// 3. Case-insensitive (/i): covers Atlas, ATLAS, atlas-V1
// Self-host suggested name pattern: atlas-v\d+-selfhost-YYYY-MM-DD (distinct from reserved)
// ============================================================================
const RESERVED_NAMES_PATTERN = /^atlas(-v\d+)?$/i;

function parseArgs(argv) {
  const args = { buildId: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
  }
  return args;
}

function readStdin() {
  try { return fs.readFileSync(0, 'utf8').trim(); } catch (e) { return ''; }
}

function deriveIdentity(userRequest, buildId) {
  const trimmed = (userRequest || '').trim();
  const nameMatch = trimmed.match(/\b([A-Z][a-zA-Z0-9]{2,})\b/);
  const name = nameMatch ? nameMatch[1] : `agent-${buildId.slice(0, 8)}`;

  return {
    build_id: buildId,
    name: name,
    proposed_role: trimmed.slice(0, 200) || 'unspecified',
    proposed_scope: trimmed.slice(0, 1000) || 'unspecified',
    locked_at: new Date().toISOString()
  };
}

function checkReserved(name) {
  if (RESERVED_NAMES_PATTERN.test(name)) {
    return {
      reserved: true,
      message: `Atlas refuses to overwrite itself. Proposed name "${name}" matches the reserved pattern (atlas, atlas-v0, atlas-v1, atlas-v2, ...). Choose a different name (suggested: "${name}-${new Date().toISOString().slice(0, 10)}" or "${name}-v2"). For self-host builds, use atlas-v{N}-selfhost-YYYY-MM-DD.`
    };
  }
  return { reserved: false };
}

function sha256(s) {
  return crypto.createHash('sha256').update(s).digest('hex');
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.buildId) { console.error('--build-id required'); process.exit(2); }
  const userRequest = readStdin();

  const identity = deriveIdentity(userRequest, args.buildId);

  const reservedCheck = checkReserved(identity.name);
  if (reservedCheck.reserved) {
    process.stdout.write(JSON.stringify({
      pass: false,
      error: 'reserved_name',
      message: reservedCheck.message,
      proposed_name: identity.name
    }));
    process.exit(1);
  }

  const identityJson = JSON.stringify(identity, Object.keys(identity).sort());
  const hash = sha256(identityJson);

  const out = {
    pass: true,
    ...identity,
    sha256: hash,
    fast_io_writes: [
      { key: `atlas-config/identity-locks/${args.buildId}`, data: identity },
      { key: `atlas-config/identity-locks/${args.buildId}.sha256`, data: hash }
    ]
  };

  process.stdout.write(JSON.stringify(out));
}

if (require.main === module) main();

module.exports = { RESERVED_NAMES_PATTERN, deriveIdentity, checkReserved, sha256 };
