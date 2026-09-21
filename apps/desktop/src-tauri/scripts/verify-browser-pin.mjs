import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

// These files own the proxy envelope, routes and extension target leases we adapt.
export async function verifyBrowserPin(packageRoot) {
  const pkg = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'));
  if (pkg.version !== '2026.8.1') throw new Error('Unsupported OpenClaw browser version');
  for (const [file, expected] of Object.entries({
    'plugin-service-DNzntOkw.js': '41dcfce80cf58e1782aa791eb12cccc23ab108cfa069f80086b5c693d92c3c66',
    'routes-qcch-5X9.js': '09fddc304d56b6f784970b9bfbcd0e7ffd45f899320e004bcedd7e059453c0a4',
    'relay-lifecycle-YMt1OqCQ.js': 'cb213f1b80de5b6069224f472081ba45a69286540ba4b4ead6ac27efec529dcc',
  })) {
    const digest = createHash('sha256').update(await readFile(join(packageRoot, 'dist', file))).digest('hex');
    if (digest !== expected) throw new Error(`Unverified browser protocol artifact: ${file}`);
  }
}

if (process.argv[1]?.endsWith('/verify-browser-pin.mjs')) await verifyBrowserPin(process.argv[2]);
