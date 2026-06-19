import type { NextConfig } from "next";
import path from "path";

// Content-Security-Policy shipped in REPORT-ONLY mode first: it never blocks,
// only reports violations to the console, so we can refine the allowlist from
// real traffic before switching to an enforcing `Content-Security-Policy`
// header. PostHog is same-origin (reverse-proxied via /ingest below), so only
// the cross-origin vendors need listing: Clerk (auth), Stripe (billing),
// Cloudflare Turnstile (Clerk bot-check), Vercel (analytics scripts), plus our
// own REST API + WebSocket gateway for connect-src. 'unsafe-inline'/'unsafe-eval'
// are required until a nonce-based policy is wired (a follow-up before enforcing).
const reportOnlyCsp = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://js.stripe.com https://*.clerk.accounts.dev https://clerk.isol8.co https://challenges.cloudflare.com https://va.vercel-scripts.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  "connect-src 'self' https://*.clerk.accounts.dev https://clerk.isol8.co https://api.isol8.co https://api-dev.isol8.co https://api.stripe.com wss://ws.isol8.co wss://ws-dev.isol8.co https://us.i.posthog.com https://us-assets.i.posthog.com",
  "frame-src 'self' https://js.stripe.com https://hooks.stripe.com https://challenges.cloudflare.com https://*.clerk.accounts.dev https://clerk.isol8.co",
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

const nextConfig: NextConfig = {
  /* config options here */
  reactCompiler: true,

  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },

  turbopack: {
    root: path.resolve(__dirname, '../../'),
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
    // beforeFiles runs ahead of Next.js routing — required for the
    // company.* host rewrites because otherwise dev.company.isol8.co/foo
    // would match Next's `/foo` route (or fall through Clerk middleware
    // which redirects unsigned users away). Vercel-only rewrites in
    // vercel.json fire AFTER Next routing for the same reason. Putting
    // them in beforeFiles routes the request to the backend proxy
    // before Next sees it.
    return {
      beforeFiles: [
        {
          source: "/:path*",
          has: [{ type: "host", value: "dev.company.isol8.co" }],
          destination: "https://api-dev.isol8.co/__paperclip_proxy__/:path*",
        },
        {
          source: "/:path*",
          has: [{ type: "host", value: "company.isol8.co" }],
          destination: "https://api.isol8.co/__paperclip_proxy__/:path*",
        },
      ],
      afterFiles: [
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

export default nextConfig;
