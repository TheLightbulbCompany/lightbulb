// The relay lives on its own CloudFront host (bv[-env].isol8.co), NOT the api
// host — api[-env].isol8.co is an API Gateway HTTP API, which cannot pass
// WebSocket upgrades (api-stack.ts, BrowserViewDistribution). Local dev has no
// CloudFront in front of uvicorn, so localhost connects straight through.
//
// Deliberately import-free: the mapping is load-bearing (a wrong host is a
// panel spinning forever against a door that drops the connection) and must
// stay testable without dragging in the component's SWR/Clerk import graph.
function bvBase(base: string, protocol = false): string {
  const url = new URL(base);
  if (url.hostname !== "localhost" && url.hostname !== "127.0.0.1") {
    url.hostname = url.hostname.replace(/^api(-|\.)/, "bv$1");
  }
  return `${protocol ? url.origin.replace(/^http/, "ws") : url.origin}${url.pathname}`;
}

export function browserViewWsUrl(base: string): string {
  return `${bvBase(base, true)}/browser-view/ws`;
}

// The CDP screencast relay rides the SAME bv door (its path is under
// /browser-view/* so the ALB listener rule carries it); only the suffix differs.
// An optional raw CDP target id (?target=) asks the relay to stream that
// specific tab — how the chrome tab strip switches which tab is shown (the
// screencast is per-page, so focus alone can't move a stream already bound to
// another target).
export function screencastWsUrl(base: string, targetId?: string | null): string {
  const url = `${bvBase(base, true)}/browser-view/screencast/ws`;
  return targetId ? `${url}?target=${encodeURIComponent(targetId)}` : url;
}

export function browserViewSessionUrl(base: string): string {
  return `${bvBase(base)}/browser-view/session`;
}

export function browserViewWebRtcRootUrl(base: string): string {
  return `${bvBase(base)}/browser-view/webrtc/`;
}

export function browserViewWhoamiUrl(base: string): string {
  return `${browserViewWebRtcRootUrl(base)}api/whoami`;
}

export function browserViewScreenUrl(base: string): string {
  return `${browserViewWebRtcRootUrl(base)}api/room/screen`;
}

export function browserViewIframeUrl(base: string, iframePath: string): string {
  const root = browserViewWebRtcRootUrl(base);
  const url = new URL(iframePath, root);
  const expected = new URL(root);
  if (url.origin !== expected.origin || !url.pathname.startsWith(expected.pathname)) {
    throw new Error("Invalid browser-view iframe path");
  }
  url.searchParams.set("cast", "1");
  url.searchParams.set("volume", "1");
  return url.toString();
}
