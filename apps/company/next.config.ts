import type { NextConfig } from "next";
import path from "path";

const securityHeaders = [
  { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "SAMEORIGIN" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
];

const nextConfig: NextConfig = {
  // The monorepo root, so Turbopack does not guess from a stray lockfile above it.
  turbopack: { root: path.resolve(__dirname, "../../") },
  // Dev only: Next 16 blocks its dev chunks for any host it does not consider
  // same-origin, and 127.0.0.1 counts as foreign to localhost. Browser automation
  // reaches the dev server by IP.
  allowedDevOrigins: ["127.0.0.1"],
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
