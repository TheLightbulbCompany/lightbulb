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
  const published = releases
    .filter((r) => !r.draft && r.tag_name.startsWith(prefix))
    .sort((a, b) =>
      (b.published_at ?? "").localeCompare(a.published_at ?? ""),
    );
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
