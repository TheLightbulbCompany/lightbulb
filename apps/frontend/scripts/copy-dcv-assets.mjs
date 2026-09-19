// The NICE DCV web client is BUNDLED inside the `bedrock-agentcore` npm package
// (not a peer dep, not CDN-hosted). `BrowserLiveView` hands DCV a hardcoded
// same-origin `baseUrl` of `/nice-dcv-web-client-sdk/dcvjs-esm`, from which DCV
// fetches its own decoder workers and wasm at connect time — so that tree has to
// sit under `public/` or the live view connects and paints nothing.
//
// Build output, not source: gitignored, and re-copied by `predev`/`prebuild` so
// a dependency bump can never leave a stale worker behind.
import { cp, rm } from "node:fs/promises";
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
console.log(`copied DCV web client SDK -> ${to}`);
