#!/usr/bin/env node
// atlas-failure-case-router.js
//
// Reads atlas-cookbook/failure-cases.json (refreshed twice-weekly), filters by archetype tag,
// returns top-N relevant cases. Per H4: refuses to inject any case whose source URL isn't
// marked `verified` in the corpus entry.

const fs = require('fs');
const { execSync } = require('child_process');

function parseArgs(argv) {
  const args = { action: 'route', maxCases: 6 };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a.startsWith('--max-cases=')) args.maxCases = parseInt(a.split('=')[1], 10);
    else if (a === '--refresh') args.action = 'refresh';
  }
  return args;
}

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function loadCorpus() {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action read --args-json '{"key":"atlas-cookbook/failure-cases.json"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) {
    return { version: 1, cases: [] };
  }
}

function filterByArchetype(cases, archetype, maxCases) {
  // H4 enforcement: refuse cases without `verified: true` on their source URL
  const verified = cases.filter(c => c.source && c.source.verified === true);
  const scored = verified.map(c => {
    let score = 0;
    if (Array.isArray(c.applicable_archetypes) && c.applicable_archetypes.includes(archetype)) score += 10;
    if (Array.isArray(c.applicable_archetypes) && c.applicable_archetypes.includes('all')) score += 3;
    return { ...c, _score: score };
  });
  scored.sort((a, b) => b._score - a._score);
  return scored.slice(0, maxCases).map(({ _score, ...c }) => c);
}

function refreshCorpus() {
  const corpus = loadCorpus();
  const verifiedCount = (corpus.cases || []).filter(c => c.source && c.source.verified).length;
  const unverifiedCount = (corpus.cases || []).length - verifiedCount;
  return {
    refreshed_at: new Date().toISOString(),
    case_count: (corpus.cases || []).length,
    verified_count: verifiedCount,
    unverified_count: unverifiedCount,
    note: 'Real refresh re-runs verification; stubbed here. See atlas-citation-verifier.js for citation re-verification logic.'
  };
}

function main() {
  const args = parseArgs(process.argv);
  if (args.action === 'refresh') {
    process.stdout.write(JSON.stringify(refreshCorpus()));
    return;
  }
  const intake = readStdin();
  const archetype = intake.proposed_archetype || 'unknown';
  const corpus = loadCorpus();
  const filtered = filterByArchetype(corpus.cases || [], archetype, args.maxCases);
  process.stdout.write(JSON.stringify({
    archetype: archetype,
    relevant_cases: filtered,
    relevant_count: filtered.length,
    total_corpus_size: (corpus.cases || []).length,
    note: 'Only verified-source cases included per H4.'
  }));
}

if (require.main === module) main();

module.exports = { filterByArchetype };
