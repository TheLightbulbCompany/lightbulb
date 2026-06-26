# Perplexity → Parallel (free MCP) Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all direct-Perplexity-`sonar` web search in the vertical agents with a keyless Parallel free-MCP helper, then republish the affected marketplace listings so every buyer deploy uses Parallel with zero setup.

**Architecture:** A single self-contained node helper (`parallel-search.js`) is dropped into each agent workspace; it runs the Parallel hosted Search MCP handshake (`initialize → notifications/initialized → tools/call web_search`) over HTTPS and emits a normalized result shape. Each Lobster pipeline's inline Perplexity-curl block is swapped for a call to that helper plus a field remap — the surrounding step structure is unchanged. Finally we land the rewrite in git, mirror it to live EFS, re-snapshot, and republish each listing (fixing the slug-vs-UUID republish resolver so Vera/Tally can republish).

**Tech Stack:** Node (plain, no TS, global `fetch`), Lobster pipeline YAML, Python/FastAPI (backend republish path), the marketplace `agent_export` + publish endpoints.

## Global Constraints

- Endpoint: `https://search.parallel.ai/mcp` — remote/hosted, Streamable HTTP, **no Authorization header by default** (anonymous free tier). Send `Authorization: Bearer <key>` **only** if `PARALLEL_API_KEY` is set and non-empty (sending an empty/garbage bearer flips the server to 401).
- MCP protocol version advertised on `initialize`: `2025-06-18`; echo the server's negotiated `protocolVersion` + `Mcp-Session-Id` on every follow-up request.
- Parallel caps `search_queries` at **5 per call**; `web_search` `arguments` require a non-empty `objective`.
- Helper output contract (every consumer relies on this): stdin `{ "queries": string[], "objective"?: string, "max_results"?: number }` → stdout `{ "results": [{ "title", "url", "excerpts": string[], "published" }], "total": number, "ok": boolean, "error"?: string }`. **Always exit 0**, carry failure in `ok`/`error` (Lobster scripts branch via `condition:`, never on exit code).
- Each agent workspace is a self-contained marketplace artifact: the helper is **copied into each agent dir**, not shared/symlinked.
- End state: zero `perplexity` / `PERPLEXITY_API_KEY` / `api.perplexity.ai` / `model:"sonar"` references anywhere under `agents/`.
- Never push to `main`; all repo changes land via a PR from a worktree branch.

---

### Task 1: Build and live-test the `parallel-search.js` helper

This is the feasibility gate. If the anonymous endpoint can't be reached and return results from a plain node client, STOP and escalate (fall back to REST-keyed) before touching any agent.

**Files:**
- Create: `agents/pulse/parallel-search.js` (canonical copy; later tasks copy it verbatim into the other agent dirs)

**Interfaces:**
- Produces: a CLI reading `{queries, objective?, max_results?}` JSON on stdin (or `argv[2]` as a single query) and writing `{results:[{title,url,excerpts,published}], total, ok, error?}` JSON to stdout. Used by every pipeline task below.

- [ ] **Step 1: Write the helper**

Create `agents/pulse/parallel-search.js`:

```js
#!/usr/bin/env node
// parallel-search.js — keyless web search via Parallel's hosted free Search MCP.
// Remote HTTPS (https://search.parallel.ai/mcp), Streamable HTTP transport.
// Anonymous by default; set PARALLEL_API_KEY for higher rate limits.
// stdin:  {queries:string[], objective?:string, max_results?:number}  (or argv[2]=single query)
// stdout: {results:[{title,url,excerpts,published}], total, ok, error?}   (always exits 0)

const ENDPOINT = "https://search.parallel.ai/mcp";
const PROTOCOL = "2025-06-18";
const TIMEOUT_MS = 30000;

function headers(sessionId, protocolVersion) {
  const h = { "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
  const key = (process.env.PARALLEL_API_KEY || "").trim();
  if (key) h.Authorization = "Bearer " + key;
  if (sessionId) h["Mcp-Session-Id"] = sessionId;
  if (protocolVersion) h["MCP-Protocol-Version"] = protocolVersion;
  return h;
}

async function post(body, sessionId, protocolVersion) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: headers(sessionId, protocolVersion),
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    return { ok: res.ok, status: res.status, text: await res.text(), sessionId: res.headers.get("mcp-session-id") };
  } finally {
    clearTimeout(timer);
  }
}

function iterMessages(text) {
  const out = [];
  const body = (text || "").trim();
  if (!body) return out;
  const emit = (p) => {
    if (Array.isArray(p)) { for (const e of p) if (e && typeof e === "object") out.push(e); }
    else if (p && typeof p === "object") out.push(p);
  };
  if (body[0] === "{" || body[0] === "[") { try { emit(JSON.parse(body)); } catch {} return out; }
  let data = [];
  const flush = () => { if (!data.length) return; try { emit(JSON.parse(data.join("\n"))); } catch {} data = []; };
  for (const raw of body.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    else if (line.trim() === "") flush();
  }
  flush();
  return out;
}

function selectEnvelope(text, id) {
  let fallback = {};
  for (const m of iterMessages(text)) {
    if (!("result" in m || "error" in m)) continue;
    if (m.id === id) return m;
    fallback = m;
  }
  return fallback;
}

function extractPayload(env) {
  if ("error" in env) throw new Error("Parallel MCP error: " + JSON.stringify(env.error).slice(0, 300));
  const result = env.result && typeof env.result === "object" ? env.result : {};
  if (result.isError) throw new Error("Parallel MCP tool error: " + JSON.stringify(result).slice(0, 300));
  if (result.structuredContent && typeof result.structuredContent === "object") return result.structuredContent;
  const content = Array.isArray(result.content) ? result.content : [];
  for (const b of content) {
    if (b && b.type === "text" && typeof b.text === "string" && b.text) {
      try { const p = JSON.parse(b.text); if (p && typeof p === "object") return p; } catch {}
    }
  }
  throw new Error("Parallel MCP returned no parseable content");
}

let rid = 0;
const nextId = () => "r" + ++rid + "-" + Date.now();

async function search(queries, objective, maxResults) {
  const initId = nextId();
  const init = await post({
    jsonrpc: "2.0", id: initId, method: "initialize",
    params: { protocolVersion: PROTOCOL, capabilities: {}, clientInfo: { name: "isol8-parallel-search", version: "1.0.0" } },
  });
  if (!init.ok) throw new Error("initialize failed (" + init.status + "): " + init.text.slice(0, 200));
  const sessionId = init.sessionId || undefined;
  const initEnv = selectEnvelope(init.text, initId);
  const negotiated = initEnv.result && typeof initEnv.result.protocolVersion === "string" ? initEnv.result.protocolVersion : PROTOCOL;

  await post({ jsonrpc: "2.0", method: "notifications/initialized" }, sessionId, negotiated);

  const callId = nextId();
  const sessId = sessionId || nextId();
  const call = await post({
    jsonrpc: "2.0", id: callId, method: "tools/call",
    params: { name: "web_search", arguments: { objective: objective || queries.join(" "), search_queries: queries, session_id: sessId } },
  }, sessionId, negotiated);
  if (!call.ok) throw new Error("tools/call failed (" + call.status + "): " + call.text.slice(0, 200));

  const payload = extractPayload(selectEnvelope(call.text, callId));
  const all = Array.isArray(payload.results) ? payload.results : [];
  return all.slice(0, Math.max(maxResults || 10, 1)).map((r) => ({
    title: (r && r.title) || "",
    url: (r && r.url) || "",
    excerpts: Array.isArray(r && r.excerpts) ? r.excerpts : [],
    published: (r && (r.publish_date || r.published)) || "",
  }));
}

async function main() {
  let input = "";
  try { input = require("fs").readFileSync(0, "utf8"); } catch {}
  let req = {};
  if (input.trim()) { try { req = JSON.parse(input) || {}; } catch { req = {}; } }
  let queries = Array.isArray(req.queries) ? req.queries.filter(Boolean) : [];
  if (!queries.length && process.argv[2]) queries = [process.argv[2]];
  if (!queries.length) { process.stdout.write(JSON.stringify({ results: [], total: 0, ok: false, error: "no queries" })); return; }
  queries = queries.slice(0, 5);
  try {
    const results = await search(queries, req.objective, req.max_results);
    process.stdout.write(JSON.stringify({ results, total: results.length, ok: results.length > 0 }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ results: [], total: 0, ok: false, error: String((e && e.message) || e) }));
  }
}

main();
```

- [ ] **Step 2: Live-test it against the real endpoint (feasibility gate)**

Run:
```bash
cd agents/pulse && echo '{"queries":["latest AI agent frameworks 2026"],"max_results":3}' | node parallel-search.js | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8")); console.log("ok=",d.ok,"total=",d.total,"first_url=",(d.results[0]||{}).url,"err=",d.error||"none");'
```
Expected: `ok= true total= 3 first_url= https://… err= none`. If `ok= false` with a non-rate-limit error (e.g. handshake/parse failure), STOP — the keyless path is broken; escalate before continuing.

- [ ] **Step 3: Verify the keyless contract holds with no key present**

Run:
```bash
cd agents/pulse && env -u PARALLEL_API_KEY sh -c 'echo "{\"queries\":[\"openclaw agent platform\"]}" | node parallel-search.js' | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8")); if(!d.ok) {console.error("FAIL keyless",d.error); process.exit(1);} console.log("keyless OK",d.total);'
```
Expected: `keyless OK <n>` (n ≥ 1).

- [ ] **Step 4: Commit**

```bash
git add agents/pulse/parallel-search.js
git commit -m "feat(agents): add keyless Parallel free-MCP web-search helper"
```

---

### Task 2: Define the call-site transform + migrate Pulse pipelines

Pulse is the worked example; the transform defined here is reused verbatim by every later agent task.

**The transform (apply at every Perplexity call site):** a Perplexity lane step today looks like —
```js
const body=JSON.stringify({model:"sonar",messages:[{role:"user",content:"<QUERY EXPR>"}]});
const {execSync}=require("child_process");
let out=""; try { out=execSync("curl -sS https://api.perplexity.ai/chat/completions -H \"Authorization: Bearer $PERPLEXITY_API_KEY\" -H \"Content-Type: application/json\" -d @-",{input:body,encoding:"utf8",timeout:60000}); } catch(e){ out=""; }
let r={}; try { r=JSON.parse(out)||{}; } catch(e){ r={}; }
const content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
const citations=r.citations||[];
process.stdout.write(JSON.stringify({lane:"<LANE>", content, citations, total:citations.length, top_urls:citations.slice(0,10), ok:!!content}));
```
Replace the body→curl→extract guts with a helper call + result remap, **preserving the exact emitted field names** (`content`, `citations`, `total`, `top_urls`, `ok`, plus any extras like `lane`/`high_signal_urls` the site already emits):
```js
const q="<QUERY EXPR>";
const {execSync}=require("child_process");
let out=""; try { out=execSync("node parallel-search.js",{input:JSON.stringify({queries:[q],max_results:10}),encoding:"utf8",timeout:60000}); } catch(e){ out=""; }
let r={}; try { r=JSON.parse(out)||{}; } catch(e){ r={}; }
const results=r.results||[];
const top_urls=results.map(x=>x.url).filter(Boolean).slice(0,10);
const content=results.map(x=>(x.title?x.title+"\n":"")+((x.excerpts||[]).join(" "))+(x.url?"\n"+x.url:"")).join("\n\n");
const citations=top_urls;
process.stdout.write(JSON.stringify({lane:"<LANE>", content, citations, total:results.length, top_urls, ok:results.length>0}));
```
Rules:
- `content` becomes a text digest of `title + excerpts + url` (stands in for sonar's prose — every downstream consumer of `content` re-feeds it to an LLM, so a digest is sufficient; do NOT add an llm-task step).
- `citations` / `top_urls` / any `high_signal_urls` come from `results[].url`.
- Keep the `LOBSTER_ARG_*` env-passed query construction exactly as-is (queries are still built in-node, never interpolated into a shell line).
- Multi-query sites (a `node -e` fetch loop over N queries) pass all N (≤5) in one `queries:[...]` helper call instead of looping.

**Files:**
- Create: `agents/pulse/parallel-search.js` already exists from Task 1 — no-op.
- Modify: `agents/pulse/content-generate.lobster` (4 lanes: `research-competitors`, `research-community`, `research-search`, `research-industry`, lines ~129-192)
- Modify: `agents/pulse/monitoring-sweep.lobster` (5 Perplexity sites)
- Modify: `agents/pulse/weekly-digest.lobster` (GEO Share-of-Model loop, lines ~112-129)
- Modify: `agents/pulse/pulse-geo-checker.js` (2 perplexity refs)

**Interfaces:**
- Consumes: the `parallel-search.js` contract from Task 1.
- Produces: pipelines whose research steps emit the unchanged `{content, citations, total, top_urls, ok}` shape downstream consumers already expect.

- [ ] **Step 1: Apply the transform to all four `content-generate.lobster` lanes**

For each of `research-competitors`, `research-community`, `research-search`, `research-industry`, replace the `body`/`execSync(curl …)`/`content`/`citations` guts with the helper-call form above, keeping each lane's existing `<QUERY EXPR>` (e.g. competitors: `"Find recent competitor content and blog posts about "+t+". Return key URLs, headlines, and the competitor brands covering this topic."`) and `lane` label.

- [ ] **Step 2: Apply the transform to `monitoring-sweep.lobster` and `weekly-digest.lobster`**

In `weekly-digest.lobster`, the GEO loop currently fetches Perplexity once per configured query inside a `node -e` loop — replace it with a single helper call passing up to 5 queries: `execSync("node parallel-search.js",{input:JSON.stringify({queries:qs.slice(0,5),max_results:5}),…})`, then map `results` onto the existing per-query output objects (`{query, platform:"parallel", response:<digest>, sources_cited:<urls>}`). Apply the standard transform to each `monitoring-sweep.lobster` site.

- [ ] **Step 3: Update `pulse-geo-checker.js`**

Replace its 2 `perplexity` references (provider/platform name string) with `parallel`.

- [ ] **Step 4: Verify no Perplexity remains in Pulse pipelines/scripts**

Run:
```bash
grep -rn "perplexity\|PERPLEXITY\|api.perplexity.ai\|sonar" agents/pulse/*.lobster agents/pulse/*.js
```
Expected: no output (exit 1).

- [ ] **Step 5: Smoke-test one rewritten pipeline path**

Run (drives the real helper through the competitors lane's node body in isolation):
```bash
cd agents/pulse && LOBSTER_ARG_TOPIC="b2b saas onboarding" node -e 'const t=process.env.LOBSTER_ARG_TOPIC||"";const q="Find recent competitor content and blog posts about "+t+".";const {execSync}=require("child_process");let out="";try{out=execSync("node parallel-search.js",{input:JSON.stringify({queries:[q],max_results:5}),encoding:"utf8",timeout:60000});}catch(e){out="";}let r={};try{r=JSON.parse(out)||{};}catch(e){r={};}const results=r.results||[];console.log("ok=",results.length>0,"urls=",results.slice(0,3).map(x=>x.url));'
```
Expected: `ok= true urls= [ 'https://…', … ]`.

- [ ] **Step 6: Commit**

```bash
git add agents/pulse
git commit -m "feat(agents): migrate Pulse web search from Perplexity to Parallel free MCP"
```

---

### Task 3: Migrate Pitch-scout

**Files:**
- Create: `agents/pitch-scout/parallel-search.js` (copy of Task 1's file)
- Modify: `agents/pitch-scout/enrich-and-score.lobster`, `prospect-enrich.lobster`, `signal-monitor.lobster`, `signal-sweep.lobster` (Perplexity call sites)
- Modify: `agents/pitch-scout/scout-vertical-router.js`, `scout-icp-scorer.js`, `scout-source-health-check.js` (provider-name refs)

**Interfaces:**
- Consumes: the Task 1 helper contract and the Task 2 transform recipe.

- [ ] **Step 1: Copy the helper into the agent dir**

```bash
cp agents/pulse/parallel-search.js agents/pitch-scout/parallel-search.js
```

- [ ] **Step 2: Apply the Task 2 transform to each `.lobster` Perplexity site**

Read each of the four `.lobster` files; at every `model:"sonar"` curl block apply the recipe (preserve each site's own query expression, lane/field labels, and any multi-query loop → single `queries:[…≤5]` call).

- [ ] **Step 3: Replace provider-name references in the router `.js` files**

In `scout-vertical-router.js`, `scout-icp-scorer.js`, `scout-source-health-check.js`, replace `perplexity` source/provider strings with `parallel` (these are routing/health labels, not HTTP calls — keep behavior identical, just rename the source).

- [ ] **Step 4: Verify**

```bash
grep -rn "perplexity\|PERPLEXITY\|api.perplexity.ai\|sonar" agents/pitch-scout
```
Expected: no output.

- [ ] **Step 5: Smoke-test the helper from this dir**

```bash
cd agents/pitch-scout && echo '{"queries":["series a fintech hiring signals"],"max_results":3}' | node parallel-search.js | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"));if(!d.ok){console.error("FAIL",d.error);process.exit(1);}console.log("OK",d.total);'
```
Expected: `OK <n>`.

- [ ] **Step 6: Commit**

```bash
git add agents/pitch-scout
git commit -m "feat(agents): migrate Pitch-scout web search to Parallel free MCP"
```

---

### Task 4: Migrate Lens

**Files:**
- Create: `agents/lens/parallel-search.js` (copy of Task 1's file)
- Modify: `agents/lens/research-execute.lobster`, `monitoring-sweep.lobster`, `weekly-maintenance.lobster` (Perplexity sites; note `research-execute.lobster` uses a raw `https.request` form, not `curl` — same transform, replace the whole request block with the helper call)
- Modify: `agents/lens/lens-source-router.js` (provider-name refs)

- [ ] **Step 1: Copy the helper**

```bash
cp agents/pulse/parallel-search.js agents/lens/parallel-search.js
```

- [ ] **Step 2: Apply the transform**

Apply the Task 2 recipe to each `.lobster` site. For `research-execute.lobster`'s `https.request(...)` block, replace the entire raw-HTTPS-to-`api.perplexity.ai` request + response assembly with `execSync("node parallel-search.js", {input: JSON.stringify({queries:[q], max_results:N}), …})` and the standard result remap.

- [ ] **Step 3: Rename provider refs in `lens-source-router.js`** (`perplexity` → `parallel`).

- [ ] **Step 4: Verify**

```bash
grep -rn "perplexity\|PERPLEXITY\|api.perplexity.ai\|sonar" agents/lens
```
Expected: no output.

- [ ] **Step 5: Smoke-test**

```bash
cd agents/lens && echo '{"queries":["quantum error correction 2026 breakthroughs"],"max_results":3}' | node parallel-search.js | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"));if(!d.ok){console.error("FAIL",d.error);process.exit(1);}console.log("OK",d.total);'
```
Expected: `OK <n>`.

- [ ] **Step 6: Commit**

```bash
git add agents/lens
git commit -m "feat(agents): migrate Lens web search to Parallel free MCP"
```

---

### Task 5: Migrate Tally

**Files:**
- Create: `agents/tally/parallel-search.js` (copy of Task 1's file)
- Modify: `agents/tally/weekly-summary.lobster`, `daily-feed.lobster` (Perplexity sites)

- [ ] **Step 1: Copy the helper**

```bash
cp agents/pulse/parallel-search.js agents/tally/parallel-search.js
```

- [ ] **Step 2: Apply the Task 2 transform to each `.lobster` Perplexity site.**

- [ ] **Step 3: Verify**

```bash
grep -rn "perplexity\|PERPLEXITY\|api.perplexity.ai\|sonar" agents/tally
```
Expected: no output.

- [ ] **Step 4: Smoke-test**

```bash
cd agents/tally && echo '{"queries":["small business accounting tax deadlines 2026"],"max_results":3}' | node parallel-search.js | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"));if(!d.ok){console.error("FAIL",d.error);process.exit(1);}console.log("OK",d.total);'
```
Expected: `OK <n>`.

- [ ] **Step 5: Commit**

```bash
git add agents/tally
git commit -m "feat(agents): migrate Tally web search to Parallel free MCP"
```

---

### Task 6: Migrate Vera + Scout addendum overlay

**Files:**
- Create: `agents/vera/parallel-search.js` (copy of Task 1's file)
- Modify: `agents/vera/agent-assist.lobster` (Perplexity sites)
- Modify: `agents/scout-addendum/scout-icp-scorer.js`, `agents/scout-addendum/scout-vertical-router (1).js` (provider-name refs only — these are overlay copies of pitch-scout routers)

- [ ] **Step 1: Copy the helper into Vera**

```bash
cp agents/pulse/parallel-search.js agents/vera/parallel-search.js
```

- [ ] **Step 2: Apply the Task 2 transform to `agents/vera/agent-assist.lobster`.**

- [ ] **Step 3: Rename provider refs in the scout-addendum router copies** (`perplexity` → `parallel`).

- [ ] **Step 4: Verify both dirs**

```bash
grep -rn "perplexity\|PERPLEXITY\|api.perplexity.ai\|sonar" agents/vera "agents/scout-addendum"
```
Expected: no output.

- [ ] **Step 5: Smoke-test Vera**

```bash
cd agents/vera && echo '{"queries":["customer support escalation best practices"],"max_results":3}' | node parallel-search.js | node -e 'const d=JSON.parse(require("fs").readFileSync(0,"utf8"));if(!d.ok){console.error("FAIL",d.error);process.exit(1);}console.log("OK",d.total);'
```
Expected: `OK <n>`.

- [ ] **Step 6: Commit**

```bash
git add agents/vera "agents/scout-addendum"
git commit -m "feat(agents): migrate Vera + scout-addendum web search to Parallel free MCP"
```

---

### Task 7: Docs sweep + global gate

Rewrite every agent's tool/onboarding docs to describe Parallel, then assert the whole `agents/` tree is Perplexity-free.

**Files:**
- Modify: `TOOLS.md`, `BOOTSTRAP.md`, `AGENTS.md` under `agents/pulse`, `agents/pitch-scout`, `agents/lens`, `agents/tally`, `agents/vera`, and any `*-addendum/AGENTS.md` that mentions Perplexity (`pitch-addendum/AGENTS.md`, `tally-addendum/AGENTS.md`, `vera-addendum/AGENTS.md`).

- [ ] **Step 1: Rewrite the Perplexity sections**

In each doc, replace the "Perplexity API (Research + Monitoring Engine)" / sonar description with a "Parallel Search (Research + Monitoring Engine)" section: hosted free Search MCP via the bundled `parallel-search.js` helper, keyless by default (optional `PARALLEL_API_KEY` for higher rate limits), returns ranked results (`title`/`url`/`excerpts`) not a synthesized answer, `basic` low-latency mode. Update the API-error-handling bullets (429/401/5xx) to reference Parallel/the helper's `error` field instead of `PERPLEXITY_API_KEY`.

- [ ] **Step 2: Global gate — no Perplexity anywhere under agents/**

Run:
```bash
grep -rin "perplexity\|api.perplexity.ai\|sonar" agents/
```
Expected: no output (exit 1). If anything remains, fix it before committing.

- [ ] **Step 3: Confirm every migrated agent has the helper**

Run:
```bash
for a in pulse pitch-scout lens tally vera; do test -f "agents/$a/parallel-search.js" && echo "$a ok" || echo "$a MISSING"; done
```
Expected: all `ok`.

- [ ] **Step 4: Commit**

```bash
git add agents/
git commit -m "docs(agents): describe Parallel search; remove all Perplexity references"
```

---

### Task 8: Fix the slug-vs-UUID republish resolver (backend)

Vera/Tally are the known-broken pre-UUID republish path: `artifact-from-agent` resolves the live EFS agent dir by **slug** instead of the agent's UUID, so republishing a pre-UUID listing fails with "agent dir not found". Fix the resolver so republish works for all listings before Task 9.

**Files:**
- Modify: `apps/backend/core/services/agent_export.py` (the EFS agent-dir resolution in the export path)
- Modify: `apps/backend/routers/marketplace_listings.py` (the `artifact-from-agent` handler that passes the identifier)
- Test: `apps/backend/tests/unit/services/test_agent_export.py` (add resolver test)

**Interfaces:**
- Consumes: the listing row (has both `slug` and the seller's `agent_id`/UUID) and the seller's EFS workspace layout (`workspaces/<agent_id>/`).
- Produces: an export that resolves the agent dir by UUID, falling back to slug only when no UUID is recorded.

- [ ] **Step 1: Write the failing test**

In `apps/backend/tests/unit/services/test_agent_export.py`:
```python
def test_export_resolves_agent_dir_by_uuid_not_slug(tmp_path, monkeypatch):
    # Workspace dir is keyed by the agent UUID, not the human slug.
    agent_uuid = "agent_11111111-2222-3333-4444-555555555555"
    ws = tmp_path / "workspaces" / agent_uuid
    ws.mkdir(parents=True)
    (ws / "AGENTS.md").write_text("vera", encoding="utf-8")
    # The listing's slug ("vera") must NOT be used to locate the dir.
    resolved = agent_export._resolve_agent_dir(
        efs_root=str(tmp_path), agent_id=agent_uuid, slug="vera"
    )
    assert resolved == str(ws)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py::test_export_resolves_agent_dir_by_uuid_not_slug -v`
Expected: FAIL (`_resolve_agent_dir` missing or resolves by slug).

- [ ] **Step 3: Implement the resolver**

Extract/repair the agent-dir resolution in `agent_export.py` into `_resolve_agent_dir(efs_root, agent_id, slug=None)` that joins `efs_root/workspaces/<agent_id>`, and only falls back to `<slug>` when `agent_id` is empty. Update `routers/marketplace_listings.py`'s `artifact-from-agent` handler to pass the listing's recorded `agent_id` (UUID) as the primary identifier.

- [ ] **Step 4: Run the test (and the file's suite)**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/agent_export.py apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/services/test_agent_export.py
git commit -m "fix(marketplace): resolve export agent dir by UUID so pre-UUID listings can republish"
```

---

### Task 9: Deploy end-to-end — PR, live EFS, re-snapshot, republish

This task crosses into prod (live EFS + marketplace publish). Do the PR + review first; run the prod steps with the user in the loop.

**Files:** none (operational). Uses the backend workspace-file API / ECS exec for the live-EFS mirror, and the marketplace `artifact-from-agent` + publish endpoints for republish.

- [ ] **Step 1: Open the PR and watch CI**

Push the branch and open a PR with all Task 1–8 commits. Run:
```bash
gh pr create --fill --base main
gh run watch --exit-status
```
Expected: green. Get the review gate (per `requesting-code-review`) before merging.

- [ ] **Step 2: Mirror the migrated agent files onto live EFS**

For the founder's prod container, write each migrated agent dir's changed files (the `.lobster`/`.js`/`parallel-search.js`/docs) into `/mnt/efs/users/<owner_id>/workspaces/<agent_uuid>/` — via the backend workspace-file write path (preferred) or ECS exec on the backend task. `chown` to UID 1000 after write (OpenClaw runs as `node`). Confirm `parallel-search.js` is present and the chokidar reload picked up the pipeline changes.

- [ ] **Step 3: Live-verify search runs in-container**

Trigger one search-bearing pipeline per agent (e.g. Pulse `monitoring-sweep`, Pitch-scout `signal-sweep`) and confirm the run completes with non-empty results and no `PERPLEXITY_API_KEY` errors in the container log.

- [ ] **Step 4: Re-snapshot + republish each listing**

For each of the 6 listings (pulse, pitch-scout, lens, tally, vera, and the scout listing), call `POST /listings/{id}/artifact-from-agent` to re-snapshot from the updated EFS agent, then publish the new version (admin approve path). Vera/Tally now resolve correctly thanks to Task 8.

- [ ] **Step 5: Verify the storefront**

For each listing, fetch `GET /api/v1/marketplace/listings/{id}` and assert the new `manifest.requirements[]` no longer lists `perplexity` (it should now show no external_api BYO for search). Spot-check the Pulse listing in the browser.

- [ ] **Step 6: Final confirmation**

Confirm: PR merged, all 6 listings republished, no Perplexity in any manifest, at least one live agent run per vertical succeeded on Parallel.

---

## Self-Review

**Spec coverage:**
- Shared keyless helper → Task 1. ✓
- Per-agent rewrite (5 agents + scout-addendum) → Tasks 2–6. ✓
- Router `.js` provider-name refs → Tasks 3, 4, 6. ✓
- Docs rewrite + global gate → Task 7. ✓
- Synthesized-answer handling (digest, no synth where content re-feeds an LLM) → Task 2 recipe. ✓
- Optional `PARALLEL_API_KEY` Bearer upgrade → Task 1 helper + Global Constraints. ✓
- Vera/Tally republish resolver fix → Task 8. ✓
- End-to-end deploy (git PR + live EFS + re-snapshot + republish) → Task 9. ✓
- Risks (rate limits, semantic shift) → documented in spec; mitigations live in Task 1/Task 2.

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Per-agent tasks reference the Task 2 recipe by content (the recipe is fully specified there) and enumerate exact files — deliberate, since 33 sites are structurally identical and verbatim-diffing each would drift from the live files. Each task has concrete grep/smoke commands with expected output.

**Type/contract consistency:** Helper stdin/stdout contract is identical across Task 1 (definition), Task 2 (recipe), and every consumer. Emitted lane fields (`content`/`citations`/`total`/`top_urls`/`ok`) match the pre-existing downstream consumers in `compile-brief` etc. `_resolve_agent_dir(efs_root, agent_id, slug)` signature matches between Task 8's test and implementation.
