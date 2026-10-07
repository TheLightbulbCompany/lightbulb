import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { GET } from "../route";
import { GET as getManifest } from "../../../desktop/update/[target]/[current_version]/route";
import { releaseDownloadToken } from "@/lib/githubReleases";

type GhAssetFixture = {
  url: string;
  name: string;
  browser_download_url: string;
};

type GhReleaseFixture = {
  tag_name: string;
  draft: boolean;
  published_at: string | null;
  assets: GhAssetFixture[];
};

/** The signed URL GitHub's asset API would redirect to for a given asset. */
function signedUrlFor(assetUrl: string) {
  return `https://objects.githubusercontent.com/signed/${encodeURIComponent(assetUrl)}`;
}

function stubGitHub(releases: GhReleaseFixture[], ok = true, status = 200) {
  const fetchMock = vi.fn(async (url: string | URL) => {
    const u = String(url);
    if (u.includes("/releases?")) {
      return {
        ok,
        status,
        json: async () => releases,
      } as unknown as Response;
    }
    if (u.includes("/releases/tags/")) {
      const tag = decodeURIComponent(u.split("/releases/tags/")[1]);
      const release = releases.find((r) => r.tag_name === tag);
      return { ok: !!release, status: release ? 200 : 404, json: async () => release } as unknown as Response;
    }
    // Asset API URL fetch with redirect:"manual" — GitHub answers 302
    // with the short-lived signed URL in Location.
    return {
      ok: false,
      status: 302,
      headers: new Headers({ location: signedUrlFor(u) }),
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock };
}

function reqFor(url: string, host: string) {
  // NextRequest doesn't honor a Host header passed via init — it reads
  // host off the URL. Build the URL with the host we want to simulate.
  const u = new URL(url, `https://${host}`);
  return new NextRequest(u, { headers: { host } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
beforeEach(() => vi.stubEnv("GITHUB_RELEASES_TOKEN", "release-test-key"));

function pinned(version: string, tag = `desktop-v${version}`) {
  return `/api/download/desktop?kind=bundle&version=${version}&token=${releaseDownloadToken(tag, "aarch64", "bundle")}`;
}

describe("GET /api/download/desktop", () => {
  const arm: GhAssetFixture = {
    url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/101",
    name: "Isol8_0.2.6_aarch64.dmg",
    browser_download_url:
      "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-v0.2.6/Isol8_0.2.6_aarch64.dmg",
  };
  const intel: GhAssetFixture = {
    url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/102",
    name: "Isol8_0.2.6_x64.dmg",
    browser_download_url:
      "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-v0.2.6/Isol8_0.2.6_x64.dmg",
  };
  const armBundle: GhAssetFixture = {
    url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/103",
    name: "Isol8_aarch64.app.tar.gz",
    browser_download_url:
      "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-v0.2.6/Isol8_aarch64.app.tar.gz",
  };
  const prodRelease: GhReleaseFixture = {
    tag_name: "desktop-v0.2.6",
    draft: false,
    published_at: "2026-05-01T00:00:00Z",
    assets: [arm, intel, armBundle],
  };

  it("downloads the manifest's signed release after a newer release is published", async () => {
    const sig = { ...armBundle, url: `${armBundle.url}-sig`, name: `${armBundle.name}.sig` };
    const oldRelease = { ...prodRelease, tag_name: "desktop-v0.4.603", assets: [armBundle, sig] };
    const newerBundle = { ...armBundle, url: `${armBundle.url}-606` };
    const newRelease = { ...oldRelease, tag_name: "desktop-v0.4.606", assets: [newerBundle, sig] };
    let published = [oldRelease];
    vi.stubGlobal("fetch", vi.fn(async (url: string | URL) => {
      const u = String(url);
      if (u.includes("/releases?")) return new Response(JSON.stringify(published));
      if (u.endsWith("/releases/tags/desktop-v0.4.603")) return new Response(JSON.stringify(oldRelease));
      if (u === sig.url) return new Response("SIGNATURE-603");
      return new Response(null, { status: 302, headers: { location: signedUrlFor(u) } });
    }));
    const response = await getManifest(reqFor("/api/desktop/update/x/y", "thelightbulb.company"), {
      params: Promise.resolve({ target: "darwin-aarch64", current_version: "0.4.600" }),
    });
    const manifest = await response.json();
    expect(manifest.version).toBe("0.4.603");
    expect(manifest.signature).toBe("SIGNATURE-603");
    // The pinned tag must remain fetchable even after it leaves the latest release page.
    published = [newRelease];
    const download = await GET(new NextRequest(manifest.url, { headers: { host: "thelightbulb.company" } }));
    expect(download.status).toBe(302);
    expect(download.headers.get("location")).toBe(signedUrlFor(armBundle.url));
  });
  const devAsset: GhAssetFixture = {
    url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/201",
    name: "Isol8_0.3.1_aarch64.dmg",
    browser_download_url:
      "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-dev-v0.3.1/Isol8_0.3.1_aarch64.dmg",
  };
  const devRelease: GhReleaseFixture = {
    tag_name: "desktop-dev-v0.3.1",
    draft: false,
    published_at: "2026-05-09T00:00:00Z",
    assets: [devAsset],
  };

  it("redirects prod host to the signed URL for the arm64 DMG by default", async () => {
    stubGitHub([prodRelease, devRelease]);
    const res = await GET(reqFor("/api/download/desktop", "thelightbulb.company"));
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(signedUrlFor(arm.url));
  });

  it("honors ?arch=x64 to select the Intel build", async () => {
    stubGitHub([prodRelease]);
    const res = await GET(
      reqFor("/api/download/desktop?arch=x64", "thelightbulb.company"),
    );
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(signedUrlFor(intel.url));
  });

  it("rejects malformed pinned versions before querying GitHub", async () => {
    const { fetchMock } = stubGitHub([prodRelease]);
    const res = await GET(reqFor("/api/download/desktop?kind=bundle&version=../0.2.6", "thelightbulb.company"));
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_version" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(["", "&token=tampered", `&token=${"0".repeat(64)}`])("rejects unissued version lookups without GitHub calls (%s)", async (token) => {
    const { fetchMock } = stubGitHub([prodRelease]);
    const res = await GET(reqFor(`/api/download/desktop?kind=bundle&version=0.2.6${token}`, "thelightbulb.company"));
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([['version', '0.2.7'], ['arch', 'x64'], ['kind', 'dmg']])("rejects changes to the issued %s before querying GitHub", async (field, value) => {
    const { fetchMock } = stubGitHub([prodRelease]);
    const url = new URL(pinned("0.2.6"), "https://thelightbulb.company");
    url.searchParams.set(field, value);
    const res = await GET(new NextRequest(url, { headers: { host: "thelightbulb.company" } }));
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not switch a missing pinned release to the latest", async () => {
    stubGitHub([prodRelease]);
    const res = await GET(reqFor(pinned("0.2.5"), "thelightbulb.company"));
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "no_release", version: "0.2.5" });
  });

  it("does not serve a draft pinned release", async () => {
    stubGitHub([{ ...prodRelease, draft: true }]);
    const res = await GET(reqFor(pinned("0.2.6"), "thelightbulb.company"));
    expect(res.status).toBe(404);
  });

  it("keeps pinned versions within the host's environment", async () => {
    const { fetchMock } = stubGitHub([prodRelease]);
    const res = await GET(reqFor(pinned("0.2.6"), "dev.thelightbulb.company"));
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("serves the updater bundle via ?kind=bundle", async () => {
    stubGitHub([prodRelease]);
    const res = await GET(
      reqFor("/api/download/desktop?kind=bundle", "thelightbulb.company"),
    );
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(signedUrlFor(armBundle.url));
  });

  it("sends the token on GitHub calls when configured", async () => {
    vi.stubEnv("GITHUB_RELEASES_TOKEN", "ghp_test123");
    const { fetchMock } = stubGitHub([prodRelease]);
    await GET(reqFor("/api/download/desktop", "thelightbulb.company"));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/releases?"),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer ghp_test123",
        }),
      }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      arm.url,
      expect.objectContaining({
        redirect: "manual",
        headers: expect.objectContaining({
          Authorization: "Bearer ghp_test123",
          Accept: "application/octet-stream",
        }),
      }),
    );
  });

  it("uses the desktop-dev-v prefix on dev.thelightbulb.company", async () => {
    stubGitHub([prodRelease, devRelease]);
    const res = await GET(reqFor("/api/download/desktop", "dev.thelightbulb.company"));
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(signedUrlFor(devAsset.url));
  });

  it("uses the desktop-dev-v prefix on localhost", async () => {
    stubGitHub([prodRelease, devRelease]);
    const res = await GET(
      reqFor("/api/download/desktop", "localhost:3000"),
    );
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(signedUrlFor(devAsset.url));
  });

  it("ignores draft releases", async () => {
    const draftOnly: GhReleaseFixture = { ...prodRelease, draft: true };
    stubGitHub([draftOnly]);
    const res = await GET(reqFor("/api/download/desktop", "thelightbulb.company"));
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body).toMatchObject({ error: "no_release" });
  });

  it("returns 404 when the requested arch has no asset", async () => {
    const armOnly: GhReleaseFixture = { ...prodRelease, assets: [arm] };
    stubGitHub([armOnly]);
    const res = await GET(
      reqFor("/api/download/desktop?arch=x64", "thelightbulb.company"),
    );
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body).toMatchObject({ error: "no_asset", arch: "x64" });
  });

  it("picks the highest-version release for the env's prefix", async () => {
    const older: GhReleaseFixture = {
      ...prodRelease,
      tag_name: "desktop-v0.2.0",
      published_at: "2026-01-01T00:00:00Z",
    };
    const newerAsset: GhAssetFixture = {
      url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/301",
      name: "Isol8_0.3.0_aarch64.dmg",
      browser_download_url:
        "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-v0.3.0/Isol8_0.3.0_aarch64.dmg",
    };
    const newer: GhReleaseFixture = {
      ...prodRelease,
      tag_name: "desktop-v0.3.0",
      published_at: "2026-05-10T00:00:00Z",
      assets: [newerAsset],
    };
    stubGitHub([older, newer]);
    const res = await GET(reqFor("/api/download/desktop", "thelightbulb.company"));
    expect(res.headers.get("location")).toBe(signedUrlFor(newerAsset.url));
  });

  it("returns 502 when GitHub is unreachable", async () => {
    stubGitHub([], false, 503);
    const res = await GET(reqFor("/api/download/desktop", "thelightbulb.company"));
    expect(res.status).toBe(502);
  });

  it("returns 502 when the asset redirect can't be resolved", async () => {
    const fetchMock = vi.fn(async (url: string | URL) => {
      const u = String(url);
      if (u.includes("/releases?")) {
        return {
          ok: true,
          status: 200,
          json: async () => [prodRelease],
        } as unknown as Response;
      }
      // Private repo without a valid token: asset API answers 404
      // instead of the signed-URL redirect.
      return {
        ok: false,
        status: 404,
        headers: new Headers(),
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    const res = await GET(reqFor("/api/download/desktop", "thelightbulb.company"));
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body).toMatchObject({ error: "asset_unresolvable", status: 404 });
  });
});
