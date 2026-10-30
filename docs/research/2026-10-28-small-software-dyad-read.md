# Dyad, read first-hand (28 Oct 2026)

Source: github.com/dyad-sh/dyad at `main` (API metadata, README, root tree, `src/` tree, `src/pro/*` listings, `src/pro/LICENSE`, `PRODUCT.md`), dyad.sh/docs (Supabase integration, release 1.14.0), dyad.sh/pro; fetched 2026-10-28.

## What it is
- "Local, open-source AI app builder for power users; v0 / Lovable / Replit / Bolt alternative." PRODUCT.md: primary user is a non-technical builder "often coming from Lovable, v0, or Bolt-style tools", running Dyad "locally on Mac or Windows"; local-first is "the core differentiator"; "no lock-in"; bring your own API keys.
- A desktop app (Electron Forge: `forge.config.ts`, `vite.main/preload/renderer` configs, `makers/`, `native/`, `windowsSign.ts`). No hosted or web version; nothing about cloud, hosting or multi-user in PRODUCT.md or the Pro page.
- 22,097 stars, 2,783 forks, 355 open issues, created 2025-04-11, last push 2026-10-28 (today). TypeScript.

## Licence
- Outside `src/pro`: Apache-2.0. Inside `src/pro`: Functional Source License 1.1 (ALv2 future licence), licensor Dyad Tech, Inc. "Competing Use" = making the software available in a commercial product that "substitutes for the Software", substitutes for any Dyad product that exists at release, or "offers the same or substantially similar functionality"; converts to Apache-2.0 two years after each release. `src/pro` holds `main/ipc/{handlers,processors}`, `main/prompts/turbo_edits_v2_prompt.ts`, `main/utils`, `shared/search_replace_*`, `ui`.
- Read: using the Apache part inside Lightbulb is fine; using anything under `src/pro` to offer an app builder would be a Competing Use for two years.

## What the engine has (top-level `src/` directories)
`app/`, `app_run/`, `app_wiring/`, `chat_stream/`, `cloudflare_deploy/` (api, build_config, convert_prompt, fix_prompt, targets), `coolify_deploy/`, `coolify_setup/`, `connection_flow/`, `db/` + `drizzle/` (local SQLite via Drizzle), `first_prompt/`, `github_ops/`, `image_generation/`, `ipc/`, `mcp_oauth/`, `neon_admin/`, `supabase_admin/`, `plan_handoff/`, `preview_console/`, `preview_iframe/`, `prompts/`, `screenshot/`, `state_machines/`, `test_run_queue/`, `version_preview/`, `voice_to_text/`, `distributed_machines/` (actor_host, remote_client, remote_transport, remote_protocol, remote_manifest, keyed_admission_gate: looks like a remote/host execution layer in progress; purpose unverified [U]). Root also has `scaffold/` (the starter app: Vite + React + Tailwind + shadcn `components.json`, `AI_RULES.md`, `vercel.json`), `workers/`, `benchmarks/`, `e2e-tests/` (Playwright), `plans/`, `rules/`.
- Backends: "Connect Supabase" opens the browser to approve and pick an organisation; token stored locally; 1.14.0: "create a Supabase project directly within Dyad"; generates Supabase Auth (email/password), tables, "reasonable default RLS policies, but you should review and adjust them", Edge Functions as "Server Functions" to keep keys out of the browser; Neon-backed apps supported (`neon_admin/`).
- Deploy: Cloudflare (`cloudflare_deploy/`), Coolify over SSH (1.14.0); Vercel listed as a topic; GitHub push (`github_ops/`).
- Modes: Build / Ask / Agent. Free "Basic Agent mode" limited to 20 messages/day (build-error checks, automatic test runs, log-based debugging). "Pro Agent mode" on Pro credits; Pro adds web search, website cloning, image generation, Turbo Edits, "Auto" model (Grok 4.6 today), "Auto Sidekick" delegating implementation to a cheaper model. Pro $20/month with 200 AI credits (1.5 credits per million tokens as an orchestration fee); Max $79/month, 900 credits.

## Read for the engine decision
- Dyad is the most complete open-source builder on the backend side: it creates the Supabase project, writes auth and RLS, deploys to Cloudflare or Coolify, keeps versions in git, and has a real agent mode with tests. That is stages 1–3, 6–7, 9–11 of the reference pipeline.
- It is a desktop app. The code runs on the user's Mac or Windows machine; the preview is a local dev server in an Electron window; app state is local SQLite. To be Lightbulb's engine it would have to run headless per build in a server environment, or have `src/main` lifted out of Electron, and the best agent parts sit behind Pro credits and the FSL non-compete. The `distributed_machines` layer hints at a remote/host mode, unverified.
- Versus open-lovable: far more complete, far more code, and a licence line through the middle. Versus VibeSDK: more vendor-neutral (Supabase/Neon/Cloudflare/Coolify) but not a server.
