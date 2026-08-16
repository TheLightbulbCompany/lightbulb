import { describe, it, expect } from "vitest";
import { browserViewWsUrl } from "../panels/browserViewUrl";

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
