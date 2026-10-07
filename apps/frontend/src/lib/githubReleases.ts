/**
 * Shared GitHub Releases lookup for the desktop download + auto-updater
 * routes.
 *
 * Unpinned downloads and update checks resolve to the same pipeline:
 *   1. List TheLightbulbCompany/lightbulb releases
 *   2. Filter to the env-appropriate tag prefix (dev hosts → `desktop-dev-v*`,
 *      everything else → `desktop-v*`)
 *   3. Pick the HIGHEST-VERSION release, not the most-recently-published
 *      one — auto-publish on every main push races, and `published_at`
 *      can hand out an older bundle if two CI runs overlap.
 * Update manifests pin their download to that version's published tag so
 * a later release cannot change the bytes their signature verifies.
 *
 * The repo is PRIVATE: listing releases and fetching assets both require
 * `GITHUB_RELEASES_TOKEN` (fine-grained PAT with contents: read on
 * TheLightbulbCompany/lightbulb). `browser_download_url` 404s without a logged-in browser
 * session, so clients are never sent there — instead we resolve the
 * asset's API URL to the short-lived signed URL GitHub issues behind it
 * (`resolveAssetLocation`) and redirect at download time. Draft releases
 * are filtered explicitly since the token sees them.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

export const LIGHTBULB_REPO = "TheLightbulbCompany/lightbulb";

const PROD_TAG_PREFIX = "desktop-v";
const DEV_TAG_PREFIX = "desktop-dev-v";

const TAG_VERSION_RE = /^desktop-(?:dev-)?v(.+)$/;

export interface GhAsset {
  /** API asset URL (api.github.com/.../releases/assets/{id}) — auth-fetchable. */
  url: string;
  name: string;
  browser_download_url: string;
}

export interface GhRelease {
  tag_name: string;
  name?: string | null;
  body?: string | null;
  draft: boolean;
  published_at: string | null;
  assets: GhAsset[];
}

/** Standard GitHub API headers, with auth when the token is configured. */
export function githubHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  const token = process.env.GITHUB_RELEASES_TOKEN;
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

/** Only a manifest-issued release selection may create a distinct authenticated tag lookup. */
export function releaseDownloadToken(tag: string, arch: string, kind: string): string | null {
  const key = process.env.GITHUB_RELEASES_TOKEN;
  return key ? createHmac("sha256", key)
    .update(JSON.stringify(["desktop-release-download", tag, arch, kind])).digest("hex") : null;
}

export function validReleaseDownloadToken(tag: string, arch: string, kind: string, token: string | null): boolean {
  if (!token || !/^[a-f0-9]{64}$/.test(token)) return false;
  const expected = releaseDownloadToken(tag, arch, kind);
  return expected !== null && timingSafeEqual(Buffer.from(token, "hex"), Buffer.from(expected, "hex"));
}

/**
 * Hostname → release tag prefix. dev hosts (and previews + localhost) read
 * dev-pointing builds; everything else gets prod.
 */
export function tagPrefixFromHost(host: string | null): string {
  const h = (host ?? "").toLowerCase();
  if (
    h.startsWith("dev.") ||
    h.startsWith("localhost") ||
    h.endsWith(".vercel.app")
  ) {
    return DEV_TAG_PREFIX;
  }
  return PROD_TAG_PREFIX;
}

/** Strip the `desktop-(dev-)?v` prefix to get a semver-ish version string. */
export function versionFromTag(tag: string): string | null {
  const m = tag.match(TAG_VERSION_RE);
  return m ? m[1] : null;
}

/**
 * Numeric `a > b` over dotted versions. Only ever needs to handle x.y.z —
 * no pre-release suffix logic.
 */
export function isNewer(a: string, b: string): boolean {
  const pa = a.split(".").map((n) => parseInt(n, 10));
  const pb = b.split(".").map((n) => parseInt(n, 10));
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x > y) return true;
    if (x < y) return false;
  }
  return false;
}

/**
 * Fetch + filter + sort. Returns the highest-version published release
 * for the given tag prefix (or `null` if none).
 *
 * Returns `{ release: null, githubStatus: number }` on a non-2xx GitHub
 * response so the caller can surface `github_unavailable` with the upstream
 * status. Throws on transport / parse errors (caller's choice how to
 * surface those).
 */
export async function pickLatestRelease(
  prefix: string,
): Promise<{ release: GhRelease | null; githubStatus: number }> {
  const res = await fetch(
    `https://api.github.com/repos/${LIGHTBULB_REPO}/releases?per_page=30`,
    {
      headers: githubHeaders(),
      next: { revalidate: 300 },
    },
  );
  if (!res.ok) {
    return { release: null, githubStatus: res.status };
  }
  const releases: GhRelease[] = await res.json();
  const candidate = releases
    .filter((r) => {
      if (r.draft) return false;
      if (!r.tag_name.startsWith(prefix)) return false;
      return versionFromTag(r.tag_name) !== null;
    })
    .sort((a, b) => {
      const va = versionFromTag(a.tag_name)!;
      const vb = versionFromTag(b.tag_name)!;
      if (isNewer(vb, va)) return 1;
      if (isNewer(va, vb)) return -1;
      return 0;
    })[0];
  return { release: candidate ?? null, githubStatus: res.status };
}

/** Fetch the published release named by an updater manifest, even after it leaves the latest page. */
export async function getPublishedRelease(
  tag: string,
): Promise<{ release: GhRelease | null; githubStatus: number }> {
  const res = await fetch(
    `https://api.github.com/repos/${LIGHTBULB_REPO}/releases/tags/${encodeURIComponent(tag)}`,
    { headers: githubHeaders(), next: { revalidate: 300 } },
  );
  if (!res.ok) return { release: null, githubStatus: res.status };
  const release: GhRelease = await res.json();
  return {
    release: !release.draft && release.tag_name === tag ? release : null,
    githubStatus: res.status,
  };
}

/**
 * Resolve a release asset to the short-lived signed URL GitHub issues
 * behind its asset API endpoint. This is the only way to hand a private
 * repo's asset to an unauthenticated client (browser or Tauri updater):
 * fetch the API URL with `Accept: application/octet-stream` but don't
 * follow the redirect — forward its Location instead.
 *
 * The signed URL expires within minutes, so resolve it per-request at
 * download time — never bake it into anything cached or long-lived.
 */
export async function resolveAssetLocation(
  asset: GhAsset,
): Promise<{ location: string | null; status: number }> {
  const res = await fetch(asset.url, {
    headers: { ...githubHeaders(), Accept: "application/octet-stream" },
    redirect: "manual",
    cache: "no-store",
  });
  const location = res.headers.get("location");
  if (res.status >= 300 && res.status < 400 && location) {
    return { location, status: res.status };
  }
  return { location: null, status: res.status };
}
