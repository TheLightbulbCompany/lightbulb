/**
 * Shared GitHub Releases lookup for the desktop download + auto-updater
 * routes.
 *
 * Both `/api/download/desktop` and `/api/desktop/update/[target]/[current_version]`
 * resolve to the same pipeline:
 *   1. List Isol8AI/isol8 releases
 *   2. Filter to the env-appropriate tag prefix (dev hosts → `desktop-dev-v*`,
 *      everything else → `desktop-v*`)
 *   3. Pick the HIGHEST-VERSION release, not the most-recently-published
 *      one — auto-publish on every main push races, and `published_at`
 *      can hand out an older bundle if two CI runs overlap.
 *
 * Public repo, so unauthenticated requests already filter out drafts; we
 * still guard with `!draft` defensively in case a token gets attached.
 */

export const ISOL8_REPO = "Isol8AI/isol8";

const PROD_TAG_PREFIX = "desktop-v";
const DEV_TAG_PREFIX = "desktop-dev-v";

const TAG_VERSION_RE = /^desktop-(?:dev-)?v(.+)$/;

export interface GhAsset {
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
    `https://api.github.com/repos/${ISOL8_REPO}/releases?per_page=30`,
    {
      headers: {
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
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
