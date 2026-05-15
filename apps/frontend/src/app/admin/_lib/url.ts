import "server-only";

/**
 * Resolve the backend API base URL for the admin server-side fetcher.
 *
 * Reads `NEXT_PUBLIC_API_URL`, the same build-time-baked value the chat app
 * uses in `src/lib/api.ts`. Each Vercel deployment bakes the env-appropriate
 * URL into its bundle via the `NEXT_PUBLIC_API_URL_DEV` / `_PROD` GitHub
 * secrets plumbed by the deploy workflow.
 *
 * Why not a separate server-only `API_URL`: Vercel project env vars are
 * resolved at runtime and apply across every deployment, so a single
 * project-wide `API_URL` overrode the per-deploy baked value and made the
 * dev deployment's Server Components / Server Actions call the prod backend.
 * The chat app's backend URL is already public (same hostname, served in
 * every client bundle), so "server-only" added no real concealment — only
 * a foot-gun.
 *
 * Throws if `NEXT_PUBLIC_API_URL` is unset so misconfiguration fails loudly
 * instead of silently routing to a wrong default.
 *
 * Exported as a function (rather than a top-level constant) so unit tests can
 * override `process.env` and re-invoke without module-level evaluation.
 */
export function apiUrl(): string {
  const url = process.env.NEXT_PUBLIC_API_URL;
  if (!url) {
    throw new Error(
      "NEXT_PUBLIC_API_URL is not set — admin server fetcher cannot resolve a backend URL.",
    );
  }
  return url;
}
