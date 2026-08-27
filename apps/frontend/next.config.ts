import type { NextConfig } from "next";
import withBundleAnalyzer from "@next/bundle-analyzer";
import path from "path";

import { ROOT_DOMAIN } from "./src/lib/domain";

// Our own origins, derived per-environment rather than enumerated. The static
// string this replaced named prod AND dev hosts at once, which cannot be right
// for both once the two environments sit on different domains.
//
// `apiOrigin` -> `wsOrigin` is the same api→ws hostname rewrite the client uses
// (`@isol8/shared`'s deriveWebSocketUrl), inlined because next.config runs in
// plain Node before that package is transpiled.
const apiOrigin = (() => {
  const raw = process.env.NEXT_PUBLIC_API_URL;
  if (!raw) return `https://api.${ROOT_DOMAIN}`;
  try {
    return new URL(raw).origin;
  } catch {
    return `https://api.${ROOT_DOMAIN}`;
  }
})();

const wsOrigin = apiOrigin
  .replace(/^https:\/\//, "wss://")
  .replace(/^http:\/\//, "ws://")
  .replace(/\/\/api(?=[-.])/, "//ws");

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
// own REST API + WebSocket gateway for connect-src. 'unsafe-inline'/'unsafe-eval'
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
// Only OUR origins are derived (apiOrigin/wsOrigin/clerkOrigins above). Every
// third-party origin below stays a literal on purpose — Stripe, Termly,
// Cloudflare Turnstile, Vercel and PostHog are not ours to rename, and there is
// nothing to derive them from.
const reportOnlyCsp = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'self'",
  `script-src 'self' 'unsafe-inline' 'unsafe-eval' https://js.stripe.com https://*.clerk.accounts.dev ${clerkOrigins.join(" ")} https://challenges.cloudflare.com https://va.vercel-scripts.com https://app.termly.io`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  `connect-src 'self' https://*.clerk.accounts.dev ${clerkOrigins.join(" ")} ${apiOrigin} https://api.stripe.com ${wsOrigin} https://us.i.posthog.com https://us-assets.i.posthog.com https://app.termly.io`,
  `frame-src 'self' https://js.stripe.com https://hooks.stripe.com https://challenges.cloudflare.com https://*.clerk.accounts.dev ${clerkOrigins.join(" ")}`,
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

  // @isol8/shared ships TypeScript source (no build step) — Next must
  // transpile it like first-party code.
  transpilePackages: ["@isol8/shared"],

  // Rewrites barrel imports to direct module paths so one named import doesn't
  // drag in a package's whole surface. Only packages whose barrel actually
  // fans out are worth listing: `radix-ui` is the unified re-export of every
  // Radix primitive (tabs/select/collapsible pull from it). Next already
  // optimizes lucide-react, date-fns, recharts et al. by default, so
  // re-declaring those here is noise; three/gsap are single prebundled files
  // with nothing to split. react-syntax-highlighter is deliberately absent:
  // MessageList deep-imports the prism-light build and per-language modules
  // directly, so its barrel is never pulled and there would be nothing to
  // rewrite.
  experimental: {
    optimizePackageImports: ["radix-ui"],
  },

  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
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
    // company.* host rewrites because otherwise dev.company.<root>/foo
    // would match Next's `/foo` route (or fall through Clerk middleware
    // which redirects unsigned users away). Vercel-only rewrites in
    // vercel.json fire AFTER Next routing for the same reason. Putting
    // them in beforeFiles routes the request to the backend proxy
    // before Next sees it.
    return {
      beforeFiles: (
        [
          [`dev.company.${ROOT_DOMAIN}`, `https://api-dev.${ROOT_DOMAIN}`],
          [`company.${ROOT_DOMAIN}`, `https://api.${ROOT_DOMAIN}`],
        ] as const
      ).map(([host, api]) => ({
        source: "/:path*",
        has: [{ type: "host" as const, value: host }],
        destination: `${api}/__paperclip_proxy__/:path*`,
      })),
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

// Bundle analysis is opt-in via ANALYZE=true — with the flag unset the plugin
// hands back the config object untouched, so dev, CI and Vercel builds are
// unaffected. Note this is the *webpack* analyzer while `next build` defaults
// to Turbopack, so the two are split across scripts: `pnpm analyze` runs
// Turbopack's own analyzer (the bundler we actually ship), while
// `pnpm analyze:webpack` forces --webpack to get the treemap report.
export default withBundleAnalyzer({ enabled: process.env.ANALYZE === "true" })(
  nextConfig,
);
