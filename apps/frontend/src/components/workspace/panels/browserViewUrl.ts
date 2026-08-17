// The relay lives on its own CloudFront host (bv[-env].isol8.co), NOT the api
// host — api[-env].isol8.co is an API Gateway HTTP API, which cannot pass
// WebSocket upgrades (api-stack.ts, BrowserViewDistribution). Local dev has no
// CloudFront in front of uvicorn, so localhost connects straight through.
//
// Deliberately import-free: the mapping is load-bearing (a wrong host is a
// panel spinning forever against a door that drops the connection) and must
// stay testable without dragging in the component's SWR/Clerk import graph.
export function browserViewWsUrl(base: string): string {
  const url = new URL(base);
  if (url.hostname !== "localhost" && url.hostname !== "127.0.0.1") {
    url.hostname = url.hostname.replace(/^api(-|\.)/, "bv$1");
  }
  return `${url.origin.replace(/^http/, "ws")}${url.pathname}/browser-view/ws`;
}
