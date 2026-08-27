// The relay lives on its own CloudFront host (bv[-env].flickmeon.ai), NOT the api
// host — api[-env].flickmeon.ai is an API Gateway HTTP API, which cannot pass
// WebSocket upgrades (api-stack.ts, BrowserViewDistribution). Local dev has no
// CloudFront in front of uvicorn, so localhost connects straight through.
//
// Deliberately import-free: the mapping is load-bearing (a wrong host is a
// panel spinning forever against a door that drops the connection) and must
// stay testable without dragging in the component's SWR/Clerk import graph.
function bvBase(base: string): string {
  const url = new URL(base);
  if (url.hostname !== "localhost" && url.hostname !== "127.0.0.1") {
    url.hostname = url.hostname.replace(/^api(-|\.)/, "bv$1");
  }
  return `${url.origin.replace(/^http/, "ws")}${url.pathname}`;
}

export function browserViewWsUrl(base: string): string {
  return `${bvBase(base)}/browser-view/ws`;
}
