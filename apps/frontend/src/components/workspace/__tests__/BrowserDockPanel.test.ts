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
// human can Close it, so the normalizer must keep every target that carries a
// targetId (the id DELETE /tabs/:targetId addresses) and drop the junk.
describe("normalizeTabs", () => {
  it("maps a /tabs payload to targetId/title/url rows", () => {
    expect(
      normalizeTabs({
        running: true,
        tabs: [
          { id: "t1", targetId: "TID-1", title: "Gmail", url: "https://mail.google.com/" },
          { id: "t2", targetId: "TID-2", title: "", url: "https://accounts.google.com/gsi/select" },
        ],
      }),
    ).toEqual([
      { targetId: "TID-1", title: "Gmail", url: "https://mail.google.com/" },
      { targetId: "TID-2", title: "", url: "https://accounts.google.com/gsi/select" },
    ]);
  });

  it("drops targets with no targetId (uncloseable) and coerces bad fields", () => {
    expect(
      normalizeTabs({ tabs: [{ title: "no id" }, { targetId: "T", title: 7, url: null }] }),
    ).toEqual([{ targetId: "T", title: "", url: "" }]);
  });

  it("returns [] for a not-connected / malformed payload", () => {
    expect(normalizeTabs(null)).toEqual([]);
    expect(normalizeTabs({})).toEqual([]);
    expect(normalizeTabs({ tabs: "nope" })).toEqual([]);
  });
});
