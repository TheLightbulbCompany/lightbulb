// In-image smoke for quickbooks-mcp/brokered-token.patch (spec 13 T8, lane V15). Runs in the
// Dockerfile right after the server builds, against the compiled dist:
//
//   node quickbooks_brokered_smoke.mjs [/opt/quickbooks-mcp/dist]
//
// Proves, with every HTTP primitive replaced by a throw: a store holding only
// QUICKBOOKS_ACCESS_TOKEN (no refresh token, no client secret) starts the server and seeds the
// client; an unchanged store at expiry makes refreshAccessToken() fail with a retryable
// "expired — retry", never an OAuth flow or an Intuit call; a rewritten store is adopted on the
// next getInstance(); and the server never writes the store.
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const dist = process.argv[2] || "/opt/quickbooks-mcp/dist";
const deny = (what) => () => {
  throw new Error(`brokered smoke: ${what} attempted`);
};
for (const mod of [http, https]) {
  mod.request = deny("network request");
  mod.get = deny("network get");
  mod.createServer = deny("oauth callback server");
}
globalThis.fetch = deny("fetch");

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qbo-brokered-smoke-"));
const store = path.join(dir, ".env");
const write = (token, expiresAt) =>
  fs.writeFileSync(
    store,
    [
      "QUICKBOOKS_CLIENT_ID=lightbulb-smoke",
      "QUICKBOOKS_REALM_ID=123456789",
      "QUICKBOOKS_ENVIRONMENT=production",
      `QUICKBOOKS_ACCESS_TOKEN=${token}`,
      ...(expiresAt ? [`QUICKBOOKS_ACCESS_TOKEN_EXPIRES_AT=${expiresAt}`] : []),
      "",
    ].join("\n"),
  );
const assert = (cond, msg) => {
  if (!cond) {
    console.error(`brokered smoke FAILED: ${msg}`);
    process.exit(1);
  }
};

// 1. An expiring token (inside the 5 min buffer) and no refresh token: the client starts, and a
//    refresh neither calls Intuit nor opens the OAuth flow -- it says retry.
write("T1", Math.floor(Date.now() / 1000) + 60);
process.env.QUICKBOOKS_TOKEN_STORE_PATH = store;
delete process.env.QUICKBOOKS_CLIENT_SECRET;
delete process.env.QUICKBOOKS_REFRESH_TOKEN;
const before = fs.readFileSync(store, "utf8");
const { QuickbooksClient, quickbooksClient } = await import(
  pathToFileURL(path.join(dist, "clients", "quickbooks-client.js")).href
);
let retryable = false;
try {
  await quickbooksClient.refreshAccessToken();
} catch (err) {
  retryable = /retry/i.test(String(err && err.message));
  assert(!/attempted/.test(String(err && err.message)), `brokered refresh reached the network: ${err.message}`);
}
assert(retryable, "an unchanged expiring store must fail refreshAccessToken() with a retryable error");
let instanceErr = null;
try {
  await QuickbooksClient.getInstance();
} catch (err) {
  instanceErr = err;
}
assert(instanceErr && /retry/i.test(instanceErr.message), "getInstance() on an expiring store must say retry");

// 2. The host rewrites the store (its turn renewer): the next call adopts T2 with no network.
write("T2", Math.floor(Date.now() / 1000) + 3600);
const written = fs.readFileSync(store, "utf8");
const qbo = await QuickbooksClient.getInstance();
assert(qbo && qbo.token === "T2", `instance must carry the rewritten token, got ${qbo && qbo.token}`);
const creds = await QuickbooksClient.getAuthCredentials();
assert(creds.accessToken === "T2" && creds.realmId === "123456789", "getAuthCredentials must vend the store's token");

// 3. A fresh token with no recorded expiry is used as is (the host's 10 min window keeps it fresh).
write("T3");
const qbo3 = await QuickbooksClient.getInstance();
assert(qbo3.token === "T3", "a store without an expiry must still be adopted");

// 3b. A host that wrote the expiry in MILLISECONDS (a bridge bug, not the contract) is read as
//     seconds: the token still expires within its hour, not in the year 57000.
write("T3ms", (Math.floor(Date.now() / 1000) + 3600) * 1000);
const ms = await quickbooksClient.refreshAccessToken();
assert(ms.access_token === "T3ms" && ms.expires_in > 0 && ms.expires_in <= 3600, `a ms expiry must be read as seconds, got expires_in=${ms.expires_in}`);
write("T3");
assert((await QuickbooksClient.getInstance()).token === "T3", "back to the seconds contract");

// 4. The server never writes the store.
assert(fs.readFileSync(store, "utf8") === fs.readFileSync(store, "utf8") && !before.includes("T2"), "sanity");
assert(fs.readFileSync(store, "utf8").includes("QUICKBOOKS_ACCESS_TOKEN=T3"), "the store must hold exactly what the host wrote");
assert(!fs.readFileSync(store, "utf8").includes("QUICKBOOKS_REFRESH_TOKEN"), "the server must not add a refresh token line");
void written;

fs.rmSync(dir, { recursive: true, force: true });
console.log("quickbooks brokered ok");
