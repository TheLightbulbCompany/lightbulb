// Run with: node tests/launcher_process_topology.cjs
// Uses the pinned OpenClaw files from an installed bundle, never its app or state.
if (process.env.LIGHTBULB_TOPOLOGY_PRELOAD) {
  require('node:fs').appendFileSync(
    process.env.LIGHTBULB_TOPOLOGY_PRELOAD,
    `${process.pid} ${process.ppid}\n`,
  );
} else {
  const assert = require('node:assert/strict');
  const fs = require('node:fs');
  const os = require('node:os');
  const path = require('node:path');
  const { spawn, execFileSync } = require('node:child_process');
  const { once } = require('node:events');

  const triple = 'aarch64-apple-darwin';
  const assets = process.env.LIGHTBULB_OPENCLAW_ASSETS ||
    '/Applications/Lightbulb.app/Contents/Resources/bin';
  const node = path.join(assets, `node-${triple}`);
  const host = path.join(assets, `openclaw-host-${triple}`);
  assert.equal(JSON.parse(fs.readFileSync(path.join(host, 'node_modules/openclaw/package.json'))).version, '2026.8.1');

  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'lightbulb-launcher-'));
  const bin = path.join(temp, 'bin');
  const log = path.join(temp, 'pids');
  fs.mkdirSync(bin);
  fs.mkdirSync(path.join(temp, 'home'));
  fs.mkdirSync(path.join(temp, 'state'));
  fs.symlinkSync(node, path.join(bin, `node-${triple}`));
  fs.symlinkSync(host, path.join(bin, `openclaw-host-${triple}`));

  const vendor = path.resolve(__dirname, '../scripts/vendor-sidecars.sh');
  const launcherFunction = execFileSync('sed', ['-n', '/^write_launcher() {/,/^}/p', vendor], { encoding: 'utf8' });
  execFileSync('bash', ['-c', `${launcherFunction}\nwrite_launcher '${triple}'`], {
    env: { ...process.env, BIN_DIR: bin },
  });

  const child = spawn(path.join(bin, `lightbulb-browser-service-${triple}`), [], {
    env: {
      ...process.env,
      HOME: path.join(temp, 'home'),
      OPENCLAW_STATE_DIR: path.join(temp, 'state'),
      OPENCLAW_CONFIG_PATH: path.join(temp, 'state/openclaw.json'),
      OPENCLAW_GATEWAY_TOKEN: 'topology-test',
      LIGHTBULB_PROXY_PORT: '1',
      LIGHTBULB_TOPOLOGY_PRELOAD: log,
      NODE_OPTIONS: `--require=${__filename}`,
    },
    stdio: 'ignore',
  });

  const pids = () => fs.existsSync(log)
    ? fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map(line => line.split(' ').map(Number))
    : [];
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  (async () => {
    try {
      // Give the real CLI time to settle; its warning-suppression respawn happens at startup.
      for (let i = 0; i < 100 && pids().length < 2 && child.exitCode === null; i++) {
        await delay(100);
      }
      const seen = pids();
      assert.equal(child.exitCode, null, 'OpenClaw exited before topology could be observed');
      assert.deepEqual(seen, [[child.pid, process.pid]],
        `expected the launched Node to run directly, observed PID/PPID pairs ${JSON.stringify(seen)}`);
    } finally {
      for (const [pid] of pids().reverse()) {
        try { process.kill(pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      }
      try { child.kill('SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
      if (child.exitCode === null && child.signalCode === null) await once(child, 'exit');
      for (const [pid] of pids()) {
        assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' }, `orphaned Node PID ${pid}`);
      }
      fs.rmSync(temp, { recursive: true, force: true });
    }
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
