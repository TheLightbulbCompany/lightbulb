import { NextRequest, NextResponse } from "next/server";

import { pickLatestRelease, tagPrefixFromHost } from "@/lib/githubReleases";

type Arch = "aarch64" | "x64";

function archFromQuery(raw: string | null): Arch {
  if (raw === "x64" || raw === "x86_64" || raw === "intel") return "x64";
  return "aarch64";
}

export async function GET(req: NextRequest) {
  const arch = archFromQuery(req.nextUrl.searchParams.get("arch"));
  const prefix = tagPrefixFromHost(req.headers.get("host"));

  const { release, githubStatus } = await pickLatestRelease(prefix);
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
