import { describe, it, expect, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  browserViewScreenUrl,
  browserViewSessionUrl,
  browserViewWebRtcRootUrl,
  browserViewWhoamiUrl,
  browserViewWsUrl,
} from "../panels/browserViewUrl";
import { normalizeTabs } from "../panels/browserTabs";
import { normalizeBrowserViewport } from "../panels/browserViewport";

// The bv host mapping is load-bearing: the api host is an API Gateway HTTP
// API that cannot pass WebSocket upgrades, so a wrong mapping here is a panel
// that spins forever against a door that drops the connection.
describe("browserViewWsUrl", () => {
  it("maps the dev api host to the CloudFront bv host", () => {
    expect(browserViewWsUrl("https://api-dev.isol8.co/api/v1")).toBe(
      "wss://bv-dev.isol8.co/api/v1/browser-view/ws",
    );
  });

  it("maps the prod api host to bv.isol8.co", () => {
    expect(browserViewWsUrl("https://api.isol8.co/api/v1")).toBe(
      "wss://bv.isol8.co/api/v1/browser-view/ws",
    );
  });

  it("connects straight to a local backend (no CloudFront locally)", () => {
    expect(browserViewWsUrl("http://localhost:8000/api/v1")).toBe(
      "ws://localhost:8000/api/v1/browser-view/ws",
    );
  });
});

describe("browser-view WebRTC URLs", () => {
  it.each([
    ["https://api-dev.isol8.co/api/v1", "https://bv-dev.isol8.co/api/v1"],
    ["https://api.isol8.co/api/v1", "https://bv.isol8.co/api/v1"],
    ["http://localhost:8000/api/v1", "http://localhost:8000/api/v1"],
  ])("keeps every Neko route under the WebRTC prefix for %s", (apiUrl, bvUrl) => {
    expect(browserViewSessionUrl(apiUrl)).toBe(`${bvUrl}/browser-view/session`);
    expect(browserViewWebRtcRootUrl(apiUrl)).toBe(`${bvUrl}/browser-view/webrtc/`);
    expect(browserViewWhoamiUrl(apiUrl)).toBe(`${bvUrl}/browser-view/webrtc/api/whoami`);
    expect(browserViewScreenUrl(apiUrl)).toBe(`${bvUrl}/browser-view/webrtc/api/room/screen`);
  });
});

describe("normalizeBrowserViewport", () => {
  it("uses the conservative stream defaults before the panel is measurable", () => {
    expect(normalizeBrowserViewport(0, 0)).toEqual({ width: 1280, height: 800, rate: 24 });
  });

  it("rounds CSS dimensions down to multiples of eight without DPR input", () => {
    expect(normalizeBrowserViewport(811.9, 622.9)).toEqual({ width: 808, height: 616, rate: 24 });
  });

  it("preserves the panel aspect ratio while enforcing the area cap", () => {
    const viewport = normalizeBrowserViewport(2000, 1000);
    expect(viewport.width * viewport.height).toBeLessThanOrEqual(1_024_000);
    expect(viewport.width / viewport.height).toBeCloseTo(2, 1);
  });

  it("keeps adversarial narrow panels step-aligned inside the pixel cap", () => {
    for (const [width, height] of [[1, 1_000_000], [1_000_000, 1]]) {
      const viewport = normalizeBrowserViewport(width, height);
      expect(viewport.width).toBeGreaterThanOrEqual(8);
      expect(viewport.height).toBeGreaterThanOrEqual(8);
      expect(viewport.width % 8).toBe(0);
      expect(viewport.height % 8).toBe(0);
      expect(viewport.width * viewport.height).toBeLessThanOrEqual(1_024_000);
    }
  });

  it("does not inspect devicePixelRatio", () => {
    const dpr = vi.spyOn(window, "devicePixelRatio", "get").mockReturnValue(3);
    expect(normalizeBrowserViewport(640, 400)).toEqual({ width: 640, height: 400, rate: 24 });
    dpr.mockRestore();
  });
});

describe("temporary browser transport selection and CSP", () => {
  const panelComponents = readFileSync(join(__dirname, "../panelComponents.ts"), "utf8");
  const nextConfig = readFileSync(join(__dirname, "../../../../next.config.ts"), "utf8");

  it("selects WebRTC only for the exact build-time value", () => {
    expect(panelComponents).toContain(
      'process.env.NEXT_PUBLIC_BROWSER_VIEW_TRANSPORT === "webrtc" ? WebRtcBrowserDockPanel : BrowserDockPanel',
    );
  });

  it("allows both browser-view hosts only through report-only frame and parent fetch policies", () => {
    expect(nextConfig).toContain("Content-Security-Policy-Report-Only");
    for (const host of ["https://bv.isol8.co", "https://bv-dev.isol8.co"]) {
      expect(nextConfig.match(new RegExp(host, "g"))).toHaveLength(2);
    }
  });
});

// The tab/window strip's whole value is surfacing a stuck OAuth popup so the
// human can Close it. Close addresses tabs by `id`, and the backend's stable
// per-profile alias (`tabId`) must win over the raw CDP `targetId`: a raw id
// is replaced by an OAuth redirect/form submit between poll and click, so a
// Close keyed on it would resolve nothing and silently no-op — the exact
// trapped case this feature exists for.
describe("normalizeTabs", () => {
  it("addresses tabs by the stable tabId alias (what Close/DELETE uses)", () => {
    const tabs = normalizeTabs({
      running: true,
      tabs: [
        { tabId: "t1", targetId: "TID-1", title: "Gmail", url: "https://mail.google.com/" },
        { tabId: "t2", targetId: "TID-2", title: "", url: "https://accounts.google.com/gsi/select" },
      ],
    });
    expect(tabs).toEqual([
      { id: "t1", targetId: "TID-1", title: "Gmail", url: "https://mail.google.com/" },
      { id: "t2", targetId: "TID-2", title: "", url: "https://accounts.google.com/gsi/select" },
    ]);
    // The panel builds the DELETE path from `id`, so it hits the stable alias.
    expect(tabs.map((t) => `/tabs/${encodeURIComponent(t.id)}`)).toEqual(["/tabs/t1", "/tabs/t2"]);
  });

  it("falls back to targetId when no alias is present", () => {
    expect(normalizeTabs({ tabs: [{ targetId: "TID-only" }] })).toEqual([
      { id: "TID-only", targetId: "TID-only", title: "", url: "" },
    ]);
  });

  it("drops targets with no targetId (uncloseable) and coerces bad fields", () => {
    expect(
      normalizeTabs({ tabs: [{ tabId: "t1" }, { targetId: "T", title: 7, url: null }] }),
    ).toEqual([{ id: "T", targetId: "T", title: "", url: "" }]);
  });

  it("returns [] for a not-connected / malformed payload", () => {
    expect(normalizeTabs(null)).toEqual([]);
    expect(normalizeTabs({})).toEqual([]);
    expect(normalizeTabs({ tabs: "nope" })).toEqual([]);
  });
});
