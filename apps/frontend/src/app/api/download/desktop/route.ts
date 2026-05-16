import { NextRequest, NextResponse } from "next/server";

// Public repo — unauthenticated GitHub API calls return only published
// (non-draft) releases, which is exactly what we want. We still filter
// `!draft` defensively in case a token gets attached later.
const REPO = "Isol8AI/isol8";

// Hostname → release tag prefix. dev.isol8.co (and any *.vercel.app preview
// or localhost) serves dev-pointing builds; everything else gets prod.
const PROD_TAG_PREFIX = "desktop-v";
const DEV_TAG_PREFIX = "desktop-dev-v";

type Arch = "aarch64" | "x64";

function archFromQuery(raw: string | null): Arch {
  if (raw === "x64" || raw === "x86_64" || raw === "intel") return "x64";
  return "aarch64";
}

// Strip the `desktop-(dev-)?v` prefix to get a semver-ish string.
function versionFromTag(tag: string): string | null {
  const m = tag.match(/^desktop-(?:dev-)?v(.+)$/);
  return m ? m[1] : null;
}

// Cheap numeric compare. We only ever bump x.y.z so this is fine —
// no pre-release suffix handling needed for our tag scheme.
function isNewer(a: string, b: string): boolean {
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

function tagPrefixFromHost(host: string | null): string {
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

type GhAsset = { name: string; browser_download_url: string };
type GhRelease = {
  tag_name: string;
  draft: boolean;
  published_at: string | null;
  assets: GhAsset[];
};

export async function GET(req: NextRequest) {
  const arch = archFromQuery(req.nextUrl.searchParams.get("arch"));
  const prefix = tagPrefixFromHost(req.headers.get("host"));

  const res = await fetch(
    `https://api.github.com/repos/${REPO}/releases?per_page=30`,
    {
      headers: {
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      next: { revalidate: 300 },
    },
  );
  if (!res.ok) {
    return NextResponse.json(
      { error: "github_unavailable", status: res.status },
      { status: 502 },
    );
  }
  const releases: GhRelease[] = await res.json();
  // Pick by VERSION (highest semver-ish), not publish time. With auto-publish
  // on every main push, two CI runs can overlap and an older tag's release
  // can publish *after* a newer tag's. Sorting by `published_at` would then
  // hand the older binary to humans hitting this download URL. Tag-derived
  // version is the race-free truth.
  const published = releases
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
    });
  const release = published[0];
  if (!release) {
    return NextResponse.json(
      { error: "no_release", tag_prefix: prefix },
      { status: 404 },
    );
  }
  const suffix = `_${arch}.dmg`;
  const asset = release.assets.find((a) => a.name.endsWith(suffix));
  if (!asset) {
    return NextResponse.json(
      { error: "no_asset", tag: release.tag_name, arch },
      { status: 404 },
    );
  }
  return NextResponse.redirect(asset.browser_download_url, 302);
}
