import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

import { GET } from "../route";

type GhReleaseFixture = {
  tag_name: string;
  name: string | null;
  body: string | null;
  draft: boolean;
  published_at: string | null;
  assets: { name: string; browser_download_url: string }[];
};

function stubGitHub(releases: GhReleaseFixture[], signatureBody = "SIG-XYZ") {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes("/releases?")) {
      return {
        ok: true,
        status: 200,
        json: async () => releases,
      } as unknown as Response;
    }
    // Otherwise it's a .sig file fetch — return the canned signature body.
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
});

describe("GET /api/desktop/update/[target]/[current_version]", () => {
  const armBundle = {
    name: "Isol8_0.2.11_aarch64.app.tar.gz",
    browser_download_url:
      "https://github.com/Isol8AI/isol8/releases/download/desktop-dev-v0.2.11/Isol8_0.2.11_aarch64.app.tar.gz",
  };
  const armSig = {
    name: "Isol8_0.2.11_aarch64.app.tar.gz.sig",
    browser_download_url:
      "https://github.com/Isol8AI/isol8/releases/download/desktop-dev-v0.2.11/Isol8_0.2.11_aarch64.app.tar.gz.sig",
  };

  const dmgOnlyRelease: GhReleaseFixture = {
    tag_name: "desktop-dev-v0.2.11",
    name: "Isol8 Desktop [DEV] desktop-dev-v0.2.11",
    body: "release notes",
    draft: false,
    published_at: "2026-05-12T22:00:00Z",
    assets: [
      {
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
    const res = await GET(reqFor("dev.isol8.co"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({
      version: "0.2.11",
      url: armBundle.browser_download_url,
      signature: "SIG-XYZ",
      notes: "release notes",
    });
  });

  it("returns 204 when current_version matches the latest", async () => {
    stubGitHub([fullDevRelease]);
    const res = await GET(reqFor("dev.isol8.co"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.2.11",
      }),
    });
    expect(res.status).toBe(204);
  });

  it("returns 204 when bundle assets aren't attached yet (mid-build)", async () => {
    stubGitHub([dmgOnlyRelease]);
    const res = await GET(reqFor("dev.isol8.co"), {
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
    const res = await GET(reqFor("isol8.co"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(204);
  });

  it("rejects unknown targets with 400", async () => {
    stubGitHub([fullDevRelease]);
    const res = await GET(reqFor("dev.isol8.co"), {
      params: Promise.resolve({
        target: "windows-x86_64",
        current_version: "0.1.0",
      }),
    });
    expect(res.status).toBe(400);
  });

  it("picks the most recently published release", async () => {
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
          name: "Isol8_0.3.0_aarch64.app.tar.gz",
          browser_download_url:
            "https://example/v0.3.0/Isol8_0.3.0_aarch64.app.tar.gz",
        },
        {
          name: "Isol8_0.3.0_aarch64.app.tar.gz.sig",
          browser_download_url: "https://example/v0.3.0/sig",
        },
      ],
    };
    stubGitHub([older, newer]);
    const res = await GET(reqFor("dev.isol8.co"), {
      params: Promise.resolve({
        target: "darwin-aarch64",
        current_version: "0.2.5",
      }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.version).toBe("0.3.0");
  });
});
