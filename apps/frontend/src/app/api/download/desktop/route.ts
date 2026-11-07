import { NextRequest, NextResponse } from "next/server";

import {
  getPublishedRelease,
  pickLatestRelease,
  resolveAssetLocation,
  tagPrefixFromHost,
  validReleaseDownloadToken,
} from "@/lib/githubReleases";

type Arch = "aarch64" | "x64";

// `dmg` serves the installer to browsers; `bundle` serves the updater's
// `.app.tar.gz` — the update manifest points Tauri here so the signed URL
// is minted fresh at download time (it expires within minutes, and the
// user may sit on the update prompt far longer than that).
type Kind = "dmg" | "bundle";

function archFromQuery(raw: string | null): Arch {
  if (raw === "x64" || raw === "x86_64" || raw === "intel") return "x64";
  return "aarch64";
}

function kindFromQuery(raw: string | null): Kind {
  return raw === "bundle" ? "bundle" : "dmg";
}

export async function GET(req: NextRequest) {
  const arch = archFromQuery(req.nextUrl.searchParams.get("arch"));
  const kind = kindFromQuery(req.nextUrl.searchParams.get("kind"));
  const prefix = tagPrefixFromHost(req.headers.get("host"));
  const version = req.nextUrl.searchParams.get("version");
  if (version !== null && !/^\d+\.\d+\.\d+$/.test(version)) {
    return NextResponse.json({ error: "invalid_version" }, { status: 400 });
  }
  if (version && !validReleaseDownloadToken(`${prefix}${version}`, arch, kind, req.nextUrl.searchParams.get("token"))) {
    return NextResponse.json({ error: "invalid_download_token" }, { status: 403 });
  }

  const { release, githubStatus } = version
    ? await getPublishedRelease(`${prefix}${version}`)
    : await pickLatestRelease(prefix);
  if (version && githubStatus === 404) {
    return NextResponse.json({ error: "no_release", version }, { status: 404 });
  }
  if (githubStatus < 200 || githubStatus >= 300) {
    return NextResponse.json(
      { error: "github_unavailable", status: githubStatus },
      { status: 502 },
    );
  }
  if (!release) {
    return NextResponse.json(
      { error: "no_release", tag_prefix: prefix },
      { status: 404 },
    );
  }
  const suffix = kind === "bundle" ? `_${arch}.app.tar.gz` : `_${arch}.dmg`;
  const asset = release.assets.find((a) => a.name.endsWith(suffix));
  if (!asset) {
    return NextResponse.json(
      { error: "no_asset", tag: release.tag_name, arch, kind },
      { status: 404 },
    );
  }
  const { location, status } = await resolveAssetLocation(asset);
  if (!location) {
    return NextResponse.json(
      { error: "asset_unresolvable", tag: release.tag_name, status },
      { status: 502 },
    );
  }
  return NextResponse.redirect(location, 302);
}
