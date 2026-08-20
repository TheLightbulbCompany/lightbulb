import { describe, it, expect } from "vitest";
import { browserViewWsUrl } from "../panels/browserViewUrl";
import { normalizeTabs } from "../panels/browserTabs";

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
