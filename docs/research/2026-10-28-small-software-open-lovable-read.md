# open-lovable, read first-hand (28 Oct 2026)

Source: github.com/firecrawl/open-lovable at `main` (API metadata, README, file tree, `lib/sandbox/factory.ts`, `lib/sandbox/providers/vercel-provider.ts`, `config/app.config.ts`, `app/api/generate-ai-code-stream/route.ts`), fetched 2026-10-28. The founder said it "seems better" than Cloudflare VibeSDK as a base for the app engine.

## What it is
- Repo description: "Clone and recreate any website as a modern React app in seconds"; README one-liner: "Chat with AI to build React apps instantly". Firecrawl's example app; README points to Lovable.dev "for a complete cloud solution". MIT. 28,816 stars, 5,500 forks, 152 open issues, created 2025-08-08, last push 2026-10-09.
- A Next.js app. Two entry points: scrape a website with Firecrawl (`/api/scrape-website`, `scrape-url-enhanced`, `scrape-screenshot`, `extract-brand-styles`) and rebuild it as a React app; or build from a prompt. Pages: `app/page.tsx`, `app/builder/page.tsx`, `app/generation/page.tsx`.
- Most of `components/` is Firecrawl's marketing-site component library (hero flames, nav, logo clouds); the engine is ~15 files under `lib/` plus ~28 API routes under `app/api/`.

## The engine, exactly
- Sandbox: `lib/sandbox/factory.ts` picks `e2b` or `vercel` from `SANDBOX_PROVIDER` (default e2b), new instance per call, no caching. `lib/sandbox/types.ts` defines the provider interface; `sandbox-manager.ts` holds state.
- Vercel provider: `Sandbox.create({ timeout: 300000 /* 5 min */, runtime: 'node22', ports: [5173] })`; writes files with `writeFiles`, runs `npm install`, starts Vite with `nohup npm run dev &` and waits a fixed 7 s; preview URL = `sandbox.domain(5173)`; `isAlive()` is `!!this.sandbox`; no reconnect, no keepalive. Config says 15 min for Vercel, 30 min for E2B.
- Generation (`generate-ai-code-stream/route.ts`): no tool calls. One big system prompt (rules like "USE STANDARD TAILWIND CLASSES ONLY", "Simple style/text change = 1 file ONLY"); the model emits `<file path="...">` blocks parsed by regex after the stream ends; `<package>` tags for installs; `maxTokens` 8,192; default model `google/gemini-3-pro-preview`, also GPT-5, Sonnet 4, Kimi K2 via Groq; optional Vercel AI Gateway. Edit mode: `/api/analyze-edit-intent` → search plan over cached files → one target file named in the prompt; optional Morph fast-apply (requested in the prompt but the parser does not read the edit blocks). Truncation recovery off by default.
- Validation: `lib/build-validator.ts`; Vite error monitoring routes (`check-vite-errors`, `monitor-vite-logs`, `report-vite-error`, `restart-vite`). No browser-based self-test.
- Export: `/api/create-zip`.

## What it does not have
No database, no sign-in for the generated app, no deploy or hosting, no stable URL, no versions or rollback, no users or workspaces or multi-tenancy, no cost metering, no secrets handling, no scheduled jobs. One sandbox at a time per server process (globals), 5-minute Vercel sandbox with no keepalive.

## Read for the engine decision ("apps with their own infrastructure")
- What it gives: a small, readable skeleton of stages 3–5 of the reference pipeline (starter stack, agent edits in a sandbox, preview URL) and the chat-plus-preview UI shape; a two-provider sandbox factory worth copying as a pattern.
- What it does not give: stages 1–2 and 6–12 (intake/plan, provision, deploy, stable URL, changes, versions, self-test, metering), i.e. everything the "with their own infrastructure" decision adds. Its generation loop (regex-parsed file blocks, 8k tokens, no tools) is weaker than the agent loop Lightbulb already runs (Hermes/Claude with tools, browser, credentials).
- Versus VibeSDK: VibeSDK has stages 1–11 but on Cloudflare's stack under the operator's account; open-lovable has 3–5 on a swappable sandbox and nothing else. Neither provisions a customer-owned database or deploys to a customer-owned host; that is the Stripe Projects piece in both cases.
