/**
 * The backend this deployment talks to, as an origin (no path, no trailing slash).
 *
 * Browsers never use it: they call `/api/v1/*` on the site's own host and
 * `next.config.ts` rewrites that to `${apiOrigin()}/api/v1/*`. Server code
 * (server components, the admin fetcher) calls `${apiOrigin()}/api/v1` itself.
 *
 * `API_ORIGIN` is server-side and set per Vercel environment (production ->
 * the prod API; preview and the `dev` custom environment -> the dev API). On
 * Vercel an unset value throws so a build cannot silently aim at the wrong
 * backend; locally it defaults to the dev API.
 *
 * Imported by `next.config.ts`, so it must stay dependency-free.
 */
export function apiOrigin(): string {
  const raw = process.env.API_ORIGIN;
  if (!raw) {
    if (process.env.VERCEL === "1") throw new Error("API_ORIGIN is not set for this Vercel environment.");
    return "https://api-dev.thelightbulb.company";
  }
  return new URL(raw).origin;
}
