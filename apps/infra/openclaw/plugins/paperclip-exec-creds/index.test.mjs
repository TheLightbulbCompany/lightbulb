// node --test coverage for the paperclip-exec-creds resolver.
//
// Imports the pure resolver from ./dist/resolve.js (not ./dist/index.js, which
// imports the OpenClaw SDK that only resolves inside the container image).
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { resolve, KEY_FILENAME } from "./dist/resolve.js";

const AGENT = "agent_11111111-2222-3333-4444-555555555555";
const FULL = {
  token: "pcp_deadbeef",
  agentId: "62749da9-5bca-4f41-a001-e4ae31d178af",
  companyId: "6ad4c23a-e709-401f-90b5-3b45e10f543f",
  apiUrl: "http://paperclip.isol8-dev.local:3100",
};
const EXPECTED = {
  PAPERCLIP_API_KEY: FULL.token,
  PAPERCLIP_AGENT_ID: FULL.agentId,
  PAPERCLIP_COMPANY_ID: FULL.companyId,
  PAPERCLIP_API_URL: FULL.apiUrl,
};

function withRoot(fn) {
  const dir = mkdtempSync(join(tmpdir(), "paperclip-exec-creds-"));
  try {
    return fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function seed(root, agentId, body) {
  const ws = join(root, `workspace-${agentId}`);
  mkdirSync(ws, { recursive: true });
  writeFileSync(join(ws, KEY_FILENAME), typeof body === "string" ? body : JSON.stringify(body));
}

test("returns all four vars for the agent's own key file", () => {
  withRoot((root) => {
    seed(root, AGENT, FULL);
    assert.deepEqual(resolve(AGENT, root), EXPECTED);
  });
});

test("selects PER AGENT — one container hosts many agents with different keys", () => {
  withRoot((root) => {
    seed(root, "agent_aaa", { ...FULL, token: "pcp_aaa" });
    seed(root, "agent_bbb", { ...FULL, token: "pcp_bbb" });
    assert.equal(resolve("agent_aaa", root).PAPERCLIP_API_KEY, "pcp_aaa");
    assert.equal(resolve("agent_bbb", root).PAPERCLIP_API_KEY, "pcp_bbb");
  });
});

test("returns {} when the agent has no key file (never claimed / non-Paperclip agent)", () => {
  withRoot((root) => {
    assert.deepEqual(resolve(AGENT, root), {});
  });
});

test("returns {} when there is no agentId on the hook context", () => {
  withRoot((root) => {
    seed(root, AGENT, FULL);
    assert.deepEqual(resolve(undefined, root), {});
    assert.deepEqual(resolve("", root), {});
  });
});

test("returns {} on malformed JSON rather than throwing into the exec tool", () => {
  withRoot((root) => {
    seed(root, AGENT, "{not json");
    assert.deepEqual(resolve(AGENT, root), {});
  });
});

test("returns {} on a JSON non-object (array / null / scalar)", () => {
  withRoot((root) => {
    seed(root, AGENT, "[]");
    assert.deepEqual(resolve(AGENT, root), {});
    seed(root, AGENT, "null");
    assert.deepEqual(resolve(AGENT, root), {});
    seed(root, AGENT, '"a string"');
    assert.deepEqual(resolve(AGENT, root), {});
  });
});

test("is ALL-OR-NOTHING — a missing or empty field yields {}, never a half-set env", () => {
  // A partially-populated env authenticates as nobody and produces a confusing
  // failure; no env at all is the honest state.
  for (const drop of ["token", "agentId", "companyId", "apiUrl"]) {
    withRoot((root) => {
      const partial = { ...FULL };
      delete partial[drop];
      seed(root, AGENT, partial);
      assert.deepEqual(resolve(AGENT, root), {}, `missing ${drop}`);
    });
    withRoot((root) => {
      seed(root, AGENT, { ...FULL, [drop]: "" });
      assert.deepEqual(resolve(AGENT, root), {}, `empty ${drop}`);
    });
    withRoot((root) => {
      seed(root, AGENT, { ...FULL, [drop]: 42 });
      assert.deepEqual(resolve(AGENT, root), {}, `non-string ${drop}`);
    });
  }
});

test("refuses a path-traversing agentId rather than reading outside the root", () => {
  withRoot((root) => {
    seed(root, AGENT, FULL);
    for (const evil of ["../../etc", "a/b", "a\\b", "..", "agent_x/../agent_y"]) {
      assert.deepEqual(resolve(evil, root), {}, evil);
    }
  });
});

test("falls back to the baked default root when none is supplied", () => {
  // DEFAULT_WORKSPACE_ROOT does not exist in the test environment, so the read
  // throws and degrades to {} — proving the fallback path is exercised and
  // still cannot throw.
  assert.deepEqual(resolve(AGENT, undefined), {});
  assert.deepEqual(resolve(AGENT, ""), {});
});
