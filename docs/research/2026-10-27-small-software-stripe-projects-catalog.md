# Stripe Projects catalog, read first-hand on 2026-10-27

Source: `stripe projects catalog --json` on this Mac (Stripe CLI, projects plugin), catalog last_updated 2026-10-28T02:39:02.236Z. Read-only; no project was initialized. Columns: categories, provider, service, kind (deployable = a resource an agent can create; plan = an account tier), scope, pricing, description. Filtered to the categories a small tool needs: compute (hosting), database, auth, storage, domains, email, cdn, sandbox, queue, communications. The Lightbulb rail (branch `feat/stripe-projects-rail-v2`) drives this catalog through an approval card: ask → awaiting_approval → connecting_provider → awaiting_user_action → provisioning → binding → active; it ships dark behind a dev flag and the account's `projects` capability (branch CLAUDE.md, 27 Oct).

| Categories | Provider | Service | Kind | Scope | Pricing | Description |
|---|---|---|---|---|---|---|
| ai/email/messaging | AgentMail | developer | plan | project | paid | For developers making small scale applications |
| ai/email/messaging | AgentMail | free | plan | project | free | Build on AgentMail for free :) |
| ai/email/messaging | AgentMail | startup | plan | project | paid | For production workloads |
| analytics/compute/ci/ai/storage/cdn/observability | GitLab | project | deployable | project | free | GitLab project with built-in CI/CD, container registry, and collaboration tools |
| analytics/database | ClickHouse | basic | plan | account | paid | ClickHouse Cloud Basic — single-replica managed ClickHouse for development, prototyping, and small analytical  |
| analytics/database | ClickHouse | clickhouse | deployable | account | component | A ClickHouse Cloud service. Provision as many as you need under your active tier; compute and storage are mete |
| analytics/database | ClickHouse | enterprise | plan | account | paid | ClickHouse Cloud Enterprise — dedicated-infrastructure managed ClickHouse with enhanced support and the larges |
| analytics/database | ClickHouse | scale | plan | account | paid | ClickHouse Cloud Scale — production-grade managed ClickHouse with multi-replica baseline, autoscaling, and bro |
| auth | Auth0 | b2b-essentials | plan | project | paid | Auth0 B2B - Essentials Plan |
| auth | Auth0 | b2b-professional | plan | project | paid | Auth0 B2B - Professional Plan |
| auth | Auth0 | b2c-essentials | plan | project | paid | Auth0 B2C - Essentials Plan |
| auth | Auth0 | b2c-professional | plan | project | paid | Auth0 B2C - Professional Plan |
| auth | Auth0 | client | deployable | project | component | Setup a mobile, web or IoT application to use Auth0 for authentication. |
| auth | Auth0 | free | plan | project | free | Auth0 Free Plan |
| auth | Clerk | auth | deployable | project | component | Clerk Authentication — drop-in auth for any framework. Free by default, paid under Pro. |
| auth | Clerk | hobby | plan | project | free | Clerk Hobby plan — authentication and user management for side projects and small apps. 50,000 MAU included. |
| auth | Clerk | pro | plan | project | paid | Clerk Pro plan — production-ready auth with MFA, SSO, custom domains, remove branding. 50,000 MAU included, th |
| auth | WorkOS | auth | deployable | project | paid | WorkOS AuthKit — drop-in authentication with SSO, MFA, and user management. |
| auth/payments | Privy | app | deployable | project | component | Auth and wallet functionality for your app |
| auth/payments | Privy | core | plan | account | paid | Privy Core — up to 2,500 MAU. 50K free monthly wallet signatures. Includes Custom JWT auth, Custom OAuth, fiat |
| auth/payments | Privy | enterprise | plan | account | paid | Privy Enterprise — contact sales to provision. |
| auth/payments | Privy | free | plan | account | free | Privy Free — 0-499 MAU. 50K free monthly wallet signatures and $1M transaction volume included. |
| auth/payments | Privy | scale | plan | account | paid | Privy Scale — up to 10,000 MAU. 50K free monthly wallet signatures. All Core features included. |
| cache/database | Upstash | redis | deployable | project | paid | Upstash Redis - Serverless Redis. |
| cdn | herenow | developer | plan | account | paid | here.now Developer plan — $20/month: unlimited sites, 2 TB storage, 20 custom domains, vanity subdomain namesp |
| cdn | herenow | hobby | plan | account | paid | here.now Hobby plan — $4/month: up to 1,000 sites, 500 GB storage, 5 custom domains, vanity subdomain namespac |
| communications | AgentPhone | number | deployable | project | paid | Provision a phone number with voice and messaging capabilities for your AI agent. Get a US or CA number and st |
| communications | PostalForm | mail | deployable | project | paid | Agentic postal mail infrastructure for uploading PDFs, quoting mailpieces, routing to the right print-mail rai |
| communications | Quo | app | deployable | project | component | Business phone numbers, calling, and messaging. Paid Starter plan with a 7-day free trial. |
| communications | Quo | starter | plan | account | paid | Starter plan for a Quo workspace |
| compute | Cloudflare | containers | deployable | project | component | Run code written in any programming language, built for any runtime, as part of apps built on Workers. |
| compute | Cloudflare | workers | deployable | project | component | Build, deploy, and scale serverless apps and APIs across Cloudflare's global network. |
| compute | Cloudflare | workers:free | plan | project | free | Shared free plan for Workers-backed deployables such as Workers, KV, Hyperdrive, Queues, D1, Browser Run, and  |
| compute | Cloudflare | workers:paid | plan | project | paid | Shared paid plan for Workers-backed deployables with a $5/month minimum charge, included usage, and additional |
| compute | E2B | hobby | plan | project | paid | E2B Hobby plan for pay-as-you-go sandbox usage. |
| compute | E2B | pro | plan | project | paid | Monthly E2B Pro plan plus pay-as-you-go usage. |
| compute | Flyio | app | deployable | project | paid | Deploy and run your applications globally |
| compute | Flyio | sprite | deployable | project | paid | Stateful sandbox environments with checkpoint & restore |
| compute | Laravel_Cloud | application | deployable | project | component | Laravel application with a default production environment. |
| compute | Laravel_Cloud | growth | plan | account | paid | Growth — $20/mo + usage |
| compute | Laravel_Cloud | starter | plan | account | paid | Starter — $5/mo + usage |
| compute | Netlify | project | deployable | project | free | A hosted website backed by Netlify's global CDN. Includes serverless functions, databases, auth, storage, AI m |
| compute | Railway | free | plan | project | free | Trial plan with limited resources. 500 hours of compute, 512 MB RAM, shared CPU, 1 GB disk. |
| compute | Railway | hobby | plan | project | paid | For personal projects. $5/month with $5 included usage. Up to 48 vCPU, 48 GB RAM, 5 replicas, 5 GB disk. |
| compute | Railway | hosting | deployable | project | component | Deploy a GitHub repository (public or private) or Docker image on Railway. |
| compute | Railway | pro | plan | project | paid | For teams and production workloads. $20/month with $20 included usage. Up to 1,000 vCPU, 1 TB RAM, 42 replicas |
| compute | Render | static-site | deployable | project | free | Free static site hosting with global CDN |
| compute | Render | web-service | deployable | project | paid | Managed web service with pricing based on instance type |
| compute | Vercel | hobby | plan | project | free | The perfect starting place for your web app or personal project. |
| compute | Vercel | pro | plan | project | paid | Everything you need to build and scale your app. |
| compute | Vercel | project | deployable | project | component | An application deployed from a Git repository with automatic deployments on every branch push. |
| compute | WordPress.com | site | deployable | project | paid | Free and Paid fully-managed WordPress sites with unlimited power, unrivaled speed, and rock-solid reliability. |
| compute | createos | beginner-monthly | plan | account | paid | Beginner plan billed monthly at $10. |
| compute | createos | enterprise-monthly | plan | account | paid | Enterprise plan billed monthly at $200. |
| compute | createos | free | plan | account | free | Free tier with basic features. |
| compute | createos | pro-monthly | plan | account | paid | Pro plan billed monthly at $75. |
| compute | createos | project | deployable | project | component | Deploy and manage cloud workloads on CreateOS. |
| compute/ai | Blaxel | tier-1 | plan | account | paid | $20/month top-up enables Tier 1. |
| compute/ai | Blaxel | tier-2 | plan | account | paid | $50/month top-up enables Tier 2. |
| compute/ai | Blaxel | tier-3 | plan | account | paid | $200/month top-up enables Tier 3. |
| compute/ai | Blaxel | tier-4 | plan | account | paid | $500/month top-up enables Tier 4. |
| compute/ai | Blaxel | tier-5 | plan | account | paid | $1,500/month top-up enables Tier 5. |
| compute/ai | Blaxel | tier-6 | plan | account | paid | $4,000/month top-up enables Tier 6. |
| compute/ai/sandbox | Daytona | sandbox | deployable | project | component | Daytona Sandbox - cloud compute environment for AI agents and developers. New accounts receive $100 in free cr |
| compute/ai/sandbox | Daytona | top-up-0025 | plan | project | paid | Add $25/mo in Daytona credits for compute usage. |
| compute/ai/sandbox | Daytona | top-up-0500 | plan | project | paid | Add $500/mo in Daytona credits for compute usage. |
| compute/ai/sandbox | Daytona | top-up-1000 | plan | project | paid | Add $1000/mo in Daytona credits for compute usage. |
| compute/ai/sandbox | Daytona | top-up-2000 | plan | project | paid | Add $2000/mo in Daytona credits for compute usage. |
| compute/ai/storage | Blaxel | agent-drive | deployable | project | free | Shared filesystem for agents to work on the same files across multiple sandboxes. |
| compute/cache/ci | Depot | api | deployable | project | component | Depot API access for automating builds, CI runners, cache, and sandbox workflows. |
| compute/cache/ci | Depot | developer-plan | plan | account | paid | Depot Developer plan for accelerated builds, CI runners, and build cache. |
| compute/cache/ci | Depot | startup-plan | plan | account | paid | Depot Startup plan for accelerated builds, CI runners, and build cache. |
| compute/database/auth/ai | Base44_Projects | app | deployable | project | free | A Base44 app with AI services, hosting, database and auth. |
| compute/sandbox | E2B | sandbox | deployable | project | component | Team-scoped E2B API access for creating and managing cloud sandboxes. |
| compute/sandbox/ai | Blaxel | sandbox | deployable | project | free | Secure sandboxes that instantly suspend when idle, and resume in 25 ms. |
| database | ClickHouse | postgres | deployable | account | free | Managed Postgres Scale — production-grade fully managed PostgreSQL with configurable VM sizes, high availabili |
| database | Cloudflare | d1 | deployable | project | component | Serverless SQL databases with SQLite semantics, Worker and HTTP API access, and built-in point-in-time recover |
| database | Cloudflare | hyperdrive | deployable | project | component | Connect Cloudflare Workers to existing Postgres or MySQL databases with connection pooling, query caching, and |
| database | Flyio | mpg | deployable | project | paid | Managed PostgreSQL — high-availability clusters with automatic failover, daily backups, and connection pooling |
| database | Laravel_Cloud | mysql | deployable | project | component | Managed MySQL database with automated backups and point-in-time recovery. |
| database | Neon | free | plan | project | free | Neon Free — serverless Postgres for prototypes and side projects |
| database | Neon | launch | plan | project | paid | Neon Launch — serverless Postgres for startups and growing teams |
| database | PlanetScale | mysql | deployable | project | paid | Fully managed MySQL database on PlanetScale |
| database | PlanetScale | postgresql | deployable | project | paid | Fully managed Postgres database on PlanetScale |
| database | Prisma | business | plan | account | paid | Business: includes 50M queries and 100 GiB-month storage per billing cycle; usage beyond the included amounts  |
| database | Prisma | database | deployable | project | component | Prisma Postgres — managed Postgres for Stripe Projects. |
| database | Prisma | free | plan | account | free | Free: includes 100k queries and 2 GiB-month storage per billing cycle; hard limits, no paid overages. |
| database | Prisma | pro | plan | account | paid | Pro: includes 10M queries and 50 GiB-month storage per billing cycle; usage beyond the included amounts is bil |
| database | Prisma | starter | plan | account | paid | Starter: includes 1M queries and 10 GiB-month storage per billing cycle; usage beyond the included amounts is  |
| database | Railway | mongo | deployable | project | component | Managed MongoDB database on Railway. |
| database | Railway | postgres | deployable | project | component | Managed PostgreSQL database on Railway. |
| database | Render | postgres | deployable | project | paid | Managed PostgreSQL database instance with pricing based on instance type |
| database | Turso | database | deployable | project | component | Free SQLite-based database that's always on: no cold starts. Instant response every time. Concurrent writes. U |
| database | Turso | developer | plan | account | paid | Unlimited DBs (500 active), 9 GB storage, 2.5B rows read, 25M rows written, 10 GB syncs, 10-day PITR |
| database | Turso | developer_overages | plan | account | paid | Unlimited DBs (500 active), 9 GB storage, 2.5B rows read, 25M rows written, 10 GB syncs, 10-day PITR |
| database | Turso | pro | plan | account | paid | Unlimited DBs (10,000 active), 50 GB storage, 250B rows read, 250M rows written, 100 GB syncs, 90-day PITR |
| database | Turso | pro_overages | plan | account | paid | Unlimited DBs (10,000 active), 50 GB storage, 250B rows read, 250M rows written, 100 GB syncs, 90-day PITR |
| database | Turso | scaler | plan | account | paid | Unlimited DBs (2,500 active), 24 GB storage, 100B rows read, 100M rows written, 24 GB syncs, 30-day PITR |
| database | Turso | scaler_overages | plan | account | paid | Unlimited DBs (2,500 active), 24 GB storage, 100B rows read, 100M rows written, 24 GB syncs, 30-day PITR |
| database | Turso | starter | plan | account | free | 100 DBs, 5 GB storage, 500M rows read, 10M rows written, 3 GB syncs, 1-day PITR |
| database/ai | Chroma | database | deployable | project | paid | A hosted search database for AI applications |
| database/ai | Upstash | vector | deployable | project | paid | Upstash Vector - Serverless vector database. |
| database/auth | Neon | postgres | deployable | project | component | Postgres with built-in auth — by Databricks |
| database/auth/storage | Supabase | free | plan | project | free | Supabase Free Plan: Unlimited API requests • Shared CPU • 500 MB RAM • 50K MAU • 500 MB database space • 5 GB  |
| database/auth/storage | Supabase | pro | plan | project | paid | Supabase Pro Plan: Dedicated CPU • 1 GB RAM • 100K MAU • 8 GB database space • 250 GB bandwidth • 100 GB file  |
| database/auth/storage | Supabase | project | deployable | project | component | A Supabase project with database, auth, and storage |
| database/auth/storage | Supabase | team | plan | project | paid | Supabase Team Plan: SOC2 • SSO for Supabase Dashboard • Priority email support & SLAs • 28-day log retention |
| database/cache | Railway | redis | deployable | project | component | Managed Redis cache on Railway. |
| database/search/ai/storage | Supermemory | memory | deployable | project | paid | Memory for AI agents - context, SuperRAG, file system, profile, and connectors. |
| domains | Cloudflare | registrar:domain | deployable | project | paid | Register a domain through Cloudflare Registrar as a one-time purchase. Cloudflare will ask for search keywords |
| domains | Spaceship | domain | deployable | project | paid | Find the perfect domain for your project with Spaceship powered domain registration. |
| domains | Squarespace | domain | deployable | project | paid | Register a domain with Squarespace |
| domains | WordPress.com | domain | deployable | project | paid | Domain registration |
| email | AgentMail | api | deployable | project | component | AgentMail API — create inboxes to send and receive email programmatically for AI agents |
| email | Resend | email | deployable | project | component | Email for developers. One API key per project, one shared sending quota. |
| email | Resend | free | plan | account | free | Resend Free: 3,000 transactional emails per month, 3 domains. |
| email | Resend | pro | plan | account | paid | Resend Pro: 50,000 transactional emails per month, 10 domains. |
| email | Twilio | business | plan | project | paid | Pay as you go, starting balance includes ~38,400 emails + 3000 free trial emails, automatically top up to $50  |
| email | Twilio | email | deployable | project | component | Send emails at scale |
| email | Twilio | hobbyist | plan | project | paid | Pay as you go, starting balance includes ~15,300 emails + 3000 free trial emails, automatically top up to $20  |
| media/storage/cdn | Cloudinary | free | plan | account | free | Cloudinary Free plan — programmable media for one product environment |
| media/storage/cdn | Cloudinary | media | deployable | project | component | The media layer for your app. Image and video API to upload or generate, store, tag, and manage media, then tr |
| notification/email/messaging | customerio | builder | plan | account | paid | Production messaging for email, in app messages, push notifications, and webhooks. Add funds to start sending. |
| notification/email/messaging | customerio | builder:sandbox | plan | account | free | Unlimited email sends in sandbox mode to up to 10 verified recipients. Test your integration and build your pr |
| notification/email/messaging | customerio | workspace | deployable | project | component | Customer.io workspace — multi-channel messaging for email, push, SMS, and webhooks. |
| queue | Cloudflare | queues | deployable | project | component | Send and receive messages with guaranteed delivery, batching, delays, retries, and dead-letter queues. |
| queue | Inngest | app | deployable | project | component | An Inngest app for deploying reliable production workflows |
| queue | Inngest | growth-2026-09-22 | plan | project | paid | Business — Inngest billing plan. |
| queue | Inngest | hobby | plan | project | free | Inngest free tier - includes up to 50,000 function executions/month, unlimited functions, and all core feature |
| queue | Inngest | pro | plan | project | paid | Inngest Pro - production-grade durable workflow engine. Includes 1M function executions/month, 7 day traces an |
| storage | Cloudflare | r2:bucket | deployable | project | paid | Create an R2 bucket with S3-compatible access for object storage, uploads, downloads, and usage-based billing. |
| storage | Railway | bucket | deployable | project | component | S3-compatible object storage on Railway. Powered by Tigris. |
| storage/cdn | herenow | hosting | deployable | project | component | here.now hosting — publish HTML, files, and full sites to live URLs at {slug}.here.now via the here.now API. P |

Counts by category across the whole catalog:

- ai: 56
- compute: 46
- database: 40
- auth: 21
- analytics: 21
- feature_flags: 16
- search: 14
- email: 13
- browser: 13
- storage: 12
- observability: 11
- payments: 10
- sandbox: 7
- messaging: 7
- cache: 7
- cdn: 6
- queue: 5
- notification: 4
- domains: 4
- communications: 4
- ci: 4
- ecommerce: 3
- media: 2

Not in the catalog on 27 Oct: Lovable, Val Town, Deno Deploy, Floot. Present and notable for this capability: Base44_Projects app (hosting + database + auth + AI, free), Supabase project, Neon postgres with built-in auth, Turso, Cloudflare workers/d1/kv/containers/queues, Vercel project, Netlify project, Render static-site, Railway hosting, Fly.io app, Clerk/Auth0/WorkOS auth, Resend/Twilio email, Quo/AgentPhone numbers, Cloudflare/Spaceship/Squarespace domains, E2B/Daytona/Blaxel sandboxes, Inngest workflows.
