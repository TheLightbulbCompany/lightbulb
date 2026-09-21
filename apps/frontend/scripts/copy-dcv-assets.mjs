// The NICE DCV web client is BUNDLED inside the `bedrock-agentcore` npm package
// (not a peer dep, not CDN-hosted). `BrowserLiveView` hands DCV a hardcoded
// same-origin `baseUrl` of `/nice-dcv-web-client-sdk/dcvjs-esm`, from which DCV
// fetches its own decoder workers and wasm at connect time — so that tree has to
// sit under `public/` or the live view connects and paints nothing.
//
// Build output, not source: gitignored, and re-copied by `predev`/`prebuild` so
// a dependency bump can never leave a stale worker behind.
import { cp, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
// A deep path into the package, deliberately: its `exports` map publishes no
// subpath for the SDK tree, so `import.meta.resolve` cannot reach it.
const from = join(
  root,
  "node_modules/bedrock-agentcore/dist/src/tools/browser/live-view/nice-dcv-web-client-sdk",
);
const to = join(root, "public/nice-dcv-web-client-sdk");

await rm(to, { recursive: true, force: true });
await cp(from, to, { recursive: true });
// The SDK bundled with bedrock-agentcore 0.4.4 mistakes Tauri's generic `ipc` handler for its native media
// redirection client, then requests microphone access during module evaluation.
// Keep the native DCV client path intact; only exclude the unrelated Tauri bridge.
// Patch generated copies, never node_modules. Fail on SDK drift for re-audit.
const nativeBridge = "window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.ipc";
for (const format of ["dcvjs-esm", "dcvjs-umd"]) {
  const file = join(to, format, "dcv.js");
  const source = await readFile(file, "utf8");
  if (source.split(nativeBridge).length !== 2) {
    throw new Error(`DCV ${format} native bridge detection changed; review the Tauri compatibility guard.`);
  }
  await writeFile(file, source.replace(nativeBridge, `!window.__TAURI_INTERNALS__&&${nativeBridge}`));
}
console.log(`copied DCV web client SDK -> ${to}`);
