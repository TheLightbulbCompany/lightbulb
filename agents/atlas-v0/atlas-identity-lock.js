#!/usr/bin/env node
// atlas-identity-lock.js
//
// Locks new agent's name/role/scope at intake (Phase 1, H3 hard rule). Writes immutable
// identity record + SHA256 to atlas-config/identity-locks/{{build_id}}. Later edits
// require explicit user override + audit log entry.

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

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
  // Deterministic extraction from user_request. In production, the intake form has
  // structured fields (name, role, scope) that pre-populate these. Free-text fallback here.
  const trimmed = (userRequest || '').trim();
  // First word that looks like a name (capitalized, alphanum)
  const nameMatch = trimmed.match(/\b([A-Z][a-zA-Z0-9]{2,})\b/);
  const name = nameMatch ? nameMatch[1] : `agent-${buildId.slice(0, 8)}`;

  // Role and scope are vetted in Step 4; intake just captures the proposed strings.
  return {
    build_id: buildId,
    name: name,
    proposed_role: trimmed.slice(0, 200) || 'unspecified',
    proposed_scope: trimmed.slice(0, 1000) || 'unspecified',
    locked_at: new Date().toISOString()
  };
}

function sha256(s) {
  return crypto.createHash('sha256').update(s).digest('hex');
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.buildId) { console.error('--build-id required'); process.exit(2); }
  const userRequest = readStdin();

  const identity = deriveIdentity(userRequest, args.buildId);
  const identityJson = JSON.stringify(identity, Object.keys(identity).sort());
  const hash = sha256(identityJson);

  const out = {
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

module.exports = { deriveIdentity, sha256 };
