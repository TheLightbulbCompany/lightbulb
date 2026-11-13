import type { NextConfig } from "next";
import withBundleAnalyzer from "@next/bundle-analyzer";
import path from "path";

import { LAUNCH_HOSTS, ROOT_DOMAIN, SITE_URL } from "./src/lib/domain";

const ENGINE_INSTALLER = "https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh";

const TURBOPACK_ROOT = path.resolve(__dirname, "../../");
// Our API origin, derived per-environment rather than enumerated. The static
// string this replaced named prod AND dev hosts at once, which cannot be right
// for both once the two environments sit on different domains.
const apiOrigin = (() => {
  const raw = process.env.NEXT_PUBLIC_API_URL;
  if (!raw) return `https://api.${ROOT_DOMAIN}`;
  try {
    return new URL(raw).origin;
  } catch {
    return `https://api.${ROOT_DOMAIN}`;
  }
})();

// Canvas frames apps: published ones on the apps domain, pre-publish opens on the Vercel preview URL.
const APPS_DOMAIN = process.env.NEXT_PUBLIC_APPS_DOMAIN || "apps.thelightbulb.company";

// A Clerk publishable key is `pk_(live|test)_<base64 of "<frontend-api-host>$">`,
// so the instance's own host comes out of the key this build was compiled with —
// more robust than a literal that silently goes stale. Anything unparseable
// falls back to the conventional host; the `*.clerk.accounts.dev` wildcard below
// covers development instances either way.
const clerkOrigins = (() => {
  const fallback = `https://clerk.${ROOT_DOMAIN}`;
  const key = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY ?? "";
  const encoded = key.replace(/^pk_(?:live|test)_/, "");
  if (!encoded || encoded === key) return [fallback];
  const host = Buffer.from(encoded, "base64").toString("utf8").replace(/\$+$/, "");
  const decoded = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(host) ? `https://${host}` : null;
  return [...new Set(decoded ? [decoded, fallback] : [fallback])];
})();

// Content-Security-Policy shipped in REPORT-ONLY mode first: it never blocks,
// only reports violations to the console, so we can refine the allowlist from
// real traffic before switching to an enforcing `Content-Security-Policy`
// header. PostHog is same-origin (reverse-proxied via /ingest below), so only
// the cross-origin vendors need listing: Clerk (auth), Stripe (billing),
// Cloudflare Turnstile (Clerk bot-check), Vercel (analytics scripts), plus our
// own REST API + AppSync Events realtime endpoint for connect-src.
// are required until a nonce-based policy is wired (a follow-up before enforcing).
//
// Termly (app.termly.io) needs BOTH script-src and connect-src: the SDK is a
// cross-origin <script> (components/legal/TermlyEmbed.tsx) which then fetches
// the policy body over XHR. Missing either blanks EVERY legal page — /privacy,
// /terms and the five other embedded documents — and a blank privacy-policy URL
// is an App Store 5.1.1 rejection, not merely a broken page. Report-only mode
// is the only reason that has not bitten; promoting this header to enforcing
// without these two entries is exactly the trap this note exists to prevent.
//
// Only OUR API/Clerk origins are derived above. Every third-party origin below
// third-party origin below stays a literal on purpose — Stripe, Termly,
// Cloudflare Turnstile, Vercel and PostHog are not ours to rename, and there is
// nothing to derive them from.
const reportOnlyCsp = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'self'",
  `script-src 'self' 'unsafe-inline' 'unsafe-eval' https://js.stripe.com https://connect-js.stripe.com https://*.clerk.accounts.dev ${clerkOrigins.join(" ")} https://challenges.cloudflare.com https://va.vercel-scripts.com https://app.termly.io`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  `connect-src 'self' https://*.clerk.accounts.dev ${clerkOrigins.join(" ")} ${apiOrigin} https://api.stripe.com wss://*.appsync-realtime-api.us-east-1.amazonaws.com https://us.i.posthog.com https://us-assets.i.posthog.com https://app.termly.io https://api.openai.com https://*.livekit.cloud wss://*.livekit.cloud`,
  // blob: is required for <iframe src="blob:...">, the PDF preview path (both
  // the new FileViewerDialog/FileContentViewer PDF branch and the pre-existing
  // Teams ArtifactViewerDialog use it) — measured live: without it, Chrome logs
  // a real `frame-src` CSP violation for a blob-URL iframe even though the
  // page that created the blob is same-origin. Report-Only means it doesn't
  // block today, but it would the moment this header is promoted to enforcing.
  `frame-src 'self' blob: https://js.stripe.com https://connect-js.stripe.com https://hooks.stripe.com https://challenges.cloudflare.com https://*.clerk.accounts.dev ${clerkOrigins.join(" ")} https://*.${APPS_DOMAIN} https://*.vercel.app`,
  "worker-src 'self' blob:",
  "manifest-src 'self'",
  "form-action 'self'",
].join("; ");

// Enforced response headers for the user-facing app. Vercel/Next.js is the
// canonical place for these — the FastAPI backend only covers the JSON API and
// the Paperclip SPA it proxies. HSTS is conservative (1y, no preload) and
// reversible.
const securityHeaders = [
  { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "SAMEORIGIN" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Content-Security-Policy-Report-Only", value: reportOnlyCsp },
];

// One id per build. deploy.yml runs `vercel build` in a GitHub Actions job,
// and ONE Vercel project takes both the dev preview and the prod deploy of the
// same commit (different API URL + Clerk key baked in), so the SHA alone would
// name two different builds — the run and the job's env prefix (dev-/prod-)
// keep them apart. Next caps a deploymentId at 32 characters. A Vercel-hosted
// build brings its own NEXT_DEPLOYMENT_ID, which Next insists on.
const { GITHUB_SHA, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, GITHUB_JOB } = process.env;
const BUILD_ID = process.env.NEXT_DEPLOYMENT_ID
  || (GITHUB_SHA
    ? `${GITHUB_SHA.slice(0, 7)}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${(GITHUB_JOB ?? "").split("-")[0]}`
      .replace(/[^A-Za-z0-9-]/g, "").slice(0, 32)
    : undefined);

// Retired thin landing pages (and one post about channels the product no longer
// offers), each 308'd to the nearest real page so old links and crawls land.
export const RETIRED_PAGE_REDIRECTS: { source: string; destination: string }[] = [
  ["/ai-agent-workspace", "/"],
  ["/ai-agent-marketplace", "/"],
  ["/autonomous-ai-company", "/"],
  ["/persistent-ai-agents", "/"],
  ["/ai-agents-for-discord", "/"],
  ["/ai-agents-for-telegram", "/"],
  ["/ai-chief-of-staff", "/"],
  ["/ai-operations-agent", "/"],
  ["/ai-customer-support-agent", "/"],
  ["/blog/connect-ai-agent-slack-discord-telegram", "/blog"],
  ["/blog/ai-native-company-operating-system", "/blog"],
  // Said Lightbulb runs "behind the scenes" of Slack; the comparison page is the one Slack story.
  ["/ai-agents-for-slack", "/compare/slack"],
  // Web session sharing went with the old chat app.
  ["/claude-code-session-sharing", "/"],
].map(([source, destination]) => ({ source, destination }));

// Only the production host may be indexed. Keyed on host, not VERCEL_ENV: the
// dev hosts are attached to the same Vercel project as production domains, so
// a dev deploy reports VERCEL_ENV=production too.
const indexableHost = `^${ROOT_DOMAIN.replace(/\./g, "\\.")}$`;

const nextConfig: NextConfig = {
  poweredByHeader: false,
  reactCompiler: true,

  // The deploy's identity, inlined into the client bundle AND served by
  // /api/version, so a long-lived tab (or the desktop webview) can tell it is
  // running an old build — components/DeployRefresh.tsx.
  env: { NEXT_PUBLIC_BUILD_ID: BUILD_ID ?? "local" },
  // Skew Protection is ON for the Vercel project (12 h max age), but deploy.yml
  // ships `vercel deploy --prebuilt`, where Vercel cannot assign the id at build
  // time: without a custom deploymentId no asset carried `?dpl=` and the
  // protection did nothing (vercel.com/changelog/skew-protection-now-supports-prebuilt-deployments). With it, an old tab's lazy chunks keep resolving
  // against its own deployment until DeployRefresh reloads it.
  deploymentId: BUILD_ID,

  // @lightbulb/shared ships TypeScript source (no build step) — Next must
  // transpile it like first-party code.
  transpilePackages: ["@lightbulb/shared"],

  // Rewrites barrel imports to direct module paths so one named import doesn't
  // drag in a package's whole surface. Only packages whose barrel actually
  // fans out are worth listing: `radix-ui` is the unified re-export of every
  // Radix primitive (tabs/select/collapsible pull from it). Next already
  // optimizes lucide-react, date-fns, recharts et al. by default, so
  // re-declaring those here is noise; three/gsap are single prebundled files
  // with nothing to split.
  experimental: {
    optimizePackageImports: ["radix-ui"],
  },

  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
      {
        source: "/:path*",
        missing: [{ type: "host" as const, value: indexableHost }],
        headers: [{ key: "X-Robots-Tag", value: "noindex" }],
      },
      {
        // Apple will not associate the domain unless this file is served as
        // application/json. It deliberately has no file extension, so Next
        // otherwise serves it as application/octet-stream and Universal Links
        // fail SILENTLY — the link just opens Safari instead of handing back to
        // the app, with no error surfaced anywhere. Verify after any deploy
        // that touches static serving:
        //   curl -I "$SITE_URL/.well-known/apple-app-site-association"
        source: "/.well-known/apple-app-site-association",
        headers: [{ key: "Content-Type", value: "application/json" }],
      },
    ];
  },

  async redirects() {
    return [
      // flickmeon.ai, the retired product domain: every path, static files
      // included (middleware's matcher skips those), 308s to the same path on
      // the product site; the query string carries over. Redirects run before
      // middleware, so Clerk never sees these hosts.
      ...LAUNCH_HOSTS.map((host) => ({
        source: "/:path*",
        has: [{ type: "host" as const, value: `^${host.replace(/\./g, "\\.")}$` }],
        destination: `${host.startsWith("dev.") ? `https://dev.${ROOT_DOMAIN}` : SITE_URL}/:path*`,
        permanent: true,
      })),
      ...RETIRED_PAGE_REDIRECTS.map((r) => ({ ...r, permanent: true })),
      // `curl -fsSL <site>/install | sh` installs the network engine: the
      // engine repo's own installer (curl -L follows the redirect).
      ...["/install", "/install.sh"].map((source) => ({
        source,
        destination: ENGINE_INSTALLER,
        permanent: false,
      })),
    ];
  },

  turbopack: {
    root: TURBOPACK_ROOT,
  },

  // Reverse-proxy PostHog through our own domain so ad-blockers /
  // privacy extensions (uBlock, Brave shields, NextDNS, etc.) don't
  // block telemetry. Requests go to isol8.co/ingest/* which Vercel
  // rewrites to the PostHog ingestion + asset origins. Per PostHog's
  // documented pattern: https://posthog.com/docs/advanced/proxy/nextjs
  // Order matters: /ingest/static/* must come before the catch-all.
  // skipTrailingSlashRedirect is required so PostHog's `decide`
  // endpoint is reached without a 307.
  skipTrailingSlashRedirect: true,
  async rewrites() {
    const localDevApiProxy =
      process.env.NODE_ENV === "development" && process.env.LOCAL_DEV_API_PROXY === "1"
        ? [
            {
              source: "/api/v1/:path*",
              destination: "https://api-dev.thelightbulb.company/api/v1/:path*",
            },
          ]
        : [];

    return {
      afterFiles: [
        ...localDevApiProxy,
        {
          source: "/ingest/static/:path*",
          destination: "https://us-assets.i.posthog.com/static/:path*",
        },
        {
          source: "/ingest/:path*",
          destination: "https://us.i.posthog.com/:path*",
        },
        {
          source: "/ingest/decide",
          destination: "https://us.i.posthog.com/decide",
        },
      ],
    };
  },
};

// Bundle analysis is opt-in via ANALYZE=true — with the flag unset the plugin
// hands back the config object untouched, so dev, CI and Vercel builds are
// unaffected. Note this is the *webpack* analyzer while `next build` defaults
// to Turbopack, so the two are split across scripts: `pnpm analyze` runs
// Turbopack's own analyzer (the bundler we actually ship), while
// `pnpm analyze:webpack` forces --webpack to get the treemap report.
export default withBundleAnalyzer({ enabled: process.env.ANALYZE === "true" })(
  nextConfig,
);
