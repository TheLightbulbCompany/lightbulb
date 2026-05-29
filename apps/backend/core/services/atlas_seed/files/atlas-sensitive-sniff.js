#!/usr/bin/env node
// atlas-sensitive-sniff.js
//
// Regex + entropy scan for likely API keys, OAuth tokens, env-var values, hardcoded user
// identifiers. Returns warnings (not blocks). Per P3, audit-logged dismissals include
// structured {type, line, matched_pattern} per warning.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const os = require('os');

const PATTERNS = Object.freeze([
  { type: 'openai_key', regex: /\bsk-[a-zA-Z0-9]{20,}/g },
  { type: 'openai_proj_key', regex: /\bsk-proj-[a-zA-Z0-9_-]{20,}/g },
  { type: 'anthropic_key', regex: /\bsk-ant-[a-zA-Z0-9_-]{20,}/g },
  { type: 'github_token', regex: /\bghp_[a-zA-Z0-9]{36,}/g },
  { type: 'github_oauth', regex: /\bgho_[a-zA-Z0-9]{36,}/g },
  { type: 'aws_access_key', regex: /\bAKIA[0-9A-Z]{16}\b/g },
  { type: 'slack_bot_token', regex: /\bxoxb-[a-zA-Z0-9-]{20,}/g },
  { type: 'slack_user_token', regex: /\bxoxp-[a-zA-Z0-9-]{20,}/g },
  { type: 'stripe_secret', regex: /\bsk_(live|test)_[a-zA-Z0-9]{20,}/g },
  { type: 'jwt', regex: /\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/g },
  { type: 'env_value_not_placeholder', regex: /^[A-Z_]+=(?!\$\{)(?!"<)[a-zA-Z0-9+/=_-]{16,}/gm },
  { type: 'hardcoded_email', regex: /\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b/g }
]);

function shannonEntropy(s) {
  const freq = {};
  for (const c of s) freq[c] = (freq[c] || 0) + 1;
  let h = 0;
  for (const c in freq) {
    const p = freq[c] / s.length;
    h -= p * Math.log2(p);
  }
  return h;
}

function parseArgs(argv) {
  const args = { mode: 'build', buildId: null, agentName: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--mode=')) args.mode = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-name=')) args.agentName = a.split('=')[1];
  }
  return args;
}

function scanFile(filepath) {
  if (!fs.existsSync(filepath)) return [];
  const content = fs.readFileSync(filepath, 'utf8');
  const lines = content.split('\n');
  const warnings = [];

  for (const { type, regex } of PATTERNS) {
    let m;
    const r = new RegExp(regex.source, regex.flags);
    while ((m = r.exec(content)) !== null) {
      const lineNum = content.slice(0, m.index).split('\n').length;
      const lineText = lines[lineNum - 1] || '';
      if (/\$\{[A-Z_]+\}/.test(lineText) && !PATTERNS.find(p => p.type === type).regex.source.includes('env_value')) {
        continue;
      }
      warnings.push({
        type: type,
        line: lineNum,
        matched_pattern: m[0].length > 60 ? m[0].slice(0, 30) + '...REDACTED' : m[0],
        file: path.basename(filepath)
      });
    }
  }

  const longStrings = content.match(/[a-zA-Z0-9+/=_-]{32,}/g) || [];
  for (const s of longStrings) {
    if (shannonEntropy(s) > 4.5 && !s.startsWith('${') && !warnings.some(w => w.matched_pattern.includes(s.slice(0, 20)))) {
      const idx = content.indexOf(s);
      const lineNum = content.slice(0, idx).split('\n').length;
      warnings.push({
        type: 'high_entropy_string',
        line: lineNum,
        matched_pattern: s.slice(0, 30) + '...REDACTED',
        file: path.basename(filepath)
      });
    }
  }
  return warnings;
}

function computeBundleSha256(bundlePath) {
  const hash = crypto.createHash('sha256');
  if (!fs.existsSync(bundlePath)) return null;
  const files = fs.readdirSync(bundlePath, { withFileTypes: true })
    .filter(d => d.isFile())
    .map(d => path.join(bundlePath, d.name))
    .sort();
  for (const f of files) {
    hash.update(path.basename(f));
    hash.update(fs.readFileSync(f));
  }
  return hash.digest('hex');
}

function main() {
  const args = parseArgs(process.argv);

  let bundlePath;
  if (args.mode === 'build') {
    if (!args.buildId) { console.error('--build-id required'); process.exit(2); }
    bundlePath = path.join(os.homedir(), '.openclaw', 'agents', `.atlas-staging-${args.buildId}`);
  } else {
    if (!args.agentName) { console.error('--agent-name required'); process.exit(2); }
    bundlePath = path.join(os.homedir(), '.openclaw', 'agents', args.agentName);
  }

  if (!fs.existsSync(bundlePath)) {
    process.stdout.write(JSON.stringify({ error: `bundle path missing: ${bundlePath}`, warnings: [], warning_count: 0 }));
    process.exit(0);
  }

  const allWarnings = [];
  const fileList = [];
  const files = fs.readdirSync(bundlePath, { withFileTypes: true })
    .filter(d => d.isFile())
    .map(d => path.join(bundlePath, d.name));

  for (const f of files) {
    fileList.push(path.relative(bundlePath, f));
    if (/\.(json|md|lobster|js)$/.test(f)) {
      allWarnings.push(...scanFile(f));
    }
  }

  const bundleSha256 = computeBundleSha256(bundlePath);

  process.stdout.write(JSON.stringify({
    pass: true,
    warnings: allWarnings,
    warning_count: allWarnings.length,
    bundle_path: bundlePath,
    bundle_file_list: fileList,
    bundle_sha256: bundleSha256
  }));
}

if (require.main === module) main();

module.exports = { PATTERNS, scanFile, computeBundleSha256 };
