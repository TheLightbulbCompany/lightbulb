import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { GET } from "../route";

type GhReleaseFixture = {
  tag_name: string;
  draft: boolean;
  published_at: string | null;
  assets: { name: string; browser_download_url: string }[];
};

function stubGitHub(releases: GhReleaseFixture[], ok = true, status = 200) {
  const json = vi.fn().mockResolvedValue(releases);
  const fetchMock = vi.fn().mockResolvedValue({ ok, status, json });
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
});

describe("GET /api/download/desktop", () => {
  const arm = {
    name: "Isol8_0.2.6_aarch64.dmg",
    browser_download_url:
      "https://github.com/Isol8AI/isol8/releases/download/desktop-v0.2.6/Isol8_0.2.6_aarch64.dmg",
  };
  const intel = {
    name: "Isol8_0.2.6_x64.dmg",
    browser_download_url:
      "https://github.com/Isol8AI/isol8/releases/download/desktop-v0.2.6/Isol8_0.2.6_x64.dmg",
  };
  const prodRelease: GhReleaseFixture = {
    tag_name: "desktop-v0.2.6",
    draft: false,
    published_at: "2026-05-01T00:00:00Z",
    assets: [arm, intel],
  };
  const devRelease: GhReleaseFixture = {
    tag_name: "desktop-dev-v0.3.1",
    draft: false,
    published_at: "2026-05-09T00:00:00Z",
    assets: [
      {
        name: "Isol8_0.3.1_aarch64.dmg",
        browser_download_url:
          "https://github.com/Isol8AI/isol8/releases/download/desktop-dev-v0.3.1/Isol8_0.3.1_aarch64.dmg",
      },
    ],
  };

  it("redirects prod host to the matching arm64 asset by default", async () => {
    stubGitHub([prodRelease, devRelease]);
    const res = await GET(reqFor("/api/download/desktop", "isol8.co"));
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(arm.browser_download_url);
  });

  it("honors ?arch=x64 to select the Intel build", async () => {
    stubGitHub([prodRelease]);
    const res = await GET(
      reqFor("/api/download/desktop?arch=x64", "isol8.co"),
    );
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(intel.browser_download_url);
  });

  it("uses the desktop-dev-v prefix on dev.isol8.co", async () => {
    stubGitHub([prodRelease, devRelease]);
    const res = await GET(reqFor("/api/download/desktop", "dev.isol8.co"));
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(
      devRelease.assets[0].browser_download_url,
    );
  });

  it("uses the desktop-dev-v prefix on localhost", async () => {
    stubGitHub([prodRelease, devRelease]);
    const res = await GET(
      reqFor("/api/download/desktop", "localhost:3000"),
    );
    expect(res.status).toBe(302);
    expect(res.headers.get("location")).toBe(
      devRelease.assets[0].browser_download_url,
    );
  });

  it("ignores draft releases", async () => {
    const draftOnly: GhReleaseFixture = { ...prodRelease, draft: true };
    stubGitHub([draftOnly]);
    const res = await GET(reqFor("/api/download/desktop", "isol8.co"));
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body).toMatchObject({ error: "no_release" });
  });

  it("returns 404 when the requested arch has no asset", async () => {
    const armOnly: GhReleaseFixture = { ...prodRelease, assets: [arm] };
    stubGitHub([armOnly]);
    const res = await GET(
      reqFor("/api/download/desktop?arch=x64", "isol8.co"),
    );
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body).toMatchObject({ error: "no_asset", arch: "x64" });
  });

  it("picks the most recently published release for the env's prefix", async () => {
    const older: GhReleaseFixture = {
      ...prodRelease,
      tag_name: "desktop-v0.2.0",
      published_at: "2026-01-01T00:00:00Z",
    };
    const newer: GhReleaseFixture = {
      ...prodRelease,
      tag_name: "desktop-v0.3.0",
      published_at: "2026-05-10T00:00:00Z",
      assets: [
        {
          name: "Isol8_0.3.0_aarch64.dmg",
          browser_download_url:
            "https://github.com/Isol8AI/isol8/releases/download/desktop-v0.3.0/Isol8_0.3.0_aarch64.dmg",
        },
      ],
    };
    stubGitHub([older, newer]);
    const res = await GET(reqFor("/api/download/desktop", "isol8.co"));
    expect(res.headers.get("location")).toBe(
      newer.assets[0].browser_download_url,
    );
  });

  it("returns 502 when GitHub is unreachable", async () => {
    stubGitHub([], false, 503);
    const res = await GET(reqFor("/api/download/desktop", "isol8.co"));
    expect(res.status).toBe(502);
  });
});
