import { NextRequest, NextResponse } from "next/server";

// Tauri updater manifest endpoint.
//
// Tauri's `tauri-plugin-updater` polls a URL like:
//   https://dev.isol8.co/api/desktop/update/darwin-aarch64/0.1.0
// where {{target}} and {{current_version}} are substituted from
// `plugins.updater.endpoints` in tauri.conf.json.
//
// On 204: client treats it as "no update available" (current build is
// already on the latest version published for that target).
// On 200 with JSON: client downloads the bundle, verifies the minisign
// signature against the public key baked into tauri.conf.json, and
// installs.
//
// Mirrors the pattern in /api/download/desktop: dev hosts (dev.isol8.co,
// *.vercel.app, localhost) read from `desktop-dev-v*` releases; prod
// hosts read from `desktop-v*`. Same GitHub repo, same release pipeline.

const REPO = "Isol8AI/isol8";
const PROD_TAG_PREFIX = "desktop-v";
const DEV_TAG_PREFIX = "desktop-dev-v";

type GhAsset = { name: string; browser_download_url: string };
type GhRelease = {
  tag_name: string;
  name: string | null;
  body: string | null;
  draft: boolean;
  published_at: string | null;
  assets: GhAsset[];
};

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

// "darwin-aarch64" → "aarch64"; "darwin-x86_64" → "x64".
// Tauri's bundler emits `Isol8_<ver>_<aarch64|x64>.app.tar.gz` so we map
// the updater target's full triple back to the bundle's arch suffix.
function archFromTarget(target: string): "aarch64" | "x64" | null {
  if (target === "darwin-aarch64" || target === "aarch64-apple-darwin") {
    return "aarch64";
  }
  if (
    target === "darwin-x86_64" ||
    target === "x86_64-apple-darwin" ||
    target === "darwin-x64"
  ) {
    return "x64";
  }
  return null;
}

// Strip the `desktop-(dev-)?v` prefix to get a semver-ish string.
// Used both to extract the latest version from a tag and to compare
// against the running version the client reports.
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

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ target: string; current_version: string }> },
) {
  const { target, current_version: currentVersion } = await ctx.params;
  const arch = archFromTarget(target);
  if (!arch) {
    return NextResponse.json(
      { error: "unsupported_target", target },
      { status: 400 },
    );
  }

  const prefix = tagPrefixFromHost(req.headers.get("host"));

  const ghRes = await fetch(
    `https://api.github.com/repos/${REPO}/releases?per_page=30`,
    {
      headers: {
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      next: { revalidate: 300 },
    },
  );
  if (!ghRes.ok) {
    return NextResponse.json(
      { error: "github_unavailable", status: ghRes.status },
      { status: 502 },
    );
  }
  const releases: GhRelease[] = await ghRes.json();
  const candidate = releases
    .filter((r) => !r.draft && r.tag_name.startsWith(prefix))
    .sort((a, b) =>
      (b.published_at ?? "").localeCompare(a.published_at ?? ""),
    )[0];
  if (!candidate) {
    // No published release for this env → behave like "up to date" rather
    // than 500'ing the client. Avoids loops where a misconfigured env
    // would cause every desktop on the network to surface an update
    // error on every check.
    return new NextResponse(null, { status: 204 });
  }

  const latestVersion = versionFromTag(candidate.tag_name);
  if (!latestVersion) {
    return NextResponse.json(
      { error: "unparseable_tag", tag: candidate.tag_name },
      { status: 500 },
    );
  }
  if (!isNewer(latestVersion, currentVersion)) {
    return new NextResponse(null, { status: 204 });
  }

  const bundleSuffix = `_${arch}.app.tar.gz`;
  const sigSuffix = `_${arch}.app.tar.gz.sig`;
  const bundle = candidate.assets.find((a) => a.name.endsWith(bundleSuffix));
  const sigAsset = candidate.assets.find((a) => a.name.endsWith(sigSuffix));
  if (!bundle || !sigAsset) {
    // The release exists but the updater bundle hasn't been attached
    // yet — common during in-flight builds where the matrix uploaded
    // the DMG first. Treat as no-update so the client retries later.
    return new NextResponse(null, { status: 204 });
  }

  // Tauri's updater wants the signature value INLINE in the manifest, not
  // a URL to fetch separately. The .sig asset is small (a few hundred
  // bytes of base64) so we fetch and embed it.
  const sigRes = await fetch(sigAsset.browser_download_url, {
    next: { revalidate: 300 },
  });
  if (!sigRes.ok) {
    return NextResponse.json(
      { error: "signature_fetch_failed", status: sigRes.status },
      { status: 502 },
    );
  }
  const signature = (await sigRes.text()).trim();

  return NextResponse.json({
    version: latestVersion,
    notes: candidate.body ?? candidate.name ?? "",
    pub_date: candidate.published_at,
    url: bundle.browser_download_url,
    signature,
  });
}
