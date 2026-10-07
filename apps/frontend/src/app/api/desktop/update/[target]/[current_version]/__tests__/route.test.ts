import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { GET } from "../route";

type GhAssetFixture = {
  url: string;
  name: string;
  browser_download_url: string;
};

type GhReleaseFixture = {
  tag_name: string;
  name: string | null;
  body: string | null;
  draft: boolean;
  published_at: string | null;
  assets: GhAssetFixture[];
};

function stubGitHub(releases: GhReleaseFixture[], signatureBody = "SIG-XYZ") {
  const fetchMock = vi.fn(async (url: string | URL) => {
    const u = String(url);
    if (u.includes("/releases?")) {
      return {
        ok: true,
        status: 200,
        json: async () => releases,
      } as unknown as Response;
    }
    // Otherwise it's the .sig asset fetch (asset API URL, redirect
    // followed) — return the canned signature body.
    return {
      ok: true,
      status: 200,
      text: async () => signatureBody,
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock };
}

function reqFor(host: string) {
  // The route handler reads host off req.headers — build a URL with the
  // host and pass the headers object explicitly.
  const url = new URL(`https://${host}/api/desktop/update/x/y`);
  return new NextRequest(url, { headers: { host } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
beforeEach(() => vi.stubEnv("GITHUB_RELEASES_TOKEN", "release-test-key"));

describe("GET /api/desktop/update/[target]/[current_version]", () => {
  const armBundle: GhAssetFixture = {
    url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/11",
    name: "Isol8_0.2.11_aarch64.app.tar.gz",
    browser_download_url:
      "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-dev-v0.2.11/Isol8_0.2.11_aarch64.app.tar.gz",
  };
  const armSig: GhAssetFixture = {
    url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/12",
    name: "Isol8_0.2.11_aarch64.app.tar.gz.sig",
    browser_download_url:
      "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-dev-v0.2.11/Isol8_0.2.11_aarch64.app.tar.gz.sig",
  };

  const dmgOnlyRelease: GhReleaseFixture = {
    tag_name: "desktop-dev-v0.2.11",
    name: "Isol8 Desktop [DEV] desktop-dev-v0.2.11",
    body: "release notes",
    draft: false,
    published_at: "2026-05-12T22:00:00Z",
    assets: [
      {
        url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/10",
        name: "Isol8_0.2.11_aarch64.dmg",
        browser_download_url: "https://example/dmg",
      },
    ],
  };
  const fullDevRelease: GhReleaseFixture = {
    ...dmgOnlyRelease,
    assets: [...dmgOnlyRelease.assets, armBundle, armSig],
  };

  it("returns 200 with manifest when current_version is older", async () => {
    stubGitHub([fullDevRelease]);
    const res = await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({
      version: "0.2.11",
      // Bundle URL points back at our download route (which mints a
      // fresh signed URL per request), NOT at GitHub directly — a
      // private repo's URLs either 404 anonymously or expire.
      signature: "SIG-XYZ",
      notes: "release notes",
    });
    const download = new URL(body.url);
    expect(download.origin).toBe("https://dev.thelightbulb.company");
    expect(download.pathname).toBe("/api/download/desktop");
    expect(download.searchParams.get("version")).toBe("0.2.11");
    expect(download.searchParams.get("token")).toMatch(/^[a-f0-9]{64}$/);
  });

  it("falls back to the canonical host when the Host header isn't ours", async () => {
    // A spoofed Host header must not be echoed into the manifest URL the
    // updater will fetch. Unknown host → prod tag prefix → thelightbulb.company.
    stubGitHub([
      {
        ...fullDevRelease,
        tag_name: "desktop-v0.2.11",
      },
    ]);
    const res = await GET(reqFor("evil.example.com"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(new URL(body.url).origin).toBe("https://thelightbulb.company");
    expect(new URL(body.url).searchParams.get("version")).toBe("0.2.11");
  });

  it("fetches the signature from the asset API URL with auth", async () => {
    vi.stubEnv("GITHUB_RELEASES_TOKEN", "ghp_test123");
    const { fetchMock } = stubGitHub([fullDevRelease]);
    await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.1.0",
      }),
    });
    expect(fetchMock).toHaveBeenCalledWith(
      armSig.url,
      expect.objectContaining({
        headers: expect.objectContaining({
          Accept: "application/octet-stream",
          Authorization: "Bearer ghp_test123",
        }),
      }),
    );
  });

  it("returns 204 when current_version matches the latest", async () => {
    stubGitHub([fullDevRelease]);
    const res = await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.2.11",
      }),
    });
    expect(res.status).toBe(204);
  });

  it("does not issue a manifest when its download token cannot be signed", async () => {
    vi.stubEnv("GITHUB_RELEASES_TOKEN", "");
    stubGitHub([fullDevRelease]);
    const res = await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({ target: "darwin-aarch64", current_version: "0.1.0" }),
    });
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ error: "download_unavailable" });
  });

  it("returns 204 when bundle assets aren't attached yet (mid-build)", async () => {
    stubGitHub([dmgOnlyRelease]);
    const res = await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(204);
  });

  it("filters by tag prefix per host (prod skips dev releases)", async () => {
    // Only a dev release exists. Prod host should see "no release" → 204.
    stubGitHub([fullDevRelease]);
    const res = await GET(reqFor("thelightbulb.company"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(204);
  });

  it("rejects unknown targets with 400", async () => {
    stubGitHub([fullDevRelease]);
    const res = await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({
        target: "windows-x86_64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(400);
  });

  it("picks the highest-version release", async () => {
    const older: GhReleaseFixture = {
      ...fullDevRelease,
      tag_name: "desktop-dev-v0.2.0",
      published_at: "2026-01-01T00:00:00Z",
    };
    const newer: GhReleaseFixture = {
      ...fullDevRelease,
      tag_name: "desktop-dev-v0.3.0",
      published_at: "2026-05-13T00:00:00Z",
      assets: [
        {
          url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/21",
          name: "Isol8_0.3.0_aarch64.app.tar.gz",
          browser_download_url:
            "https://example/v0.3.0/Isol8_0.3.0_aarch64.app.tar.gz",
        },
        {
          url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/22",
          name: "Isol8_0.3.0_aarch64.app.tar.gz.sig",
          browser_download_url: "https://example/v0.3.0/sig",
        },
      ],
    };
    stubGitHub([older, newer]);
    const res = await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.2.5",
      }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.version).toBe("0.3.0");
  });

  it("picks the highest-version release even when an older tag publishes later (race)", async () => {
    // Auto-publish on overlapping CI runs can flip `published_at` ordering:
    // run-A starts first with v0.3.0, run-B starts later with v0.3.1, but
    // run-B finishes faster (warm cache), publishing v0.3.1 BEFORE run-A
    // publishes v0.3.0. If we sorted by published_at we'd offer v0.3.0
    // to clients on v0.3.1 — a downgrade. Sorting by version-from-tag
    // closes that race.
    const newerVersionPublishedFirst: GhReleaseFixture = {
      ...fullDevRelease,
      tag_name: "desktop-dev-v0.3.1",
      published_at: "2026-05-15T00:00:00Z",
      assets: [
        {
          url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/31",
          name: "Isol8_0.3.1_aarch64.app.tar.gz",
          browser_download_url:
            "https://example/v0.3.1/Isol8_0.3.1_aarch64.app.tar.gz",
        },
        {
          url: "https://api.github.com/repos/TheLightbulbCompany/lightbulb/releases/assets/32",
          name: "Isol8_0.3.1_aarch64.app.tar.gz.sig",
          browser_download_url: "https://example/v0.3.1/sig",
        },
      ],
    };
    const olderVersionPublishedLater: GhReleaseFixture = {
      ...fullDevRelease,
      tag_name: "desktop-dev-v0.3.0",
      published_at: "2026-05-16T00:00:00Z",
    };
    stubGitHub([
      newerVersionPublishedFirst,
      olderVersionPublishedLater,
    ]);
    const res = await GET(reqFor("dev.thelightbulb.company"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.2.5",
      }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.version).toBe("0.3.1");
  });
});
