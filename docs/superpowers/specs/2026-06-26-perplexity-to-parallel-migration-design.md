# Perplexity → Parallel (free MCP) migration — design

**Date:** 2026-06-26
**Status:** approved (design), pending implementation plan

## Problem

The founder vertical agents (Pulse, Pitch-scout, Lens, Tally, Vera, plus the
Scout addendum overlay) reach web search by **curling Perplexity `sonar`
directly** inside Lobster `command:` steps — `curl https://api.perplexity.ai/chat/completions -H "Authorization: Bearer $PERPLEXITY_API_KEY"`.
They do this deliberately: the embedded Lobster runner cannot call the
`web.search` tool (`openclaw.invoke` 401s in-process), so search has to be a
direct HTTP step to stay deterministic.

That leaves a dead end for buyers:

- Platform-provided Perplexity was removed (`2026-04-12-remove-platform-perplexity`),
  and the old BYOK key endpoint (`PUT /settings/keys/perplexity`) now 400s.
- `PERPLEXITY_API_KEY` is therefore **not injectable** — not in `providers.yaml`,
  not in the container env, no UI rail.
- The platform standardized web search on **Parallel** (`providers.yaml: parallel`,
  writing `tools.web.search.*`), but the agents never moved, and Parallel's
  config-based key wouldn't reach a curl step anyway.

Net: a buyer who hires Pulse gets a research engine that 401s on every call with
no way to fix it. We are standardizing the agents on Parallel to match the
platform and remove the credential dead-end.

## Confirmed facts (Parallel docs + OpenClaw provider source)

From `docs.parallel.ai/integrations/mcp/search-mcp` and
`openclaw.nosync/extensions/parallel/src/parallel-mcp-search.runtime.ts`:

- **Remote/hosted MCP**, not local. HTTPS endpoint `https://search.parallel.ai/mcp`.
  No stdio process, no `npx`, no `mcp-remote` wrapper — we call it directly.
- **Transport:** Streamable HTTP. Request sequence: `initialize` (capture the
  `Mcp-Session-Id` response header + negotiated `protocolVersion`) →
  `notifications/initialized` → `tools/call web_search`. Response body may be a
  single JSON object **or** an SSE `text/event-stream`; both must be parsed.
- **Keyless by default.** *"Free to use anonymously at lower rate limits. Pass a
  Parallel API key as a Bearer token (`Authorization: Bearer <key>`) to unlock
  higher limits."* The `/mcp` endpoint serves anonymous requests; sending an
  empty/garbage bearer flips it to 401, so we send **no** Authorization header
  unless a real key is present.
- **Result shape** (`web_search` tool payload, reused by the provider's
  normalizer): `{ results: [{ title, url, excerpts: [...], publish_date? }], session_id?, search_id?, warnings?, usage? }`.
  Excerpts capped ~25,000 chars/call; runs in `basic` (low-latency) mode.

## Goal

Replace **all** direct-Perplexity web search in the vertical agents with the
keyless Parallel free MCP, end-to-end: agent definitions (git reference + live
EFS), re-snapshot, and republish every affected marketplace listing — so the
storefront and every future buyer deploy use Parallel with zero setup.

## Design

### 1. Shared search helper — `parallel-search.js`

One self-contained node script dropped into each agent's workspace (duplicated
per agent, because each marketplace artifact must be self-contained — agents are
independently deployable). It encapsulates the entire MCP interaction so the
pipelines never reimplement the handshake.

Responsibilities:

- Read one or more queries (argv/stdin) plus optional `objective` and
  `max_results`.
- Run `initialize → notifications/initialized → tools/call web_search` against
  `https://search.parallel.ai/mcp`, threading the server-assigned
  `Mcp-Session-Id` and negotiated protocol version; parse JSON **and** SSE
  bodies; select the JSON-RPC envelope by request id.
- **Auth:** keyless by default. If `process.env.PARALLEL_API_KEY` is set and
  non-empty, add `Authorization: Bearer <key>` for higher limits; otherwise send
  no auth header.
- Emit a stable normalized shape on stdout:
  `{ results: [{ title, url, excerpts: [...], published? }], total }`.
- Surface throttle/5xx/timeout errors as a clean, structured error object the
  pipelines can branch on (mirrors today's 429/401/5xx handling), never a raw
  stack trace.

This is the only genuinely new logic. **Implementation step 1 is building it and
running it live against the endpoint** — that is the feasibility gate. If the
anonymous tier proves unusable under real load even with the Bearer upgrade, we
stop and fall back to REST-keyed (`api.parallel.ai/v1/search`) before touching
agents.

### 2. Per-agent rewrite

For each affected agent:

- **Call sites:** replace every `curl api.perplexity.ai/chat/completions …
  $PERPLEXITY_API_KEY` block with a call to `parallel-search.js`.
- **Normalizers:** rewrite the inline `node -e` normalizers that consumed
  sonar's `{ choices[].message.content, citations[] }` to consume Parallel's
  `{ results[] }`. Sources move from `citations[]` → `results[].url`; per-result
  context comes from `excerpts[]`.
- **Synthesized-answer cases:** sonar returned a *synthesized prose answer*;
  Parallel returns *raw results* (more genuinely deterministic). Where a pipeline
  actually consumed the prose `content`, insert a cheap `llm-task` synth step
  over the results. Where it only needed sources/freshness (GEO Share-of-Model,
  mention scans, competitor sweeps), hand `results[]` downstream directly. The
  implementation plan enumerates each call site and its disposition.
- **Router `.js` files** (e.g. `scout-vertical-router.js`, `scout-icp-scorer.js`,
  `lens-source-router.js`, `pulse-geo-checker.js`): replace `perplexity`
  provider/source-name references with `parallel`.
- **Docs:** rewrite each agent's `TOOLS.md`, `BOOTSTRAP.md`, `AGENTS.md` to
  describe Parallel (endpoint, keyless default, the `basic`-mode/results-not-prose
  character, error handling). Remove every `PERPLEXITY_API_KEY` reference.

**Footprint (to be enumerated exactly in the plan):** ~33 direct
`api.perplexity.ai` call lines across ~17 files in **lens, pitch-scout, pulse,
tally, vera**; provider-name references in the router `.js` files and the
`scout-addendum` overlay; plus per-agent docs. End state: zero `perplexity` /
`PERPLEXITY_API_KEY` references in `agents/`.

### 3. Verify

- Run `parallel-search.js` standalone against the live endpoint; assert the
  normalized shape.
- Dry-run at least one pipeline per agent that exercises search
  (pulse `content-generate`, pitch-scout `signal-sweep`, lens `research-execute`)
  and confirm the new shape flows through end to end.

### 4. Deploy end-to-end

Per the marketplace model, **live EFS is canonical and git `agents/` is
reference-only** (CI does not deploy agents). So:

1. Land the rewrite in git `agents/` via PR (the reference source-of-record).
2. Apply the same files to the live EFS agents (founder's prod container).
3. Re-snapshot each agent (`agent_export`) and republish each affected listing so
   buyers get the Parallel version.
4. **Vera/Tally republish fix:** those are the known-broken pre-UUID republish
   path (the republish resolves the EFS agent dir by **slug** instead of UUID →
   "agent dir not found"). Fix that resolver as part of this work rather than
   hand-patching, since all six are being republished anyway.

## Risks (carried, not blocking)

- **Free-tier rate limits.** Anonymous limits are unpublished and "lower." Heavy
  cron load (Pulse daily multi-query sweeps, pitch-scout signal sweeps) may
  throttle. Mitigations: helper surfaces throttling cleanly; same-endpoint Bearer
  upgrade via optional `PARALLEL_API_KEY` (one line, no code change). A future
  rail to materialize a per-user Parallel key into the container env is **out of
  scope** here.
- **Semantic shift.** Results-not-answers changes output character for any
  pipeline that leaned on sonar's prose; handled per call site with an `llm-task`
  synth where needed. Net determinism improves.

## Out of scope

- A credential rail that injects `PARALLEL_API_KEY` into the container process
  env (free tier needs none; Bearer upgrade is best-effort via env if the
  operator sets it).
- The marketplace requirements-ledger UI / scanner de-noise (tracked separately).
- The Earnings dashboard 500 (tracked separately).
